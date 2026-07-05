import os
import re
import logging
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


async def _run_runbook(runbook_id: str, environment_id: str) -> dict:
    """Trigger a runbook run and poll for completion, returning the final task status."""
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=_octopus_headers()) as client:
        # Get the published runbook snapshot
        resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/runbooks/{runbook_id}")
        resp.raise_for_status()
        runbook = resp.json()
        snapshot_id = runbook.get("PublishedRunbookSnapshotId")
        if not snapshot_id:
            return {"status": "Failed", "error": "Runbook has no published snapshot"}

        # Create the runbook run
        payload = {
            "RunbookId": runbook_id,
            "RunbookSnapshotId": snapshot_id,
            "EnvironmentId": environment_id,
        }
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


def _register_runbook_tool(runbook: dict, environments: list[dict]) -> None:
    """Register a single runbook as an MCP tool with task support."""
    runbook_id = runbook["Id"]
    runbook_name = runbook["Name"]
    project_id = runbook.get("ProjectId", "")
    description = runbook.get("Description") or f"Run the '{runbook_name}' runbook"
    tool_name = f"run_runbook_{_sanitize_tool_name(runbook_name)}"

    logger.info(
        f"Registering runbook tool: {tool_name} (runbook_id={runbook_id}, project_id={project_id})"
    )

    env_names = [e["Name"] for e in environments]
    env_help = ", ".join(env_names) if env_names else "No environments found"

    async def run_tool(environment_name: str) -> dict:
        """placeholder"""
        # Resolve environment name to ID
        env_map = {e["Name"].lower(): e["Id"] for e in environments}
        env_id: str | None = env_map.get(environment_name.lower())
        if not env_id:
            return {
                "status": "Failed",
                "error": f"Environment '{environment_name}' not found. Available: {env_help}",
            }
        return await _run_runbook(runbook_id, env_id)

    # Set the docstring dynamically for the tool description
    run_tool.__doc__ = (
        f"{description}\n\n"
        f"Project ID: {project_id}\n"
        f"Available environments: {env_help}\n\n"
        f"Args:\n"
        f"    environment_name: The name of the environment to run the runbook in"
    )
    run_tool.__name__ = tool_name

    mcp.tool(name=tool_name, description=description, task=True)(run_tool)


async def register_all_runbook_tools() -> None:
    """Fetch runbooks and environments, then register each runbook as a tool."""
    runbooks, environments = await asyncio.gather(
        _get_all_runbooks(),
        _get_environments(),
    )
    for runbook in runbooks:
        _register_runbook_tool(runbook, environments)


# Register tools at import time by running the async setup
asyncio.run(register_all_runbook_tools())


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
