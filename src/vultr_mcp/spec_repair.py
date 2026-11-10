"""Repairs for the defects in Vultr's published openapi.json that would stop it parsing or resolving.

Every repair is idempotent, so the server and the interface layer can each apply it to the same dict.
"""

from __future__ import annotations

import re

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

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# The paging parameters a file-relative $ref names, as every other list operation in the spec declares them.
SHARED_QUERY_PARAMETERS = {
    "per-page": {
        "name": "per_page", "in": "query", "schema": {"type": "integer"},
        "description": "Number of items requested per page. Default is 100 and Max is 500.",
    },
    "cursor": {
        "name": "cursor", "in": "query", "schema": {"type": "string"},
        "description": "Cursor for paging. See [Meta and Pagination](#section/Introduction/Meta-and-Pagination).",
    },
}

PATH_PLACEHOLDER = re.compile(r"\{([^}]+)\}")


def sanitize_spec(spec: dict) -> dict:
    """Fix the spec-validity defects that FastMCP's strict parser and the interface layer reject, in place."""
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue

            # (1) response objects missing the required `description`
            for response in op.get("responses", {}).values():
                if isinstance(response, dict) and "$ref" not in response:
                    response.setdefault("description", "")

            # (4) security requirements wrapped in an extra list, or null where a list belongs
            if "security" in op:
                if op["security"] is None:
                    del op["security"]
                else:
                    op["security"] = _flatten_security(op["security"])

        # (5) parameters that $ref a YAML file the bundler never inlined
        for holder in (path_item, *(op for m, op in path_item.items() if m in HTTP_METHODS and isinstance(op, dict))):
            if isinstance(holder.get("parameters"), list):
                holder["parameters"] = [_inline_file_ref(param, path) for param in holder["parameters"]]

    # (2) programming-language type names → JSON Schema types, everywhere but inside example payloads.
    _fix_type_aliases(spec)

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


def _fix_type_aliases(node: object) -> None:
    if isinstance(node, dict):
        declared = node.get("type")
        if isinstance(declared, str) and declared in TYPE_ALIASES:
            node["type"] = TYPE_ALIASES[declared]
        for key, value in node.items():
            if key not in EXAMPLE_KEYS:
                _fix_type_aliases(value)
    elif isinstance(node, list):
        for item in node:
            _fix_type_aliases(item)


def _flatten_security(requirements: list) -> list:
    flat: list = []
    for requirement in requirements:
        flat.extend(_flatten_security(requirement) if isinstance(requirement, list) else [requirement])
    return flat


def _inline_file_ref(param: object, path: str) -> object:
    """A parameter for `$ref: ./parameters.yaml#/<name>`, from the paging table or the path placeholder it names."""
    if not isinstance(param, dict):
        return param
    ref = param.get("$ref")
    if not isinstance(ref, str) or ref.startswith("#") or "#/" not in ref:
        return param

    name = ref.rsplit("#/", 1)[1]
    if name in SHARED_QUERY_PARAMETERS:
        return {**SHARED_QUERY_PARAMETERS[name], "schema": dict(SHARED_QUERY_PARAMETERS[name]["schema"])}

    # The file names the placeholder with a prefix: nat-gateway-firewall-rule-id is the path's {firewall-rule-id}.
    placeholders = PATH_PLACEHOLDER.findall(path)
    matches = [p for p in placeholders if p == name] or [p for p in placeholders if name.endswith(f"-{p}")]
    if len(matches) != 1:
        return param
    return {"name": matches[0], "in": "path", "required": True, "schema": {"type": "string"}}
