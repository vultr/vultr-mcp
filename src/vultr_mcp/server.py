"""Vultr MCP server: the tool surface, built from ``openapi.json``.

Each caller's own credential is forwarded to api.vultr.com per request, resolved
in the order ``PerRequestVultrAuth`` documents. The surface is read-only unless
``VULTR_MCP_WRITES_ENABLED`` is set.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Generator

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.providers.openapi import MCPType, RouteMap

from vultr_mcp.interface.compiler import CompiledInterface, compile_interface
from vultr_mcp.interface.tools import InterfaceTool

VULTR_API_BASE = os.environ.get("VULTR_API_BASE_URL", "https://api.vultr.com/v2")

# Identity and credential-management tags, kept off the hosted surface: these
# tools would always 403 under the OAuth client's IAM policy, and excluding them
# keeps identity mutations out of prompt-injection reach on every auth path.
# Override with VULTR_MCP_EXCLUDED_CATEGORIES (empty string disables).
DEFAULT_EXCLUDED_CATEGORIES = frozenset(
    {"api-keys", "users", "iam", "scim", "organizations", "oidc", "oauth"}
)

# Everything that is not a GET mutates something, including the two OPTIONS
# routes -- they mint container-registry Docker credentials despite the verb.
WRITE_METHODS: tuple[str, ...] = (
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "TRACE",
)

# Non-GET operations that only read. A POST because its filter arrives in a
# request body, but it creates nothing. (method, anchored OpenAPI path regex)
READ_ONLY_METHOD_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("POST", r"^/databases/\{database-id\}/alerts$"),
)


class PerRequestVultrAuth(httpx.Auth):
    """Resolve the caller's Vultr credential at call time.

    In priority order: the verified OAuth ``AccessToken``, then the incoming
    request's ``Authorization`` / ``X-Vultr-API-Key`` header, then
    ``VULTR_API_KEY`` -- the last reachable only outside an HTTP request.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token: str | None = None

        # AccessToken.token is the UPSTREAM Vultr token FastMCP swapped in, not
        # the proxy-issued one the client holds -- forwarding that gives
        # "Invalid API token".
        try:
            from fastmcp.server.dependencies import get_access_token

            access = get_access_token()
            if access is not None and getattr(access, "token", None):
                token = f"Bearer {access.token}"
        except Exception:
            pass

        # include_all=True is required: get_http_headers() strips `authorization`
        # by default. An empty dict means no HTTP request is in scope at all. If
        # that cannot be determined, assume there is one -- being wrong that way
        # only withholds a local convenience.
        incoming: dict[str, str] = {}
        in_http_request = True
        if not token:
            try:
                incoming = get_http_headers(include_all=True)
                in_http_request = bool(incoming)
            except Exception:
                pass

            auth_header = incoming.get("authorization", "")
            if auth_header:
                token = auth_header
            else:
                api_key = incoming.get("x-vultr-api-key", "")
                if api_key:
                    token = f"Bearer {api_key}"

        # Unreachable from an HTTP request, deliberately: substituting the
        # server's own key for a caller who presented none would serve anonymous
        # requests as the operator. Over HTTP, no credential must mean a 401.
        if not token and not in_http_request:
            env_key = os.environ.get("VULTR_API_KEY", "")
            if env_key:
                token = f"Bearer {env_key}"

        if token:
            request.headers["Authorization"] = token

        yield request


def load_spec(path: str | Path | None = None) -> dict:
    """Load the Vultr OpenAPI spec bundled next to the package root."""
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent / "openapi.json"
    with open(path, encoding="utf-8") as fh:
        return sanitize_spec(json.load(fh))


# Programming-language type names appearing where JSON Schema types belong.
# Keyed by alias rather than validity: `securitySchemes.type: http` is a legal
# non-schema type and must survive untouched.
TYPE_ALIASES = {
    "int": "integer",
    "bool": "boolean",
    "float": "number",
    # Enum values live in the description; the author meant a string.
    "enum": "string",
}

# Sample payloads, not schemas: a startup script whose `type` is "pxe" is data,
# and rewriting it would corrupt the examples.
EXAMPLE_KEYS = frozenset({"example", "examples", "x-examples", "x-codeSamples"})


