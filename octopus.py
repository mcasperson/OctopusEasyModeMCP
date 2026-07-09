"""Octopus Deploy API client functions."""

import os
import logging

import httpx

from fastmcp.server.dependencies import get_access_token

logger = logging.getLogger(__name__)

OCTOPUS_URL = os.environ["EASY_MODE_MCP_OCTOPUS_URL"]
OCTOPUS_API_KEY = os.environ["EASY_MODE_MCP_OCTOPUS_API_KEY"]
OCTOPUS_SPACE_ID = os.environ["EASY_MODE_MCP_OCTOPUS_SPACE_ID"]

# Auth type: "google", "github", or "none" (default: "google")
AUTH_TYPE = os.environ.get("EASY_MODE_MCP_AUTH_TYPE", "google").lower()
AUTH_ENABLED = AUTH_TYPE != "none"


def octopus_headers(bearer_token: str | None = None) -> dict[str, str]:
    """Build Octopus API headers with either a bearer token or API key."""
    if bearer_token:
        return {"Authorization": f"Bearer {bearer_token}"}
    return {"X-Octopus-ApiKey": OCTOPUS_API_KEY}


async def exchange_token_for_octopus_token(id_token: str) -> str:
    """Exchange an ID token for an Octopus access token via token exchange.

    Args:
        id_token: The ID token (JWT) to exchange.

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


async def get_authenticated_headers() -> dict[str, str]:
    """Get Octopus API headers based on current auth mode."""
    if AUTH_ENABLED:
        google_access_token = get_access_token()
        access_token = await exchange_token_for_octopus_token(google_access_token.id_token)
        return octopus_headers(access_token)
    return octopus_headers()


async def get_all_runbooks() -> list[dict]:
    """Fetch all runbooks from the Octopus space (database-backed and config-as-code)."""
    runbooks = []
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=octopus_headers()) as client:
        # Fetch database-backed runbooks (only published ones)
        runbooks.extend(await _get_all_database_runbooks(client))

        # Fetch config-as-code runbooks from version-controlled projects
        runbooks.extend(await _get_all_cac_runbooks(client))

    return runbooks


async def _get_all_cac_runbooks(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all config-as-code runbooks from version-controlled projects."""
    runbooks = []
    try:
        projects = await _get_all_projects(client)
    except Exception:
        logger.exception("Failed to fetch projects for config-as-code runbooks")
        return runbooks

    for project in projects:
        if not project.get("IsVersionControlled"):
            continue
        persistence = project.get("PersistenceSettings", {})
        if persistence.get("Type") != "VersionControlled":
            continue
        git_ref: str = persistence.get("DefaultBranch", "")
        if not git_ref:
            continue
        project_id = project["Id"]
        skip = 0
        take = 30
        try:
            while True:
                data = await _fetch_cac_runbooks_page(client, project_id, git_ref, skip, take)
                items = data.get("Items", [])
                runbooks.extend(items)
                if skip + take >= data.get("TotalResults", 0):
                    break
                skip += take
        except Exception:
            logger.exception(
                "Failed to fetch config-as-code runbooks for project %s (skip=%d)",
                project_id, skip,
            )

    return runbooks


