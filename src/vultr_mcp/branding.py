"""The Vultr icon: served at ``/icon.png`` and ``/favicon.ico``, and advertised in ``initialize`` so clients can show it.

Both files are Vultr's own public site icons (www.vultr.com's apple-touch-icon.png and favicon.ico).
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

STATIC_DIR = Path(__file__).parent / "static"
LOGO_PNG = STATIC_DIR / "vultr-logo.png"
FAVICON = STATIC_DIR / "favicon.ico"

# A day: the icon changes only with a release, and clients refetch it on every connector list.
_CACHE = "public, max-age=86400"


def _public_base() -> str:
    return os.environ.get("MCP_RESOURCE_URL", "https://mcp.vultr.com").rstrip("/")


def server_icons() -> list[Icon]:
    """Absolute URLs, since a client resolves them on its own; empty when the file is missing so nothing points at a 404."""
    if not LOGO_PNG.is_file():
        return []
    return [Icon(src=f"{_public_base()}/icon.png", mimeType="image/png", sizes=["180x180"])]


def _file_route(path: str, file: Path, media_type: str) -> Route:
    async def serve(_request: Request) -> Response:
        if not file.is_file():
            return Response(status_code=404)
        return Response(file.read_bytes(), media_type=media_type, headers={"Cache-Control": _CACHE})

    return Route(path, serve, methods=["GET", "HEAD"])


def icon_routes() -> list[Route]:
    return [
        _file_route("/icon.png", LOGO_PNG, "image/png"),
        _file_route("/favicon.ico", FAVICON, "image/x-icon"),
    ]