def sanitize_spec(spec: dict) -> dict:
    """Fix spec-validity defects in Vultr's published openapi.json.

    FastMCP's parser enforces the OpenAPI schema strictly where the old PHP
    generator was lenient, which is how these shipped unnoticed. All three
    classes below are reported upstream.
    """
    # (1) response objects missing the required `description`
    for path_item in spec.get("paths", {}).values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for response in op.get("responses", {}).values():
                if isinstance(response, dict) and "$ref" not in response:
                    response.setdefault("description", "")

    # (2) programming-language type names → JSON Schema types, everywhere but
    # inside example payloads.
    def fix_type_aliases(node: object) -> None:
        if isinstance(node, dict):
            declared = node.get("type")
            if isinstance(declared, str) and declared in TYPE_ALIASES:
                node["type"] = TYPE_ALIASES[declared]
            for key, value in node.items():
                if key not in EXAMPLE_KEYS:
                    fix_type_aliases(value)
        elif isinstance(node, list):
            for item in node:
                fix_type_aliases(item)

    fix_type_aliases(spec)

    # (3) parameters missing `in`; vcr_region is the {region} path param of
    # /registry/{registry-id}/replication/{region}
    for pname, param in spec.get("components", {}).get("parameters", {}).items():
        if isinstance(param, dict) and "$ref" not in param and "in" not in param:
            if pname == "vcr_region":
                param["name"] = "region"
                param["in"] = "path"
                param.setdefault("schema", {"type": "string"})
            else:
                # Unknown future defect: default to a query param so the
                # parser accepts it rather than dropping the whole spec.
                param["in"] = "query"
            param.setdefault("required", param.get("in") == "path")

    return spec


# A record of tags somebody reviewed, not a filter: its only job is to make a
# NEW tag fail the build until a human sorts it into this set or
# DEFAULT_EXCLUDED_CATEGORIES. Without it, one spec update silently added 24
# OAuth client-management operations and an endpoint returning `s3_secret_key`.
REVIEWED_CATEGORIES = frozenset(
    {
        "CDNs",
        "Container Registry",
        "VFS",
        "VPCs",
        "account",
        "application",
        "backup",
        "baremetal",
        "billing",
        "block",
        "clusters",
        "dns",
        "firewall",
        "instance-templates",
        "instances",
        "iso",
        "kubernetes",
        "load-balancer",
        "logs",
        "managed-databases",
        "marketplace",
        "os",
        "plans",
        "private Networks",
        "region",
        "reserved-ip",
        "s3",
        "serverless-inference",
        "snapshot",
        "ssh",
        "startup",
        "storage-gateways",
        "subaccount",
        "tickets",
    }
)


def unreviewed_categories(spec: dict) -> set[str]:
    """Tags nobody has decided about: neither excluded nor reviewed.

    The category-level twin of the interface layer's drift report. A spec update
    that introduces a product area is news that has to reach a person, because
    the default is exposure and the cost of missing one is a tool surface nobody
    chose.
    """
    return all_categories(spec) - set(DEFAULT_EXCLUDED_CATEGORIES) - REVIEWED_CATEGORIES


def all_categories(spec: dict) -> set[str]:
    """Every OpenAPI operation tag present in the spec."""
    tags: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for op in path_item.values():
            if isinstance(op, dict):
                tags.update(op.get("tags") or [])
    return tags


def excluded_categories_from_env() -> set[str] | None:
    """Excluded categories from VULTR_MCP_EXCLUDED_CATEGORIES.

    Returns None when the variable is unset (caller applies the default),
    or the parsed set (possibly empty, to disable exclusions) when set.
    """
    raw = os.environ.get("VULTR_MCP_EXCLUDED_CATEGORIES")
    if raw is None:
        return None
    return {tag.strip() for tag in raw.split(",") if tag.strip()}


