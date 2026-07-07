from typing import Literal

import httpx
from fastmcp.server.auth.providers.google import _normalize_google_scope, GoogleTokenVerifier
from fastmcp.utilities.auth import parse_scopes
from fastmcp.utilities.logging import get_logger
from key_value.aio.protocols import AsyncKeyValue
from pydantic import AnyHttpUrl

from oidc_proxy import OidcOAuthProxy

logger = get_logger(__name__)

class GoogleOidcProvider(OidcOAuthProxy):
    """Complete Google OAuth provider for FastMCP.

    This provider makes it trivial to add Google OAuth protection to any
    FastMCP server. Just provide your Google OAuth app credentials and
    a base URL, and you're ready to go.

    Features:
    - Transparent OAuth proxy to Google
    - Automatic token validation via Google's tokeninfo API
    - User information extraction from Google APIs
    - Minimal configuration required

    Example:
        ```python
        from fastmcp import FastMCP
        from fastmcp.server.auth.providers.google import GoogleProvider

        auth = GoogleOidcProvider(
            client_id="123456789.apps.googleusercontent.com",
            client_secret="GOCSPX-abc123...",
            base_url="https://my-server.com"
        )

        mcp = FastMCP("My App", auth=auth)
        ```
    """

    def __init__(
            self,
            *,
            client_id: str,
            client_secret: str | None = None,
            base_url: AnyHttpUrl | str,
            resource_base_url: AnyHttpUrl | str | None = None,
            issuer_url: AnyHttpUrl | str | None = None,
            redirect_path: str | None = None,
            required_scopes: list[str] | None = None,
            valid_scopes: list[str] | None = None,
            timeout_seconds: int = 10,
            allowed_client_redirect_uris: list[str] | None = None,
            client_storage: AsyncKeyValue | None = None,
            jwt_signing_key: str | bytes | None = None,
            require_authorization_consent: bool | Literal["remember", "external"] = True,
            consent_csp_policy: str | None = None,
            forward_resource: bool = True,
            fallback_refresh_token_expiry_seconds: int | None = None,
            fastmcp_access_token_expiry_seconds: int | None = None,
            token_expiry_threshold_seconds: int = 0,
            extra_authorize_params: dict[str, str] | None = None,
            http_client: httpx.AsyncClient | None = None,
            enable_cimd: bool = True,
    ):
        """Initialize Google OAuth provider.

        Args:
            client_id: Google OAuth client ID (e.g., "123456789.apps.googleusercontent.com")
            client_secret: Google OAuth client secret (e.g., "GOCSPX-abc123...").
                Optional for PKCE public clients (e.g., native apps). When omitted,
                jwt_signing_key must be provided.
            base_url: Public URL where OAuth endpoints will be accessible (includes any mount path)
            resource_base_url: Optional public base URL for the protected resource metadata
                and token audience. Defaults to ``base_url``.
            issuer_url: Issuer URL for OAuth metadata (defaults to base_url). Use root-level URL
                to avoid 404s during discovery when mounting under a path.
            redirect_path: Redirect path configured in Google OAuth app (defaults to "/auth/callback")
            required_scopes: Required Google scopes (defaults to ["openid"]). Common scopes include:
                - "openid" for OpenID Connect (default)
                - "https://www.googleapis.com/auth/userinfo.email" for email access
                - "https://www.googleapis.com/auth/userinfo.profile" for profile info
                Google scope shorthands like "email" and "profile" are automatically
                normalized to their full URI forms for token verification.
            valid_scopes: All scopes that clients are allowed to request, advertised through
                well-known endpoints. Defaults to required_scopes if not provided. Use this
                when you want clients to be able to request additional scopes beyond the
                required minimum. Shorthands are normalized to full URI forms.
            timeout_seconds: HTTP request timeout for Google API calls (defaults to 10)
            allowed_client_redirect_uris: List of allowed redirect URI patterns for MCP clients.
                If None (default), all URIs are allowed. If empty list, no URIs are allowed.
            client_storage: Storage backend for OAuth state (client registrations, encrypted tokens).
                If None, an encrypted file store will be created in the data directory
                (derived from `platformdirs`).
            jwt_signing_key: Secret for signing FastMCP JWT tokens (any string or bytes). If bytes are provided,
                they will be used as is. If a string is provided, it will be derived into a 32-byte key. If not
                provided, the upstream client secret will be used to derive a 32-byte key using PBKDF2.
            require_authorization_consent: Whether to require user consent before authorizing clients (default True).
                When True, users see a consent screen before being redirected to Google.
                When False, authorization proceeds directly without user confirmation.
                When "external", the built-in consent screen is skipped but no warning is
                logged, indicating that consent is handled externally (e.g. by Google's own consent).
                SECURITY WARNING: Only set to False for local development or testing environments.
            extra_authorize_params: Additional parameters to forward to Google's authorization endpoint.
                By default, GoogleProvider sets {"access_type": "offline", "prompt": "consent"} to ensure
                refresh tokens are returned. You can override these defaults or add additional parameters.
                Example: {"prompt": "select_account"} to let users choose their Google account.
            http_client: Optional httpx.AsyncClient for connection pooling in token verification.
                When provided, the client is reused across verify_token calls and the caller
                is responsible for its lifecycle. When None (default), a fresh client is created per call.
            enable_cimd: Enable CIMD (Client ID Metadata Document) support for URL-based
                client IDs (default True). Set to False to disable.
            fallback_refresh_token_expiry_seconds: Lifetime for the FastMCP-issued
                refresh token when the upstream provider omits `refresh_expires_in`
                (e.g. Cognito, GitHub, many OIDC IdPs). Defaults to 1 year. The upstream
                refresh remains the source of truth. See `OAuthProxy` for details.
            fastmcp_access_token_expiry_seconds: Lifetime for the FastMCP-issued access
                token, decoupling it from the upstream provider's `expires_in`. Defaults
                to None (mirror the upstream lifetime). Set this for bridges whose
                upstream issues short-lived access tokens that some MCP clients can't
                refresh gracefully (e.g. `mcp-remote`). See `OAuthProxy` for details.
            token_expiry_threshold_seconds: Number of seconds before actual expiry to
                treat a token as expired, refreshing early to avoid races. Defaults to 0.
        """
        # Parse scopes if provided as string
        # Google requires at least one scope - openid is the minimal OIDC scope
        required_scopes_final = (
            parse_scopes(required_scopes) if required_scopes is not None else ["openid"]
        )

        # Normalize valid_scopes if provided
        parsed_valid_scopes = (
            parse_scopes(valid_scopes) if valid_scopes is not None else None
        )
        valid_scopes_final = (
            [_normalize_google_scope(s) for s in parsed_valid_scopes]
            if parsed_valid_scopes is not None
            else None
        )

        # Create Google token verifier
        # Normalization of shorthand scopes (e.g. "email" -> full URI) happens
        # inside GoogleTokenVerifier so required_scopes match what Google returns.
        token_verifier = GoogleTokenVerifier(
            required_scopes=required_scopes_final,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )

        # Set Google-specific defaults for extra authorize params
        # access_type=offline ensures refresh tokens are returned
        # prompt=consent forces consent screen to get refresh token (Google only issues on first auth otherwise)
        google_defaults = {
            "access_type": "offline",
            "prompt": "consent",
        }
        # User-provided params override defaults
        if extra_authorize_params:
            google_defaults.update(extra_authorize_params)
        extra_authorize_params_final = google_defaults

        # Initialize OAuth proxy with Google endpoints
        super().__init__(
            upstream_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            upstream_token_endpoint="https://oauth2.googleapis.com/token",
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=token_verifier,
            base_url=base_url,
            resource_base_url=resource_base_url,
            redirect_path=redirect_path,
            issuer_url=issuer_url or base_url,  # Default to base_url if not specified
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            client_storage=client_storage,
            jwt_signing_key=jwt_signing_key,
            require_authorization_consent=require_authorization_consent,
            consent_csp_policy=consent_csp_policy,
            forward_resource=forward_resource,
            fallback_refresh_token_expiry_seconds=fallback_refresh_token_expiry_seconds,
            fastmcp_access_token_expiry_seconds=fastmcp_access_token_expiry_seconds,
            token_expiry_threshold_seconds=token_expiry_threshold_seconds,
            extra_authorize_params=extra_authorize_params_final,
            valid_scopes=valid_scopes_final,
            enable_cimd=enable_cimd,
        )

        logger.debug(
            "Initialized Google OAuth provider for client %s with scopes: %s",
            client_id,
            required_scopes_final,
        )