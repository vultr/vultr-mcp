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
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Generator

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.providers.openapi import MCPType, RouteMap

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


class PerRequestVultrAuth(httpx.Auth):
    """httpx auth hook resolving the Vultr credential at call time.

    ``get_http_headers()`` reads FastMCP's request context (a contextvar), so
    inside an HTTP-transport tool call it returns the incoming MCP request's
    headers; outside any HTTP context (STDIO, in-process tests) it returns an
    empty dict and we fall back to the environment key.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        token: str | None = None

        try:
            # include_all=True is REQUIRED: get_http_headers() strips
            # `authorization` (and x-*, host, content-*) by default, which
            # silently dropped the user's key and 401'd every call. This is
            # the documented setting for proxy transports forwarding auth
            # upstream.
            incoming = get_http_headers(include_all=True)
            auth_header = incoming.get("authorization", "")
            if auth_header:
                token = auth_header
            else:
                # Header-auth parity with the PHP server: a raw Vultr API key
                # may arrive as X-Vultr-API-Key instead of a Bearer token.
                api_key = incoming.get("x-vultr-api-key", "")
                if api_key:
                    token = f"Bearer {api_key}"
        except Exception:
            # No request context at all — fall through to env.
            pass

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


def _build_route_maps(exclude_tags: set[str]) -> list[RouteMap]:
    """One EXCLUDE RouteMap per excluded tag.

    Each operation carries exactly one category tag, so a single RouteMap with
    a multi-tag set would never match (RouteMap requires all its tags to be
    present on the route). Hence one map per tag.
    """
    return [RouteMap(tags={tag}, mcp_type=MCPType.EXCLUDE) for tag in sorted(exclude_tags)]


def create_server(
    spec: dict | None = None,
    *,
    exclude_categories: set[str] | None = None,
    only_categories: set[str] | None = None,
    auth=None,
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
    """
    if spec is None:
        spec = load_spec()

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

    return FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Vultr MCP Server",
        route_maps=_build_route_maps(exclude_tags),
        auth=auth,
    )


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
