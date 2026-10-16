"""Vultr MCP server: the tool surface, built from ``openapi.json``.

Each caller's own credential is forwarded to api.vultr.com per request, resolved
in the order ``PerRequestVultrAuth`` documents. The surface is read-only: every
state-changing operation is dropped, and there is no switch to restore them.
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

from vultr_mcp.audit import AuditMiddleware, audit_enabled
from vultr_mcp.diagnostics import InstrumentedTransport, PathTemplates
from vultr_mcp.interface.compiler import CompiledInterface, compile_interface
from vultr_mcp.interface.tools import InterfaceTool
from vultr_mcp.suspension import SuspendedTransport
from vultr_mcp.tool_list_refresh import ToolListRefreshMiddleware

VULTR_API_BASE = os.environ.get("VULTR_API_BASE_URL", "https://api.vultr.com/v2")

# Sent on API calls and token refreshes alike, so a Use MCP grant conditioned on User-Agent decides both the same way.
USER_AGENT = "vultr-mcp-server/2.0 (python; fastmcp)"


def package_version() -> str:
    """The installed package version, for /healthz and the MCP handshake."""
    try:
        from importlib.metadata import version

        return version("vultr-mcp")
    except Exception:  # noqa: BLE001 - never fail a boot or a health check over this
        return "unknown"

# Identity and credential tags, kept off the surface on every auth path; VULTR_MCP_EXCLUDED_CATEGORIES overrides.
DEFAULT_EXCLUDED_CATEGORIES = frozenset(
    {"api-keys", "users", "iam", "scim", "organizations", "oidc", "oauth"}
)

# Everything but GET mutates something; the OPTIONS routes mint registry Docker credentials.
WRITE_METHODS: tuple[str, ...] = (
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "TRACE",
)

# Non-GET operations that only read, as (method, anchored OpenAPI path regex).
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

        # AccessToken.token is the upstream Vultr token, not the proxy-issued one the client holds.
        try:
            from fastmcp.server.dependencies import get_access_token

            access = get_access_token()
            if access is not None and getattr(access, "token", None):
                token = f"Bearer {access.token}"
        except Exception:
            pass

        # include_all=True, since authorization is stripped by default; no headers means no HTTP request in scope.
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

        # Never over HTTP: a caller with no credential must get a 401, not the operator's key.
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


# Language type names the spec uses where JSON Schema types belong.
TYPE_ALIASES = {
    "int": "integer",
    "bool": "boolean",
    "float": "number",
    # Enum values live in the description; the author meant a string.
    "enum": "string",
}

# Sample payloads, not schemas, so their `type` values are data and left alone.
EXAMPLE_KEYS = frozenset({"example", "examples", "x-examples", "x-codeSamples"})


def sanitize_spec(spec: dict) -> dict:
    """Fix the spec-validity defects in Vultr's openapi.json that FastMCP's strict parser rejects."""
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

    # (3) parameters missing `in`; vcr_region is the {region} of /registry/{registry-id}/replication/{region}
    for pname, param in spec.get("components", {}).get("parameters", {}).items():
        if isinstance(param, dict) and "$ref" not in param and "in" not in param:
            if pname == "vcr_region":
                param["name"] = "region"
                param["in"] = "path"
                param.setdefault("schema", {"type": "string"})
            else:
                # Unknown: a query param, so the parser accepts the spec rather than dropping it.
                param["in"] = "query"
            param.setdefault("required", param.get("in") == "path")

    return spec


# Tags someone reviewed, not a filter: a new tag fails the build until it is sorted here or into the exclusions.
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
        "software-licenses",
        "ssh",
        "startup",
        "storage-gateways",
        "subaccount",
        "tickets",
    }
)


def unreviewed_categories(spec: dict) -> set[str]:
    """Tags nobody has decided about: neither excluded nor reviewed."""
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
    """Drop the generated ``outputSchema``s, which nearly triple the tool listing; VULTR_MCP_OUTPUT_SCHEMAS=true keeps them."""
    component.output_schema = None


def _output_schemas_enabled() -> bool:
    return os.environ.get("VULTR_MCP_OUTPUT_SCHEMAS", "false").lower() in ("1", "true", "yes")


def interface_dir_from_env() -> Path | None:
    """``interface/`` beside openapi.json, VULTR_MCP_INTERFACE_DIR, or None when VULTR_MCP_INTERFACE=off."""
    if os.environ.get("VULTR_MCP_INTERFACE", "on").lower() in ("0", "off", "false", "no"):
        return None
    override = os.environ.get("VULTR_MCP_INTERFACE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "interface"


# One compiled interface per directory, reused across every server while the spec object is the same one.
_INTERFACE_CACHE: dict[str, tuple[dict, CompiledInterface]] = {}


def clear_interface_cache() -> None:
    """Drop the compiled-interface cache, for tests that rewrite definitions on disk."""
    _INTERFACE_CACHE.clear()


def load_interface(spec: dict, interface_dir: Path | None) -> CompiledInterface:
    """Compile the interface layer (cached), or an empty one when absent; a broken one raises."""
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
    interface_routes: list[tuple[str, str]] | None = None,
) -> list[RouteMap]:
    """RouteMaps in order, first match wins, default TOOL.

    Interface-owned operations, then excluded tags (one map each, since a map needs all its tags),
    then read-only overrides, then every write method. GETs fall through to the default.
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
    maps += [
        RouteMap(methods=[method], pattern=pattern, mcp_type=MCPType.TOOL)
        for method, pattern in READ_ONLY_METHOD_OVERRIDES
    ]
    maps.append(RouteMap(methods=list(WRITE_METHODS), mcp_type=MCPType.EXCLUDE))
    return maps


