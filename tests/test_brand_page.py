"""The sign-in pages in Vultr's layout, including FastMCP's consent page, which is restyled rather than replaced."""

from __future__ import annotations

import pytest
from fastmcp.server.auth.oauth_proxy.ui import create_consent_html
from starlette.responses import HTMLResponse

from vultr_mcp import brand_page


def _consent() -> str:
    return create_consent_html(
        client_id="client-1",
        redirect_uri="claude://claude.ai/mcp-auth-callback/sdk",
        scopes=[],
        txn_id="txn-1",
        csrf_token="csrf-1",
        client_name="Claude Code",
        server_name="Vultr MCP Server",
    )


def test_the_logo_and_line_pattern_are_embedded():
    """The consent page's CSP allows data: images and nothing fetched from elsewhere."""
    assert brand_page.LOGO.startswith("data:image/svg+xml;base64,")
    assert brand_page.WAVES.startswith("data:image/svg+xml;base64,")


def test_consent_keeps_its_form_and_gains_the_panel():
    styled = brand_page.restyle_consent(_consent())

    # What makes consent work must survive untouched: the transaction, the CSRF token and both choices.
    for needed in ('name="txn_id" value="txn-1"', 'name="csrf_token" value="csrf-1"', 'value="approve"', 'value="deny"'):
        assert needed in styled
    assert 'class="vx-panel"' in styled and 'class="vx-main"' in styled
    assert styled.count("<main") == styled.count("</main>") == 1
    # The form sits inside the main area, the help link after it.
    assert styled.index('class="vx-main"') < styled.index('id="consentForm"') < styled.index("</main>")
    assert "The Vultr MCP Server requires" in styled


def test_a_page_that_is_not_the_expected_shape_is_left_alone():
    """A FastMCP upgrade that changes the page must not produce broken markup."""
    assert brand_page.restyle_consent("<p>no head or body</p>") == "<p>no head or body</p>"


@pytest.mark.anyio
async def test_the_proxy_restyles_consent_and_keeps_its_cookies_and_headers():
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    from vultr_mcp.auth import VultrOAuthProxy

    async def base(_self, _request):
        response = HTMLResponse(_consent())
        response.headers["X-Frame-Options"] = "DENY"
        response.set_cookie("MCP_CONSENT_STATE", "token")
        return response

    proxy = object.__new__(VultrOAuthProxy)
    original = OAuthProxy._show_consent_page
    try:
        OAuthProxy._show_consent_page = base
        response = await proxy._show_consent_page(None)
    finally:
        OAuthProxy._show_consent_page = original

    assert 'class="vx-panel"' in response.body.decode()
    assert response.headers["content-length"] == str(len(response.body))
    assert response.headers["x-frame-options"] == "DENY"
    assert "MCP_CONSENT_STATE=token" in response.headers["set-cookie"]


@pytest.mark.anyio
async def test_a_redirect_from_consent_is_passed_through():
    """Auto-approve and errors answer with redirects, which have no page to restyle."""
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    from starlette.responses import RedirectResponse

    from vultr_mcp.auth import VultrOAuthProxy

    async def base(_self, _request):
        return RedirectResponse("https://example.com/cb?code=x", status_code=302)

    proxy = object.__new__(VultrOAuthProxy)
    original = OAuthProxy._show_consent_page
    try:
        OAuthProxy._show_consent_page = base
        response = await proxy._show_consent_page(None)
    finally:
        OAuthProxy._show_consent_page = original

    assert response.status_code == 302