async def _get_all_database_runbooks(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all published database-backed runbooks."""
    runbooks = []
    skip = 0
    take = 30
    try:
        while True:
            data = await _fetch_database_runbooks_page(client, skip, take)
            items = data.get("Items", [])
            for item in items:
                if item.get("PublishedRunbookSnapshotId"):
                    runbooks.append(item)
            if skip + take >= data.get("TotalResults", 0):
                break
            skip += take
    except Exception:
        logger.exception("Failed to fetch database-backed runbooks (skip=%d)", skip)
    return runbooks


async def _fetch_cac_runbooks_page(client: httpx.AsyncClient, project_id: str, git_ref: str, skip: int, take: int) -> dict:
    """Fetch a single page of config-as-code runbooks for a project."""
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/projects/{project_id}/{git_ref}/runbooks",
        params={"skip": skip, "take": take},
    )
    resp.raise_for_status()
    return resp.json()


async def _fetch_database_runbooks_page(client: httpx.AsyncClient, skip: int, take: int) -> dict:
    """Fetch a single page of database-backed runbooks."""
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/runbooks",
        params={"skip": skip, "take": take},
    )
    resp.raise_for_status()
    return resp.json()


async def _get_all_projects(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all projects from the Octopus space."""
    projects = []
    skip = 0
    take = 30
    while True:
        resp = await client.get(
            f"/api/{OCTOPUS_SPACE_ID}/projects",
            params={"skip": skip, "take": take},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("Items", [])
        projects.extend(items)
        if skip + take >= data.get("TotalResults", 0):
            break
        skip += take
    return projects


async def get_project_prompted_variables(project_id: str) -> list[dict]:
    """Fetch prompted variables for a project."""
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=octopus_headers()) as client:
        variable_set_id = f"variableset-{project_id}"
        resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/variables/{variable_set_id}")
        resp.raise_for_status()
        data = resp.json()
        prompted = []
        for var in data.get("Variables", []):
            prompt = var.get("Prompt")
            if prompt:
                scope = var.get("Scope", {})
                process_owners = scope.get("ProcessOwner", [])
                prompted.append({
                    "id": var["Id"],
                    "name": var["Name"],
                    "label": prompt.get("Label", var["Name"]),
                    "description": prompt.get("Description", ""),
                    "required": prompt.get("Required", False),
                    "default": var.get("Value", ""),
                    "process_owners": process_owners,
                })
        return prompted


async def get_runbook_preview_form(client: httpx.AsyncClient, snapshot_id: str, environment_id: str) -> tuple[list[dict], dict[str, str]]:
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
    form_values = dict(form.get("Values", {}))
    return elements, form_values


async def create_runbook_run(client: httpx.AsyncClient, runbook_id: str, snapshot_id: str, environment_id: str, form_values: dict[str, str], tenant_id: str | None = None) -> str:
    """Create a runbook run and return the task ID.

    Args:
        client: The HTTP client to use
        runbook_id: The runbook to run
        snapshot_id: The published runbook snapshot ID
        environment_id: The environment to run in
        form_values: Form values to submit with the run
        tenant_id: Optional tenant ID for tenanted runs

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
    if tenant_id:
        payload["TenantId"] = tenant_id

    resp = await client.post(
        f"/api/{OCTOPUS_SPACE_ID}/runbookRuns",
        json=payload,
    )
    resp.raise_for_status()
    run = resp.json()
    return run["TaskId"]


async def get_task_raw_log(client: httpx.AsyncClient, task_id: str) -> str:
    """Download the raw log for a server task."""
    log_resp = await client.get(f"/api/tasks/{task_id}/raw")
    log_resp.raise_for_status()
    return log_resp.text


async def get_task_status(client: httpx.AsyncClient, task_id: str) -> dict:
    """Fetch a server task and return its JSON representation."""
    resp = await client.get(f"/api/tasks/{task_id}")
    resp.raise_for_status()
    return resp.json()


async def get_pending_interruptions(client: httpx.AsyncClient, task_id: str) -> list[dict]:
    """Fetch pending interruptions for a server task."""
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions",
        params={"regarding": task_id, "pendingOnly": "true"},
    )
    resp.raise_for_status()
    return resp.json().get("Items", [])


async def submit_interruption(client: httpx.AsyncClient, interruption_id: str, payload: dict) -> None:
    """Submit a response to a manual intervention interruption."""
    resp = await client.post(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption_id}/submit",
        json=payload,
    )
    if resp.status_code != 200:
        logger.error(f"Intervention submit failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()


async def take_interruption_responsibility(client: httpx.AsyncClient, interruption_id: str) -> None:
    """Take responsibility for a manual intervention interruption."""
    resp = await client.put(
        f"/api/{OCTOPUS_SPACE_ID}/interruptions/{interruption_id}/responsible",
    )
    if resp.status_code != 200:
        logger.error(f"Taking responsibility failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    logger.info(f"Took responsibility for interruption '{interruption_id}'")


async def search_tenants(client: httpx.AsyncClient, tenant_name: str) -> list[dict]:
    """Search for tenants by partial name match."""
    resp = await client.get(
        f"/api/{OCTOPUS_SPACE_ID}/tenants",
        params={"partialName": tenant_name, "take": 100},
    )
    resp.raise_for_status()
    return resp.json().get("Items", [])


async def get_tenant_detail(client: httpx.AsyncClient, tenant_id: str) -> dict:
    """Fetch full tenant details by ID."""
    resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/tenants/{tenant_id}")
    resp.raise_for_status()
    return resp.json()


async def resolve_tenant(client: httpx.AsyncClient, tenant_name: str, project_id: str, environment_id: str) -> tuple[str | None, str | None]:
    """Resolve a tenant name to an ID and validate it's linked to the project and environment.

    Returns:
        A tuple of (tenant_id, error_message). If successful, error_message is None.
    """
    tenants = await search_tenants(client, tenant_name)

    # Find exact match
    tenant = None
    for t in tenants:
        if t["Name"].lower() == tenant_name.lower():
            tenant = t
            break

    if tenant is None:
        available = [t["Name"] for t in tenants[:10]]
        return None, f"Tenant '{tenant_name}' not found. Partial matches: {available}"

    tenant_id = tenant["Id"]

    # Fetch full tenant details to check project/environment linkage
    tenant_detail = await get_tenant_detail(client, tenant_id)

    project_envs = tenant_detail.get("ProjectEnvironments", {})
    if project_id not in project_envs:
        return None, f"Tenant '{tenant_name}' is not linked to project '{project_id}'."

    linked_envs = project_envs[project_id]
    if environment_id not in linked_envs:
        return None, f"Tenant '{tenant_name}' is not linked to environment '{environment_id}' for this project."

    return tenant_id, None


async def get_published_snapshot_id(client: httpx.AsyncClient, runbook_id: str) -> str | None:
    """Fetch a runbook and return its published snapshot ID."""
    resp = await client.get(f"/api/{OCTOPUS_SPACE_ID}/runbooks/{runbook_id}")
    resp.raise_for_status()
    runbook = resp.json()
    return runbook.get("PublishedRunbookSnapshotId")


async def get_environments() -> list[dict]:
    """Fetch all environments from the Octopus space."""
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=octopus_headers()) as client:
        resp = await client.get(
            f"/api/{OCTOPUS_SPACE_ID}/environments",
            params={"take": 1000},
        )
        resp.raise_for_status()
        return resp.json().get("Items", [])


async def get_runbook_environments(runbook: dict) -> list[dict]:
    """Fetch environments available for a runbook via its RunbookEnvironments link."""
    environments_link = runbook.get("Links", {}).get("RunbookEnvironments")
    if not environments_link:
        return []
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=octopus_headers()) as client:
        resp = await client.get(environments_link)
        resp.raise_for_status()
        return resp.json()


async def get_project_ids_by_names(project_names: list[str]) -> set[str]:
    """Fetch project IDs for the given project names."""
    project_ids = set()
    async with httpx.AsyncClient(base_url=OCTOPUS_URL, headers=octopus_headers()) as client:
        for name in project_names:
            resp = await client.get(
                f"/api/{OCTOPUS_SPACE_ID}/projects",
                params={"partialName": name, "take": 100},
            )
            resp.raise_for_status()
            for project in resp.json().get("Items", []):
                if project["Name"].lower() == name.lower():
                    project_ids.add(project["Id"])
    return project_ids


def parse_interruption_form(interruption: dict) -> tuple[str, str | None, str | None]:
    """Parse an interruption's form to extract instructions and element IDs.

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


def map_variables_to_form_values(variable_values: dict[str, str], elements: list[dict], form_values: dict[str, str]) -> dict[str, str]:
    """Map variable names to form element IDs, overriding default form values.

    Matches by control label, control name, element ID, or description.
    """
    for element in elements:
        element_id = element.get("Name", "")
        control = element.get("Control", {})
        control_label = control.get("Label", "")
        control_name = control.get("Name", "")
        control_description = control.get("Description", "")

        for var_name, var_value in variable_values.items():
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

    return form_values


def build_task_result(task: dict, task_id: str, raw_log: str) -> dict:
    """Build the final result dict from a completed task."""
    return {
        "status": task.get("State"),
        "taskId": task_id,
        "description": task.get("Description", ""),
        "errorMessage": task.get("ErrorMessage", ""),
        "duration": task.get("Duration", ""),
        "logs": raw_log,
    }


async def build_form_values(client: httpx.AsyncClient, snapshot_id: str, environment_id: str, variable_values: dict[str, str]) -> dict[str, str]:
    """Fetch the preview form and map variable values to form element IDs."""
    elements, form_values = await get_runbook_preview_form(client, snapshot_id, environment_id)
    logger.info(f"Form elements: {[(e.get('Name'), e.get('Control', {})) for e in elements]}")
    logger.info(f"Form default values: {form_values}")
    return map_variables_to_form_values(variable_values, elements, form_values)

