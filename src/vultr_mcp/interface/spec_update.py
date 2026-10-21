"""Take Vultr's published API spec and say what it changes, for a person reviewing the update.

vultr.com/api is a ReDoc page that embeds the spec in its page state rather than linking a file, so
the spec is read out of that state. The diff is keyed on operationId and component name, and names
the reviewed tools whose operation, or any schema it reaches through $ref, changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vultr_mcp.interface.validator import area_files, load_manifest

DOCS_URL = "https://www.vultr.com/api/"

_METHODS = ("get", "put", "post", "delete", "patch")
_REDOC_STATE = "__redoc_state"


class SpecExtractionError(Exception):
    """The docs page did not hold a spec where one was expected."""


def extract_redoc_spec(html: str) -> dict[str, Any]:
    """The OpenAPI document embedded in a ReDoc page's server-rendered state."""
    marker = html.find(_REDOC_STATE)
    if marker < 0:
        raise SpecExtractionError(f"no {_REDOC_STATE} on the page; the docs page has changed shape")
    start = html.find("{", marker)
    try:
        state, _ = json.JSONDecoder().raw_decode(html, start)
        spec = state["spec"]["data"]
    except (ValueError, KeyError, TypeError) as error:
        raise SpecExtractionError(f"{_REDOC_STATE} is not the expected ReDoc state: {error}") from error
    if not str(spec.get("openapi", "")).startswith("3.") or not spec.get("paths"):
        raise SpecExtractionError("the embedded document is not an OpenAPI 3 spec with paths")
    return spec


def check_plausible(new: dict[str, Any], old: dict[str, Any], *, max_loss: float = 0.1) -> None:
    """Refuse a spec that lost more than max_loss of the operations: more likely a broken page than a real removal."""
    before, after = len(_operations(old)), len(_operations(new))
    if before and after < before * (1 - max_loss):
        raise SpecExtractionError(
            f"the new spec has {after} operations against {before}; refusing to replace it"
        )


def dump_spec(spec: dict[str, Any]) -> str:
    """The spec formatted as openapi.json is kept, so a sync diff shows only real changes."""
    return json.dumps(spec, indent=2, ensure_ascii=False)


@dataclass(frozen=True)
class OperationInfo:
    operation_id: str
    method: str
    path: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SpecDiff:
    added: tuple[OperationInfo, ...] = ()
    removed: tuple[OperationInfo, ...] = ()
    # operationId -> the parts of the operation that changed (parameters, responses, ...).
    changed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # "schemas/cluster" -> property names added (+), removed (-) or changed (~).
    components: dict[str, tuple[str, ...]] = field(default_factory=dict)
    new_tags: tuple[str, ...] = ()
    # Reviewed tool name -> why it is touched.
    tools_touched: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.components)


def _operations(spec: dict[str, Any]) -> dict[str, tuple[OperationInfo, dict[str, Any]]]:
    operations = {}
    for path, item in (spec.get("paths") or {}).items():
        for method in _METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId") or f"{method.upper()} {path}"
            info = OperationInfo(operation_id, method.upper(), path, tuple(operation.get("tags") or ()))
            # Path-level parameters apply to every method, so they count as part of the operation.
            merged = dict(operation, parameters=(item.get("parameters") or []) + (operation.get("parameters") or []))
            operations[operation_id] = (info, merged)
    return operations


def _components(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{section}/{name}": value
        for section, entries in (spec.get("components") or {}).items()
        if isinstance(entries, dict)
        for name, value in entries.items()
    }


def _refs(node: Any) -> set[str]:
    """Every component a node points at directly, as "section/name"."""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/"):
                found.add(ref.removeprefix("#/components/"))
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _reachable(node: Any, components: dict[str, Any]) -> set[str]:
    """Every component a node reaches, following $ref through other components."""
    seen: set[str] = set()
    pending = _refs(node)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending |= _refs(components.get(name)) - seen
    return seen


def _component_change(old: Any, new: Any) -> tuple[str, ...]:
    """Which properties a component gained, lost or changed; the whole thing when it has none."""
    old_props = (old or {}).get("properties") if isinstance(old, dict) else None
    new_props = (new or {}).get("properties") if isinstance(new, dict) else None
    if not isinstance(old_props, dict) or not isinstance(new_props, dict):
        return ("changed",)
    marks = [f"+{name}" for name in new_props.keys() - old_props.keys()]
    marks += [f"-{name}" for name in old_props.keys() - new_props.keys()]
    marks += [f"~{name}" for name in old_props.keys() & new_props.keys() if old_props[name] != new_props[name]]
    if not marks:
        marks = ["changed outside its properties"]
    return tuple(sorted(marks, key=lambda mark: mark.lstrip("+-~")))


