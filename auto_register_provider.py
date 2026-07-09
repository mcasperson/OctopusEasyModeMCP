"""Google OAuth provider that auto-registers unknown clients.

When a client attempts to authorize without first calling Dynamic Client Registration,
the standard OAuthProxy returns None from get_client(), which causes the SDK to return
a 400 "Client Not Registered" error. This subclass intercepts that case and synthesizes
a permissive ProxyDCRClient on-the-fly, persisting it to the client store so subsequent
requests succeed without re-synthesis.
"""

from __future__ import annotations

import logging

from mcp.server.auth.provider import OAuthClientInformationFull
from pydantic import AnyUrl

from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
from fastmcp.server.auth.providers.google import GoogleProvider

logger = logging.getLogger(__name__)


class AutoRegisterGoogleProvider(GoogleProvider):
    """Google provider that auto-registers unknown OAuth clients.

    This was necessary for the Intellij Copilot plugin.
    """

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a client, auto-registering it if not found."""
        # Try the normal lookup first (storage, CIMD, upstream match)
        client = await super().get_client(client_id)
        if client is not None:
            # Ensure allow_unregistered_redirect_uris is set, since it's
            # excluded from serialization (exclude=True) and reverts to False
            # when loaded from storage.
            if isinstance(client, ProxyDCRClient):
                client.allow_unregistered_redirect_uris = True
            return client

        # Auto-register the unknown client
        logger.info(
            "Auto-registering unknown client_id=%s",
            client_id,
        )

        proxy_client = ProxyDCRClient(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl("http://localhost")],
            grant_types=["authorization_code", "refresh_token"],
            scope=self._default_scope_str,
            token_endpoint_auth_method="none",
            allowed_redirect_uri_patterns=self._allowed_client_redirect_uris,
            allow_unregistered_redirect_uris=True,
        )

        await self._client_store.put(key=client_id, value=proxy_client)
        return proxy_client

