"""Which credential PerRequestVultrAuth forwards to api.vultr.com, and when.

This is the credential-resolution path: it decides, per request, what ends up in
the Authorization header sent upstream. It had no test coverage at all, which is
uncomfortable for the one place a mix-up substitutes one caller's authority for
another's.

Three sources, in priority order:

    1. the verified AccessToken   (OAuth path — the UPSTREAM Vultr token)
    2. the incoming request       (no auth layer — the caller's own credential)
    3. VULTR_API_KEY from env     (STDIO/local ONLY — never from HTTP)

The third one is the interesting one, and the reason this file exists.
"""

from __future__ import annotations

import httpx
import pytest

from vultr_mcp import server as S
from vultr_mcp.server import PerRequestVultrAuth


def forwarded(monkeypatch, *, headers=None, access_token=None, env_key=None) -> str | None:
    """The Authorization header this request would carry upstream.

    ``headers=None`` models no HTTP request in scope at all (STDIO, in-process),
    which is what get_http_headers() reports by returning an empty dict.
    """
    monkeypatch.setattr(S, "get_http_headers", lambda **_: dict(headers or {}))

    import fastmcp.server.dependencies as deps

    monkeypatch.setattr(deps, "get_access_token", lambda: access_token, raising=False)

    if env_key is None:
        monkeypatch.delenv("VULTR_API_KEY", raising=False)
    else:
        monkeypatch.setenv("VULTR_API_KEY", env_key)

    request = httpx.Request("GET", "https://api.vultr.com/v2/account")
    next(PerRequestVultrAuth().auth_flow(request))
    return request.headers.get("Authorization")


class _AccessToken:
    """Stands in for the verified token FastMCP exposes after auth."""

    def __init__(self, token: str) -> None:
        self.token = token


# --------------------------------------------------------------------------
# 1. The OAuth path.
# --------------------------------------------------------------------------


def test_the_verified_upstream_token_wins(monkeypatch):
    """What api.vultr.com accepts is the upstream token, not the raw header.

    Forwarding the FastMCP token instead is what produced "Invalid API token",
    so the precedence here is load-bearing rather than cosmetic.
    """
    got = forwarded(
        monkeypatch,
        headers={"host": "vultrmcp.com", "authorization": "Bearer fastmcp-issued"},
        access_token=_AccessToken("upstream-vultr-token"),
    )
    assert got == "Bearer upstream-vultr-token"


def test_the_env_key_never_overrides_an_authenticated_caller(monkeypatch):
    """An operator key present in env must not displace a real identity."""
    got = forwarded(
        monkeypatch,
        headers={"host": "vultrmcp.com"},
        access_token=_AccessToken("upstream-vultr-token"),
        env_key="operator-key",
    )
    assert got == "Bearer upstream-vultr-token"


# --------------------------------------------------------------------------
# 2. No auth layer: the caller's own credential is forwarded.
# --------------------------------------------------------------------------


def test_an_incoming_bearer_is_forwarded_verbatim(monkeypatch):
    got = forwarded(
        monkeypatch,
        headers={"host": "localhost", "authorization": "Bearer caller-key"},
    )
    assert got == "Bearer caller-key"


def test_an_api_key_header_is_wrapped_as_bearer(monkeypatch):
    """X-Vultr-API-Key is accepted only on the no-auth-layer path."""
    got = forwarded(
        monkeypatch,
        headers={"host": "localhost", "x-vultr-api-key": "caller-key"},
    )
    assert got == "Bearer caller-key"


def test_the_caller_credential_beats_the_env_key(monkeypatch):
    got = forwarded(
        monkeypatch,
        headers={"host": "localhost", "authorization": "Bearer caller-key"},
        env_key="operator-key",
    )
    assert got == "Bearer caller-key"


# --------------------------------------------------------------------------
# 3. The env fallback, and the fact that HTTP cannot reach it.
# --------------------------------------------------------------------------


def test_stdio_falls_back_to_the_env_key(monkeypatch):
    """No HTTP request in scope: the local key is the intended credential."""
    got = forwarded(monkeypatch, headers=None, env_key="local-key")
    assert got == "Bearer local-key"


def test_stdio_without_a_key_sends_nothing(monkeypatch):
    """Better an unauthenticated call that 401s than a silent wrong identity."""
    assert forwarded(monkeypatch, headers=None) is None


def test_an_http_request_with_no_credential_does_not_borrow_the_env_key(monkeypatch):
    """The one that matters: HTTP must fail closed, not fall back.

    A deployment running HTTP without an auth layer and with VULTR_API_KEY set
    would otherwise serve an anonymous caller as the operator -- an open proxy
    to whatever account that key belongs to. No credential in means no
    Authorization header out, and Vultr answers 401.
    """
    got = forwarded(
        monkeypatch,
        headers={"host": "vultrmcp.com", "user-agent": "curl/8.0"},
        env_key="operator-key",
    )
    assert got is None, "an HTTP caller was served with the server's own credential"


def test_the_fallback_is_refused_when_the_context_cannot_be_determined(monkeypatch):
    """An unexpected failure resolves toward withholding, not disclosing."""

    def explode(**_):
        raise RuntimeError("context unavailable")

    monkeypatch.setattr(S, "get_http_headers", explode)

    import fastmcp.server.dependencies as deps

    monkeypatch.setattr(deps, "get_access_token", lambda: None, raising=False)
    monkeypatch.setenv("VULTR_API_KEY", "operator-key")

    request = httpx.Request("GET", "https://api.vultr.com/v2/account")
    next(PerRequestVultrAuth().auth_flow(request))

    assert request.headers.get("Authorization") is None


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "vultrmcp.com"},
        {"host": "vultrmcp.com", "authorization": ""},
        {"host": "vultrmcp.com", "x-vultr-api-key": ""},
        {"content-type": "application/json"},
    ],
)
def test_no_shape_of_credential_less_http_request_reaches_the_fallback(
    monkeypatch, headers
):
    """Empty header values are not credentials, and must not open the fallback."""
    assert forwarded(monkeypatch, headers=headers, env_key="operator-key") is None
