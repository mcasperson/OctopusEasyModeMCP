from datetime import datetime, timezone

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.tasks.context import get_task_context, _recall_snapshot
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from oidc_proxy import OidcAccessToken
from mcp.server.auth.middleware.auth_context import (
    get_access_token as _sdk_get_access_token,
)


def get_oidc_access_token() -> OidcAccessToken | None:
    """Get the FastMCP access token from the current context.

    This function first tries to get the token from the current HTTP request's scope,
    which is more reliable for long-lived connections where the SDK's auth_context_var
    may become stale after token refresh. Falls back to the SDK's context var if no
    request is available. In background tasks (Docket workers), falls back to the
    token snapshot stored in Redis at task submission time.

    Returns:
        The access token if an authenticated user is available, None otherwise.
    """
    access_token: OidcAccessToken | None = None

    # First, try to get from current HTTP request's scope (issue #1863)
    # This is more reliable than auth_context_var for Streamable HTTP sessions
    # where tokens may be refreshed between MCP messages
    try:
        request = get_http_request()
        user = request.scope.get("user")
        if isinstance(user, AuthenticatedUser):
            access_token = user.access_token
    except RuntimeError:
        # No HTTP request available, fall back to context var
        pass

    # Fall back to SDK's context var if we didn't get a token from the request
    if access_token is None:
        access_token = _sdk_get_access_token()

    # Fall back to background task snapshot (#3095).  In Docket workers,
    # neither the HTTP request nor the SDK context var is available; the
    # snapshot is preloaded by restore_task_snapshot before user code runs.
    if access_token is None:
        task_info = get_task_context()
        snapshot = _recall_snapshot(task_info.task_id) if task_info else None
        if snapshot is not None and snapshot.access_token_json is not None:
            task_token = OidcAccessToken.model_validate_json(snapshot.access_token_json)
            if task_token.expires_at is not None:
                if task_token.expires_at < int(datetime.now(timezone.utc).timestamp()):
                    return None
            return task_token

    if access_token is None or isinstance(access_token, OidcAccessToken):
        return access_token

    # If the object is not a FastMCP AccessToken, convert it to one if the
    # fields are compatible (e.g. `claims` is not present in the SDK's AccessToken).
    # This is a workaround for the case where the SDK or auth provider returns a different type
    # If it fails, it will raise a TypeError
    try:
        access_token_as_dict = access_token.model_dump()
        return OidcAccessToken(
            token=access_token_as_dict["token"],
            id_token=access_token_as_dict["id_token"],
            client_id=access_token_as_dict["client_id"],
            scopes=access_token_as_dict["scopes"],
            # Optional fields
            expires_at=access_token_as_dict.get("expires_at"),
            resource=access_token_as_dict.get("resource"),
            claims=access_token_as_dict.get("claims") or {},
        )
    except Exception as e:
        raise TypeError(
            f"Expected fastmcp.server.auth.auth.AccessToken, got {type(access_token).__name__}. "
            "Ensure the SDK is using the correct AccessToken type."
        ) from e