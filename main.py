import os
import re
import logging
import inspect
import httpx
import asyncio
from enum import Enum
from contextlib import asynccontextmanager

from auto_register_provider import AutoRegisterGoogleProvider
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.azure import AzureProvider
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from pydantic import BaseModel, Field

from fastmcp import FastMCP, Context
from fastmcp.server.context import AcceptedElicitation

from octopus import (
    OCTOPUS_URL,
    get_authenticated_headers,
    get_all_runbooks,
    get_project_prompted_variables,
    create_runbook_run,
    get_task_raw_log,
    get_task_status,
    get_pending_interruptions,
    submit_interruption,
    take_interruption_responsibility,
    parse_interruption_form,
    resolve_tenant,
    get_published_snapshot_id,
    get_environments,
    get_runbook_environments,
    get_project_ids_by_names,
    build_form_values,
    build_task_result,
    octopus_headers,
)

base_url = os.environ.get("EASY_MODE_MCP_BASE_URL", "http://localhost:8000")

# Auth type: "google", "github", "azure", "oauth_proxy", or "none" (default: "google")
AUTH_TYPE = os.environ.get("EASY_MODE_MCP_AUTH_TYPE", "google").lower()
AUTH_ENABLED = AUTH_TYPE != "none"

logging.info(f"Base URL: {base_url}")

class InterventionResponse(BaseModel):
    """Choose whether to proceed with or abort the deployment, and provide any instructions."""
    action: str = Field(title="Action", description="Choose whether to proceed with or reject the deployment", json_schema_extra={"enum": ["Proceed", "Reject Deployment"]})
    instructions: str = Field(default="", title="Instructions", description="Additional instructions or notes for this intervention")

# Optional: comma-separated list of project names to expose (empty = all projects)
OCTOPUS_PROJECT_FILTER = [
    name.strip() for name in os.environ.get("EASY_MODE_MCP_OCTOPUS_PROJECTS", "").split(",") if name.strip()
]

def _create_auth():
    """Create the OAuth auth provider based on EASY_MODE_MCP_AUTH_TYPE."""
    if AUTH_TYPE == "none":
        return None

    from key_value.aio.stores.azure_tables import AzureTablesStore
    from key_value.aio.stores.azure_tables.store import AzureTablesSanitizationStrategy

    storage_backend = AzureTablesStore(
        connection_string=os.environ["EASY_MODE_MCP_AZURE_STORAGE_CONNECTION_STRING"],
        table_name="mcpsessions",
        key_sanitization_strategy=AzureTablesSanitizationStrategy(),
        collection_sanitization_strategy=AzureTablesSanitizationStrategy(),
    )

    if AUTH_TYPE == "github":
        return GitHubProvider(
            client_id=os.environ["EASY_MODE_MCP_GITHUB_CLIENT_ID"],
            client_secret=os.environ["EASY_MODE_MCP_GITHUB_CLIENT_SECRET"],
            base_url=base_url,
            required_scopes=["read:user","user:email"],
            client_storage=storage_backend,
            jwt_signing_key=os.environ["EASY_MODE_MCP_JWT_SIGNING_KEY"],
        )

    if AUTH_TYPE == "azure":
        return AzureProvider(
            client_id=os.environ["EASY_MODE_MCP_AZURE_CLIENT_ID"],
            client_secret=os.environ["EASY_MODE_MCP_AZURE_CLIENT_SECRET"],
            tenant_id=os.environ["EASY_MODE_MCP_AZURE_TENANT_ID"],
            base_url=base_url,
            required_scopes=["openid", "email", "profile"],
            client_storage=storage_backend,
            jwt_signing_key=os.environ["EASY_MODE_MCP_JWT_SIGNING_KEY"],
        )

    if AUTH_TYPE == "oauth_proxy":
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        token_verifier = JWTVerifier(
            jwks_uri=os.environ["EASY_MODE_MCP_OAUTH_JWKS_URI"],
            issuer=os.environ.get("EASY_MODE_MCP_OAUTH_ISSUER"),
            audience=os.environ.get("EASY_MODE_MCP_OAUTH_AUDIENCE"),
            required_scopes=os.environ.get("EASY_MODE_MCP_OAUTH_SCOPES", "").split(",") if os.environ.get("EASY_MODE_MCP_OAUTH_SCOPES") else None,
        )

        return OAuthProxy(
            upstream_authorization_endpoint=os.environ["EASY_MODE_MCP_OAUTH_AUTHORIZATION_ENDPOINT"],
            upstream_token_endpoint=os.environ["EASY_MODE_MCP_OAUTH_TOKEN_ENDPOINT"],
            upstream_client_id=os.environ["EASY_MODE_MCP_OAUTH_CLIENT_ID"],
            upstream_client_secret=os.environ.get("EASY_MODE_MCP_OAUTH_CLIENT_SECRET"),
            upstream_revocation_endpoint=os.environ.get("EASY_MODE_MCP_OAUTH_REVOCATION_ENDPOINT"),
            token_verifier=token_verifier,
            base_url=base_url,
            client_storage=storage_backend,
            jwt_signing_key=os.environ["EASY_MODE_MCP_JWT_SIGNING_KEY"],
        )

    # Default: google
    return AutoRegisterGoogleProvider(
        client_id=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_ID"],
        client_secret=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_SECRET"],
        base_url=base_url,
        required_scopes=["openid", "email", "profile"],
        client_storage=storage_backend,
        jwt_signing_key=os.environ["EASY_MODE_MCP_JWT_SIGNING_KEY"],
    )


