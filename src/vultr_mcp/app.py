"""HTTP composition: root server plus path-based group endpoints.

``/`` and ``/mcp`` serve the full surface; ``/mcp/<group>`` serves one product family (ENDPOINT_GROUPS),
and the older per-category paths (``/instances``, ``/mcp/instances``) are aliases of their group.
Apps are built eagerly, since a lazily mounted one cannot start its lifespan under a running parent.
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from vultr_mcp.server import (
    DEFAULT_EXCLUDED_CATEGORIES,
    all_categories,
    create_server,
    excluded_categories_from_env,
    load_spec,
    package_version,
)

VERSION = package_version()


def _resolve_exclusions() -> set[str]:
    env = excluded_categories_from_env()
    return env if env is not None else set(DEFAULT_EXCLUDED_CATEGORIES)


def slugify(tag: str) -> str:
    """URL path slug for a category tag: "Container Registry" -> "container-registry"."""
    return "-".join(tag.lower().split())


# Product-family endpoints, as category slugs; every category appears exactly once (test_http_app checks).
# Kubernetes stays apart from compute because agents confuse VKE clusters with compute clusters.
ENDPOINT_GROUPS: dict[str, tuple[str, ...]] = {
    "compute": ("instances", "baremetal", "snapshot", "backup", "startup", "instance-templates", "clusters"),
    "network": ("dns", "firewall", "load-balancer", "reserved-ip", "vpcs", "cdns", "private-networks"),
    "databases": ("managed-databases",),
    "account": ("account", "billing", "logs", "ssh", "subaccount", "tickets", "software-licenses"),
    "catalog": ("plans", "region", "os", "iso", "application", "marketplace"),
    "storage": ("block", "s3", "vfs", "storage-gateways"),
    "registry": ("container-registry",),
    "kubernetes": ("kubernetes",),
    "inference": ("serverless-inference",),
}


def _group_endpoints(spec: dict, excluded: set[str]) -> list[tuple[str, set[str], list[str]]]:
    """(group, tags, category slugs) for each endpoint to mount.

    VULTR_MCP_CATEGORY_ENDPOINTS narrows the groups by group or category name; empty mounts only the root.
    A category in no group is served on its own, with a warning.
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
                print(f"warning: unknown/excluded endpoint ignored: {name!r}")
            else:
                selected.add(group)
        groups = {g: cats for g, cats in groups.items() if g in selected}

    return [(g, {available[c] for c in cats}, cats) for g, cats in sorted(groups.items())]


def create_http_app(spec: dict | None = None):
    if spec is None:
        spec = load_spec()

    excluded = _resolve_exclusions()

    # Built once and shared, so every endpoint validates the same Vultr token.
    from vultr_mcp.auth import build_auth

    auth = build_auth()

    # DNS-rebinding protection checks Host, so the public host must be allow-listed.
    from urllib.parse import urlparse

    resource_url = os.environ.get("MCP_RESOURCE_URL", "https://mcp.vultr.com")
    resource_host = urlparse(resource_url).netloc
    allowed_hosts = [h for h in (resource_host, "localhost", "127.0.0.1") if h]
    allowed_hosts += [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]

    # The consent page POSTs to /consent from our own origin.
    allowed_origins = [o for o in (resource_url.rstrip("/"),) if o]
    allowed_origins += [
        o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    # stateless_http, because the ingress round-robins across pods.
    def _http(server) -> object:
        return server.http_app(
            path="/",
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
            stateless_http=True,
        )

    root_server = create_server(spec, exclude_categories=excluded, auth=auth)
    root_app = _http(root_server)

    # One server per group; its category paths are aliases of the same app.
    group_apps: list[tuple[str, object]] = []
    alias_apps: list[tuple[str, object]] = []
    for group, tags, categories in _group_endpoints(spec, excluded):
        server = create_server(
            spec,
            exclude_categories=excluded,
            only_categories=tags,
            auth=auth,
        )
        app = _http(server)
        group_apps.append((group, app))
        alias_apps += [(category, app) for category in categories]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(root_app.lifespan(root_app))
            # Once per server, not per path: a session manager refuses to start twice.
            for _, sub_app in group_apps:
                await stack.enter_async_context(sub_app.lifespan(sub_app))
            yield

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "vultr-mcp-server",
                "version": VERSION,
                # Always true now that there is no write switch; kept so monitors reading it still work.
                "read_only": True,
            }
        )

    # First match wins, so every mount precedes any shorter prefix of it.
    routes = [Route("/healthz", healthz, methods=["GET"])]
    # dict() drops the repeat where a group shares its name with a category (kubernetes).
    mcp_paths = dict(group_apps + alias_apps)
    routes += [Mount(f"/mcp/{name}", app=sub_app) for name, sub_app in mcp_paths.items()]
    # "/mcp" is the conventional streamable-HTTP path and the documented one; the root serves the same app.
    routes.append(Mount("/mcp", app=root_app))
    # Undocumented top-level category aliases, kept for existing configs; groups are never added here.
    routes += [Mount(f"/{name}", app=sub_app) for name, sub_app in alias_apps]
    routes.append(Mount("/", app=root_app))

    starlette_app = Starlette(routes=routes, lifespan=lifespan)

    # Starlette would 307 "/instances" to "/instances/", which MCP clients do not follow on POST.
    bare_paths = {f"/{name}" for name, _ in alias_apps} | {f"/mcp/{name}" for name in mcp_paths} | {"/mcp"}

    async def app_with_bare_paths(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in bare_paths:
            fixed = scope["path"] + "/"
            scope = {**scope, "path": fixed, "raw_path": fixed.encode()}
        await starlette_app(scope, receive, send)

    return app_with_bare_paths


def __getattr__(name: str):
    """ASGI entrypoint for `uvicorn vultr_mcp.app:app`, built on first access since it builds every server."""
    if name == "app":
        return create_http_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
