"""Validate interface layer definitions against the schema and openapi.json.

Two passes. The schema pass catches shape errors (missing fields, bad names,
unknown keys). The semantic pass catches drift: an operation that no longer
exists, a filter on a field the API stopped returning, an expression naming
something that isn't there.

The semantic pass is the one that matters. Product area files are drafted by an
LLM from openapi.json, so the failure mode is not a typo, it's a confidently
invented field. Every reference is therefore resolved against the spec and the
build fails on anything that cannot be found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from vultr_mcp.interface import expressions
from vultr_mcp.interface.spec_index import Operation, SpecIndex

# Parameter locations the runtime knows how to fill. A header or cookie
# parameter would need credentials or transport plumbing the layer doesn't own.
SUPPORTED_PARAMETER_LOCATIONS = frozenset({"query", "path"})


@dataclass
class Problem:
    """One validation failure, located precisely enough to fix without hunting.

    Severity is about whether the tool is *served*, not about how bad the
    problem is. A disabled tool is not registered, so its problems cannot
    reach an agent -- failing the build over one would mean a scaffolded draft
    could stop the server from starting. They are still reported, because a
    draft nobody can see is exactly the kind of thing that rots.
    """

    where: str
    message: str
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_warning(self) -> "Problem":
        return Problem(self.where, self.message, "warning")

    def __str__(self) -> str:
        prefix = "" if self.is_error else "warning: "
        return f"{prefix}{self.where}: {self.message}"


def errors(problems: list[Problem]) -> list[Problem]:
    """Just the problems that must stop a build."""
    return [problem for problem in problems if problem.is_error]


def _check_access(tool: dict[str, Any], operation: Operation, where: str) -> list[Problem]:
    """`access` must agree with the HTTP method, so read-only stays enforceable."""
    declared = tool.get("access")
    if operation.is_write and declared != "write":
        return [
            Problem(
                where,
                f"access is '{declared}' but {operation.operation_id} is "
                f"{operation.method.upper()} {operation.path}, which changes state",
            )
        ]
    if not operation.is_write and declared != "read":
        return [
            Problem(
                where,
                f"access is '{declared}' but {operation.operation_id} is "
                f"{operation.method.upper()}, which does not change state",
            )
        ]
    return []


def _check_inputs(tool: dict[str, Any], operation: Operation, where: str) -> list[Problem]:
    """Every input either reaches the API or is applied here as a filter.

    An input that is neither is silently ignored at runtime, which is exactly
    the kind of thing a drafting model produces.
    """
    problems: list[Problem] = []
    schema = tool.get("input", {})
    properties = schema.get("properties", {})
    item_fields = operation.response.item_fields

    for name in schema.get("required", []):
        if name not in properties:
            problems.append(
                Problem(f"{where}.input", f"'{name}' is required but not declared")
            )

    for name, prop in properties.items():
        location = f"{where}.input.{name}"
        filter_spec = prop.get("filter")

        if filter_spec:
            if filter_spec["field"].split(".")[0] not in item_fields:
                problems.append(
                    Problem(
                        location,
                        f"filters on '{filter_spec['field']}' which "
                        f"{operation.operation_id} does not return",
                    )
                )
            if filter_spec["match"] == "one_of" and prop.get("type") != "array":
                problems.append(
                    Problem(location, "match 'one_of' needs an input of type array")
                )
            continue

        target = prop.get("maps_to", name)
        parameter = operation.parameters.get(target)
        if parameter is None:
            accepted = ", ".join(sorted(operation.parameters))
            hint = (
                f"add a filter block, or maps_to one of: {accepted}"
                if accepted
                else "the operation declares no parameters, so this needs a filter block"
            )
            problems.append(
                Problem(
                    location,
                    f"'{target}' is not a parameter of {operation.operation_id}; {hint}",
                )
            )
        elif parameter.location not in SUPPORTED_PARAMETER_LOCATIONS:
            problems.append(
                Problem(
                    location,
                    f"'{target}' is a {parameter.location} parameter, which the "
                    "interface runtime does not fill",
                )
            )

    # A required API parameter with no input behind it can never be supplied.
    declared_targets = {
        prop.get("maps_to", name)
        for name, prop in properties.items()
        if "filter" not in prop
    }
    for parameter in operation.parameters.values():
        if parameter.required and parameter.name not in declared_targets:
            problems.append(
                Problem(
                    f"{where}.input",
                    f"{operation.operation_id} requires the {parameter.location} "
                    f"parameter '{parameter.name}', which no input supplies",
                )
            )

    return problems


def _check_output(tool: dict[str, Any], operation: Operation, where: str) -> list[Problem]:
    """Response shaping must name fields that exist, or the agent loses them."""
    output = tool.get("output") or {}
    if not output.get("from_openapi"):
        return []

    problems: list[Problem] = []
    shape = operation.response

    if shape.container_key is None and output.get("include"):
        return [
            Problem(
                f"{where}.output",
                f"{operation.operation_id} has no readable 200 response schema, "
                "so include cannot be checked; drop include or fix the spec",
            )
        ]

    if shape.extra_containers:
        problems.append(
            Problem(
                f"{where}.output",
                f"{operation.operation_id} returns more than one collection "
                f"({shape.container_key}, {', '.join(shape.extra_containers)}); "
                f"shaping would only apply to '{shape.container_key}'",
            )
        )

    for name in output.get("include", []):
        if name not in shape.known_fields:
            problems.append(
                Problem(
                    f"{where}.output.include",
                    f"'{name}' is not in the 200 response of {operation.operation_id}",
                )
            )
    return problems


def _check_computed(tool: dict[str, Any], operation: Operation, where: str) -> list[Problem]:
    """Computed fields must not collide with real ones or read absent ones."""
    problems: list[Problem] = []
    shape = operation.response

    for name, definition in (tool.get("computed") or {}).items():
        location = f"{where}.computed.{name}"
        if name in shape.known_fields:
            problems.append(
                Problem(location, f"'{name}' already exists in the response; drop it")
            )

        expression, error = expressions.parse(definition["from"])
        if error or expression is None:
            problems.append(Problem(location, error or "could not be parsed"))
            continue
        for root in expression.roots:
            if root not in shape.item_fields:
                problems.append(
                    Problem(
                        location,
                        f"reads '{root}' which {operation.operation_id} does not return",
                    )
                )
    return problems


def validate_tool(tool: dict[str, Any], index: SpecIndex, where: str) -> list[Problem]:
    """Semantic checks for one tool. Assumes the schema pass already ran."""
    operation_id = tool.get("operation", "")
    operation = index.get(operation_id)
    if operation is None:
        return [Problem(where, f"operation '{operation_id}' is not in openapi.json")]

    problems = _check_access(tool, operation, where)

    if operation.has_request_body:
        problems.append(
            Problem(
                where,
                f"{operation_id} takes a request body, which the interface runtime "
                "does not build yet",
            )
        )

    problems += _check_inputs(tool, operation, where)
    problems += _check_output(tool, operation, where)
    problems += _check_computed(tool, operation, where)
    return problems


def validate_product_area(
    path: Path, schema: dict[str, Any], index: SpecIndex
) -> list[Problem]:
    """Validate one product area file, schema pass then semantic pass."""
    where = path.name
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [Problem(where, f"is not valid YAML: {exc}")]

    problems = [
        Problem(
            f"{where}:{'.'.join(str(part) for part in error.absolute_path) or 'root'}",
            error.message,
        )
        for error in sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: list(error.absolute_path),
        )
    ]
    # A shape error makes the semantic pass meaningless -- report and stop.
    if problems:
        return problems

    family = document["family"]
    product_area = document["product_area"]
    declined = document.get("declined") or {}
    tool_operations = {tool["operation"] for tool in document["tools"]}

    # Declined operations are reviewed decisions, not served tools, so a stale
    # one cannot mislead an agent -- it can only mislead the next person, and
    # skew a drift report. Warnings, except where it contradicts a live tool.
    for operation_id in declined:
        location = f"{where}.declined.{operation_id}"
        operation = index.get(operation_id)
        if operation is None:
            problems.append(
                Problem(
                    location,
                    f"'{operation_id}' is not in openapi.json; the decision is stale",
                    "warning",
                )
            )
            continue
        if operation_id in tool_operations:
            problems.append(
                Problem(location, "is declined but also has a tool in this file")
            )
        if product_area not in operation.tags:
            problems.append(
                Problem(
                    location,
                    f"is tagged {', '.join(operation.tags) or '(untagged)'} in "
                    f"openapi.json, not '{product_area}'; declining it here leaves "
                    "it unreviewed where it actually belongs",
                    "warning",
                )
            )

    seen: set[str] = set()
    for position, tool in enumerate(document["tools"]):
        name = tool["name"]
        location = f"{where}.tools[{position}] ({name})"
        # Everything found on a disabled tool is reported as a warning: it is
        # never registered, so it cannot mislead an agent, and a draft must not
        # be able to stop the server from starting.
        served = tool.get("enabled", True)
        found: list[Problem] = []

        if name in seen:
            found.append(Problem(location, "duplicate tool name in this file"))
        seen.add(name)

        # The family prefix is the agent's first signal, and the reason
        # vultr_compute_clusters_search cannot be mistaken for a VKE tool. A
        # name that drops it looks fine in isolation and quietly costs that
        # distinction, so the file's declared family is enforced rather than
        # trusted.
        prefix = f"vultr_{family}_"
        if not name.startswith(prefix):
            found.append(
                Problem(location, f"name must start with '{prefix}' (family: {family})")
            )

        found.extend(validate_tool(tool, index, location))
        problems.extend(
            found if served else [problem.as_warning() for problem in found]
        )

    return problems


def load_manifest(interface_dir: Path) -> dict[str, Any]:
    """The interface.yaml manifest: version, schema path, product areas."""
    return yaml.safe_load((interface_dir / "interface.yaml").read_text(encoding="utf-8"))


def load_schema(interface_dir: Path) -> dict[str, Any]:
    """The format contract named by interface.yaml."""
    manifest = load_manifest(interface_dir)
    return json.loads((interface_dir / manifest["schema"]).read_text(encoding="utf-8"))


def validate_manifest(
    interface_dir: Path, spec: Path | str | dict[str, Any]
) -> list[Problem]:
    """Validate every product area named by interface.yaml.

    ``spec`` is either an already-loaded openapi document or a path to one.
    Returns an empty list when the whole interface layer is valid.
    """
    manifest = load_manifest(interface_dir)
    schema = json.loads((interface_dir / manifest["schema"]).read_text(encoding="utf-8"))

    if not isinstance(spec, dict):
        spec = json.loads(Path(spec).read_text(encoding="utf-8"))
    index = SpecIndex.load(spec)

    problems: list[Problem] = []
    names_across_files: dict[str, str] = {}

    for area, filename in (manifest.get("product_areas") or {}).items():
        path = interface_dir / filename
        if not path.exists():
            problems.append(
                Problem(
                    "interface.yaml",
                    f"product area '{area}' points at missing {filename}",
                )
            )
            continue

        problems.extend(validate_product_area(path, schema, index))

        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        declared_area = document.get("product_area")
        if declared_area and declared_area != area:
            problems.append(
                Problem(
                    filename,
                    f"declares product_area '{declared_area}' but interface.yaml "
                    f"lists it under '{area}'",
                )
            )

        # Tool names are the agent's whole vocabulary, so a collision across
        # product areas is as bad as one within a file.
        for tool in document.get("tools", []) or []:
            name = tool.get("name")
            if not name:
                continue
            if name in names_across_files:
                problems.append(
                    Problem(
                        filename,
                        f"tool name '{name}' already defined in {names_across_files[name]}",
                    )
                )
            names_across_files[name] = filename

    return problems