auth = _create_auth()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _periodic_refresh() -> None:
    """Periodically re-register all runbook tools every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            logger.info("Refreshing runbook tools...")
            await register_all_runbook_tools()
            logger.info("Runbook tools refreshed successfully.")
        except Exception as e:
            logger.error(f"Failed to refresh runbook tools: {e}")


@asynccontextmanager
async def _app_lifespan(app: FastMCP):
    """Start the periodic refresh background task on server startup."""
    task = asyncio.create_task(_periodic_refresh())
    try:
        yield
    finally:
        task.cancel()


mcp = FastMCP("OctopusEasyMode", auth=auth, lifespan=_app_lifespan)


def _sanitize_tool_name(name: str) -> str:
    """Convert a runbook name into a valid MCP tool name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return sanitized.strip("_")[:64]


def _sanitize_param_name(name: str) -> str:
    """Convert a variable name into a valid Python parameter name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"^[0-9]", "_", sanitized)
    return sanitized.strip("_").lower()


async def _handle_intervention(client: httpx.AsyncClient, interruption: dict, ctx: Context, task_id: str, task: dict) -> dict | None:
    """Handle a single manual intervention. Returns a result dict if the task should stop, else None."""
    instructions, notes_element_id, result_element_id = parse_interruption_form(interruption)

    title = interruption.get("Title", "Manual Intervention")
    message = f"**{title}**\n\n{instructions}" if instructions else title

    # Ask the user to take responsibility or cancel
    responsibility_result = await ctx.elicit(
        message=f"{message}\n\nDo you want to take responsibility for this intervention?",
        response_type=["Assign to me", "Cancel"],
        response_title="Responsibility",
        response_description="Choose whether to assign this intervention to yourself or cancel",
    )

    if not isinstance(responsibility_result, AcceptedElicitation) or responsibility_result.data == "Cancel":
        logger.info(f"User cancelled taking responsibility for interruption '{interruption['Id']}'")
        return {
            "status": "Cancelled",
            "taskId": task_id,
            "description": task.get("Description", ""),
            "errorMessage": "User declined to take responsibility for the manual intervention.",
        }

    # Take responsibility and elicit a response
    await take_interruption_responsibility(client, interruption['Id'])
    result = await ctx.elicit(
        message=message,
        response_type=InterventionResponse,
    )

    if isinstance(result, AcceptedElicitation):
        action = result.data.action
        user_instructions = result.data.instructions
    else:
        action = "Reject Deployment"
        user_instructions = ""

    notes_text = f"Responded via MCP: {action}"
    if user_instructions:
        notes_text += f"\nInstructions: {user_instructions}"

    submit_payload = {
        "Instructions": None,
        "Notes": notes_text,
        "Result": action,
    }

    logger.info(f"Submitting intervention with payload: {submit_payload}")
    await submit_interruption(client, interruption['Id'], submit_payload)
    logger.info(f"Manual intervention '{title}' resolved with: {action}")
    return None


async def _handle_pending_interventions(client: httpx.AsyncClient, task_id: str, task: dict, ctx: Context) -> dict | None:
    """Process all pending interventions for a task. Returns a result dict if the task should stop."""
    interruptions = await get_pending_interruptions(client, task_id)
    for interruption in interruptions:
        if not interruption.get("IsPending"):
            continue
        logger.info(f"Interruption details: {interruption}")
        stop_result = await _handle_intervention(client, interruption, ctx, task_id, task)
        if stop_result:
            return stop_result
    return None


async def _poll_task_to_completion(client: httpx.AsyncClient, task_id: str, ctx: Context | None = None) -> dict:
    """Poll a server task until it completes, handling interventions along the way."""
    while True:
        task = await get_task_status(client, task_id)
        state = task.get("State")

        if state in ("Success", "Failed", "Canceled", "TimedOut"):
            raw_log = await get_task_raw_log(client, task_id)
            return build_task_result(task, task_id, raw_log)

        if task.get("HasPendingInterruptions") and ctx:
            stop_result = await _handle_pending_interventions(client, task_id, task, ctx)
            if stop_result:
                return stop_result

        await asyncio.sleep(5)


async def _run_runbook(runbook_id: str, environment_id: str, variable_values: dict[str, str] | None = None, ctx: Context | None = None, tenant_id: str | None = None, project_id: str | None = None) -> dict:
    """Trigger a runbook run and poll for completion, returning the final task status."""
    headers = await get_authenticated_headers()

    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=headers) as client:
        snapshot_id = await get_published_snapshot_id(client, runbook_id)
        if not snapshot_id:
            return {"status": "Failed", "error": "Runbook has no published snapshot"}

        form_values = {}
        if variable_values:
            form_values = await build_form_values(client, snapshot_id, environment_id, variable_values)

        task_id = await create_runbook_run(client, runbook_id, snapshot_id, environment_id, form_values, tenant_id=tenant_id)
        return await _poll_task_to_completion(client, task_id, ctx)


def _build_tool_params(single_env: bool, EnvironmentEnum, param_to_var: dict, is_tenanted: bool, multi_tenancy_mode: str) -> list[inspect.Parameter]:
    """Build the list of inspect.Parameter objects for a runbook tool."""
    if single_env:
        params = [
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
        ]
    else:
        params = [
            inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
            inspect.Parameter("environment_name", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=EnvironmentEnum),
        ]

    for param_name, var in param_to_var.items():
        default = var["default"] if var["default"] else (inspect.Parameter.empty if var["required"] else None)
        params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=str | None if not var["required"] else str,
            )
        )

    if is_tenanted:
        tenant_required = multi_tenancy_mode == "Tenanted"
        params.append(
            inspect.Parameter(
                "tenant_name",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty if tenant_required else None,
                annotation=str if tenant_required else str | None,
            )
        )

    # Sort: ctx first, then required, then optional
    ctx_params = [p for p in params if p.name == "ctx"]
    required_params = [p for p in params if p.name != "ctx" and p.default is inspect.Parameter.empty]
    optional_params = [p for p in params if p.name != "ctx" and p.default is not inspect.Parameter.empty]
    return ctx_params + required_params + optional_params


def _build_tool_docstring(description: str, project_id: str, env_help: str, single_env: bool, is_tenanted: bool, multi_tenancy_mode: str, param_to_var: dict) -> str:
    """Build the docstring for a runbook tool."""
    if single_env:
        args_doc = ""
    else:
        args_doc = "    environment_name: The name of the environment to run the runbook in\n"
    if is_tenanted:
        tenant_req_str = " (required)" if multi_tenancy_mode == "Tenanted" else " (optional)"
        args_doc += f"    tenant_name: The name of the tenant to run the runbook for{tenant_req_str}\n"
    for param_name, var in param_to_var.items():
        required_str = " (required)" if var["required"] else " (optional)"
        var_desc = var["description"] or var["label"]
        args_doc += f"    {param_name}: {var_desc}{required_str}\n"

    return (
        f"{description}\n\n"
        f"Project ID: {project_id}\n"
        f"Available environments: {env_help}\n\n"
        f"Args:\n"
        f"{args_doc}"
    )


def _build_tool_annotations(single_env: bool, EnvironmentEnum, is_tenanted: bool, multi_tenancy_mode: str, param_to_var: dict) -> dict:
    """Build the __annotations__ dict for a runbook tool."""
    annotations = {"return": dict, "ctx": Context}
    if not single_env:
        annotations["environment_name"] = EnvironmentEnum
    if is_tenanted:
        annotations["tenant_name"] = str if multi_tenancy_mode == "Tenanted" else str | None
    for param_name, var in param_to_var.items():
        annotations[param_name] = str | None if not var["required"] else str
    return annotations


async def _resolve_environment(environment_name: str, environments: list[dict]) -> tuple[str | None, str | None]:
    """Resolve an environment name to its ID."""
    env_map = {e["Name"].lower(): e["Id"] for e in environments}
    env_id = env_map.get(environment_name.lower())
    if not env_id:
        env_help = ", ".join(e["Name"] for e in environments)
        return None, f"Environment '{environment_name}' not found. Available: {env_help}"
    return env_id, None


async def _collect_variable_values(kwargs: dict, param_to_var: dict, ctx: Context | None) -> tuple[dict[str, str], dict | None]:
    """Collect variable values from kwargs, eliciting missing required values."""
    variable_values = {}
    for param_name, var in param_to_var.items():
        value = kwargs.get(param_name)
        if value is not None:
            variable_values[var["name"]] = value
        elif var["required"] and not var["default"] and ctx:
            var_desc = var["description"] or var["label"]
            elicit_result = await ctx.elicit(
                message=f"Please provide a value for **{var['label']}**\n\n{var_desc}",
                response_type=str,
            )
            if isinstance(elicit_result, AcceptedElicitation):
                variable_values[var["name"]] = elicit_result.data
            else:
                return {}, {
                    "status": "Failed",
                    "error": f"Required variable '{var['label']}' was not provided and user declined to supply a value.",
                }
    return variable_values, None


async def _resolve_tenant_for_tool(kwargs: dict, is_tenanted: bool, multi_tenancy_mode: str, project_id: str, env_id: str) -> tuple[str | None, dict | None]:
    """Resolve tenant name from kwargs to a tenant ID."""
    if not is_tenanted:
        return None, None

    tenant_name_val = kwargs.get("tenant_name")
    if tenant_name_val:
        headers = await get_authenticated_headers()
        async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=headers) as tenant_client:
            resolved_tenant_id, error = await resolve_tenant(tenant_client, tenant_name_val, project_id, env_id)
            if error:
                return None, {"status": "Failed", "error": error}
            return resolved_tenant_id, None
    elif multi_tenancy_mode == "Tenanted":
        return None, {"status": "Failed", "error": "Tenant name is required for this runbook."}
    return None, None


def _register_runbook_tool(runbook: dict, environments: list[dict], prompted_variables: list[dict]) -> None:
    """Register a single runbook as an MCP tool with task support."""
    runbook_id = runbook["Id"]
    runbook_name = runbook["Name"]
    project_id = runbook.get("ProjectId", "")
    description = runbook.get("Description") or f"Run the '{runbook_name}' runbook"
    tool_name = _sanitize_tool_name(runbook_name)
    multi_tenancy_mode = runbook.get("MultiTenancyMode", "Untenanted")
    is_tenanted = multi_tenancy_mode in ("Tenanted", "TenantedOrUntenanted")

    logger.info(
        f"Registering runbook tool: {tool_name} (runbook_id={runbook_id}, project_id={project_id}, "
        f"prompted_variables={[v['name'] for v in prompted_variables]})"
    )

    env_names = [e["Name"] for e in environments]
    env_help = ", ".join(env_names) if env_names else "No environments found"
    single_env = len(environments) == 1

    # Create a dynamic Enum for environment names
    if not single_env and env_names:
        EnvironmentEnum = Enum(
            f"Environment_{_sanitize_tool_name(runbook_name)}",
            {name: name for name in env_names},
            type=str,
        )
    else:
        EnvironmentEnum = None

    # Build a mapping from sanitized param name to variable info
    param_to_var = {}
    for var in prompted_variables:
        param_name = _sanitize_param_name(var["name"])
        param_to_var[param_name] = var

    params = _build_tool_params(single_env, EnvironmentEnum, param_to_var, is_tenanted, multi_tenancy_mode)

    async def run_tool(**kwargs) -> dict:
        """placeholder"""
        ctx = kwargs.pop("ctx", None)
        environment_name = kwargs.get("environment_name", environments[0]["Name"] if single_env else None)
        if not environment_name:
            return {"status": "Failed", "error": f"Environment name is required. Available: {env_help}"}

        env_id, env_error = await _resolve_environment(environment_name, environments)
        if env_error:
            return {"status": "Failed", "error": env_error}

        variable_values, var_error = await _collect_variable_values(kwargs, param_to_var, ctx)
        if var_error:
            return var_error

        resolved_tenant_id, tenant_error = await _resolve_tenant_for_tool(kwargs, is_tenanted, multi_tenancy_mode, project_id, env_id)
        if tenant_error:
            return tenant_error

        return await _run_runbook(runbook_id, env_id, variable_values if variable_values else None, ctx=ctx, tenant_id=resolved_tenant_id, project_id=project_id)

    run_tool.__doc__ = _build_tool_docstring(description, project_id, env_help, single_env, is_tenanted, multi_tenancy_mode, param_to_var)
    run_tool.__name__ = tool_name
    run_tool.__signature__ = inspect.Signature(params)
    run_tool.__annotations__ = _build_tool_annotations(single_env, EnvironmentEnum, is_tenanted, multi_tenancy_mode, param_to_var)

    mcp.tool(name=tool_name, description=description, task=True)(run_tool)


async def _remove_all_tools() -> None:
    """Remove all currently registered tools from the MCP server."""
    tools = await mcp.list_tools()
    for tool in tools:
        try:
            mcp.local_provider.remove_tool(tool.name)
        except Exception as e:
            logger.warning(f"Failed to remove tool '{tool.name}': {e}")


async def register_all_runbook_tools() -> None:
    """Fetch runbooks and environments, then register each runbook as a tool."""
    await _remove_all_tools()

    runbooks, environments = await asyncio.gather(
        get_all_runbooks(),
        get_environments(),
    )

    # Filter by project names if configured
    if OCTOPUS_PROJECT_FILTER:
        allowed_project_ids = await get_project_ids_by_names(OCTOPUS_PROJECT_FILTER)
        runbooks = [rb for rb in runbooks if rb.get("ProjectId") in allowed_project_ids]
        logger.info(f"Filtered to {len(runbooks)} runbooks from projects: {OCTOPUS_PROJECT_FILTER}")

    # Fetch prompted variables for each unique project
    project_ids = list({rb.get("ProjectId", "") for rb in runbooks if rb.get("ProjectId")})
    project_vars = await asyncio.gather(
        *[get_project_prompted_variables(pid) for pid in project_ids]
    )
    project_prompted_vars = dict(zip(project_ids, project_vars))

    # Fetch lifecycle environments for runbooks with FromProjectLifecycles scope
    lifecycle_runbooks = [rb for rb in runbooks if rb.get("EnvironmentScope") == "FromProjectLifecycles"]
    lifecycle_envs = await asyncio.gather(
        *[get_runbook_environments(rb) for rb in lifecycle_runbooks]
    )
    lifecycle_env_map = {rb["Id"]: envs for rb, envs in zip(lifecycle_runbooks, lifecycle_envs)}

    for runbook in runbooks:
        all_prompted = project_prompted_vars.get(runbook.get("ProjectId", ""), [])
        runbook_id = runbook["Id"]

        # Filter prompted variables: include only those with no ProcessOwner scope
        # or where this runbook is listed as a process owner
        prompted = [
            var for var in all_prompted
            if not var.get("process_owners") or runbook_id in var["process_owners"]
        ]

        # Filter environments based on the runbook's EnvironmentScope
        scope = runbook.get("EnvironmentScope")
        if scope == "Specified":
            allowed_env_ids = set(runbook.get("Environments", []))
            runbook_environments = [e for e in environments if e["Id"] in allowed_env_ids]
        elif scope == "FromProjectLifecycles":
            runbook_environments = lifecycle_env_map.get(runbook["Id"], environments)
        else:
            runbook_environments = environments

        _register_runbook_tool(runbook, runbook_environments, prompted)


# Register tools at import time by running the async setup
asyncio.run(register_all_runbook_tools())


if __name__ == "__main__":
    transport = os.environ.get("EASY_MODE_MCP_TRANSPORT", "streamable-http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8000, allowed_hosts=["*"], allowed_origins=["*"])
