"""Report where the spec has moved out from under the interface layer.

The validator already fails the build when something we reference disappears.
This is the other direction: operations that exist and nobody has looked at.

Scope is the point. A report over the whole spec would say "519 operations
uncovered", which is noise, so this only looks at product areas the layer
already claims. Within one of those, every operation is in exactly one of four
states, and only the last is worth anyone's attention:

    served       a tool the agent can call
    drafted      a tool in the file, still disabled
    declined     reviewed and deliberately left to the generated surface
    unreviewed   nobody has looked

Without the declined state this report would be unreadable -- instances alone
would list thirteen intentional omissions beside anything genuinely new, and a
report that cries wolf twice gets ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vultr_mcp.interface.scaffold import slug
from vultr_mcp.interface.spec_index import Operation, SpecIndex
from vultr_mcp.interface.validator import load_manifest


@dataclass(frozen=True)
class OperationRef:
    """An operation named in a report line."""

    operation_id: str
    method: str
    path: str

    @classmethod
    def of(cls, operation: Operation) -> "OperationRef":
        return cls(operation.operation_id, operation.method.upper(), operation.path)


@dataclass(frozen=True)
class AreaDrift:
    """One product area, measured against the operations carrying its tag."""

    product_area: str
    tag: str | None
    served: int
    drafted: int
    declined: int
    unreviewed_reads: tuple[OperationRef, ...] = ()
    unreviewed_writes: tuple[OperationRef, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.unreviewed_reads or self.unreviewed_writes or self.stale)


@dataclass(frozen=True)
class DriftReport:
    version: str
    areas: tuple[AreaDrift, ...] = ()

    @property
    def unreviewed_reads(self) -> int:
        return sum(len(area.unreviewed_reads) for area in self.areas)

    @property
    def unreviewed_writes(self) -> int:
        return sum(len(area.unreviewed_writes) for area in self.areas)

    @property
    def stale(self) -> int:
        return sum(len(area.stale) for area in self.areas)

    @property
    def is_clean(self) -> bool:
        return all(area.is_clean for area in self.areas)


def _tag_for_area(area: str, index: SpecIndex) -> str | None:
    """The OpenAPI tag a product area covers.

    Areas are named after tags, but tags are not always identifiers --
    'Container Registry' slugs to container_registry -- so the match is made on
    the slug rather than assumed.
    """
    for tag in {tag for operation in index.operations.values() for tag in operation.tags}:
        if slug(tag) == area:
            return tag
    return None


def _area_drift(
    area: str, document: dict[str, Any], index: SpecIndex
) -> AreaDrift:
    tools = document.get("tools") or []
    declined = document.get("declined") or {}

    served = {tool["operation"] for tool in tools if tool.get("enabled", True)}
    drafted = {tool["operation"] for tool in tools if not tool.get("enabled", True)}
    accounted = served | drafted | set(declined)

    stale = tuple(sorted(op for op in accounted if index.get(op) is None))

    tag = _tag_for_area(area, index)
    if tag is None:
        return AreaDrift(
            product_area=area,
            tag=None,
            served=len(served),
            drafted=len(drafted),
            declined=len(declined),
            stale=stale,
        )

    unreviewed = [
        operation
        for operation in index.operations.values()
        if tag in operation.tags and operation.operation_id not in accounted
    ]
    by_path = sorted(unreviewed, key=lambda operation: (operation.path, operation.method))

    return AreaDrift(
        product_area=area,
        tag=tag,
        served=len(served),
        drafted=len(drafted),
        declined=len(declined),
        unreviewed_reads=tuple(
            OperationRef.of(operation) for operation in by_path if not operation.is_write
        ),
        unreviewed_writes=tuple(
            OperationRef.of(operation) for operation in by_path if operation.is_write
        ),
        stale=stale,
    )


def detect_drift(interface_dir: Path, spec: dict[str, Any]) -> DriftReport:
    """Compare every covered product area against the spec."""
    manifest = load_manifest(interface_dir)
    index = SpecIndex.load(spec)

    areas = []
    for area, filename in (manifest.get("product_areas") or {}).items():
        path = interface_dir / filename
        if not path.exists():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        areas.append(_area_drift(area, document, index))

    return DriftReport(version=str(manifest["version"]), areas=tuple(areas))


def format_report(report: DriftReport, *, show_writes: bool = False) -> str:
    """The report as a human reads it, one area at a time."""
    lines = [f"interface {report.version}: drift against openapi.json", ""]

    for area in report.areas:
        header = f"{area.product_area}"
        if area.tag and area.tag != area.product_area:
            header += f"  (tag: {area.tag})"
        lines.append(header)

        if area.tag is None:
            lines.append("  no operation in the spec carries a matching tag")
            lines.append("")
            continue

        lines.append(
            f"  reviewed: {area.served} served, {area.drafted} drafted, "
            f"{area.declined} declined"
        )

        if area.stale:
            lines.append(f"  STALE, no longer in the spec: {', '.join(area.stale)}")

        if area.unreviewed_reads:
            lines.append(f"  unreviewed reads: {len(area.unreviewed_reads)}")
            for ref in area.unreviewed_reads:
                lines.append(f"      {ref.operation_id:<34} {ref.method} {ref.path}")
        else:
            lines.append("  unreviewed reads: none")

        if area.unreviewed_writes:
            if show_writes:
                lines.append(f"  unreviewed writes: {len(area.unreviewed_writes)}")
                for ref in area.unreviewed_writes:
                    lines.append(f"      {ref.operation_id:<34} {ref.method} {ref.path}")
            else:
                lines.append(
                    f"  unreviewed writes: {len(area.unreviewed_writes)} "
                    "(not reachable while the surface is read-only; --writes to list)"
                )
        lines.append("")

    if report.is_clean:
        lines.append("no drift: every operation in every covered area is accounted for")
    else:
        lines.append(
            f"totals: {report.unreviewed_reads} unreviewed read(s), "
            f"{report.unreviewed_writes} unreviewed write(s), {report.stale} stale"
        )
        lines.append(
            "scaffold a draft for one with: "
            "python -m vultr_mcp.interface --scaffold <tag>"
        )

    return "\n".join(lines)
