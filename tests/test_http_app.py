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