def _strip_output_schema(route, component) -> None:
    """Drop the generated ``outputSchema`` from every tool.

    Derived from each operation's response schema, these were 64% of the root
    listing's bytes (480KB of 750KB) and took it to ~187k tokens, which clients
    reject. Dropping them lands at ~66k with no tool removed and no change to
    results -- only the schema describing their shape goes.

    ``from_openapi(validate_output=False)`` looks like the lever and is not: it
    disables validation while still advertising the schema. Set
    VULTR_MCP_OUTPUT_SCHEMAS=true to keep them.
    """
    component.output_schema = None


def _output_schemas_enabled() -> bool:
    return os.environ.get("VULTR_MCP_OUTPUT_SCHEMAS", "false").lower() in ("1", "true", "yes")


def read_only_from_env() -> bool:
    """Whether the tool surface is read-only. Default: yes.

    Writes are opt-in (VULTR_MCP_WRITES_ENABLED), not opt-out, so a deployment
    that forgets to set anything ships the safe surface.
    """
    return os.environ.get("VULTR_MCP_WRITES_ENABLED", "false").lower() not in (
        "1",
        "true",
        "yes",
    )


def interface_dir_from_env() -> Path | None:
    """Where the interface layer lives, or None when it is switched off.

    Defaults to the ``interface/`` directory beside openapi.json. Set
    VULTR_MCP_INTERFACE=off to run the generated surface alone (useful for
    measuring what the layer changes), or VULTR_MCP_INTERFACE_DIR to point at
    another copy.
    """
    if os.environ.get("VULTR_MCP_INTERFACE", "on").lower() in ("0", "off", "false", "no"):
        return None
    override = os.environ.get("VULTR_MCP_INTERFACE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "interface"


# One compiled interface per (directory, spec), reused across the ~35 servers
# create_http_app builds -- recompiling per server took boot from ~4.7s to ~26s.
# The spec is compared by identity and held by reference: a dict is unhashable,
# and holding it keeps the comparison sound. A different spec object recompiles,
# which tests that build a scratch spec rely on.
_INTERFACE_CACHE: dict[str, tuple[dict, CompiledInterface]] = {}


def clear_interface_cache() -> None:
    """Drop the compiled-interface cache.

    For tests that rewrite definitions on disk under a path they have already
    loaded. Nothing in the server's own lifecycle needs it: the directory does
    not change under a running process.
    """
    _INTERFACE_CACHE.clear()


def load_interface(spec: dict, interface_dir: Path | None) -> CompiledInterface:
    """Compile the interface layer, or return an empty one when absent.

    A missing directory is fine; a broken one raises, because a half-loaded
    layer means the served surface is not the reviewed one. The result is
    cached and shared, which is safe because every compiled type is a frozen
    dataclass that ``InterfaceTool.build`` only reads.
    """
    if interface_dir is None or not (interface_dir / "interface.yaml").exists():
        return CompiledInterface(version="none")

    key = str(interface_dir.resolve())
    cached = _INTERFACE_CACHE.get(key)
    if cached is not None and cached[0] is spec:
        return cached[1]

    compiled = compile_interface(interface_dir, spec)
    _INTERFACE_CACHE[key] = (spec, compiled)
    return compiled


def _build_route_maps(
    exclude_tags: set[str],
    read_only: bool,
    interface_routes: list[tuple[str, str]] | None = None,
) -> list[RouteMap]:
    """RouteMaps applied in order -- first match wins, default is TOOL.

    1. EXCLUDE per operation the interface layer owns; most specific, so first.
    2. EXCLUDE per excluded tag, one map each: RouteMap requires *all* its tags
       on a route, and an operation carries exactly one, so a multi-tag map
       would never match. Before the method maps, so an identity exclusion
       cannot be undone by a later one.
    3. Read-only overrides re-admitted as TOOLs...
    4. ...then every remaining write method excluded. GETs match nothing and
       fall through to the default.
    """
    maps = [
        RouteMap(
            methods=[method],
            pattern=f"^{re.escape(path)}$",
            mcp_type=MCPType.EXCLUDE,
        )
        for method, path in sorted(interface_routes or [])
    ]
    maps += [RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE) for tag in sorted(exclude_tags)]
    if read_only:
        maps += [
            RouteMap(methods=[method], pattern=pattern, mcp_type=MCPType.TOOL)
            for method, pattern in READ_ONLY_METHOD_OVERRIDES
        ]
        maps.append(RouteMap(methods=list(WRITE_METHODS), mcp_type=MCPType.EXCLUDE))
    return maps


