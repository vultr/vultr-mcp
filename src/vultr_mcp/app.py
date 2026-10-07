"""HTTP composition: root server plus path-based group endpoints.

``/`` and ``/mcp`` serve the full surface; ``/mcp/compute`` and the other eight
serve one product family each (see ENDPOINT_GROUPS), so a client can load a
few dozen tools instead of ~190. The older per-category paths -- ``/instances``,
``/mcp/instances`` -- still work, as aliases of their group. Exclusions and the
read-only gate always apply on top.

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

from vultr_mcp import landing
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


# The endpoints a client can load instead of the full surface, each a product
# family: /mcp/compute serves the vultr_compute_* tools, and so on. They used to
# be one per OpenAPI tag -- 34 of them, several with one or two tools -- which
# is far finer than a person thinks about their infrastructure, and left tool
# descriptions pointing at related tools the endpoint did not load.
#
# Grouped by the family in the tool names, with two deliberate choices:
# kubernetes stays apart from compute, because VKE clusters and Compute Clusters
# are the pair agents confuse; and catalog absorbs marketplace, since both
# answer "what can I deploy". Values are category slugs (slugify of the tag).
# Every category must appear exactly once -- test_http_app fails otherwise, so a
# new tag in the spec cannot quietly go without an endpoint.
ENDPOINT_GROUPS: dict[str, tuple[str, ...]] = {
    "compute": ("instances", "baremetal", "snapshot", "backup", "startup", "instance-templates", "clusters"),
    "network": ("dns", "firewall", "load-balancer", "reserved-ip", "vpcs", "cdns", "private-networks"),
    "databases": ("managed-databases",),
    "account": ("account", "billing", "logs", "ssh", "subaccount", "tickets"),
    "catalog": ("plans", "region", "os", "iso", "application", "marketplace"),
    "storage": ("block", "s3", "vfs", "storage-gateways"),
    "registry": ("container-registry",),
    "kubernetes": ("kubernetes",),
    "inference": ("serverless-inference",),
}


def _group_endpoints(spec: dict, excluded: set[str]) -> list[tuple[str, set[str], list[str]]]:
    """(group, tags, category slugs) for each endpoint to mount.

    The tags filter the group's tools; the category slugs are the old
    one-endpoint-per-tag paths, which stay mounted as aliases of their group so
    no configuration written against them breaks. Default: every group with a
    category that survives exclusion. VULTR_MCP_CATEGORY_ENDPOINTS narrows that
    to the groups named -- by group, or by an old category name, which selects
    the group it now belongs to. An empty string mounts the root server only.

    A category in no group still gets an endpoint of its own, with a warning:
    serving it beats hiding it, and the test catches it before it ships.
    """
    available = {slugify(tag): tag for tag in (all_categories(spec) - excluded)}
    groups = {g: [c for c in cats if c in available] for g, cats in ENDPOINT_GROUPS.items()}
    assigned = {c for cats in ENDPOINT_GROUPS.values() for c in cats}
    for slug in sorted(available.keys() - assigned):
        print(f"warning: category {slug!r} is in no endpoint group; serving it on its own")
        groups[slug] = [slug]
    groups = {g: cats for g, cats in groups.items() if cats}

    raw = os.environ.get("VULTR_MCP_CATEGORY_ENDPOINTS")
    if raw is not None:
        group_of = {c: g for g, cats in groups.items() for c in cats}
        selected = set()
        for name in (slugify(n) for n in raw.split(",") if n.strip()):
            group = name if name in groups else group_of.get(name)
            if group is None:
                # Don't fail the whole server over a typo — skip and log.
                print(f"warning: unknown/excluded endpoint ignored: {name!r}")
            else:
                selected.add(group)
        groups = {g: cats for g, cats in groups.items() if g in selected}

    return [(g, {available[c] for c in cats}, cats) for g, cats in sorted(groups.items())]


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

    resource_url = os.environ.get("MCP_RESOURCE_URL", "https://mcp.vultr.com")
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

    # One server per group. Each is also reachable at its old category paths,
    # which are aliases: the same app, not another server to build and hold.
    group_apps: list[tuple[str, object]] = []
    alias_apps: list[tuple[str, object]] = []
    # The servers themselves, kept for the docs page's endpoint list.
    category_servers: list[tuple[str, object]] = []
    for group, tags, categories in _group_endpoints(spec, excluded):
        server = create_server(
            spec,
            exclude_categories=excluded,
            only_categories=tags,
            read_only=read_only,
            auth=auth,
        )
        app = _http(server)
        group_apps.append((group, app))
        alias_apps += [(category, app) for category in categories]
        category_servers.append((group, server))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Run every sub-app's lifespan (each starts its MCP session manager).
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(root_app.lifespan(root_app))
            # Once per server, not per path: an alias shares its group's app,
            # and a session manager refuses to start twice.
            for _, sub_app in group_apps:
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

    # Route order matters: the first match wins, so every mount comes before
    # any shorter prefix of it.
    routes = [Route("/healthz", healthz, methods=["GET"])]
    # Groups live under "/mcp", beside the full surface: "/mcp/compute", with
    # each old category path an alias of its group: "/mcp/instances". They must
    # precede Mount("/mcp"), which would otherwise take them. dict() drops the
    # repeat where a group shares its name with a category (kubernetes).
    mcp_paths = dict(group_apps + alias_apps)
    routes += [Mount(f"/mcp/{name}", app=sub_app) for name, sub_app in mcp_paths.items()]
    # "/mcp" is the conventional streamable-HTTP path and the first thing people
    # try, but it is not a category, so it used to 404 in the catch-all. Root
    # stays canonical: a browser hitting it gets the docs page, which "/mcp"
    # has no reason to do.
    routes.append(Mount("/mcp", app=root_app))
    # The bare "/instances" form came first. It stays so no existing client
    # configuration breaks, but it is no longer the documented path: at the top
    # level, category names share one namespace with /authorize, /token,
    # /consent and /healthz, and a new category could collide with a route.
    # Only the old category names, for the same reason: groups are not added
    # at the top level.
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in alias_apps]
    routes.append(Mount("/", app=root_app))

    starlette_app = Starlette(routes=routes, lifespan=lifespan)

    # Starlette would 307 "/instances" -> "/instances/", and MCP clients don't
    # follow that on POST. Rewriting internally means nobody has to remember
    # the trailing slash.
    bare_paths = {f"/{name}" for name, _ in alias_apps} | {f"/mcp/{name}" for name in mcp_paths} | {"/mcp"}

    landing_template = _load_landing_html()
    landing_cache: dict[str, str] = {}

    async def landing_page() -> str:
        # Built on first view rather than at boot: listing tools is async, and
        # boot is kept to what serving MCP needs. The tool set is fixed for the
        # life of the process, so the page is built once.
        if "html" not in landing_cache:
            try:
                endpoints = [(slug, await landing.tools_of(server)) for slug, server in category_servers]
                total = len(await landing.tools_of(root_server))
                landing_cache["html"] = landing.render(landing_template, total, endpoints)
            except Exception:  # noqa: BLE001 - the docs page must not fail over its list
                return landing.unavailable(landing_template)
        return landing_cache["html"]

    async def app_with_bare_paths(scope, receive, send):
        # Browser hitting the root gets human docs; MCP clients (POST, or GET
        # for the SSE stream) fall through to the protocol app at "/".
        if _wants_landing_page(scope):
            await HTMLResponse(await landing_page())(scope, receive, send)
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
