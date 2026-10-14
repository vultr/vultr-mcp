"""Sessions survive Vultr's brief outages and never present one upstream refresh token twice.

Driven through the real proxy and FastMCP's own token stores; only Vultr's token
endpoint is faked, and the upstream token check honours each token's exp the way
the JWKS verifier does.
"""

from __future__ import annotations

import base64
import json
import time

import anyio
import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from vultr_mcp import refresh_coalescer
from vultr_mcp.refresh_coalescer import UpstreamUnavailable, is_transient

CLIENT = OAuthClientInformationFull(client_id="mcp-client-1", redirect_uris=["http://127.0.0.1:9/cb"])


def _upstream_jwt(n: int, exp: float) -> str:
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': int(exp), 'sub': '7', 'acctid': 1234, 'n': n})}.sig"


class _ExpiryVerifier:
    async def verify_token(self, token: str):
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims.get("exp", 0) <= time.time():
            return None
        return AccessToken(token=token, client_id="vultr-mcp-app", scopes=[], expires_at=claims["exp"], claims=claims)


class _Vultr:
    """Vultr's token endpoint: answers each refresh from a script, then issues fresh tokens; slow enough to overlap."""

    def __init__(self) -> None:
        self.script: list[int] = []
        self.presented: list[str] = []
        self.issued = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.presented.append(dict(httpx.QueryParams(request.content.decode())).get("refresh_token", ""))
        await anyio.sleep(0.05)
        status = self.script.pop(0) if self.script else 200
        if status == 400:
            return httpx.Response(400, json={"error": "Invalid refresh token"})
        if status != 200:
            return httpx.Response(status, text="Bad Gateway")
        self.issued += 1
        return httpx.Response(200, json={
            "access_token": _upstream_jwt(self.issued, time.time() + 3600),
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"vultr-refresh-{self.issued}",
        })


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(refresh_coalescer, "UPSTREAM_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(refresh_coalescer, "POLL_S", 0.01)


@pytest.fixture
def vultr():
    return _Vultr()


@pytest.fixture
def auth_records(monkeypatch):
    from vultr_mcp import audit

    records: list[dict] = []
    monkeypatch.setattr(audit, "emit", lambda r: records.append(r) if r.get("event") == "mcp.auth" else None)
    monkeypatch.setenv("VULTR_MCP_AUDIT_LOG", "true")
    return records


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
    built.get_routes()
    built._token_validator = _ExpiryVerifier()
    built._create_upstream_oauth_client = lambda: AsyncOAuth2Client(
        client_id="client-abc", client_secret="secret-xyz", transport=httpx.MockTransport(vultr.handle)
    )
    return built


async def _sign_in(proxy) -> tuple[str, str]:
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


async def _expire_upstream(proxy, access_token: str) -> None:
    payload = proxy.jwt_issuer.verify_token(access_token)
    upstream = await proxy._upstream_for_jti(payload["jti"])
    upstream.expires_at = time.time() - 1
    upstream.access_token = _upstream_jwt(-1, upstream.expires_at)
    await proxy._upstream_token_store.put(key=upstream.upstream_token_id, value=upstream, ttl=86400)


async def _client_refresh(proxy, refresh_token: str):
    loaded = await proxy.load_refresh_token(CLIENT, refresh_token)
    assert loaded is not None
    return await proxy.exchange_refresh_token(CLIENT, loaded, [])


# --- Vultr's brief outages ----------------------------------------------------


async def test_a_gateway_error_is_retried_and_the_refresh_succeeds(proxy, vultr):
    _, refresh = await _sign_in(proxy)
    vultr.script = [502]

    token = await _client_refresh(proxy, refresh)

    assert token.access_token
    assert vultr.presented == ["vultr-refresh-0", "vultr-refresh-0"]


async def test_an_outage_that_outlasts_the_retries_is_temporary_not_invalid_grant(proxy, vultr, auth_records):
    _, refresh = await _sign_in(proxy)
    vultr.script = [502, 503, 504]

    with pytest.raises(UpstreamUnavailable):
        await _client_refresh(proxy, refresh)

    assert len(vultr.presented) == 3
    assert auth_records[-1]["action"] == "refresh"
    assert auth_records[-1]["error"] == "temporarily_unavailable"


async def test_the_session_survives_the_outage_and_refreshes_afterwards(proxy, vultr):
    _, refresh = await _sign_in(proxy)
    vultr.script = [502, 502, 502]
    with pytest.raises(UpstreamUnavailable):
        await _client_refresh(proxy, refresh)

    token = await _client_refresh(proxy, refresh)

    assert token.access_token
    assert vultr.presented[-1] == "vultr-refresh-0"


async def test_vultrs_refusal_is_never_retried_and_stays_invalid_grant(proxy, vultr):
    _, refresh = await _sign_in(proxy)
    vultr.script = [400]

    with pytest.raises(TokenError) as raised:
        await _client_refresh(proxy, refresh)

    assert raised.value.error == "invalid_grant"
    assert len(vultr.presented) == 1


def test_only_failures_that_never_spent_the_token_are_retried():
    response = lambda status: httpx.Response(status, request=httpx.Request("POST", "https://x"))  # noqa: E731
    assert is_transient(httpx.HTTPStatusError("x", request=response(502).request, response=response(502)))
    assert is_transient(httpx.ConnectError("refused"))
    assert not is_transient(httpx.HTTPStatusError("x", request=response(500).request, response=response(500)))
    assert not is_transient(httpx.ReadTimeout("sent, no answer"))


def test_the_token_endpoint_answers_503_with_retry_after():
    from vultr_mcp.auth import _TemporarilyUnavailableToken

    async def down(scope, receive, send):
        raise UpstreamUnavailable("502 Bad Gateway")

    app = Starlette(routes=[Route("/token", _TemporarilyUnavailableToken(down), methods=["POST"])])
    response = TestClient(app).post("/token", data={"grant_type": "refresh_token"})

    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    assert response.headers["retry-after"] == "30"


def test_every_oauth_deployment_gets_the_503_wrapper(proxy):
    from vultr_mcp.auth import _TemporarilyUnavailableToken

    token_routes = [route for route in proxy.get_routes() if getattr(route, "path", None) == "/token"]

    assert token_routes
    endpoint = token_routes[0].endpoint
    assert isinstance(endpoint, _TemporarilyUnavailableToken) or isinstance(
        getattr(endpoint, "_original", None), _TemporarilyUnavailableToken
    )


# --- one upstream refresh token, presented once -----------------------------


async def test_a_transparent_and_a_client_refresh_at_once_present_vultrs_token_once(proxy, vultr):
    access, refresh = await _sign_in(proxy)
    await _expire_upstream(proxy, access)
    results: dict[str, object] = {}

    async def transparent():
        results["access"] = await proxy.load_access_token(access)

    async def client():
        results["refresh"] = await _client_refresh(proxy, refresh)

    async with anyio.create_task_group() as tg:
        tg.start_soon(transparent)
        tg.start_soon(client)

    assert vultr.presented == ["vultr-refresh-0"]
    assert results["access"] is not None and results["refresh"].access_token


async def test_a_transparent_refresh_is_audited(proxy, vultr, auth_records):
    access, _ = await _sign_in(proxy)
    await _expire_upstream(proxy, access)

    assert await proxy.load_access_token(access) is not None

    assert [(r["action"], r["outcome"]) for r in auth_records][-1] == ("transparent_refresh", "ok")


# --- clients that never refresh ----------------------------------------------


async def test_sign_in_issues_a_day_long_token_over_vultrs_hour_long_one(proxy):
    access, _ = await _sign_in(proxy)

    payload = proxy.jwt_issuer.verify_token(access)
    upstream = await proxy._upstream_for_jti(payload["jti"])
    assert payload["exp"] - time.time() > 23 * 3600
    assert upstream.expires_at - time.time() <= 3600


async def test_a_client_that_never_refreshes_keeps_working_past_the_hour(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _expire_upstream(proxy, access)

    assert await proxy.load_access_token(access) is not None
    assert vultr.presented == ["vultr-refresh-0"], "the proxy renewed Vultr's token itself"


async def test_a_grant_revoked_on_vultr_still_ends_the_day_long_token(proxy, vultr):
    access, _ = await _sign_in(proxy)
    await _expire_upstream(proxy, access)
    vultr.script = [400]

    assert await proxy.load_access_token(access) is None


# --- why a request got a 401 --------------------------------------------------


async def test_a_rejected_token_records_why(proxy, vultr, auth_records):
    access, _ = await _sign_in(proxy)
    payload = proxy.jwt_issuer.verify_token(access)
    await proxy._jti_mapping_store.delete(key=payload["jti"])

    assert await proxy.load_access_token(access) is None

    assert auth_records[-1]["action"] == "access"
    assert auth_records[-1]["outcome"] == "rejected"
    assert auth_records[-1]["error"] == "session_not_found"
    assert auth_records[-1]["client_id"] == CLIENT.client_id


async def test_a_lapsed_session_whose_refresh_fails_is_recorded_as_rejected_upstream(proxy, vultr, auth_records):
    access, _ = await _sign_in(proxy)
    await _expire_upstream(proxy, access)
    vultr.script = [400]

    assert await proxy.load_access_token(access) is None

    actions = [(r["action"], r["outcome"]) for r in auth_records]
    assert actions[-2:] == [("transparent_refresh", "error"), ("access", "rejected")]
    assert "Invalid refresh token" in auth_records[-2]["error_description"]
    assert auth_records[-1]["error"] == "upstream_token_rejected"


async def test_a_garbage_bearer_is_recorded_as_invalid(proxy, auth_records):
    assert await proxy.load_access_token("not.a.jwt") is None

    assert auth_records[-1]["error"] == "token_invalid"
