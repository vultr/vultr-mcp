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
    args = parser.parse_args(argv)

    problems = validate_manifest(args.interface_dir, args.spec)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s)", file=sys.stderr)
        return 1

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
