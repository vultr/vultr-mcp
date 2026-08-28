"""Turn validated product area files into everything the runtime needs.

Compilation happens once, at build time. Nothing here reads a YAML file or
resolves a $ref while a tool call is in flight: the runtime receives plain
dataclasses that say which parameter goes where, which fields to keep, and how
each computed value is derived.

The other half of a compiler's job is refusal. Anything the runtime could not
faithfully execute -- a header parameter, a request body, an operation the spec
no longer has -- fails here with a located message rather than at 3am in a tool
call. ``compile_interface`` runs the validator first for exactly that reason.
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
    errors,
    load_manifest,
    validate_manifest,
)

# Keys the layer adds to an input property that are instructions to the
# compiler, not part of the JSON Schema the agent sees.
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
    """How to shape the response before the agent pays tokens for it.

    ``container_key`` is the envelope key holding the results (``clusters`` for
    GET /clusters), derived from the response schema rather than configured, so
    it cannot drift out of step with the API.
    """

    container_key: str | None
    is_collection: bool
    envelope_include: frozenset[str] | None
    item_include: frozenset[str] | None

# Vultr pages every collection the same way: `per_page` sets the size, `cursor`
# walks forward, and `meta.links.next` carries the next cursor. The names are
# read off the operation rather than assumed, so an endpoint that pages
# differently simply does not auto-page.
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
        """Whether results are filtered here rather than by the API.

        This is what makes the fetch policy necessary: a filter applied after
        the fact only sees the page it was given.
        """
        return bool(self.filters)


@dataclass(frozen=True)
class CompiledInterface:
    """The whole layer: its version, its tools, and what they replace."""

    version: str
    tools: tuple[CompiledTool, ...] = ()


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

    # The output plan is built whether or not the file declares one: the
    # container key is how the runtime finds the results at all, so a tool that
    # only filters or computes still needs it. Declaring `output` adds trimming
    # on top.
    shape = operation.response
    declared_output = definition.get("output") or {}
    include = declared_output.get("include") if declared_output.get("from_openapi") else None
    envelope_include: frozenset[str] | None = None
    item_include: frozenset[str] | None = None
    if include:
        requested = set(include)
        kept_envelope = requested & shape.envelope
        # The container key itself is always kept -- dropping it would throw
        # away the results the tool exists to return.
        if shape.container_key:
            kept_envelope.add(shape.container_key)
        envelope_include = frozenset(kept_envelope)
        item_include = frozenset(requested & shape.item_fields) or None
    output = OutputPlan(
        container_key=shape.container_key,
        is_collection=shape.is_collection,
        envelope_include=envelope_include,
        item_include=item_include,
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
    """Compile the whole layer.

    Raises InterfaceError when validation fails, because a layer that does not
    compile means the agent-facing surface is not the reviewed one -- shipping
    a partial version of it silently is worse than not starting.
    """
    if validate:
        # Only errors stop the build. Warnings belong to disabled tools, which
        # are never registered -- see Problem.severity.
        fatal = errors(validate_manifest(interface_dir, spec))
        if fatal:
            raise InterfaceError(fatal)

    manifest = load_manifest(interface_dir)
    index = SpecIndex.load(spec)

    tools: list[CompiledTool] = []
    for area, filename in (manifest.get("product_areas") or {}).items():
        document = yaml.safe_load((interface_dir / filename).read_text(encoding="utf-8"))
        for definition in document.get("tools", []) or []:
            if not definition.get("enabled", True):
                continue
            tools.append(compile_tool(definition, area, document["family"], index))

    return CompiledInterface(version=str(manifest["version"]), tools=tuple(tools))
