"""HTTP composition: root server plus path-based category endpoints.

``/`` and ``/mcp`` serve the full surface; ``/instances`` and friends serve one
category each, so a client can load ~15 tools instead of ~180. Exclusions and
the read-only gate always apply on top.

Each app is built eagerly: a lazily mounted one cannot start its MCP session
manager's lifespan after the parent is already running.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route

from vultr_mcp.server import (
    DEFAULT_EXCLUDED_CATEGORIES,
    all_categories,
    create_server,
    excluded_categories_from_env,
    load_spec,
    package_version,
    read_only_from_env,
)

# Read rather than hard-coded so a bump in pyproject.toml is the only place it
# lives, and so /healthz can answer "did the new image land?" from outside the
# cluster. Shared with the MCP handshake, which reports the same string.
VERSION = package_version()

_LANDING_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def _load_landing_html() -> str:
    """The docs page served on browser GETs to ``/``. A missing file is
    non-fatal: the server must not fail to boot over a docs asset.
    """
    try:
        return _LANDING_PATH.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Vultr MCP</title><h1>Vultr MCP Server</h1>"


def _wants_landing_page(scope: dict) -> bool:
    """True only for a genuine top-level browser navigation to ``/``.

    The docs page and the MCP endpoint share the root URL, so a human opening
    the page is told apart from a client connecting by request shape: MCP uses
    POST or a GET accepting ``text/event-stream``; a browser navigation sets
    ``Sec-Fetch-Mode: navigate``, while a browser-based MCP client's fetch/XHR
    sets ``cors``/``no-cors`` and falls through to the protocol. With no
    ``Sec-Fetch-*`` at all, require an explicit ``Accept: text/html``.
    """
    if scope.get("type") != "http" or scope.get("method") != "GET":
        return False
    if scope.get("path") != "/":
        return False
    accept = ""
    sec_fetch_mode = ""
    for name, value in scope.get("headers") or []:
        if name == b"accept":
            accept = value.decode("latin-1").lower()
        elif name == b"sec-fetch-mode":
            sec_fetch_mode = value.decode("latin-1").lower()
    if "text/event-stream" in accept:
        return False
    if sec_fetch_mode:
        # Only a top-level navigation is a human opening the page; a fetch/XHR
        # connection (cors/no-cors) is a client and must reach the MCP app.
        return sec_fetch_mode == "navigate"
    # No Sec-Fetch metadata: fall back to an explicit browser Accept.
    return "text/html" in accept


def _resolve_exclusions() -> set[str]:
    env = excluded_categories_from_env()
    return env if env is not None else set(DEFAULT_EXCLUDED_CATEGORIES)


def slugify(tag: str) -> str:
    """URL-safe path slug for a category tag.

    OpenAPI tags include spaces and capitals ("Container Registry", "VPC2"),
    which make ugly/invalid URL paths. Lowercase + spaces->dashes gives clean
    endpoints: "Container Registry" -> "container-registry".
    """
    return "-".join(tag.lower().split())


def _category_endpoints(spec: dict, excluded: set[str]) -> list[tuple[str, str]]:
    """(slug, tag) pairs for the categories that get their own endpoint.

    The slug is the URL path; the tag is the real OpenAPI tag used to filter
    tools. Default: every category that survives exclusion. Override with
    VULTR_MCP_CATEGORY_ENDPOINTS (comma-separated slugs; empty string mounts
    the root server only). Requests are matched by slug so users never need to
    type a space or capital.
    """
    available = {slugify(tag): tag for tag in (all_categories(spec) - excluded)}
    raw = os.environ.get("VULTR_MCP_CATEGORY_ENDPOINTS")
    if raw is None:
        return sorted(available.items())
    requested = {slugify(c) for c in raw.split(",") if c.strip()}
    unknown = requested - available.keys()
    if unknown:
        # Don't fail the whole server over a typo — skip and log.
        print(f"warning: unknown/excluded category endpoints ignored: {sorted(unknown)}")
    return sorted((s, available[s]) for s in requested & available.keys())


def create_http_app(spec: dict | None = None):
    if spec is None:
        spec = load_spec()

    excluded = _resolve_exclusions()

    # Resolved once so every mount shares one posture, and /healthz reports what
    # the servers were actually built with.
    read_only = read_only_from_env()

    # Built once and shared, so every endpoint validates the same Vultr token.
    from vultr_mcp.auth import build_auth

    auth = build_auth()

    # DNS-rebinding protection validates Host: behind an ingress the public host
    # must be allow-listed or requests 421.
    from urllib.parse import urlparse

    resource_url = os.environ.get("MCP_RESOURCE_URL", "https://vultrmcp.com")
    resource_host = urlparse(resource_url).netloc
    allowed_hosts = [h for h in (resource_host, "localhost", "127.0.0.1") if h]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]

    # The consent page POSTs to /consent from our own origin; without it
    # allow-listed, DNS-rebinding protection 403s that POST.
    allowed_origins = [o for o in (resource_url.rstrip("/"),) if o]
    allowed_origins += [
        o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    # stateless_http: required behind a round-robin ingress, or a session
    # created on pod A is "not found" when the next request lands on pod B.
    # path="/" keeps URLs clean: "/instances", not "/instances/mcp".
    def _http(server) -> object:
        return server.http_app(
            path="/",
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            stateless_http=True,
        )

    root_server = create_server(
        spec, exclude_categories=excluded, read_only=read_only, auth=auth
    )
    root_app = _http(root_server)

    mounted: list[tuple[str, object]] = []
    for slug, tag in _category_endpoints(spec, excluded):
        server = create_server(
            spec,
            exclude_categories=excluded,
            only_categories={tag},
            read_only=read_only,
            auth=auth,
        )
        mounted.append((slug, _http(server)))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Run every sub-app's lifespan (each starts its MCP session manager).
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(root_app.lifespan(root_app))
            for _, sub_app in mounted:
                await stack.enter_async_context(sub_app.lifespan(sub_app))
            yield

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "vultr-mcp-server",
                "version": VERSION,
                # So a deploy's write posture is verifiable without listing
                # tools -- the thing worth catching a misconfiguration on.
                "read_only": read_only,
            }
        )

    # Route order matters: health + specific category mounts before the
    # catch-all root mount.
    routes = [Route("/healthz", healthz, methods=["GET"])]
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in mounted]
    # "/mcp" is the conventional streamable-HTTP path and the first thing people
    # try, but it is not a category, so it used to 404 in the catch-all. Root
    # stays canonical: a browser hitting it gets the docs page, which "/mcp"
    # has no reason to do.
    routes.append(Mount("/mcp", app=root_app))
    routes.append(Mount("/", app=root_app))

    starlette_app = Starlette(routes=routes, lifespan=lifespan)

    # Starlette would 307 "/instances" -> "/instances/", and MCP clients don't
    # follow that on POST. Rewriting internally means nobody has to remember
    # the trailing slash.
    bare_paths = {f"/{name}" for name, _ in mounted} | {"/mcp"}

    landing_html = _load_landing_html()

    async def app_with_bare_paths(scope, receive, send):
        # Browser hitting the root gets human docs; MCP clients (POST, or GET
        # for the SSE stream) fall through to the protocol app at "/".
        if _wants_landing_page(scope):
            await HTMLResponse(landing_html)(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") in bare_paths:
            fixed = scope["path"] + "/"
            scope = {**scope, "path": fixed, "raw_path": fixed.encode()}
        await starlette_app(scope, receive, send)

    return app_with_bare_paths


def __getattr__(name: str):
    """ASGI entrypoint for `uvicorn vultr_mcp.app:app`, built on first access.

    Lazy because building it constructs every mounted server, and this module is
    also imported for `create_http_app` and `slugify` by callers wanting neither.
    The container runs `python -m vultr_mcp`, so only a hand-run uvicorn gets
    here.
    """
    if name == "app":
        return create_http_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
