"""The docs page's endpoint list.

It was typed into the HTML and drifted: by September it listed 180 tools under
names retired a month earlier, including one the server had stopped serving.
These pin that the page is built from what is actually served.
"""

from __future__ import annotations

import json
import re

from starlette.testclient import TestClient

from vultr_mcp import landing
from vultr_mcp.app import _load_landing_html, create_http_app
from vultr_mcp.server import load_spec

BROWSER = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}


def test_render_fills_both_placeholders():
    page = landing.render(
        "<span><!--EP_COUNT--></span><div><!--EP_LIST--></div>",
        3,
        [("kubernetes", [("vultr_kubernetes_clusters_list", "Lists clusters.")]),
         ("account", [("vultr_account_get", 'Gets the <account> & "owner".'), ("b_tool", "")])],
    )
    assert "2 endpoints · 3 tools" in page
    assert "<!--EP_" not in page
    # Ordered by endpoint, then tool; the description escaped into a hover title.
    assert page.index("/account") < page.index("/kubernetes")
    assert page.index("b_tool") < page.index("vultr_account_get")
    assert 'title="Gets the &lt;account&gt; &amp; &quot;owner&quot;."' in page
    assert "1 tool<" in page and "2 tools<" in page


def test_first_line_is_the_opening_sentence():
    assert landing.first_line("\n  Lists things.\n\nUse this tool to...") == "Lists things."
    assert landing.first_line(None) == ""
    assert landing.first_line("**Deprecated**: use [List Instance VPCs](#operation/list-instance-vpcs) instead.") == (
        "Deprecated: use List Instance VPCs instead."
    )


def test_a_failed_listing_still_serves_a_page():
    page = landing.unavailable(_load_landing_html())
    assert "<!--EP_" not in page and "could not be built" in page


def test_the_shipped_template_has_both_placeholders():
    template = _load_landing_html()
    assert landing.COUNT_MARK in template and landing.LIST_MARK in template


def test_the_served_page_lists_exactly_what_is_served():
    app = create_http_app(load_spec())
    with TestClient(app) as client:
        page = client.get("/", headers=BROWSER).text
        tools = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        ).text

    body = json.loads(tools[tools.index("{"):])  # the SSE frame's JSON payload
    served = {tool["name"] for tool in body["result"]["tools"]}
    assert served, "tools/list returned nothing to compare against"
    assert f"· {len(served)} tools" in page
    listed = set(re.findall(r'<code title="[^"]*">([^<]+)</code>', page))
    # Every tool on the page is served by the root; the stale names are gone.
    assert listed <= served
    assert "vultr_compute_instances_list" in listed
    assert "purge" not in page and "get_account_bgp" not in page
