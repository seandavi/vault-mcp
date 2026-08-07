"""Pluggable OAuth for the HTTP transport.

The tailnet deployment runs with no auth (Tailscale is the auth layer);
a public deployment picks a provider with `--auth <name>`. Providers are
one registry entry each, so adding another (e.g. GitHub for the public
Bioconductor layer) means one line here and nothing else.

Credentials come from the environment — never argv or config files — so
they can be injected from a secrets manager at launch.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider

CLIENT_ID_ENV = "VAULT_MCP_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "VAULT_MCP_OAUTH_CLIENT_SECRET"

# name -> provider class; all take (client_id, client_secret, base_url).
PROVIDERS: dict[str, type[AuthProvider]] = {
    "google": GoogleProvider,
    "github": GitHubProvider,
}


def build_auth(provider: str, base_url: str | None) -> AuthProvider:
    """Construct the named OAuth provider from environment credentials.

    base_url is the public URL clients reach the server at (the ts.net URL
    when fronted by `tailscale serve`) — OAuth callbacks are derived from it,
    so `<base_url>/auth/callback` must be registered with the provider.
    """
    try:
        cls = PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unknown auth provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}"
        ) from None
    if not base_url:
        raise ValueError(
            "an auth provider needs the public base URL clients use "
            "(--base-url or VAULT_MCP_BASE_URL), e.g. https://mac.tailnet.ts.net"
        )
    client_id = os.environ.get(CLIENT_ID_ENV)
    client_secret = os.environ.get(CLIENT_SECRET_ENV)
    if not client_id or not client_secret:
        raise ValueError(f"set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} in the environment")
    return cls(client_id=client_id, client_secret=client_secret, base_url=base_url)
