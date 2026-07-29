"""Phase 3: the composed HTTP app — healthz, root, and category endpoints."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from vultr_mcp.app import create_http_app
from vultr_mcp.server import load_spec

# Keep boot fast: mount only a couple of category endpoints for the test.


@pytest.fixture(scope="module")
def spec():
    return load_spec()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(app, port):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return server, task
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError("server did not start")


async def _names(port, path):
    transport = StreamableHttpTransport(f"http://127.0.0.1:{port}{path}")
    async with Client(transport) as client:
        return [t.name for t in await client.list_tools()]


async def test_root_serves_landing_page_to_browsers(monkeypatch, spec):
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        import httpx

        async with httpx.AsyncClient() as hc:
            # Browser navigation: GET / with an HTML Accept header.
            page = await hc.get(
                f"http://127.0.0.1:{port}/",
                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            body = page.text
            assert "Vultr MCP Server" in body
            # The single consolidated client-setup section + flow diagram.
            assert "Connect your client" in body
            assert "<svg" in body
            # Every documented client must appear in that one section.
            for client in (
                "Claude.ai",
                "Cursor",
                "VS Code",
                "Codex CLI",
                "opencode",
                "Hermes",
                "OpenClaw",
            ):
                assert client in body, f"client section missing {client!r}"

            # Every default-mounted category endpoint must be documented, so the
            # page can't drift from the real surface as the spec grows. Compute
            # the full default set directly (the env override above only trims
            # what this test process boots, not what the page should list).
            from vultr_mcp.app import slugify
            from vultr_mcp.server import DEFAULT_EXCLUDED_CATEGORIES, all_categories

            excluded = set(DEFAULT_EXCLUDED_CATEGORIES)
            for slug in sorted(slugify(t) for t in all_categories(spec) - excluded):
                assert f"/{slug}" in body, f"landing page missing endpoint /{slug}"

            # The expandable tool lists must carry real tool names, not just slugs.
            for tool_name in ("list_instances", "create_dns_domain", "list_kubernetes_clusters"):
                assert tool_name in body, f"endpoint accordion missing tool {tool_name!r}"

            # MCP SSE probe: GET / asking for an event-stream must NOT get docs.
            sse = await hc.get(
                f"http://127.0.0.1:{port}/",
                headers={"Accept": "text/event-stream"},
            )
            assert "Vultr MCP Server" not in sse.text

            # An MCP client that opens the bare host with a generic Accept must
            # NOT get the docs page — it has to fall through to the MCP app so it
            # can connect (regression guard for the vultrmcp.com-vs-/ bug).
            for probe_accept in ("*/*", "application/json"):
                probe = await hc.get(
                    f"http://127.0.0.1:{port}/",
                    headers={"Accept": probe_accept},
                )
                assert "Vultr MCP Server" not in probe.text, (
                    f"docs page leaked to an MCP-style GET (Accept: {probe_accept})"
                )

            # A browser-based MCP client (fetch/XHR) sends Sec-Fetch-Mode: cors
            # and may still send text/html — it must reach the MCP app, not docs.
            xhr = await hc.get(
                f"http://127.0.0.1:{port}/",
                headers={"Accept": "text/html", "Sec-Fetch-Mode": "cors"},
            )
            assert "Vultr MCP Server" not in xhr.text, (
                "docs page leaked to a browser fetch/XHR MCP client"
            )

            # A genuine top-level navigation (Sec-Fetch-Mode: navigate) gets docs.
            nav = await hc.get(
                f"http://127.0.0.1:{port}/",
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Sec-Fetch-Mode": "navigate",
                },
            )
            assert "Vultr MCP Server" in nav.text
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_root_still_serves_mcp_over_post(monkeypatch, spec):
    # The docs page must not shadow the MCP protocol at "/".
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        root_names = await _names(port, "/")
        assert any("instance" in n.lower() for n in root_names)
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_healthz_and_category_scoping(monkeypatch, spec):
    # Mount only 'instances' and 'dns' endpoints so the test boots quickly.
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances,dns")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        import httpx

        async with httpx.AsyncClient() as hc:
            health = await hc.get(f"http://127.0.0.1:{port}/healthz")
        assert health.status_code == 200
        assert health.json()["service"] == "vultr-mcp-server"

        root_names = await _names(port, "/")
        instances_names = await _names(port, "/instances")
        dns_names = await _names(port, "/dns")

        # Root is the broad surface; category endpoints are strict subsets.
        assert len(root_names) > len(instances_names)
        assert len(instances_names) < 100, "instances endpoint should be scoped"

        inst_joined = " ".join(instances_names).lower()
        assert "instance" in inst_joined
        assert "dns_domain" not in inst_joined, "instances endpoint leaked dns tools"

        dns_joined = " ".join(dns_names).lower()
        assert "dns" in dns_joined
        assert "kubernetes" not in dns_joined
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
