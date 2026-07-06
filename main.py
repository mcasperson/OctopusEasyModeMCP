import os
import re
import logging
import inspect
import httpx
import asyncio


from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

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


async def _run_runbook(runbook_id: str, environment_id: str, variable_values: dict[str, str] | None = None) -> dict:
    """Trigger a runbook run and poll for completion, returning the final task status.

    Args:
        runbook_id: The runbook to run
        environment_id: The environment to run in
        variable_values: Dict mapping variable names to their values for prompted variables
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

    # Build a mapping from sanitized param name to variable info
    param_to_var = {}
    for var in prompted_variables:
        param_name = _sanitize_param_name(var["name"])
        param_to_var[param_name] = var

    # Build function parameters dynamically
    # If there's only one environment, don't include environment_name as a parameter
    if single_env:
        params = []
    else:
        params = [
            inspect.Parameter("environment_name", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
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

        return await _run_runbook(runbook_id, env_id, variable_values if variable_values else None)

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
    annotations = {"return": dict}
    if not single_env:
        annotations["environment_name"] = str
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
