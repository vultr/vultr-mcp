"""Turn validated product area files into the plain dataclasses the runtime executes, once at startup.

Anything the runtime could not execute faithfully fails here, via the validator, rather than in a live call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vultr_mcp.interface import expressions
from vultr_mcp.interface.expressions import Expression
from vultr_mcp.interface.spec_index import SpecIndex
from vultr_mcp.interface.validator import (
    Problem,
    area_files,
    errors,
    load_manifest,
    validate_manifest,
)

# Input-property keys meant for the compiler, not the JSON Schema the agent sees.
_LAYER_ONLY_KEYS = frozenset({"maps_to", "filter"})


class InterfaceError(Exception):
    """The interface layer does not compile. Carries every problem found."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        detail = "\n  ".join(str(problem) for problem in problems)
        super().__init__(f"interface layer is invalid:\n  {detail}")


@dataclass(frozen=True)
class ParameterPlan:
    """An input that reaches the API, under the name the API uses for it."""

    agent_name: str
    api_name: str
    location: str  # "query" | "path"
    default: Any = None


@dataclass(frozen=True)
class FilterPlan:
    """An input the API cannot accept, applied to the response instead."""

    agent_name: str
    path: tuple[str, ...]
    match: str  # equals | contains | contains_ci | one_of


@dataclass(frozen=True)
class ComputedPlan:
    """A field added to each result item, derived from what the API returned."""

    name: str
    expression: Expression


@dataclass(frozen=True)
class OutputPlan:
    """How to shape the response; ``container_key`` (``clusters`` for GET /clusters) comes from the response schema."""

    container_key: str | None
    is_collection: bool
    envelope_include: frozenset[str] | None
    item_include: frozenset[str] | None
    # The payload is the resource itself, not a wrapper around it.
    unwrapped: bool = False

# per_page and cursor, read off the operation, so an endpoint that pages differently does not auto-page.
_PAGE_PARAM = "per_page"
_CURSOR_PARAM = "cursor"


@dataclass(frozen=True)
class PaginationPlan:
    """How to walk this operation's pages, and what the agent calls them."""

    api_page_param: str
    api_cursor_param: str
    agent_page_param: str | None
    agent_cursor_param: str | None


@dataclass(frozen=True)
class CompiledTool:
    """One agent-facing tool, ready to register and execute."""

    name: str
    description: str
    access: str
    operation_id: str
    method: str
    path_template: str
    tags: frozenset[str]
    product_area: str
    family: str
    input_schema: dict[str, Any]
    parameters: tuple[ParameterPlan, ...] = ()
    filters: tuple[FilterPlan, ...] = ()
    output: OutputPlan | None = None
    computed: tuple[ComputedPlan, ...] = ()
    pagination: PaginationPlan | None = None

    @property
    def is_write(self) -> bool:
        return self.access == "write"

    @property
    def filters_client_side(self) -> bool:
        """Whether results are filtered here rather than by the API."""
        return bool(self.filters)


@dataclass(frozen=True)
class DeclinedOperation:
    """An operation reviewed and left to its generated tool, so drift does not report it as unreviewed."""

    operation_id: str
    product_area: str
    reason: str


@dataclass(frozen=True)
class ExcludedOperation:
    """An operation that must not be served at all, dropped by an EXCLUDE route on its method and path."""

    operation_id: str
    product_area: str
    reason: str
    method: str
    path_template: str


@dataclass(frozen=True)
class CompiledInterface:
    """The whole layer: its version, its tools, and what they replace."""

    version: str
    tools: tuple[CompiledTool, ...] = ()
    declined: tuple[DeclinedOperation, ...] = ()
    excluded: tuple[ExcludedOperation, ...] = ()


def _input_schema(definition: dict[str, Any]) -> dict[str, Any]:
    """The JSON Schema the agent sees: the declared input minus layer keys."""
    declared = definition.get("input", {})
    properties = {
        name: {key: value for key, value in prop.items() if key not in _LAYER_ONLY_KEYS}
        for name, prop in declared.get("properties", {}).items()
    }
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if declared.get("required"):
        schema["required"] = list(declared["required"])
    return schema


