"""Validate and compile the interface layer from the command line.

    python -m vultr_mcp.interface            # validate the shipped layer
    python -m vultr_mcp.interface --list     # ...and print what it compiles to

Exits non-zero on any problem, so CI fails on drift the moment openapi.json
moves under a definition rather than at the tool call that needed it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vultr_mcp.interface.compiler import compile_interface
from vultr_mcp.interface.validator import validate_manifest


def _scaffold(args) -> int:
    """Print a draft product area file, with the review notes on stderr.

    The draft goes to stdout so it can be redirected into interface/, and
    everything the reviewer needs to know goes to stderr so redirecting does
    not swallow it.
    """
    import json

    from vultr_mcp.interface.scaffold import scaffold_area
    from vultr_mcp.interface.spec_index import SpecIndex

    index = SpecIndex.load(json.loads(args.spec.read_text(encoding="utf-8")))
    tags = {tag for operation in index.operations.values() for tag in operation.tags}
    if args.scaffold not in tags:
        print(f"no such tag: {args.scaffold!r}", file=sys.stderr)
        print(f"tags: {', '.join(sorted(tags))}", file=sys.stderr)
        return 1

    drafted = scaffold_area(args.scaffold, args.family, index)
    print(drafted.text, end="")

    print(
        f"\ndrafted {drafted.tool_count} read operation(s) as disabled tools; "
        f"skipped {drafted.skipped_writes} write operation(s)",
        file=sys.stderr,
    )
    if drafted.withheld_fields:
        print(
            "held back as possible credentials: "
            + ", ".join(drafted.withheld_fields),
            file=sys.stderr,
        )
    print(
        "every tool is enabled: false and every description is a stub; review "
        "before enabling",
        file=sys.stderr,
    )
    return 0


def _drift(args) -> int:
    """Report unreviewed operations in covered areas.

    A new endpoint appearing upstream is news, not a failure -- breaking the
    build over someone else's release is how a check like this gets switched
    off. Stale references are different: those mean the layer is pointing at
    something that no longer exists. CI that wants the strict gate can ask for
    it with --fail-on-unreviewed.
    """
    import json

    from vultr_mcp.interface.drift import detect_drift, format_report

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    report = detect_drift(args.interface_dir, spec)
    print(format_report(report, show_writes=args.writes))

    if report.stale:
        return 1
    if args.fail_on_unreviewed and not report.is_clean:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    parser = argparse.ArgumentParser(prog="python -m vultr_mcp.interface")
    parser.add_argument(
        "--interface-dir", type=Path, default=repo_root / "interface",
        help="directory holding interface.yaml (default: ./interface)",
    )
    parser.add_argument(
        "--spec", type=Path, default=repo_root / "openapi.json",
        help="OpenAPI document to validate against (default: ./openapi.json)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the compiled tools"
    )
    parser.add_argument(
        "--scaffold", metavar="TAG",
        help="draft a product area file for an OpenAPI tag and print it to stdout",
    )
    parser.add_argument(
        "--family", default="compute",
        help="product family for scaffolded tool names (default: compute)",
    )
    parser.add_argument(
        "--drift", action="store_true",
        help="report operations in covered product areas that nobody has reviewed",
    )
    parser.add_argument(
        "--writes", action="store_true",
        help="with --drift, list unreviewed write operations too",
    )
    parser.add_argument(
        "--fail-on-unreviewed", action="store_true",
        help="with --drift, exit non-zero when anything is unreviewed (for CI)",
    )
    args = parser.parse_args(argv)

    if args.scaffold:
        return _scaffold(args)
    if args.drift:
        return _drift(args)

    problems = validate_manifest(args.interface_dir, args.spec)
    fatal = [problem for problem in problems if problem.is_error]
    warnings = [problem for problem in problems if not problem.is_error]

    for problem in fatal + warnings:
        print(problem, file=sys.stderr)
    if fatal:
        print(
            f"\n{len(fatal)} error(s), {len(warnings)} warning(s)", file=sys.stderr
        )
        return 1
    if warnings:
        # Disabled tools only. Worth seeing on every run, since a draft nobody
        # looks at is how drift gets in, but never a reason to fail a build.
        print(f"\n{len(warnings)} warning(s) on disabled tools", file=sys.stderr)

    import json

    interface = compile_interface(
        args.interface_dir, json.loads(args.spec.read_text(encoding="utf-8")),
        validate=False,
    )
    print(f"interface {interface.version}: {len(interface.tools)} tool(s), valid")

    if args.list:
        for tool in interface.tools:
            passed = ", ".join(
                f"{plan.agent_name}->{plan.api_name}" for plan in tool.parameters
            )
            filtered = ", ".join(plan.agent_name for plan in tool.filters)
            print(f"\n  {tool.name}  [{tool.access}]  {tool.method.upper()} {tool.path_template}")
            print(f"    operation: {tool.operation_id}")
            print(f"    to api:    {passed or '-'}")
            print(f"    filtered:  {filtered or '-'}")
            if tool.computed:
                print(
                    "    computed:  "
                    + ", ".join(
                        f"{plan.name}={plan.expression.source}" for plan in tool.computed
                    )
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
