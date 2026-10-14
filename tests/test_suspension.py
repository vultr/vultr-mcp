"""A suspended Vultr grant keeps its MCP session, so restoring access needs no reconnect.

Driven through the real proxy and FastMCP's own token stores and issuance; only
Vultr's token endpoint is faked, and the upstream token check honours each
token's exp the way the JWKS verifier does.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from vultr_mcp import suspension
from vultr_mcp.suspension import (
    SUSPENDED_MESSAGE_CLAIM,
    SUSPENDED_REASON_CLAIM,
    SUSPENSION_GRACE_S,
    SUSPENSION_REASONS,
    SuspendedTransport,
)

ORG_DISABLED = (
    "Access Denied. MCP access is not enabled for your organization. "
    "Ask an administrator of your organization to enable it in your organization's settings."
)
USER_REMOVED = (
    "MCP access is not currently granted to your Vultr user. "
    "An administrator of your Vultr organization can restore it, and this connection resumes when they do."
)
CLIENT = OAuthClientInformationFull(client_id="mcp-client-1", redirect_uris=["http://127.0.0.1:9/cb"])


def _upstream_jwt(n: int, exp: float) -> str:
    """A stand-in for a Vultr access token: only its exp and identity claims matter here."""
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': int(exp), 'sub': '7', 'acctid': 1234, 'n': n})}.sig"


class _ExpiryVerifier:
    """Accepts an upstream token until its exp, as the JWKS verifier would."""

    async def verify_token(self, token: str):
        claims = suspension.unverified_claims(token)
        if claims.get("exp", 0) <= time.time():
            return None
        return AccessToken(token=token, client_id="vultr-mcp-app", scopes=[], expires_at=claims["exp"], claims=claims)


class _Vultr:
    """Vultr's token endpoint: issues fresh tokens, or refuses with whatever it is told to say."""

    def __init__(self) -> None:
        self.refusal: str | None = None
        self.refresh_calls = 0
        self.issued = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.refresh_calls += 1
        if self.refusal is not None:
            return httpx.Response(400, json={"error": self.refusal})
        self.issued += 1
        return httpx.Response(200, json={
            "access_token": _upstream_jwt(self.issued, time.time() + 3600),
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"vultr-refresh-{self.issued}",
        })


@pytest.fixture
def vultr():
    return _Vultr()


@pytest.fixture
def proxy(monkeypatch, vultr):
    from vultr_mcp.auth import UpstreamEndpoints, build_auth

    for name, value in {
        "VULTR_OIDC_ENABLED": "true",
        "VULTR_OIDC_PROVIDER_ID": "prov-123",
        "VULTR_OAUTH_CLIENT_ID": "client-abc",
        "VULTR_OAUTH_CLIENT_SECRET": "secret-xyz",
        "MCP_RESOURCE_URL": "https://mcp.vultr.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    built = build_auth(endpoints=UpstreamEndpoints(
        authorization_endpoint="https://my.vultr.com/oauth/authorize",
        token_endpoint="https://api.vultr.com/v2/oidc/provider/prov-123/token",
        jwks_uri="https://api.vultr.com/v2/oidc/issuer/iss-456/jwks",
        issuer="https://api.vultr.com/v2/oidc/provider/prov-123",
    ))
    # Building the routes is what creates FastMCP's token issuer, as the app does at startup.
    built.get_routes()
    built._token_validator = _ExpiryVerifier()
    built._create_upstream_oauth_client = lambda: AsyncOAuth2Client(
        client_id="client-abc", client_secret="secret-xyz", transport=httpx.MockTransport(vultr.handle)
    )
    return built


async def _sign_in(proxy) -> tuple[str, str]:
    """A signed-in session, through FastMCP's own code exchange; returns (proxy access token, proxy refresh token)."""
    code = "code-1"
    await proxy._code_store.put(key=code, value=ClientCode(
        code=code, client_id=CLIENT.client_id, redirect_uri="http://127.0.0.1:9/cb",
        code_challenge=None, code_challenge_method="S256", scopes=[],
        idp_tokens={"access_token": _upstream_jwt(0, time.time() + 3600), "token_type": "Bearer",
                    "expires_in": 3600, "refresh_token": "vultr-refresh-0"},
        expires_at=time.time() + 300, created_at=time.time(),
    ))
    token = await proxy.exchange_authorization_code(CLIENT, AuthorizationCode(
        code=code, scopes=[], expires_at=time.time() + 300, client_id=CLIENT.client_id,
        code_challenge="x", redirect_uri="http://127.0.0.1:9/cb", redirect_uri_provided_explicitly=True,
    ))
    return token.access_token, token.refresh_token