def compile_tool(
    definition: dict[str, Any], product_area: str, family: str, index: SpecIndex
) -> CompiledTool:
    """Compile one validated tool definition. Assumes validation passed."""
    operation = index.get(definition["operation"])
    assert operation is not None  # validated

    parameters: list[ParameterPlan] = []
    filters: list[FilterPlan] = []
    for name, prop in definition.get("input", {}).get("properties", {}).items():
        filter_spec = prop.get("filter")
        if filter_spec:
            filters.append(
                FilterPlan(
                    agent_name=name,
                    path=tuple(filter_spec["field"].split(".")),
                    match=filter_spec["match"],
                )
            )
            continue
        api_name = prop.get("maps_to", name)
        parameters.append(
            ParameterPlan(
                agent_name=name,
                api_name=api_name,
                location=operation.parameters[api_name].location,
                default=prop.get("default"),
            )
        )

    # Always built, since the runtime finds results by the container key; declaring `output` adds trimming.
    shape = operation.response
    declared_output = definition.get("output") or {}
    include = declared_output.get("include") if declared_output.get("from_openapi") else None
    envelope_include: frozenset[str] | None = None
    item_include: frozenset[str] | None = None
    if include:
        requested = set(include)
        kept_envelope = requested & shape.envelope
        # The container key itself is always kept.
        if shape.container_key:
            kept_envelope.add(shape.container_key)
        envelope_include = frozenset(kept_envelope)
        item_include = frozenset(requested & shape.item_fields) or None
    output = OutputPlan(
        container_key=shape.container_key,
        is_collection=shape.is_collection,
        envelope_include=envelope_include,
        item_include=item_include,
        unwrapped=shape.unwrapped,
    )

    computed: list[ComputedPlan] = []
    for name, spec in (definition.get("computed") or {}).items():
        expression, error = expressions.parse(spec["from"])
        assert expression is not None, error  # validated
        computed.append(ComputedPlan(name=name, expression=expression))

    pagination = None
    if (
        _PAGE_PARAM in operation.parameters
        and _CURSOR_PARAM in operation.parameters
        and operation.response.is_collection
    ):
        by_api_name = {plan.api_name: plan.agent_name for plan in parameters}
        pagination = PaginationPlan(
            api_page_param=_PAGE_PARAM,
            api_cursor_param=_CURSOR_PARAM,
            agent_page_param=by_api_name.get(_PAGE_PARAM),
            agent_cursor_param=by_api_name.get(_CURSOR_PARAM),
        )

    return CompiledTool(
        name=definition["name"],
        description=definition["description"].strip(),
        access=definition["access"],
        operation_id=operation.operation_id,
        method=operation.method,
        path_template=operation.path,
        tags=frozenset(operation.tags),
        product_area=product_area,
        family=family,
        input_schema=_input_schema(definition),
        parameters=tuple(parameters),
        filters=tuple(filters),
        output=output,
        computed=tuple(computed),
        pagination=pagination,
    )


def compile_interface(
    interface_dir: Path, spec: dict[str, Any], *, validate: bool = True
) -> CompiledInterface:
    """Compile the whole layer, raising InterfaceError when validation fails rather than serving part of it."""
    if validate:
        # Only errors stop the build; warnings belong to disabled tools.
        fatal = errors(validate_manifest(interface_dir, spec))
        if fatal:
            raise InterfaceError(fatal)

    manifest = load_manifest(interface_dir)
    index = SpecIndex.load(spec)

    tools: list[CompiledTool] = []
    declined: list[DeclinedOperation] = []
    excluded: list[ExcludedOperation] = []
    for area, entry in (manifest.get("product_areas") or {}).items():
        # An area is one file, or several read as though concatenated.
        for path in area_files(interface_dir, entry):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for definition in document.get("tools", []) or []:
                if not definition.get("enabled", True):
                    continue
                tools.append(compile_tool(definition, area, document["family"], index))
            for operation_id, declined_entry in (document.get("declined") or {}).items():
                declined.append(
                    DeclinedOperation(
                        operation_id=operation_id,
                        product_area=area,
                        reason=declined_entry["reason"].strip(),
                    )
                )
            for operation_id, excluded_entry in (document.get("excluded") or {}).items():
                operation = index.resolve(operation_id)
                # Unknown operationId, already reported by the validator.
                if operation is None:
                    continue
                excluded.append(
                    ExcludedOperation(
                        operation_id=operation_id,
                        product_area=area,
                        reason=excluded_entry["reason"].strip(),
                        method=operation.method.upper(),
                        path_template=operation.path,
                    )
                )

    return CompiledInterface(
        version=str(manifest["version"]),
        tools=tuple(tools),
        declined=tuple(declined),
        excluded=tuple(excluded),
    )
