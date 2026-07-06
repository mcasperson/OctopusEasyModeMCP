import os
import re
import logging
import inspect
import httpx
import asyncio
from enum import Enum


from fastmcp import FastMCP, Context
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.context import AcceptedElicitation

# Octopus Deploy configuration from environment
OCTOPUS_URL = os.environ["EASY_MODE_MCP_OCTOPUS_URL"]
OCTOPUS_API_KEY = os.environ["EASY_MODE_MCP_OCTOPUS_API_KEY"]
OCTOPUS_SPACE_ID = os.environ["EASY_MODE_MCP_OCTOPUS_SPACE_ID"]

# Google OAuth configuration
auth = GoogleProvider(
    client_id=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_ID"],
    client_secret=os.environ["EASY_MODE_MCP_GOOGLE_CLIENT_SECRET"],
    base_url=os.environ.get("EASY_MODE_MCP_BASE_URL", "http://localhost:8000"),
    required_scopes=["openid", "email", "profile"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("OctopusEasyMode", auth=auth)


def _octopus_headers() -> dict[str, str]:
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


async def _run_runbook(runbook_id: str, environment_id: str, variable_values: dict[str, str] | None = None, ctx: Context | None = None) -> dict:
    """Trigger a runbook run and poll for completion, returning the final task status.

    Args:
        runbook_id: The runbook to run
        environment_id: The environment to run in
        variable_values: Dict mapping variable names to their values for prompted variables
        ctx: MCP context for elicitation during manual interventions
    """
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        # Get the published runbook snapshot
        resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/runbooks/{runbook_id}")
        resp.raise_for_status()
        runbook = resp.json()
        snapshot_id = runbook.get("PublishedRunbookSnapshotId")
        if not snapshot_id:
            return {"status": "Failed", "error": "Runbook has no published snapshot"}

        # Build FormValues by resolving variable names to form element IDs from the snapshot preview
        form_values = {}
        if variable_values:
            resp = await client.get(
                f"/api/{OCTOPUS_SPACE_ID}/runbookSnapshots/{snapshot_id}/runbookRuns/preview/{environment_id}"
            )
            resp.raise_for_status()
            preview = resp.json()
            form = preview.get("Form", {})
            elements = form.get("Elements", [])

            # Start with default form values from the preview
            form_values = dict(form.get("Values", {}))

            logger.info(f"Form elements: {[(e.get('Name'), e.get('Control', {})) for e in elements]}")
            logger.info(f"Form default values: {form.get('Values', {})}")

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
        task_id = run["TaskId"]

        # Poll the server task until completion
        while True:
            resp = await client.get(f"/api/tasks/{task_id}")
            resp.raise_for_status()
            task = resp.json()
            state = task.get("State")
            if state in ("Success", "Failed", "Canceled", "TimedOut"):
                # Download the task logs
                log_resp = await client.get(f"/api/tasks/{task_id}/raw")
                log_resp.raise_for_status()
                raw_log = log_resp.text

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
                interruptions_resp = await client.get(
                    f"/api/{OCTOPUS_SPACE_ID}/interruptions",
                    params={"regarding": task_id, "pendingOnly": "true"},
                )
                interruptions_resp.raise_for_status()
                interruptions = interruptions_resp.json().get("Items", [])

                for interruption in interruptions:
                    if not interruption.get("IsPending"):
                        continue

                    logger.info(f"Interruption details: {interruption}")

                    # Extract intervention instructions from the form
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
                    responsible_resp = await client.put(
                        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption['Id']}/responsible",
                    )
                    if responsible_resp.status_code != 200:
                        logger.error(f"Taking responsibility failed: {responsible_resp.status_code} {responsible_resp.text}")
                    responsible_resp.raise_for_status()
                    logger.info(f"Took responsibility for interruption '{interruption['Id']}'")

                    # Then, elicit a response to proceed or abort
                    result = await ctx.elicit(
                        message=message,
                        response_type=["Proceed", "Reject Deployment"],
                        response_title="Action",
                        response_description="Choose whether to proceed with or abort the deployment",
                    )

                    if isinstance(result, AcceptedElicitation):
                        action = result.data
                    else:
                        # User declined or cancelled the elicitation - reject the deployment
                        action = "Reject Deployment"

                    notes_text = f"Responded via MCP: {action}"

                    submit_payload = {
                        "Instructions": None,
                        "Notes": notes_text,
                        "Result": action,
                    }

                    logger.info(f"Submitting intervention with payload: {submit_payload}")


                    submit_resp = await client.post(
                        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption['Id']}/submit",
                        json=submit_payload,
                    )
                    if submit_resp.status_code != 200:
                        logger.error(f"Intervention submit failed: {submit_resp.status_code} {submit_resp.text}")
                    submit_resp.raise_for_status()
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
    tool_name = f"run_runbook_{_sanitize_tool_name(runbook_name)}"

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


async def register_all_runbook_tools() -> None:
    """Fetch runbooks and environments, then register each runbook as a tool."""
    runbooks, environments = await asyncio.gather(
        _get_all_runbooks(),
        _get_environments(),
    )

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
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
