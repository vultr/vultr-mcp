"""Phase 4: OAuthProxy wiring.

Full browser OAuth can't run in a unit test, so these assert the pieces:
build_auth honours the enable flag, constructs an OAuthProxy from resolved
endpoints (no network), attaches to the server, and the server then advertises
the OAuth metadata MCP clients use for Dynamic Client Registration.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from vultr_mcp.auth import UpstreamEndpoints, build_auth
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
    assert auth.__class__.__name__ == "OAuthProxy"


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
