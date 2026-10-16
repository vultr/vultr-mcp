"""Draft a product area file from openapi.json: the mechanical half, for a person to review.

Every tool is emitted ``enabled: false`` with a stubbed description, and credential-looking fields
are left out of ``output.include`` and listed in a comment, so including one is deliberate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from vultr_mcp.interface.spec_index import Operation, SpecIndex

# Field-name tokens (underscore-separated, not substrings) that must be opted into rather than out of.
SENSITIVE_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "key",
        "keys",
        "apikey",
        "kvm",
        # Client certificates authenticate like passwords; CA certificates are unwanted PEM blobs.
        "cert",
        "certificate",
        "certs",
    }
)


def is_sensitive(field: str) -> bool:
    """Whether a response field looks like something an agent should not echo; deliberately over-inclusive."""
    return any(token in SENSITIVE_TOKENS for token in field.lower().split("_"))


# Paging parameters get agent-facing names and bounds rather than the spec's.
_PAGE_PARAM = "per_page"
_CURSOR_PARAM = "cursor"

_VERB_PREFIXES = ("list", "get", "create", "update", "delete", "patch", "put")

# The schema's identifier patterns; names that do not fit are reported in a comment rather than emitted.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TOOL_NAME = re.compile(r"^vultr_[a-z0-9]+(_[a-z0-9]+)+$")


def _scalar(value: str) -> str:
    """A string as a YAML scalar that cannot break the document (JSON strings are valid YAML)."""
    return json.dumps(value)


def slug(text: str) -> str:
    """A tag as a product area slug: 'Container Registry' -> container_registry."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", cleaned) or "area"


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def tool_name(family: str, area: str, operation: Operation) -> str:
    """A draft name in the family_resource_action convention: list-instances -> vultr_compute_instances_list."""
    tokens = [token for token in re.split(r"[-_]", operation.operation_id) if token]
    if tokens and tokens[0].lower() in _VERB_PREFIXES:
        tokens = tokens[1:]

    if tokens and _singular(tokens[0].lower()) == _singular(area):
        tokens[0] = area

    action = "list" if operation.response.is_collection else "get"
    stem = "_".join(_snake(token) for token in tokens if token) or area
    return f"vultr_{family}_{stem}_{action}"


@dataclass
class ScaffoldedArea:
    """A drafted product area file plus what the reviewer needs to know."""

    text: str
    tool_count: int
    skipped_writes: int
    withheld_fields: list[str]


def _describe(operation: Operation) -> str:
    """A stub built from the spec's own words, marked as unfinished."""
    summary = (operation.raw.get("summary") or "").strip()
    detail = " ".join((operation.raw.get("description") or "").split()).strip()
    source = " ".join(part for part in (summary, detail) if part) or operation.operation_id
    return source


def _input_lines(operation: Operation) -> tuple[list[str], list[str]]:
    """Input property lines for every parameter, and the parameters skipped for want of a legal name."""
    lines: list[str] = ["    input:", "      type: object"]
    skipped: list[str] = []

    required = [
        parameter
        for parameter in operation.parameters.values()
        if parameter.location == "path" and _IDENTIFIER.match(_snake(parameter.name))
    ]
    if required:
        lines.append("      required:")
        lines += [f"        - {_snake(parameter.name)}" for parameter in required]

    usable = [
        parameter
        for parameter in operation.parameters.values()
        if parameter.location in ("query", "path")
        and _IDENTIFIER.match(
            "page_size" if parameter.name == _PAGE_PARAM else _snake(parameter.name)
        )
    ]
    # An explicit empty mapping, because a bare `properties:` parses as null.
    lines.append("      properties: {}" if not usable else "      properties:")

    for parameter in operation.parameters.values():
        if parameter.location not in ("query", "path"):
            continue

        agent_name = (
            "page_size" if parameter.name == _PAGE_PARAM else _snake(parameter.name)
        )
        if not _IDENTIFIER.match(agent_name):
            skipped.append(parameter.name)
            continue
        description = parameter.description
        if not description:
            description = (
                f"{'Path' if parameter.location == 'path' else 'Query'} parameter "
                f"{parameter.name}."
            )

        lines.append(f"        {agent_name}:")
        if parameter.name == _PAGE_PARAM:
            lines += [
                "          type: integer",
                "          description: >-",
                "            Maximum number of results to return. When a server-side filter",
                "            is added below, this becomes the cap on matches.",
                "          default: 100",
                "          minimum: 1",
                "          maximum: 500",
                f"          maps_to: {_PAGE_PARAM}",
            ]
            continue

        schema_type = parameter.schema.get("type") or "string"
        lines.append(f"          type: {schema_type if schema_type != 'array' else 'string'}")
        lines.append(f"          description: {_scalar(description)}")
        if agent_name != parameter.name:
            lines.append(f"          maps_to: {_scalar(parameter.name)}")

    return lines, skipped


