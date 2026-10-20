"""Clients show the Vultr logo for the connector, not a generated letter.

``initialize`` advertises the icon (MCP serverInfo.icons) as an absolute URL on
the public host, and the HTTP app serves that URL and a favicon.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from starlette.applications import Starlette
from starlette.testclient import TestClient

from vultr_mcp import branding
from vultr_mcp.server import create_server, load_spec


@pytest.fixture(scope="module")
def spec():
    return load_spec()


async def test_initialize_advertises_the_logo_on_the_public_host(spec, monkeypatch):
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://mcp.example.test/")
    async with Client(create_server(spec, only_categories={"dns"})) as client:
        icons = client.initialize_result.serverInfo.icons
    assert [(i.src, i.mimeType, i.sizes) for i in icons] == [
        ("https://mcp.example.test/icon.png", "image/png", ["180x180"])
    ]


def test_no_icon_is_advertised_when_the_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(branding, "LOGO_PNG", tmp_path / "missing.png")
    assert branding.server_icons() == []


@pytest.mark.parametrize(
    ("path", "media_type", "source"),
    [("/icon.png", "image/png", branding.LOGO_PNG), ("/favicon.ico", "image/x-icon", branding.FAVICON)],
)
def test_the_icon_files_are_served(path, media_type, source):
    with TestClient(Starlette(routes=branding.icon_routes())) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.content == source.read_bytes()


def test_the_logo_is_a_png():
    assert branding.LOGO_PNG.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
