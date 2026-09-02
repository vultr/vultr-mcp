"""Phase 1/2 spike: prove the two riskiest pieces of the FastMCP port.

1. ``FastMCP.from_openapi`` generates the full Vultr tool surface from
   openapi.json (replacing the PHP generator + 39 generated tool classes).
2. Per-request credential forwarding: the ``Authorization`` header on the
   incoming MCP HTTP request reaches api.vultr.com on the upstream call.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from vultr_mcp.server import create_server


# Function-scoped on purpose: the server's httpx.AsyncClient binds to the
# event loop of the test that first uses it, and pytest-asyncio runs each
# test in a fresh loop — sharing one server across tests trips
# "attached to a different loop" failures.
@pytest.fixture
def server():
    return create_server()


async def test_tools_generated_from_openapi():
    """The whole 494-operation spec becomes tools, all with MCP-legal names.

    Generation completeness, so writes are enabled here — the default surface
    is read-only (see test_read_only.py) and would only exercise the GETs.
    """
    async with Client(create_server(read_only=False)) as client:
        tools = await client.list_tools()

    assert len(tools) > 400, f"expected 400+ tools, got {len(tools)}"

    names = [t.name for t in tools]
    too_long = [n for n in names if len(n) >= 64]
    assert not too_long, f"tool names >= 64 chars: {too_long}"

    # Spot-check that recognisable operations exist.
    joined = " ".join(names)
    for fragment in ("regions", "instance", "account"):
        assert fragment in joined, f"no tool name mentions '{fragment}'"


async def test_public_endpoint_call_in_process(server):
    """End-to-end tool call through the generated stack (no auth needed)."""
    async with Client(server) as client:
        tools = await client.list_tools()
        # The interface layer owns GET /regions now, so its name no longer
        # begins with the operationId's verb. Prefer the hand-authored tool and
        # fall back to a generated one, so this keeps working whichever serves.
        names = [t.name for t in tools]
        regions_tool = next(
            (
                name
                for name in names
                if name == "vultr_catalog_regions_list"
                or ("regions" in name and name.startswith(("list", "get")))
            ),
        )
        result = await client.call_tool(regions_tool, {})

    text = str(result)
    assert "ewr" in text or "regions" in text, f"unexpected regions payload: {text[:200]}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_authorization_header_forwarded_to_vultr(server):
    """A bogus Bearer token sent to the MCP server must reach api.vultr.com.

    Proof shape: calling an authenticated endpoint (get-account) with a
    garbage token should surface Vultr's 401 — meaning the header travelled
    MCP client -> FastMCP HTTP transport -> httpx auth hook -> api.vultr.com.
    If forwarding were broken we'd instead see a "no credential" style
    failure that never left the server, or a hang.
    """
    port = _free_port()
    task = asyncio.create_task(
        server.run_http_async(host="127.0.0.1", port=port, show_banner=False)
    )
    try:
        # Wait for the server to accept connections.
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": "Bearer bogus-spike-token-123"},
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            # The interface layer owns GET /account and replaces the
            # generated tool, so the hand-authored name is the one to call.
            # Both take no arguments, so the probe is unchanged.
            account_tool = next(
                t.name
                for t in tools
                if t.name in ("vultr_account_get", "get-account", "get_account")
            )
            result = await client.call_tool(account_tool, {}, raise_on_error=False)

        text = str(result).lower()
        assert "401" in text or "unauthorized" in text or "invalid api" in text, (
            f"expected Vultr 401 through the forwarded bogus token, got: {text[:300]}"
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
