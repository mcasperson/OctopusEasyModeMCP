import os
import re
import logging
import inspect
import httpx
import asyncio
from enum import Enum
from contextlib import asynccontextmanager

from key_value.aio.stores.azure_tables import AzureTablesStore
from key_value.aio.stores.azure_tables.store import AzureTablesSanitizationStrategy

from auto_register_provider import AutoRegisterGoogleProvider
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field

from fastmcp import FastMCP, Context
from fastmcp.server.context import AcceptedElicitation

base_url = os.environ.get("EASY_MODE_MCP_BASE_URL", "http://localhost:8000")

logging.info(f"Base URL: {base_url}")

class InterventionResponse(BaseModel):
    """Choose whether to proceed with or abort the deployment, and provide any instructions."""
    action: str = Field(title="Action", description="Choose whether to proceed with or reject the deployment", json_schema_extra={"enum": ["Proceed", "Reject Deployment"]})
    instructions: str = Field(default="", title="Instructions", description="Additional instructions or notes for this intervention")

# Octopus Deploy configuration from environment
OCTOPUS_URL = os.environ["EASY_MODE_MCP_OCTOPUS_URL"]
OCTOPUS_API_KEY = os.environ["EASY_MODE_MCP_OCTOPUS_API_KEY"]
OCTOPUS_SPACE_ID = os.environ["EASY_MODE_MCP_OCTOPUS_SPACE_ID"]

storage_backend = AzureTablesStore(
    connection_string=os.environ["EASY_MODE_MCP_AZURE_STORAGE_CONNECTION_STRING"],
    table_name="mcpsessions",
    key_sanitization_strategy=AzureTablesSanitizationStrategy(),
    collection_sanitization_strategy=AzureTablesSanitizationStrategy(),
)

# Google OAuth configuration
auth = AutoRegisterGoogleProvider(
    client_id=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_ID"],
    client_secret=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_SECRET"],
    base_url=base_url,
    required_scopes=["openid", "email", "profile"],
    client_storage=storage_backend,
    jwt_signing_key=os.environ["EASY_MODE_MCP_JWT_SIGNING_KEY"],
)

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


async def exchange_token_for_octopus_token(id_token: str) -> str:
    """Exchange a Google ID token for an Octopus access token via token exchange.

    Args:
        id_token: The Google ID token (JWT) to exchange.

    Returns:
        The Octopus access token.

    Raises:
        RuntimeError: If the token exchange fails or no access token is returned.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{OCTOPUS_URL}/token/v1",
            json={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "audience": os.environ["EASY_MODE_MCP_OCTOPUS_AUDIENCE"],
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "subject_token": id_token,
            },
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Token exchange failed with status {response.status_code}: {response.text}"
            )

        token_data = response.json()

        if not token_data.get("access_token"):
            raise RuntimeError("Authentication error: Unable to get Octopus token")

        return token_data["access_token"]


def _octopus_headers(bearer_token: str | None = None) -> dict[str, str]:
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    return {"X-Octopus-ApiKey": OCTOPUS_API_KEY}


def _sanitize_tool_name(name: str) -> str:
    """Convert a runbook name into a valid MCP tool name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return sanitized.strip("_")[:64]


