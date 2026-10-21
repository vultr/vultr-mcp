"""Replace openapi.json with the spec published on vultr.com/api and summarise what changed.

    uv run python scripts/sync_openapi.py [--summary summary.md]

Writes openapi.json only when the published spec differs from --base, then validates the interface
layer and runs drift against it. Prints the summary, or writes it to --summary for a pull request
body. Under GitHub Actions it sets the step output changed=true|false.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from vultr_mcp.interface.drift import detect_drift, format_report  # noqa: E402
from vultr_mcp.interface.spec_update import (  # noqa: E402
    DOCS_URL,
    SpecExtractionError,
    check_plausible,
    diff_specs,
    dump_spec,
    extract_redoc_spec,
    format_summary,
)
from vultr_mcp.interface.validator import validate_manifest  # noqa: E402
from vultr_mcp.server import DEFAULT_EXCLUDED_CATEGORIES, REVIEWED_CATEGORIES  # noqa: E402


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _checks(interface_dir: Path, spec_path: Path, spec: dict) -> list[tuple[str, bool, str]]:
    problems = validate_manifest(interface_dir, spec_path)
    errors = [problem for problem in problems if problem.is_error]
    validation = "\n".join(str(problem) for problem in problems) or "valid"

    report = detect_drift(interface_dir, spec)
    # Only areas needing a decision; unreviewed writes are unreachable while the surface is read-only.
    report = dataclasses.replace(report, areas=tuple(
        area for area in report.areas if area.unreviewed_reads or area.stale or area.deprecated_in_use
    ))
    return [
        ("Interface validation", not errors, validation),
        ("Interface drift", not report.stale, format_report(report)),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/sync_openapi.py")
    parser.add_argument("--url", default=DOCS_URL, help=f"docs page embedding the spec (default: {DOCS_URL})")
    parser.add_argument("--html", type=Path, help="read the docs page from a saved file instead of fetching it")
    parser.add_argument("--base", type=Path, default=REPO / "openapi.json", help="spec to compare against")
    parser.add_argument("--out", type=Path, default=REPO / "openapi.json", help="where to write a changed spec")
    parser.add_argument("--interface-dir", type=Path, default=REPO / "interface")
    parser.add_argument("--summary", type=Path, help="write the markdown summary here instead of printing it")
    args = parser.parse_args(argv)

    if args.html:
        html = args.html.read_text(encoding="utf-8")
    else:
        response = httpx.get(args.url, follow_redirects=True, timeout=60, headers={"User-Agent": "vultr-mcp spec sync"})
        response.raise_for_status()
        html = response.text

    base = json.loads(args.base.read_text(encoding="utf-8"))
    try:
        spec = extract_redoc_spec(html)
        check_plausible(spec, base)
    except SpecExtractionError as error:
        print(f"refusing to update openapi.json: {error}", file=sys.stderr)
        return 2

    if spec == base:
        print(f"openapi.json already matches {args.url}")
        _set_output("changed", "false")
        return 0

    args.out.write_text(dump_spec(spec), encoding="utf-8", newline="\n")
    diff = diff_specs(
        base, spec, interface_dir=args.interface_dir,
        assessed_tags=set(DEFAULT_EXCLUDED_CATEGORIES) | REVIEWED_CATEGORIES,
    )
    summary = format_summary(diff, source=args.url, checks=_checks(args.interface_dir, args.out, spec))

    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
        print(f"wrote {args.out} and {args.summary}")
    else:
        print(summary)
    _set_output("changed", "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
