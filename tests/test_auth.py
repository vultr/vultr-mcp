"""OAuthProxy wiring.

Browser OAuth can't run in a unit test, so these assert the pieces: build_auth
honours the enable flag, builds a proxy from resolved endpoints without the
network, attaches to the server, and the server advertises the metadata clients
use for Dynamic Client Registration.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from fastmcp.server.auth.oauth_proxy import OAuthProxy

from vultr_mcp.auth import UpstreamEndpoints, VultrOAuthProxy, build_auth
from vultr_mcp.server import create_server, load_spec

FAKE = UpstreamEndpoints(
    authorization_endpoint="https://my.vultr.com/oauth/authorize",
    token_endpoint="https://api.vultr.com/v2/oidc/provider/prov-123/token",
    jwks_uri="https://api.vultr.com/v2/oidc/issuer/iss-456/jwks",
    issuer="https://api.vultr.com/v2/oidc/provider/prov-123",
)


def _enable_env(monkeypatch):
    monkeypatch.setenv("VULTR_OIDC_ENABLED", "true")
    monkeypatch.setenv("VULTR_OIDC_PROVIDER_ID", "prov-123")
    monkeypatch.setenv("VULTR_OAUTH_CLIENT_ID", "client-abc")
    monkeypatch.setenv("VULTR_OAUTH_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://vultrmcp.com")


def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("VULTR_OIDC_ENABLED", raising=False)
    assert build_auth(endpoints=FAKE) is None


def test_enabled_missing_vars_raises(monkeypatch):
    monkeypatch.setenv("VULTR_OIDC_ENABLED", "true")
    monkeypatch.delenv("VULTR_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("VULTR_OIDC_PROVIDER_ID", "prov-123")
    monkeypatch.setenv("VULTR_OAUTH_CLIENT_ID", "client-abc")
    with pytest.raises(RuntimeError, match="CLIENT_SECRET"):
        build_auth(endpoints=FAKE)


def test_build_auth_constructs_proxy(monkeypatch):
    _enable_env(monkeypatch)
    auth = build_auth(endpoints=FAKE)
    assert auth is not None
    assert isinstance(auth, OAuthProxy)
    # Specifically the subclass that restores the raw-API-key bearer path —
    # see VultrOAuthProxy's docstring for why plain OAuthProxy 401s it.
    assert isinstance(auth, VultrOAuthProxy)


def test_raw_api_key_bearer_accepted_without_oauth_swap(monkeypatch):
    """A non-JWT-shaped bearer must be accepted immediately, without going
    through OAuthProxy's JWT/token-swap machinery at all — that machinery
    requires a proxy-issued JWT and 401s ("invalid_token") anything else,
    which is exactly the bug this override fixes. Regression test for the
    2026-09 finding: raw Vultr API keys could never authenticate once
    VULTR_OIDC_ENABLED=true, regardless of key validity.
    """
    _enable_env(monkeypatch)
    auth = build_auth(endpoints=FAKE)
    assert isinstance(auth, VultrOAuthProxy)

    raw_key = "2CYDYSI5HSN4WHBZIDCV7ZDM5JRBUVQOZ4AA"  # opaque, no dots
    result = asyncio.run(auth.load_access_token(raw_key))

    assert result is not None
    assert result.token == raw_key
    assert result.client_id == "vultr-api-key"
    assert result.claims == {"auth_method": "api_key"}


def test_jwt_shaped_bearer_still_goes_through_oauth_swap(monkeypatch):
    """A JWT-shaped bearer that isn't actually a proxy-issued JWT should
    still be rejected by the normal OAuth swap path (None, not a raw-key
    accept) — the override must not widen acceptance beyond opaque tokens.
    """
    _enable_env(monkeypatch)
    auth = build_auth(endpoints=FAKE)

    fake_jwt = "header.payload.signature"  # JWT-shaped, not proxy-issued
    result = asyncio.run(auth.load_access_token(fake_jwt))

    assert result is None


def test_server_advertises_oauth_metadata(monkeypatch):
    """With auth attached, the HTTP app serves protected-resource metadata."""
    _enable_env(monkeypatch)
    auth = build_auth(endpoints=FAKE)
    server = create_server(load_spec(), auth=auth)
    app = server.http_app()

    port = _free_port()

    async def run():
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        uv = uvicorn.Server(config)
        task = asyncio.create_task(uv.serve())
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            async with httpx.AsyncClient() as hc:
                # AS metadata — MCP clients read this to find the registration
                # (DCR) + authorize + token endpoints for the zero-config flow.
                r = await hc.get(
                    f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server"
                )
            return r
        finally:
            uv.should_exit = True
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    resp = asyncio.run(run())
    assert resp.status_code == 200, f"expected metadata, got {resp.status_code}"
    body = resp.json()
    # The registration endpoint is what makes the paste-a-URL (no client_id /
    # secret) experience possible — its presence is the Phase 4 win.
    assert "registration_endpoint" in body, f"no DCR endpoint advertised: {body}"
    assert "authorization_endpoint" in body and "token_endpoint" in body

    # The device grant is discovered only through this document, so an
    # unadvertised endpoint is an absent feature however well it works. A
    # client on a headless host (an agent on a Vultr instance, reached over
    # SSH) reads these two fields to learn it has an alternative to a loopback
    # callback it cannot receive.
    assert body["device_authorization_endpoint"].endswith("/device_authorization"), (
        f"device endpoint not advertised: {body}"
    )
    assert "urn:ietf:params:oauth:grant-type:device_code" in body["grant_types_supported"]
    # Patching the document must not cost the grants that were already there.
    assert "authorization_code" in body["grant_types_supported"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