async def _get_all_runbooks() -> list[dict]:
    """Fetch all runbooks from the Octopus space."""
    runbooks = []
    skip = 0
    take = 30
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        while True:
            resp = await client.get(
                f"/api/{OCTOPUS_SPACE_ID}/runbooks",
                params={"skip": skip, "take": take},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("Items", [])
            runbooks.extend(items)
            if skip + take >= data.get("TotalResults", 0):
                break
            skip += take
    return runbooks


async def _get_project_prompted_variables(project_id: str) -> list[dict]:
    """Fetch prompted variables for a project."""
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        variable_set_id = f"variableset-{project_id}"
        resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/variables/{variable_set_id}")
        resp.raise_for_status()
        data = resp.json()
        prompted = []
        for var in data.get("Variables", []):
            prompt = var.get("Prompt")
            if prompt:
                prompted.append({
                    "id": var["Id"],
                    "name": var["Name"],
                    "label": prompt.get("Label", var["Name"]),
                    "description": prompt.get("Description", ""),
                    "required": prompt.get("Required", False),
                    "default": var.get("Value", ""),
                })
        return prompted


def _sanitize_param_name(name: str) -> str:
    """Convert a variable name into a valid Python parameter name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"^[0-9]", "_", sanitized)
    return sanitized.strip("_").lower()


async def _get_runbook_preview_form(client: httpx.AsyncClient, snapshot_id: str, environment_id: str) -> tuple[list[dict], dict[str, str]]:
    """Fetch the runbook run preview and return form elements and default values.

    Returns:
        A tuple of (elements, form_values) where elements is the list of form elements
        and form_values is a dict of default form values.
    """
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/runbookSnapshots/{snapshot_id}/runbookRuns/preview/{environment_id}"
    )
    resp.raise_for_status()
    preview = resp.json()
    form = preview.get("Form", {})
    elements = form.get("Elements", [])

    # Start with default form values from the preview
    form_values = dict(form.get("Values", {}))

    return elements, form_values


async def _create_runbook_run(client: httpx.AsyncClient, runbook_id: str, snapshot_id: str, environment_id: str, form_values: dict[str, str]) -> str:
    """Create a runbook run and return the task ID.

    Args:
        client: The HTTP client to use
        runbook_id: The runbook to run
        snapshot_id: The published runbook snapshot ID
        environment_id: The environment to run in
        form_values: Form values to submit with the run

    Returns:
        The server task ID for the created run.
    """
    payload = {
        "RunbookId": runbook_id,
        "RunbookSnapshotId": snapshot_id,
        "EnvironmentId": environment_id,
    }
    if form_values:
        payload["FormValues"] = form_values

    resp = await client.post(
        f"/api/{OCTOPUS_SPACE_ID}/runbookRuns",
        json=payload,
    )
    resp.raise_for_status()
    run = resp.json()
    return run["TaskId"]


async def _get_task_raw_log(client: httpx.AsyncClient, task_id: str) -> str:
    """Download the raw log for a server task.

    Args:
        client: The HTTP client to use
        task_id: The server task ID

    Returns:
        The raw log text.
    """
    log_resp = await client.get(f"/api/tasks/{task_id}/raw")
    log_resp.raise_for_status()
    return log_resp.text


async def _get_task_status(client: httpx.AsyncClient, task_id: str) -> dict:
    """Fetch a server task and return its JSON representation.

    Args:
        client: The HTTP client to use
        task_id: The server task ID to fetch

    Returns:
        The task JSON dict.
    """
    resp = await client.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    return resp.json()


async def _get_pending_interruptions(client: httpx.AsyncClient, task_id: str) -> list[dict]:
    """Fetch pending interruptions for a server task.

    Args:
        client: The HTTP client to use
        task_id: The server task ID

    Returns:
        A list of pending interruption dicts.
    """
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions",
        params={"regarding": task_id, "pendingOnly": "true"},
    )
    resp.raise_for_status()
    return resp.json().get("Items", [])


async def _submit_interruption(client: httpx.AsyncClient, interruption_id: str, payload: dict) -> None:
    """Submit a response to a manual intervention interruption.

    Args:
        client: The HTTP client to use
        interruption_id: The interruption ID to submit a response for
        payload: The submission payload dict
    """
    resp = await client.post(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption_id}/submit",
        json=payload,
    )
    if resp.status_code != 200:
        logger.error(f"Intervention submit failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()


async def _take_interruption_responsibility(client: httpx.AsyncClient, interruption_id: str) -> None:
    """Take responsibility for a manual intervention interruption.

    Args:
        client: The HTTP client to use
        interruption_id: The interruption ID to take responsibility for
    """
    resp = await client.put(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption_id}/responsible",
    )
    if resp.status_code != 200:
        logger.error(f"Taking responsibility failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    logger.info(f"Took responsibility for interruption '{interruption_id}'")


def _parse_interruption_form(interruption: dict) -> tuple[str, str | None, str | None]:
    """Parse an interruption's form to extract instructions and element IDs.

    Args:
        interruption: The interruption JSON dict

    Returns:
        A tuple of (instructions, notes_element_id, result_element_id).
    """
    form = interruption.get("Form", {})
    elements = form.get("Elements", [])
    instructions = ""
    notes_element_id = None
    result_element_id = None
    for element in elements:
        control = element.get("Control", {})
        control_type = control.get("Type", "")
        if control_type == "Paragraph":
            instructions = control.get("Text", "")
        elif control_type == "TextArea":
            notes_element_id = element.get("Name", "")
        elif control_type == "Select":
            result_element_id = element.get("Name", "")
    return instructions, notes_element_id, result_element_id


async def _get_published_snapshot_id(client: httpx.AsyncClient, runbook_id: str) -> str | None:
    """Fetch a runbook and return its published snapshot ID.

    Args:
        client: The HTTP client to use
        runbook_id: The runbook ID

    Returns:
        The published runbook snapshot ID, or None if not published.
    """
    resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/runbooks/{runbook_id}")
    resp.raise_for_status()
    runbook = resp.json()
    return runbook.get("PublishedRunbookSnapshotId")


async def _run_runbook(runbook_id: str, environment_id: str, variable_values: dict[str, str] | None = None, ctx: Context | None = None) -> dict:
    """Trigger a runbook run and poll for completion, returning the final task status.

    Args:
        runbook_id: The runbook to run
        environment_id: The environment to run in
        variable_values: Dict mapping variable names to their values for prompted variables
        ctx: MCP context for elicitation during manual interventions
    """
    # Exchange the user's Google ID token for an Octopus access token
    google_access_token = get_access_token()

    access_token = await exchange_token_for_octopus_token(google_access_token.id_token)

    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers(access_token)) as client:
        # Get the published runbook snapshot
        snapshot_id = await _get_published_snapshot_id(client, runbook_id)
        if not snapshot_id:
            return {"status": "Failed", "error": "Runbook has no published snapshot"}

        # Build FormValues by resolving variable names to form element IDs from the snapshot preview
        form_values = {}
        if variable_values:
            elements, form_values = await _get_runbook_preview_form(client, snapshot_id, environment_id)

            logger.info(f"Form elements: {[(e.get('Name'), e.get('Control', {})) for e in elements]}")
            logger.info(f"Form default values: {form_values}")

            # Map variable names to form element IDs and override defaults
            for element in elements:
                element_id = element.get("Name", "")
                control = element.get("Control", {})
                control_label = control.get("Label", "")
                control_name = control.get("Name", "")
                control_description = control.get("Description", "")

                for var_name, var_value in variable_values.items():
                    # Try matching by control label, control name, element ID, or description
                    if var_name in (control_label, control_name, element_id, control_description):
                        form_values[element_id] = var_value
                        logger.info(f"Mapped variable '{var_name}' to form element '{element_id}' = '{var_value}'")
                        break

            if variable_values and not any(
                var_name in (e.get("Control", {}).get("Label", ""), e.get("Control", {}).get("Name", ""), e.get("Name", ""))
                for e in elements
                for var_name in variable_values.keys()
            ):
                logger.warning(
                    f"Could not map any variables to form elements. "
                    f"Variables: {list(variable_values.keys())}, "
                    f"Elements: {[(e.get('Name'), e.get('Control', {}).get('Label'), e.get('Control', {}).get('Name')) for e in elements]}"
                )

        # Create the runbook run
        task_id = await _create_runbook_run(client, runbook_id, snapshot_id, environment_id, form_values)

        # Poll the server task until completion
        while True:
            task = await _get_task_status(client, task_id)
            state = task.get("State")
            if state in ("Success", "Failed", "Canceled", "TimedOut"):
                # Download the task logs
                raw_log = await _get_task_raw_log(client, task_id)

                return {
                    "status": state,
                    "taskId": task_id,
                    "description": task.get("Description", ""),
                    "errorMessage": task.get("ErrorMessage", ""),
                    "duration": task.get("Duration", ""),
                    "logs": raw_log,
                }

            # Check for pending manual interventions
            if task.get("HasPendingInterruptions") and ctx:
                interruptions = await _get_pending_interruptions(client, task_id)

                for interruption in interruptions:
                    if not interruption.get("IsPending"):
                        continue

                    logger.info(f"Interruption details: {interruption}")

                    # Extract intervention instructions from the form
                    instructions, notes_element_id, result_element_id = _parse_interruption_form(interruption)

                    # Build the elicitation message
                    title = interruption.get("Title", "Manual Intervention")
                    guidance_options = interruption.get("ResponsibleTeamIds", [])
                    message = f"**{title}**\n\n{instructions}" if instructions else title

                    # First, ask the user to take responsibility or cancel
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

                    # Take responsibility for the interruption
                    await _take_interruption_responsibility(client, interruption['Id'])
                    # Then, elicit a response to proceed or abort with instructions
                    result = await ctx.elicit(
                        message=message,
                        response_type=InterventionResponse,
                    )

                    if isinstance(result, AcceptedElicitation):
                        action = result.data.action
                        user_instructions = result.data.instructions
                    else:
                        # User declined or cancelled the elicitation - reject the deployment
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

                    await _submit_interruption(client, interruption['Id'], submit_payload)
                    logger.info(f"Manual intervention '{title}' resolved with: {action}")

            await asyncio.sleep(5)


async def _get_environments() -> list[dict]:
    """Fetch all environments from the Octopus space."""
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        resp = await client.get(
            f"/api/{OCTOPUS_SPACE_ID}/environments",
            params={"take": 1000},
        )
        resp.raise_for_status()
        return resp.json().get("Items", [])


def _register_runbook_tool(runbook: dict, environments: list[dict], prompted_variables: list[dict]) -> None:
    """Register a single runbook as an MCP tool with task support."""
    runbook_id = runbook["Id"]
    runbook_name = runbook["Name"]
    project_id = runbook.get("ProjectId", "")
    description = runbook.get("Description") or f"Run the '{runbook_name}' runbook"
    tool_name = _sanitize_tool_name(runbook_name)

    logger.info(
        f"Registering runbook tool: {tool_name} (runbook_id={runbook_id}, project_id={project_id}, "
        f"prompted_variables={[v['name'] for v in prompted_variables]})"
    )

    env_names = [e["Name"] for e in environments]
    env_help = ", ".join(env_names) if env_names else "No environments found"
    single_env = len(environments) == 1

    # Create a dynamic Enum for environment names to provide a dropdown list
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

    # Build function parameters dynamically
    # If there's only one environment, don't include environment_name as a parameter
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

    # Sort params so that required (no default) come before optional (have default)
    # Keep ctx first always
    ctx_params = [p for p in params if p.name == "ctx"]
    required_params = [p for p in params if p.name != "ctx" and p.default is inspect.Parameter.empty]
    optional_params = [p for p in params if p.name != "ctx" and p.default is not inspect.Parameter.empty]
    params = ctx_params + required_params + optional_params

    async def run_tool(**kwargs) -> dict:
        """placeholder"""
        ctx = kwargs.pop("ctx", None)
        environment_name = kwargs.get("environment_name", environments[0]["Name"] if single_env else None)
        if not environment_name:
            return {
                "status": "Failed",
                "error": f"Environment name is required. Available: {env_help}",
            }
        # Resolve environment name to ID
        env_map = {e["Name"].lower(): e["Id"] for e in environments}
        env_id: str | None = env_map.get(environment_name.lower())
        if not env_id:
            return {
                "status": "Failed",
                "error": f"Environment '{environment_name}' not found. Available: {env_help}",
            }

        # Build variable values from prompted variable arguments (keyed by variable name)
        variable_values = {}
        for param_name, var in param_to_var.items():
            value = kwargs.get(param_name)
            if value is not None:
                variable_values[var["name"]] = value
            elif var["required"] and not var["default"] and ctx:
                # Elicit the value from the user for required variables with no default
                var_desc = var["description"] or var["label"]
                elicit_result = await ctx.elicit(
                    message=f"Please provide a value for **{var['label']}**\n\n{var_desc}",
                    response_type=str,
                )
                if isinstance(elicit_result, AcceptedElicitation):
                    variable_values[var["name"]] = elicit_result.data
                else:
                    return {
                        "status": "Failed",
                        "error": f"Required variable '{var['label']}' was not provided and user declined to supply a value.",
                    }

        return await _run_runbook(runbook_id, env_id, variable_values if variable_values else None, ctx=ctx)

    # Build docstring with prompted variable info
    if single_env:
        args_doc = ""
    else:
        args_doc = "    environment_name: The name of the environment to run the runbook in\n"
    for param_name, var in param_to_var.items():
        required_str = " (required)" if var["required"] else " (optional)"
        var_desc = var["description"] or var["label"]
        args_doc += f"    {param_name}: {var_desc}{required_str}\n"

    run_tool.__doc__ = (
        f"{description}\n\n"
        f"Project ID: {project_id}\n"
        f"Available environments: {env_help}\n\n"
        f"Args:\n"
        f"{args_doc}"
    )
    run_tool.__name__ = tool_name

    # Apply the dynamic signature and annotations
    sig = inspect.Signature(params)
    run_tool.__signature__ = sig

    # Set __annotations__ so that typing.get_type_hints() can resolve them
    annotations = {"return": dict, "ctx": Context}
    if not single_env:
        annotations["environment_name"] = EnvironmentEnum
    for param_name, var in param_to_var.items():
        annotations[param_name] = str | None if not var["required"] else str
    run_tool.__annotations__ = annotations

    mcp.tool(name=tool_name, description=description, task=True)(run_tool)


async def _get_runbook_environments(runbook: dict) -> list[dict]:
    """Fetch environments available for a runbook via its RunbookEnvironments link."""
    environments_link = runbook.get("Links", {}).get("RunbookEnvironments")
    if not environments_link:
        return []
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        resp = await client.get(environments_link)
        resp.raise_for_status()
        return resp.json()


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
        _get_all_runbooks(),
        _get_environments(),
    )

    # Only include runbooks that have a published snapshot
    runbooks = [rb for rb in runbooks if rb.get("PublishedRunbookSnapshotId")]

    # Fetch prompted variables for each unique project
    project_ids = list({rb.get("ProjectId", "") for rb in runbooks if rb.get("ProjectId")})
    project_vars = await asyncio.gather(
        *[_get_project_prompted_variables(pid) for pid in project_ids]
    )
    project_prompted_vars = dict(zip(project_ids, project_vars))

    # Fetch lifecycle environments for runbooks with FromProjectLifecycles scope
    lifecycle_runbooks = [rb for rb in runbooks if rb.get("EnvironmentScope") == "FromProjectLifecycles"]
    lifecycle_envs = await asyncio.gather(
        *[_get_runbook_environments(rb) for rb in lifecycle_runbooks]
    )
    lifecycle_env_map = {rb["Id"]: envs for rb, envs in zip(lifecycle_runbooks, lifecycle_envs)}

    for runbook in runbooks:
        prompted = project_prompted_vars.get(runbook.get("ProjectId", ""), [])

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
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000, allowed_hosts=["*"], allowed_origins=["*"])
