"""Vultr MCP server — FastMCP edition.

Generates the full tool surface from Vultr's OpenAPI spec (`openapi.json`)
via ``FastMCP.from_openapi`` and forwards each caller's own credential to
api.vultr.com per request.

Credential resolution, per tool call:
  1. HTTP mode: the ``Authorization`` header of the incoming MCP request is
     forwarded upstream verbatim (works for both raw Vultr API keys and, in
     later phases, OAuth access tokens — api.vultr.com accepts both as
     ``Bearer`` credentials).
  2. STDIO / local mode (no HTTP context): falls back to the ``VULTR_API_KEY``
     environment variable, mirroring the PHP server's behaviour.

The surface is **read-only by default**: state-changing operations are dropped
unless ``VULTR_MCP_WRITES_ENABLED`` is set. See ``WRITE_METHODS``.
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

# Identity/credential-management categories (OpenAPI tags) excluded from the
# hosted tool surface by default — same posture as the PHP server and the
# GitHub/Stripe/DigitalOcean MCPs. Enforcement of these permissions lives in
# the IAM policy attached to the OAuth client app; excluding the tools here is
# UX-layer hygiene (OAuth users never see tools that would always 403) and
# keeps identity mutations out of prompt-injection reach on every auth path.
#
# These are OpenAPI *tags*, matched by RouteMap. Override with
# VULTR_MCP_EXCLUDED_CATEGORIES (comma-separated tags; empty string disables).
DEFAULT_EXCLUDED_CATEGORIES = frozenset(
    {"api-keys", "users", "iam", "scim", "organizations", "oidc"}
)

# Read-only mode (the default) exposes only operations that cannot change
# state. Everything that is not a GET mutates something — including the two
# OPTIONS routes, which mint container-registry Docker credentials despite the
# verb — so the rule is "GET, plus an explicit allowlist".
#
# Write access is opt-in via VULTR_MCP_WRITES_ENABLED. The hosted deployment
# leaves it off; local STDIO users who want the full surface set it to true.
WRITE_METHODS: tuple[str, ...] = (
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "TRACE",
)

# Non-GET operations that only *read* state, kept in read-only mode.
# Vultr models this one as a POST because its filter arrives in a request body,
# but it creates nothing — it lists a managed database's existing alerts.
# (method, anchored path regex) — the path is the OpenAPI template.
READ_ONLY_METHOD_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("POST", r"^/databases/\{database-id\}/alerts$"),
)


class PerRequestVultrAuth(httpx.Auth):
    """httpx auth hook resolving the Vultr credential at call time.

    ``get_http_headers()`` reads FastMCP's request context (a contextvar), so
    inside an HTTP-transport tool call it returns the incoming MCP request's
    headers; outside any HTTP context (STDIO, in-process tests) it returns an
    empty dict and we fall back to the environment key.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token: str | None = None

        # 1. Authenticated AccessToken (OAuth path). With OAuthProxy, the client
        #    holds a FastMCP-issued token; FastMCP swaps it for the stored
        #    UPSTREAM Vultr token and validates that via our verifier, exposing
        #    it here as AccessToken.token. That upstream token is what
        #    api.vultr.com accepts — NOT the FastMCP token in the raw header
        #    (forwarding that gave "Invalid API token"). Also covers the
        #    header-auth path when an auth layer is active.
        try:
            from fastmcp.server.dependencies import get_access_token

            access = get_access_token()
            if access is not None and getattr(access, "token", None):
                token = f"Bearer {access.token}"
        except Exception:
            pass

        # 2. No auth layer (OIDC disabled): forward the raw incoming credential.
        #    include_all=True is REQUIRED — get_http_headers() strips
        #    `authorization` (and x-*, host, content-*) by default.
        if not token:
            try:
                incoming = get_http_headers(include_all=True)
                auth_header = incoming.get("authorization", "")
                if auth_header:
                    token = auth_header
                else:
                    # A raw Vultr API key may arrive as X-Vultr-API-Key.
                    api_key = incoming.get("x-vultr-api-key", "")
                    if api_key:
                        token = f"Bearer {api_key}"
            except Exception:
                pass

        # 3. STDIO / local fallback.
        if not token:
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