async def _upstream_of(proxy, access_token: str):
    payload = proxy.jwt_issuer.verify_token(access_token)
    return await proxy._upstream_for_jti(payload["jti"])


async def _age(proxy, access_token: str, seconds_since_last_access: float) -> None:
    """Make the session's upstream token expire as if its last working access was this long ago."""
    upstream = await _upstream_of(proxy, access_token)
    upstream.expires_at = time.time() - seconds_since_last_access
    upstream.access_token = _upstream_jwt(-1, upstream.expires_at)
    await proxy._upstream_token_store.put(key=upstream.upstream_token_id, value=upstream, ttl=86400 * 60)


# --- the session on each request --------------------------------------------


async def test_an_expired_upstream_token_is_refreshed_as_normal(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)

    loaded = await proxy.load_access_token(access)

    assert loaded is not None and SUSPENDED_MESSAGE_CLAIM not in (loaded.claims or {})
    assert vultr.issued == 1


async def test_a_suspended_grant_keeps_its_session_with_vultrs_reason(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = ORG_DISABLED

    loaded = await proxy.load_access_token(access)

    assert loaded is not None
    assert loaded.claims[SUSPENDED_MESSAGE_CLAIM] == ORG_DISABLED
    assert loaded.claims[SUSPENDED_REASON_CLAIM] == "org_disabled"
    assert loaded.claims["acctid"] == 1234, "the held session still names its account for the audit log"
    assert loaded.expires_at > time.time()


async def test_restoring_access_resumes_the_same_session_on_the_next_request(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = USER_REMOVED
    assert (await proxy.load_access_token(access)).claims[SUSPENDED_REASON_CLAIM] == "user_access_removed"

    vultr.refusal = None
    resumed = await proxy.load_access_token(access)

    assert resumed is not None and SUSPENDED_MESSAGE_CLAIM not in (resumed.claims or {})


async def test_every_request_asks_vultr_again_so_nothing_is_cached(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = ORG_DISABLED

    for _ in range(3):
        await proxy.load_access_token(access)

    assert vultr.refresh_calls == 3


async def test_the_grace_ends_a_day_after_the_last_working_access(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, SUSPENSION_GRACE_S + 60)
    vultr.refusal = ORG_DISABLED

    assert await proxy.load_access_token(access) is None


async def test_any_other_refusal_ends_the_session_as_before(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = "Refresh token has expired"

    assert await proxy.load_access_token(access) is None


async def test_a_reworded_suspension_is_not_softened(proxy, vultr):
    """Matched exactly: if Vultr's wording drifts, the safe fallback is a reconnect."""
    access, _ = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = "MCP access has been paused for your organization."

    assert await proxy.load_access_token(access) is None


# --- the client's own refresh ------------------------------------------------


async def test_a_client_refresh_during_suspension_gets_new_tokens_over_the_same_grant(proxy, vultr):
    access, refresh = await _sign_in(proxy)
    await _age(proxy, access, 60)
    before = await _upstream_of(proxy, access)
    vultr.refusal = ORG_DISABLED

    loaded = await proxy.load_refresh_token(CLIENT, refresh)
    token = await proxy.exchange_refresh_token(CLIENT, loaded, loaded.scopes)

    after = await _upstream_of(proxy, token.access_token)
    assert after.upstream_token_id == before.upstream_token_id
    assert after.refresh_token == before.refresh_token == "vultr-refresh-0", "Vultr's refresh token is kept, never rotated"
    assert abs(after.expires_at - before.expires_at) < 2, "the grace keeps counting from the last working access"
    assert token.expires_in == 3600
    held = await proxy.load_access_token(token.access_token)
    assert held.claims[SUSPENDED_REASON_CLAIM] == "org_disabled"
    # FastMCP's own store, not the proxy's: the coalescer deliberately honours a just-rotated token for 60s so a racing duplicate gets the same result.
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    assert await OAuthProxy.load_refresh_token(proxy, CLIENT, refresh) is None, "the presented proxy refresh token was rotated out"


async def test_a_held_session_resumes_with_vultrs_original_refresh_token(proxy, vultr):
    access, refresh = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = ORG_DISABLED
    loaded = await proxy.load_refresh_token(CLIENT, refresh)
    held = await proxy.exchange_refresh_token(CLIENT, loaded, loaded.scopes)

    vultr.refusal = None
    resumed = await proxy.load_access_token(held.access_token)

    assert resumed is not None and SUSPENDED_MESSAGE_CLAIM not in (resumed.claims or {})
    assert vultr.issued == 1


async def test_a_client_refresh_after_the_grace_is_refused_so_the_user_signs_in(proxy, vultr):
    access, refresh = await _sign_in(proxy)
    await _age(proxy, access, SUSPENSION_GRACE_S + 60)
    vultr.refusal = ORG_DISABLED

    loaded = await proxy.load_refresh_token(CLIENT, refresh)
    with pytest.raises(TokenError) as refused:
        await proxy.exchange_refresh_token(CLIENT, loaded, loaded.scopes)
    assert refused.value.error == "invalid_grant"


async def test_a_client_refresh_refused_for_any_other_reason_is_unchanged(proxy, vultr):
    access, refresh = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = "Authorization has been revoked"

    loaded = await proxy.load_refresh_token(CLIENT, refresh)
    with pytest.raises(TokenError) as refused:
        await proxy.exchange_refresh_token(CLIENT, loaded, loaded.scopes)
    assert refused.value.error == "invalid_grant"


# --- what the agent and the audit log see ------------------------------------


async def test_a_held_sessions_calls_get_vultrs_refusal_without_reaching_vultr(monkeypatch):
    from vultr_mcp.interface.runtime import VultrAPIError

    sent: list[httpx.Request] = []
    monkeypatch.setattr(suspension, "current_suspension", lambda: ORG_DISABLED)
    transport = SuspendedTransport(httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={})))

    async with httpx.AsyncClient(transport=transport, base_url="https://api.vultr.com/v2") as client:
        response = await client.get("/instances")

    assert sent == []
    assert response.status_code == 403
    assert str(VultrAPIError(response)) == f"Vultr API returned 403: {ORG_DISABLED}"


async def test_other_sessions_calls_go_through(monkeypatch):
    monkeypatch.setattr(suspension, "current_suspension", lambda: None)
    transport = SuspendedTransport(httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))

    async with httpx.AsyncClient(transport=transport, base_url="https://api.vultr.com/v2") as client:
        response = await client.get("/instances")

    assert response.json() == {"ok": True}


def test_the_audit_record_names_a_held_sessions_reason(monkeypatch):
    from fastmcp.server import dependencies

    from vultr_mcp import audit

    token = AccessToken(token="t", client_id="c", scopes=[], claims={SUSPENDED_REASON_CLAIM: "org_disabled", "acctid": 1})
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)

    assert audit._identity()["suspended"] == "org_disabled"


async def test_a_held_client_refresh_is_audited_as_held(proxy, vultr, monkeypatch):
    from vultr_mcp import auth

    records: list[tuple] = []
    monkeypatch.setattr(auth, "emit_auth", lambda action, outcome, client_id, started, **f: records.append((action, outcome, f)))
    access, refresh = await _sign_in(proxy)
    await _age(proxy, access, 60)
    vultr.refusal = ORG_DISABLED

    loaded = await proxy.load_refresh_token(CLIENT, refresh)
    await proxy.exchange_refresh_token(CLIENT, loaded, loaded.scopes)

    assert ("refresh", "held") in [(a, o) for a, o, _ in records]
    assert [f for a, o, f in records if o == "held"][0]["suspended"] == "org_disabled"


def test_the_suspension_messages_are_vultrs_exact_words():
    """These must match what Vultr returns; a mismatch silently turns every suspension into a reconnect."""
    assert SUSPENSION_REASONS == {
        "The Vultr MCP server is not available to the public yet.": "mcp_unavailable",
        ORG_DISABLED: "org_disabled",
        USER_REMOVED: "user_access_removed",
    }


def test_the_proxy_issues_hour_long_access_tokens(proxy):
    assert proxy._fastmcp_access_token_expiry_seconds == 3600