def _output_lines(operation: Operation) -> tuple[list[str], list[str]]:
    """An include list of everything safe, and the fields held back."""
    shape = operation.response
    if not shape.item_fields:
        return [], []

    all_fields = sorted(shape.item_fields)
    # Fields the format cannot name, reported so a reviewer knows the include list misses them.
    unexpressible = [field for field in all_fields if not _IDENTIFIER.match(field)]
    fields = [field for field in all_fields if _IDENTIFIER.match(field)]
    withheld = [
        field
        for field in fields
        if is_sensitive(field)
    ]
    keep = [field for field in fields if field not in withheld]

    lines = ["    output:", "      from_openapi: true"]
    if unexpressible:
        lines.append(
            "      # Returned by the API but not expressible as an include entry:"
        )
        lines += [f"      #   {field}" for field in unexpressible]
    if withheld:
        lines.append(
            "      # Held back as possible credentials -- add back deliberately, "
            "if at all:"
        )
        lines += [f"      #   {field}" for field in withheld]
    if not keep:
        # Nothing nameable to allowlist; say whether that is because every field looked like a credential.
        lines.append(
            "      # Nothing to allowlist: no field here can be named in an include"
            " list."
            if not withheld
            else "      # WARNING: every field matched the credential heuristic, so"
            " this returns them all. Decide explicitly before enabling."
        )
        return lines, withheld

    lines.append("      include:")
    lines += [f"        - {field}" for field in keep]
    if "meta" in shape.envelope:
        lines.append("        - meta")
    return lines, withheld


def scaffold_area(tag: str, family: str, index: SpecIndex) -> ScaffoldedArea:
    """Draft every read operation carrying ``tag`` as a disabled tool."""
    area = slug(tag)
    tagged = [
        operation
        for operation in index.operations.values()
        if tag in operation.tags
    ]
    reads = sorted(
        (operation for operation in tagged if not operation.is_write),
        key=lambda operation: (operation.response.is_collection is False, operation.path),
    )
    writes = len(tagged) - len(reads)

    header = [
        f"# DRAFT scaffolded from openapi.json for tag {tag!r}. Every tool below is",
        "# disabled: the mechanical parts are derived from the spec, the parts that",
        "# make a tool worth having are not.",
        "#",
        "# Before enabling any of these:",
        "#   1. Rewrite the description. Say what the tool is for, and say explicitly",
        "#      what it is NOT for -- name the sibling products it could be confused",
        "#      with. This is the single biggest driver of tool selection accuracy.",
        "#   2. Cut output.include to what an agent needs. Everything kept costs",
        "#      tokens on every call.",
        "#   3. Add filters for what the API cannot filter on itself, and check the",
        "#      name reads well beside the tools already in this family.",
        "#   4. Delete the tools that do not deserve to exist, and record them under",
        "#      declined: with a reason.",
        "",
        f"product_area: {area}",
        f"family: {family}",
        "",
        "tools:",
    ]

    body: list[str] = []
    all_withheld: list[str] = []

    unnameable: list[str] = []
    for operation in reads:
        name = tool_name(family, area, operation)
        if not _TOOL_NAME.match(name):
            unnameable.append(f"{operation.operation_id} -> {name}")
            continue
        body += [
            f"  - name: {name}",
            "    access: read",
            "    enabled: false",
            "",
            "    description: |",
            "      TODO review. Scaffolded from the spec, which says:",
            f"      {_describe(operation)}",
            "",
            "      Say when to use this tool, and when not to.",
            "",
            f"    operation: {operation.operation_id}",
            "",
        ]
        inputs, skipped_params = _input_lines(operation)
        body += inputs
        if skipped_params:
            body.append(
                "        # No usable agent-facing name: " + ", ".join(skipped_params)
            )
        output, withheld = _output_lines(operation)
        all_withheld += withheld
        if output:
            body.append("")
            body += output
        body.append("")

    if unnameable:
        header.insert(
            len(header) - 2,
            "# Skipped, no valid tool name could be derived: " + "; ".join(unnameable),
        )

    return ScaffoldedArea(
        text="\n".join(header + body).rstrip() + "\n",
        tool_count=len(reads) - len(unnameable),
        skipped_writes=writes,
        withheld_fields=sorted(set(all_withheld)),
    )