def sanitize_spec(spec: dict) -> dict:
    """Fix spec-validity defects in Vultr's published openapi.json.

    FastMCP's parser enforces the OpenAPI schema strictly (the old PHP
    generator was lenient, which is how these shipped unnoticed). Three
    defect classes exist in the current spec — all reported upstream:

    1. Response objects missing the REQUIRED ``description`` field
       (e.g. GET /storage-gateways 200).
    2. ``"type": "enum"`` — not a valid JSON-Schema type; the author meant
       ``"type": "string"`` (the enum values live in the description).
    3. ``components.parameters.vcr_region`` missing ``name``/``in`` — it is
       the ``{region}`` path parameter of
       /registry/{registry-id}/replication/{region}.
    """
    # (1) responses missing `description`
    for path_item in spec.get("paths", {}).values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for response in op.get("responses", {}).values():
                if isinstance(response, dict) and "$ref" not in response:
                    response.setdefault("description", "")

    # (2) "type": "enum" → "type": "string" (recursive)
    def fix_enum_type(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "enum":
                node["type"] = "string"
            for value in node.values():
                fix_enum_type(value)
        elif isinstance(node, list):
            for item in node:
                fix_enum_type(item)

    fix_enum_type(spec.get("components", {}).get("schemas", {}))

    # (3) parameters missing `in` — vcr_region is the {region} path param
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

    FastMCP derives an ``outputSchema`` from each operation's OpenAPI *response*
    schema. Those are the single largest thing in the tool listing — 64% of the
    root endpoint's bytes (480KB of 750KB across 279 of 409 tools) — because a
    response schema describes every field of every nested object, while an agent
    only needs the *input* schema to make a call.

    That size is why the root endpoint fails in practice: ~187k tokens of tool
    definitions, which clients reject (VS Code caps at 128 tools; others truncate
    or blow their context budget) even though the server answers correctly.
    Dropping it takes the root listing to ~66k tokens with no tools removed and
    no change to tool *results* — responses still come back in full, they are
    just no longer accompanied by a schema describing their shape.

    Note: ``FastMCP.from_openapi(validate_output=False)`` looks like the lever
    for this but is not — it disables output *validation* while still
    advertising the schema on the wire.

    Set VULTR_MCP_OUTPUT_SCHEMAS=true to keep them.
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


def load_interface(spec: dict, interface_dir: Path | None) -> CompiledInterface:
    """Compile the interface layer, or return an empty one when absent.

    A missing directory is not an error — the server predates the layer and
    still works without it. A *broken* one is: compile_interface raises, because
    a layer that half-loads means the agent-facing surface is not the reviewed
    one, and silently serving the generated tools instead would hide that.
    """
    if interface_dir is None or not (interface_dir / "interface.yaml").exists():
        return CompiledInterface(version="none")
    return compile_interface(interface_dir, spec)


def _build_route_maps(
    exclude_tags: set[str],
    read_only: bool,
    interface_routes: list[tuple[str, str]] | None = None,
) -> list[RouteMap]:
    """RouteMaps applied in order — first match wins, default is TOOL.

    1. One EXCLUDE per operation the interface layer owns. A hand-authored tool
       *replaces* the generated one; leaving both would give the agent two tools
       for one operation, which is the ambiguity the layer exists to remove.
       These come first because they are the most specific.
    2. One EXCLUDE per excluded tag. Each operation carries exactly one
       category tag, so a single RouteMap with a multi-tag set would never
       match (RouteMap requires all its tags to be present on the route);
       hence one map per tag. These come before the method maps so an identity
       exclusion can never be undone by a later map.
    3. In read-only mode, the read-only overrides are re-admitted as TOOLs...
    4. ...and every remaining write method is excluded. GETs match no map and
       fall through to the default TOOL.
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

    # The same gates the generated surface passes through apply to hand-authored
    # tools, so the layer can never reintroduce a write or an identity tool that
    # policy excluded. Tools filtered out here keep their generated counterpart.
    interface_tools = [
        tool
        for tool in interface.tools
        if not (read_only and tool.is_write) and not (tool.tags & exclude_tags)
    ]

    server = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Vultr MCP Server",
        route_maps=_build_route_maps(
            exclude_tags,
            read_only,
            [(tool.method.upper(), tool.path_template) for tool in interface_tools],
        ),
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