# Sent in `initialize`; clients that defer tools show the model nothing else about this server until it searches.
PRODUCT_NAMES: dict[str, str] = {
    "instances": "compute instances",
    "baremetal": "bare metal servers",
    "snapshot": "snapshots",
    "backup": "backups",
    "startup": "startup scripts",
    "instance-templates": "instance templates",
    "clusters": "compute clusters",
    "kubernetes": "Kubernetes (VKE) clusters",
    "firewall": "firewall groups",
    "dns": "DNS domains and records",
    "load-balancer": "load balancers",
    "reserved-ip": "reserved IPs",
    "VPCs": "VPCs",
    "private Networks": "private networks",
    "CDNs": "CDN pull and push zones",
    "block": "block storage",
    "s3": "object storage",
    "VFS": "file systems (VFS)",
    "storage-gateways": "storage gateways",
    "managed-databases": "managed databases",
    "Container Registry": "container registries",
    "serverless-inference": "serverless inference",
    "account": "account details",
    "billing": "billing and invoices",
    "logs": "account activity logs",
    "ssh": "SSH keys",
    "subaccount": "subaccounts",
    "tickets": "support tickets",
    "software-licenses": "software licenses",
    "plans": "plans",
    "region": "regions",
    "os": "operating systems",
    "iso": "ISOs",
    "application": "applications",
    "marketplace": "Marketplace apps",
}

# The whole account, for the root endpoint, where listing every product would bury the point.
ACCOUNT_SUMMARY = (
    "compute instances and bare metal, firewall groups, DNS, load balancers, VPCs, block and object storage, "
    "Kubernetes (VKE), managed databases, container registries, billing and support tickets"
)


def _product_list(tags: set[str]) -> str:
    names = [name for tag, name in PRODUCT_NAMES.items() if tag in tags]
    names += sorted(tag for tag in tags if tag not in PRODUCT_NAMES)
    return ", ".join(names)


def server_instructions(only_categories: set[str] | None = None) -> str:
    """The instructions sent in `initialize`, naming only the products this endpoint serves."""
    products = ACCOUNT_SUMMARY if only_categories is None else _product_list(only_categories)
    return (
        f"Tools for the user's Vultr cloud account: {products}. "
        "Use them for any question about the user's Vultr infrastructure. "
        "When the user names a server, instance, cluster, domain or other resource, it is most likely "
        "in their Vultr account: look it up with these tools rather than asking what it is. "
        "This server is read-only: it cannot change anything."
    )


def create_server(
    spec: dict | None = None,
    *,
    exclude_categories: set[str] | None = None,
    only_categories: set[str] | None = None,
    auth=None,
    interface_dir: Path | None = None,
    use_interface: bool | None = None,
) -> FastMCP:
    """Build a FastMCP server over the Vultr tool surface.

    exclude_categories:
        Tags to drop; defaults to the env value, else DEFAULT_EXCLUDED_CATEGORIES. An empty set keeps all.
    only_categories:
        Serve only these tags; the exclusions still apply on top.
    interface_dir:
        Reviewed tool definitions, each replacing the generated tool for its operation.
    use_interface:
        False serves the generated surface alone.
    """
    if spec is None:
        spec = load_spec()

    if exclude_categories is None:
        exclude_categories = excluded_categories_from_env()
        if exclude_categories is None:
            exclude_categories = set(DEFAULT_EXCLUDED_CATEGORIES)

    exclude_tags = set(exclude_categories)
    if only_categories is not None:
        exclude_tags |= all_categories(spec) - set(only_categories)

    ssl_verify = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

    # Instrumented at the transport, which the generated and hand-authored tools share; httpx ignores client `verify` here.
    transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(verify=ssl_verify)
    if audit_enabled():
        transport = InstrumentedTransport(transport, PathTemplates(spec.get("paths", {})))
    # Outermost, so a held session's calls never reach Vultr or the upstream audit record.
    transport = SuspendedTransport(transport)

    client = httpx.AsyncClient(
        base_url=VULTR_API_BASE,
        auth=PerRequestVultrAuth(),
        transport=transport,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )

    if use_interface is False:
        interface = CompiledInterface(version="none")
    else:
        if interface_dir is None:
            interface_dir = interface_dir_from_env()
        interface = load_interface(spec, interface_dir)

    # Hand-authored tools pass the same gates as generated ones.
    interface_tools = [
        tool
        for tool in interface.tools
        if not tool.is_write and not (tool.tags & exclude_tags)
    ]

    # Generated routes a hand-authored tool replaces, or that are excluded outright.
    suppressed = [
        (tool.method.upper(), tool.path_template) for tool in interface_tools
    ] + [
        (excluded.method, excluded.path_template) for excluded in interface.excluded
    ]

    server = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Vultr MCP Server",
        # Otherwise `initialize` reports FastMCP's own version.
        version=package_version(),
        instructions=server_instructions(only_categories),
        route_maps=_build_route_maps(exclude_tags, suppressed),
        mcp_component_fn=None if _output_schemas_enabled() else _strip_output_schema,
        auth=auth,
    )

    for tool in interface_tools:
        server.add_tool(InterfaceTool.build(tool, client))

    # After a release, tells each connected client once to refetch its tool list.
    server.add_middleware(ToolListRefreshMiddleware())

    # Attached last so it wraps every tool, generated and hand-authored alike.
    if audit_enabled():
        server.add_middleware(AuditMiddleware())

    return server


def main() -> None:
    transport = os.environ.get("VULTR_MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
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
