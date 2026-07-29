"""HTTP composition: root server + path-based category endpoints.

Mirrors the PHP server's path-based tool filtering. A client connects to:

  * ``/`` or ``/mcp``  -> the full (default-excluded) tool surface
  * ``/instances``     -> only the ``instances`` category, etc.

Category endpoints are token-efficient: a client that only manages VPS
instances loads ~40 tools instead of ~400. The identity exclusions always
apply on top, so a category endpoint can never expose an excluded tool.

Build cost is a one-time ~0.1s per mounted category at boot; each app is
created eagerly so its MCP session-manager lifespan runs at startup (lazy
mounting can't start a lifespan after the parent is already running).
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
)

VERSION = "2.0.0"

_LANDING_PATH = Path(__file__).resolve().parent / "static" / "index.html"


def _load_landing_html() -> str:
    """Read the human-facing docs page served on browser GETs to ``/``.

    Missing file is non-fatal — a browser just gets a tiny fallback rather
    than the server failing to boot over a docs asset.
    """
    try:
        return _LANDING_PATH.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Vultr MCP</title><h1>Vultr MCP Server</h1>"


def _wants_landing_page(scope: dict) -> bool:
    """True only for a genuine top-level browser navigation to ``/``.

    The docs page and the MCP endpoint share the root URL, and the server can't
    see whether the client was configured with ``vultrmcp.com`` or
    ``vultrmcp.com/`` — both arrive as path ``/``. So we distinguish a *human
    opening the page* from a *client connecting* by request shape:

    * MCP Streamable HTTP uses POST (JSON-RPC) and a GET with
      ``Accept: text/event-stream`` (SSE) — never the docs.
    * A real browser navigation sets ``Sec-Fetch-Mode: navigate``. A
      browser-based MCP client (e.g. a hosted agent web UI) connects with
      ``fetch``/XHR, which sets ``Sec-Fetch-Mode: cors``/``no-cors`` — so it
      falls through to the protocol even though it runs in a browser, which is
      what makes the bare host work for those clients without a trailing slash.
    * When ``Sec-Fetch-*`` is absent (older browsers, header-stripping proxies,
      curl), fall back to an explicit ``Accept: text/html`` and never ``*/*``.
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

    # OAuthProxy (Phase 4) — built once, shared across root + category servers
    # so every endpoint validates the same Vultr token. None when OIDC is off.
    from vultr_mcp.auth import build_auth

    auth = build_auth()

    # DNS-rebinding protection validates the Host header. Behind an ingress the
    # public host (e.g. vultrmcp.com) must be allow-listed or requests 421.
    # Derive it from MCP_RESOURCE_URL; extend via MCP_ALLOWED_HOSTS.
    from urllib.parse import urlparse

    resource_url = os.environ.get("MCP_RESOURCE_URL", "https://vultrmcp.com")
    resource_host = urlparse(resource_url).netloc
    allowed_hosts = [h for h in (resource_host, "localhost", "127.0.0.1") if h]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]

    # allowed_origins: the OAuthProxy consent page POSTs to /consent from the
    # server's own origin. Without the public origin allow-listed, FastMCP's
    # DNS-rebinding protection 403s that POST ("forbidden origin on the allow
    # screen"). Allow our own origin plus any configured extras.
    allowed_origins = [o for o in (resource_url.rstrip("/"),) if o]
    allowed_origins += [
        o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    # stateless_http: each request is self-contained, so no MCP session lives
    # in one replica's memory. Required for multi-replica behind a round-robin
    # ingress — otherwise a session created on pod A is "not found" when the
    # next request lands on pod B. (The PHP server solved the same problem with
    # a shared Redis session store.)
    # path="/" serves each server's MCP endpoint at its mount root, so URLs are
    # clean: root at "/", a category at "/instances" (not "/instances/mcp").
    def _http(server) -> object:
        return server.http_app(
            path="/",
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            stateless_http=True,
        )

    # Root ("/") serves the full non-excluded tool surface. Each category is
    # mounted at its slug ("/container-registry") and exposes only that
    # category's tools — for clients with limited MCP slots.
    root_server = create_server(spec, exclude_categories=excluded, auth=auth)
    root_app = _http(root_server)

    mounted: list[tuple[str, object]] = []
    for slug, tag in _category_endpoints(spec, excluded):
        server = create_server(
            spec, exclude_categories=excluded, only_categories={tag}, auth=auth
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
            {"status": "ok", "service": "vultr-mcp-server", "version": VERSION}
        )

    # Route order matters: health + specific category mounts before the
    # catch-all root mount.
    routes = [Route("/healthz", healthz, methods=["GET"])]
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in mounted]
    routes.append(Mount("/", app=root_app))

    starlette_app = Starlette(routes=routes, lifespan=lifespan)

    # Users connect to a bare category path — https://vultrmcp.com/instances —
    # with NO trailing slash. Starlette would otherwise 307-redirect
    # "/instances" -> "/instances/" (the mounted sub-app's canonical path), and
    # MCP clients don't follow that redirect on POST. This ASGI shim rewrites
    # the bare path to its trailing-slash form internally, so the redirect never
    # happens and nobody has to remember the slash. Root ("/") already has one.
    bare_paths = {f"/{name}" for name, _ in mounted}

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


# ASGI entrypoint for `uvicorn vultr_mcp.app:app`
app = create_http_app() if os.environ.get("VULTR_MCP_TRANSPORT", "").lower() == "http" else None
