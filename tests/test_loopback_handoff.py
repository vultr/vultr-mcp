"""Loopback callbacks the viewing browser may not be able to reach.

Observed, not hypothetical: an agent on an instance reached over SSH ends a
successful authorization on ``ERR_CONNECTION_REFUSED`` holding a valid code.

Only the browser can tell whether a listener is there, so these pin the halves
a test can own: which targets get the hand-off, and what the page carries.
"""

from __future__ import annotations

import html

import pytest
from starlette.responses import RedirectResponse

from vultr_mcp.loopback_handoff import completion_page, is_loopback_target

CODE_URL = "http://127.0.0.1:8989/oauth/callback?code=vd-abc123&state=s1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8989/oauth/callback?code=x",
        "http://127.0.0.1/cb",
        # 127.0.0.0/8 is all loopback, not just .0.1 -- some clients bind here.
        "http://127.0.0.53:9000/cb",
        "http://localhost:8989/oauth/callback?code=x",
        "http://LocalHost:8989/cb",
        "http://[::1]:8989/cb",
    ],
)
def test_loopback_targets_are_detected(url):
    assert is_loopback_target(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # A hosted client, or OpenClaw's gateway mode -- reachable, leave alone.
        "https://gateway.example.com/oauth/mcp/callback?code=x",
        "https://claude.ai/api/mcp/auth_callback?code=x",
        # A LAN address is reachable in principle; not ours to second-guess.
        "http://192.168.1.50:8989/cb",
        "http://10.0.0.4:8989/cb",
        "",
        "not a url at all",
    ],
)
def test_non_loopback_targets_are_left_alone(url):
    assert is_loopback_target(url) is False


def test_page_carries_the_code_and_the_delivery_target():
    page = completion_page(CODE_URL)
    body = page.body.decode()

    # The code has to be visible to be pasted, and the target has to be in the
    # script or the same-machine case cannot complete automatically. The
    # target is escaped on the way in, so compare against the escaped form.
    assert "vd-abc123" in body
    assert html.escape(CODE_URL, quote=True) in body
    assert 'id="manual"' in body and 'id="done"' in body


def test_page_is_never_cached():
    """It carries an authorization code; nothing should retain it."""
    page = completion_page(CODE_URL)
    assert "no-store" in page.headers["cache-control"]
    assert page.headers["referrer-policy"] == "no-referrer"


def test_page_escapes_hostile_parameters():
    """The target is attacker-influenceable via the registered redirect_uri."""
    page = completion_page(
        'http://127.0.0.1:8989/cb?code=">-<script>alert(1)</script>&state=s'
    )
    body = page.body.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# -- the proxy hook -----------------------------------------------------------


async def _run_override(location: str):
    """Drive ``VultrOAuthProxy._handle_idp_callback`` over a stubbed base.

    The override calls zero-argument ``super()``, which needs a genuine
    ``VultrOAuthProxy`` instance -- so build one without running ``__init__``
    (which would want live upstream endpoints) and stub the base method that
    ``super()`` resolves to.
    """
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    from vultr_mcp.auth import VultrOAuthProxy

    async def base(_self, _request):
        return RedirectResponse(location, status_code=302)

    proxy = object.__new__(VultrOAuthProxy)
    original = OAuthProxy._handle_idp_callback
    try:
        OAuthProxy._handle_idp_callback = base
        return await proxy._handle_idp_callback(None)
    finally:
        OAuthProxy._handle_idp_callback = original


@pytest.mark.anyio
async def test_loopback_success_gets_the_handoff_page():
    resp = await _run_override(CODE_URL)
    assert resp.status_code == 200
    assert "vd-abc123" in resp.body.decode()


@pytest.mark.anyio
async def test_remote_redirect_is_untouched():
    url = "https://claude.ai/api/mcp/auth_callback?code=vd-abc123&state=s1"
    resp = await _run_override(url)
    assert resp.status_code == 302
    assert resp.headers["location"] == url


@pytest.mark.anyio
async def test_oauth_errors_keep_their_redirect():
    """An error has no code to show; the client must see the OAuth error."""
    url = "http://127.0.0.1:8989/oauth/callback?error=access_denied&state=s1"
    resp = await _run_override(url)
    assert resp.status_code == 302
    assert resp.headers["location"] == url
