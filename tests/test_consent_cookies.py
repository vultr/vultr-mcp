"""Sign-ins started at the same time each keep their own consent state, and a failed one tells its client.

Reproduces connecting several MCP servers on one host at once: every flow reads
the same cookie jar before any of them writes back, as a browser does when the
sign-in tabs open together.
"""

from __future__ import annotations

import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import OAuthTransaction
from starlette.requests import Request
from starlette.responses import HTMLResponse

from vultr_mcp.auth import SIGN_IN_FAILED_MESSAGE, UpstreamEndpoints, build_auth

ENDPOINTS = UpstreamEndpoints(
    authorization_endpoint="https://my.vultr.com/oauth/authorize",
    token_endpoint="https://api.vultr.com/v2/oidc/provider/prov-123/token",
    jwks_uri="https://api.vultr.com/v2/oidc/issuer/iss-456/jwks",
    issuer="https://api.vultr.com/v2/oidc/provider/prov-123",
)


@pytest.fixture
def proxy(monkeypatch):
    for name, value in {
        "VULTR_OIDC_ENABLED": "true",
        "VULTR_OIDC_PROVIDER_ID": "prov-123",
        "VULTR_OAUTH_CLIENT_ID": "client-abc",
        "VULTR_OAUTH_CLIENT_SECRET": "secret-xyz",
        "MCP_RESOURCE_URL": "https://mcp.vultr.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    built = build_auth(endpoints=ENDPOINTS)
    assert built._is_https
    return built


def _request(jar: dict[str, str]) -> Request:
    cookie = "; ".join(f"{name}={value}" for name, value in jar.items())
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookie.encode())]})


def _apply(jar: dict[str, str], response: HTMLResponse) -> None:
    """What the browser does with a response's Set-Cookie headers."""
    for header, value in response.raw_headers:
        if header.lower() != b"set-cookie":
            continue
        for name, morsel in SimpleCookie(value.decode()).items():
            if morsel["max-age"] == "0":
                jar.pop(name, None)
            else:
                jar[name] = morsel.value


def test_bindings_from_overlapping_sign_ins_all_survive(proxy):
    jar: dict[str, str] = {}
    flows = {f"txn-{n}": f"token-{n}" for n in range(3)}
    # Every flow reads the jar before any of them has written back.
    responses = []
    for txn_id, token in flows.items():
        response = HTMLResponse("ok")
        proxy._set_consent_binding_cookie(_request(dict(jar)), response, txn_id, token)
        responses.append(response)
    for response in responses:
        _apply(jar, response)

    for txn_id, token in flows.items():
        assert proxy._verify_consent_binding_cookie(_request(jar), txn_id, token), txn_id


def test_fastmcps_shared_cookie_loses_them(proxy):
    """The bug itself, so this file fails loudly if the override stops applying."""
    jar: dict[str, str] = {}
    responses = []
    for n in range(2):
        response = HTMLResponse("ok")
        OAuthProxy._set_consent_binding_cookie(proxy, _request(dict(jar)), response, f"txn-{n}", f"token-{n}")
        responses.append(response)
    for response in responses:
        _apply(jar, response)

    assert not OAuthProxy._verify_consent_binding_cookie(proxy, _request(jar), "txn-0", "token-0")


def test_csrf_tokens_from_overlapping_consent_pages_all_survive(proxy):
    jar: dict[str, str] = {}
    responses = []
    for n in range(3):
        response = HTMLResponse("ok")
        existing = proxy._decode_list_cookie(_request(dict(jar)), "MCP_CONSENT_STATE")
        proxy._set_list_cookie(response, "MCP_CONSENT_STATE", proxy._encode_list_cookie(existing + [f"csrf-{n}"]), 900)
        responses.append(response)
    for response in responses:
        _apply(jar, response)

    tokens = proxy._decode_list_cookie(_request(jar), "MCP_CONSENT_STATE")
    assert {"csrf-0", "csrf-1", "csrf-2"} <= set(tokens)


def test_a_finished_sign_in_clears_only_its_own_binding(proxy):
    jar: dict[str, str] = {}
    for n in range(2):
        response = HTMLResponse("ok")
        proxy._set_consent_binding_cookie(_request(jar), response, f"txn-{n}", f"token-{n}")
        _apply(jar, response)

    response = HTMLResponse("ok")
    proxy._clear_consent_binding_cookie(_request(jar), response, "txn-0")
    _apply(jar, response)

    assert not proxy._verify_consent_binding_cookie(_request(jar), "txn-0", "token-0")
    assert proxy._verify_consent_binding_cookie(_request(jar), "txn-1", "token-1")


def test_a_forged_binding_is_rejected(proxy):
    jar = {proxy._entry_cookie_name("MCP_CONSENT_BINDING", "txn-x"): "eyJ0eG4iOiJ0eG4teCJ9.not-a-signature"}

    assert not proxy._verify_consent_binding_cookie(_request(jar), "txn-x", "anything")


# --- telling the client when sign-in fails -------------------------------------


async def _transaction(proxy, txn_id="txn-1", redirect_uri="http://127.0.0.1:9/cb"):
    await proxy._transaction_store.put(key=txn_id, value=OAuthTransaction(
        txn_id=txn_id, client_id="client-1", client_redirect_uri=redirect_uri, client_state="client-state",
        code_challenge="x", code_challenge_method="S256", scopes=[], created_at=time.time(),
        csrf_token="csrf-1", csrf_expires_at=time.time() + 600, consent_token="consent-1",
    ), ttl=600)


def _get(path: str, query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query.encode(), "headers": []})


def _post_form(path: str, body: str) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "path": path, "query_string": b"",
                    "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}, receive)


def _error_sent_to_client(response) -> dict[str, list[str]]:
    assert response.status_code == 302, response.status_code
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "http://127.0.0.1:9/cb"
    return parse_qs(target.query)


async def test_a_callback_that_fails_in_the_browser_is_sent_back_to_the_client(proxy):
    """The session mismatch from connecting several servers at once: the client hears instead of spinning."""
    await _transaction(proxy)

    response = await proxy._handle_idp_callback(_get("/auth/callback", "code=idp-code&state=txn-1"))

    params = _error_sent_to_client(response)
    assert params["error"] == ["access_denied"]
    assert params["error_description"] == [SIGN_IN_FAILED_MESSAGE]
    assert params["state"] == ["client-state"]
    assert "code" not in params


async def test_a_consent_submission_that_fails_is_sent_back_to_the_client(proxy):
    await _transaction(proxy)

    response = await proxy._submit_consent(_post_form("/consent", "txn_id=txn-1&action=approve&csrf_token=csrf-1"))

    assert _error_sent_to_client(response)["error"] == ["access_denied"]


async def test_an_unknown_sign_in_still_gets_the_browser_page(proxy):
    """Without its transaction there is no trusted client address to send anything to."""
    response = await proxy._handle_idp_callback(_get("/auth/callback", "code=idp-code&state=nobody"))

    assert response.status_code == 400
    assert "location" not in response.headers


async def test_an_unregistered_redirect_address_still_gets_the_browser_page(proxy):
    proxy._allowed_client_redirect_uris = ["http://127.0.0.1:*"]
    await _transaction(proxy, redirect_uri="https://attacker.example/cb")

    response = await proxy._handle_idp_callback(_get("/auth/callback", "code=idp-code&state=txn-1"))

    assert response.status_code >= 400
    assert "location" not in response.headers