def _reviewed_tools(interface_dir: Path) -> dict[str, str]:
    """Tool name -> operationId for every reviewed tool, read from the YAML so a broken spec cannot hide one."""
    manifest = load_manifest(interface_dir)
    tools = {}
    for entry in (manifest.get("product_areas") or {}).values():
        for path in area_files(interface_dir, entry):
            if not path.exists():
                continue
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for tool in document.get("tools") or []:
                tools[tool["name"]] = tool["operation"]
    return tools


def diff_specs(
    old: dict[str, Any], new: dict[str, Any], *, interface_dir: Path | None = None,
    assessed_tags: set[str] | frozenset[str] = frozenset(),
) -> SpecDiff:
    old_ops, new_ops = _operations(old), _operations(new)
    old_components, new_components = _components(old), _components(new)

    added = tuple(sorted((new_ops[op][0] for op in new_ops.keys() - old_ops.keys()), key=lambda i: (i.path, i.method)))
    removed = tuple(sorted((old_ops[op][0] for op in old_ops.keys() - new_ops.keys()), key=lambda i: (i.path, i.method)))

    changed = {}
    for operation_id in sorted(old_ops.keys() & new_ops.keys()):
        before, after = old_ops[operation_id][1], new_ops[operation_id][1]
        parts = tuple(sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key)))
        if parts:
            changed[operation_id] = parts

    components = {
        name: ("added",) if name not in old_components
        else ("removed",) if name not in new_components
        else _component_change(old_components[name], new_components[name])
        for name in sorted(old_components.keys() | new_components.keys())
        if old_components.get(name) != new_components.get(name)
    }

    old_tags = {tag for info, _ in old_ops.values() for tag in info.tags}
    new_tags = tuple(sorted({
        tag for info, _ in new_ops.values() for tag in info.tags
        if tag not in old_tags and tag not in assessed_tags
    }))

    touched = {}
    if interface_dir is not None:
        for name, operation_id in sorted(_reviewed_tools(interface_dir).items()):
            if operation_id in old_ops and operation_id not in new_ops:
                touched[name] = f"{operation_id} was removed"
            elif operation_id in changed:
                touched[name] = f"{operation_id}: {', '.join(changed[operation_id])}"
            elif operation_id in new_ops:
                hit = sorted(_reachable(new_ops[operation_id][1], new_components) & components.keys())
                if hit:
                    touched[name] = f"{operation_id} reaches {', '.join(hit)}"

    return SpecDiff(added, removed, changed, components, new_tags, touched)


def format_summary(diff: SpecDiff, *, source: str, checks: list[tuple[str, bool, str]] = ()) -> str:
    """The pull request body: what moved, what it touches of ours, and how our checks took it."""
    lines = [
        f"openapi.json as published at {source}.",
        "",
        f"**Operations:** {len(diff.added)} added, {len(diff.removed)} removed, {len(diff.changed)} changed. "
        f"**Components:** {len(diff.components)} changed.",
        "",
    ]

    if diff.tools_touched:
        lines += [f"### Reviewed tools to re-check ({len(diff.tools_touched)})", ""]
        lines += [f"- `{name}`: {why}" for name, why in diff.tools_touched.items()]
        lines.append("")

    if diff.new_tags:
        lines += [
            "### New tags",
            "",
            "Exposed by default, so pytest fails until each is added to `REVIEWED_CATEGORIES` or "
            "`DEFAULT_EXCLUDED_CATEGORIES` in `server.py`:",
            "",
        ]
        lines += [f"- `{tag}`" for tag in diff.new_tags]
        lines.append("")

    for title, operations in (("Added operations", diff.added), ("Removed operations", diff.removed)):
        if operations:
            lines += [f"### {title}", "", "| operationId | request | tag |", "|---|---|---|"]
            lines += [f"| `{op.operation_id}` | `{op.method} {op.path}` | {', '.join(op.tags)} |" for op in operations]
            lines.append("")

    if diff.changed:
        lines += ["### Changed operations", ""]
        lines += [f"- `{op}`: {', '.join(parts)}" for op, parts in diff.changed.items()]
        lines.append("")

    if diff.components:
        lines += ["### Changed components", "", "`+` added, `-` removed, `~` changed.", ""]
        lines += [f"- `{name}`: {' '.join(marks)}" for name, marks in diff.components.items()]
        lines.append("")

    if checks:
        lines += ["### Checks against this spec", ""]
        for name, passed, output in checks:
            lines += [
                f"<details><summary>{'✅' if passed else '❌'} {name}</summary>",
                "",
                "```",
                output.strip() or "(no output)",
                "```",
                "",
                "</details>",
                "",
            ]

    return "\n".join(lines)