def create_server(
    spec: dict | None = None,
    *,
    exclude_categories: set[str] | None = None,
    only_categories: set[str] | None = None,
    read_only: bool | None = None,
    auth=None,
    interface_dir: Path | None = None,
    use_interface: bool | None = None,
) -> FastMCP:
    """Build a FastMCP server over the Vultr tool surface.

    exclude_categories:
        Tags to drop. Defaults to the env value, else
        DEFAULT_EXCLUDED_CATEGORIES. Pass an explicit empty set to keep
        everything (e.g. local STDIO power use).
    only_categories:
        When set, exposes ONLY these tags (the path-based category-endpoint
        model from the PHP server, e.g. an /instances-only connection). The
        default identity exclusions still apply on top, so a category
        endpoint can never resurface an excluded identity tool.
    read_only:
        Drop every state-changing operation. Defaults to the env value
        (read-only unless VULTR_MCP_WRITES_ENABLED opts in).
    interface_dir:
        Directory of reviewed tool definitions. Defaults to the env value,
        else ``interface/`` beside openapi.json. Each tool it defines replaces
        the generated tool for the same operation.
    use_interface:
        Pass False to serve the generated surface alone, which is how the
        eval framework measures what the layer is worth.
    """
    if spec is None:
        spec = load_spec()

    if read_only is None:
        read_only = read_only_from_env()

    if exclude_categories is None:
        exclude_categories = excluded_categories_from_env()
        if exclude_categories is None:
            exclude_categories = set(DEFAULT_EXCLUDED_CATEGORIES)

    exclude_tags = set(exclude_categories)
    if only_categories is not None:
        # Exclude every category not requested (identity exclusions merge in).
        exclude_tags |= all_categories(spec) - set(only_categories)

    ssl_verify = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

    client = httpx.AsyncClient(
        base_url=VULTR_API_BASE,
        auth=PerRequestVultrAuth(),
        verify=ssl_verify,
        timeout=30.0,
        headers={"User-Agent": "vultr-mcp-server/2.0 (python; fastmcp)"},
    )

    if use_interface is False:
        interface = CompiledInterface(version="none")
    else:
        if interface_dir is None:
            interface_dir = interface_dir_from_env()
        interface = load_interface(spec, interface_dir)

    # Hand-authored tools pass the same gates as the generated surface, so the
    # layer can never reintroduce a write or identity tool that policy excluded.
    interface_tools = [
        tool
        for tool in interface.tools
        if not (read_only and tool.is_write) and not (tool.tags & exclude_tags)
    ]

    # Two reasons to drop a generated route: a hand-authored tool replaces its
    # twin (serving both gives the agent two tools for one operation), or the
    # operation is excluded outright, with no replacement.
    suppressed = [
        (tool.method.upper(), tool.path_template) for tool in interface_tools
    ] + [
        (excluded.method, excluded.path_template) for excluded in interface.excluded
    ]

    server = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Vultr MCP Server",
        route_maps=_build_route_maps(exclude_tags, read_only, suppressed),
        mcp_component_fn=None if _output_schemas_enabled() else _strip_output_schema,
        auth=auth,
    )

    for tool in interface_tools:
        server.add_tool(InterfaceTool.build(tool, client))

    return server


def main() -> None:
    transport = os.environ.get("VULTR_MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        # HTTP mode serves the composed app (root + path-based category
        # endpoints + /healthz) via uvicorn.
        import uvicorn

        from vultr_mcp.app import create_http_app

        uvicorn.run(
            create_http_app(),
            host=os.environ.get("SERVER_HOST", "0.0.0.0"),
            port=int(os.environ.get("SERVER_PORT", "8080")),
        )
    else:
        # STDIO/local: single full server, credential from VULTR_API_KEY.
        create_server().run()


if __name__ == "__main__":
    main()
