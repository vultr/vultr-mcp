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

            # The endpoint list is built from the servers this process mounts, so
            # it cannot drift from the real surface. (It used to be typed into the
            # HTML, and this test asserted the typed names -- which is how the page
            # still advertised tools renamed a month earlier.) This process mounts
            # only /instances; test_landing covers the full default set.
            # Selecting the old "instances" category mounts its group, compute.
            assert "/mcp/compute" in body
            assert "1 endpoint · " in body

            # The expandable tool lists carry the real, current tool names.
            for tool_name in ("vultr_compute_instances_list", "vultr_compute_instances_get"):
                assert tool_name in body, f"endpoint accordion missing tool {tool_name!r}"

            # The page must not advertise tools the read-only server won't serve.
            for write_tool in ("create_dns_domain", "delete_instance", "create_kubernetes_cluster"):
                assert write_tool not in body, (
                    f"landing page still lists write tool {write_tool!r}"
                )

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
        # The public deploy must report its write posture, and default to safe.
        assert health.json()["read_only"] is True

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


async def test_mcp_alias_serves_the_same_surface_as_root(monkeypatch, spec):
    """"/mcp" is the conventional streamable-HTTP path, so clients try it first.

    It used to 404: not a category, so it fell through to the catch-all mount
    and the root app had nothing at that path. The alias makes both work, and
    they must be the SAME surface -- an alias that served a different tool set
    would be worse than the 404 it replaced.
    """
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        assert await _names(port, "/mcp") == await _names(port, "/")
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_mcp_alias_works_without_a_trailing_slash(monkeypatch, spec):
    """The bare-path shim has to cover the alias too.

    Starlette would 307 "/mcp" -> "/mcp/", and MCP clients do not follow a
    redirect on POST -- which is the whole reason the shim exists for category
    paths. Adding a mount without adding it to bare_paths would have produced a
    route that works in a browser and fails from every client.
    """
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        import httpx

        async with httpx.AsyncClient() as hc:
            # A POST to the bare path must be handled, not redirected.
            resp = await hc.post(
                f"http://127.0.0.1:{port}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code != 307, "bare /mcp redirected; clients will not follow"
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_categories_are_served_under_mcp(monkeypatch, spec):
    """"/mcp/instances" is the documented form of a category endpoint.

    It must serve exactly what the older "/instances" does -- the same server,
    reached two ways -- and must not fall through to Mount("/mcp"), which
    matches the same prefix and would hand back the full surface instead.
    """
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances,dns")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        nested = await _names(port, "/mcp/instances")
        assert nested == await _names(port, "/instances")
        assert len(nested) < len(await _names(port, "/mcp"))
        assert "dns_domain" not in " ".join(nested).lower()
        assert await _names(port, "/mcp/dns") == await _names(port, "/dns")
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_mcp_category_paths_work_without_a_trailing_slash(monkeypatch, spec):
    """Same reason as the /mcp alias: a 307 on POST strands every client."""
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    port = _free_port()
    server, task = await _serve(app, port)
    try:
        import httpx

        async with httpx.AsyncClient() as hc:
            resp = await hc.post(
                f"http://127.0.0.1:{port}/mcp/instances",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code != 307, "bare /mcp/instances redirected; clients will not follow"
        assert resp.status_code == 200
    finally:
        server.should_exit = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def test_the_audit_record_names_the_endpoint_and_host(monkeypatch, spec, capsys):
    """Which address a call used is what decides when an old one can go.

    An unknown argument is refused before any request to Vultr, so this makes
    no network call.
    """
    import json

    from starlette.testclient import TestClient

    monkeypatch.setenv("VULTR_MCP_TRANSPORT", "http")
    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "instances")
    app = create_http_app(spec)
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "vultr_compute_instances_get", "arguments": {"cluster_id": "x"}},
    }
    with TestClient(app) as client:
        capsys.readouterr()
        for path in ("/mcp/instances", "/instances/"):
            client.post(path, json=call, headers={"Accept": "application/json, text/event-stream"})

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"mcp.tool_call"' in line
    ]
    assert [r["endpoint"] for r in records] == ["/mcp/instances", "/instances"]
    assert all(r["host"] == "testserver" for r in records)


def test_every_category_is_in_exactly_one_endpoint_group(spec):
    """A tag in no group would still get an endpoint of its own, with only a
    warning at boot -- this is where that gets caught, before it ships."""
    from vultr_mcp.app import ENDPOINT_GROUPS, _resolve_exclusions, slugify
    from vultr_mcp.server import all_categories

    listed = [c for cats in ENDPOINT_GROUPS.values() for c in cats]
    assert len(listed) == len(set(listed)), "a category is in two groups"
    served = {slugify(t) for t in all_categories(spec) - _resolve_exclusions()}
    assert served - set(listed) == set(), "categories with no endpoint group"
    assert set(listed) - served == set(), "groups name categories the spec does not serve"


def _tools(client, path):
    import json

    text = client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    ).text
    return {t["name"] for t in json.loads(text[text.index("{"):])["result"]["tools"]}


def test_the_groups_partition_the_full_surface(monkeypatch, spec):
    """Nine endpoints, together exactly the root's tools, no tool in two."""
    from starlette.testclient import TestClient

    from vultr_mcp.app import ENDPOINT_GROUPS

    monkeypatch.delenv("VULTR_MCP_CATEGORY_ENDPOINTS", raising=False)
    with TestClient(create_http_app(spec)) as client:
        root = _tools(client, "/mcp")
        groups = {g: _tools(client, f"/mcp/{g}") for g in ENDPOINT_GROUPS}
        page = client.get("/", headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}).text

    assert all(groups.values()), "a group endpoint serves nothing"
    union = set().union(*groups.values())
    assert union == root
    assert sum(len(t) for t in groups.values()) == len(root), "a tool is served by two groups"
    assert f"{len(ENDPOINT_GROUPS)} endpoints · {len(root)} tools" in page


def test_old_category_paths_serve_their_group(monkeypatch, spec):
    """/instances and /mcp/instances were documented; they must keep working."""
    from starlette.testclient import TestClient

    monkeypatch.setenv("VULTR_MCP_CATEGORY_ENDPOINTS", "compute,network,kubernetes")
    with TestClient(create_http_app(spec)) as client:
        compute = _tools(client, "/mcp/compute")
        assert _tools(client, "/mcp/instances") == compute
        assert _tools(client, "/instances") == compute
        assert _tools(client, "/baremetal") == compute
        network = _tools(client, "/mcp/network")
        assert _tools(client, "/dns") == network
        assert _tools(client, "/mcp/load-balancer") == network
        # A group is not added at the top level: only old category names are.
        assert client.post("/compute", json={}).status_code == 404
        kubernetes = _tools(client, "/mcp/kubernetes")

    # The pair agents confuse stays on separate endpoints.
    assert "vultr_compute_clusters_list" in compute and "vultr_compute_clusters_list" not in kubernetes
    assert "vultr_kubernetes_clusters_list" in kubernetes and "vultr_kubernetes_clusters_list" not in compute
