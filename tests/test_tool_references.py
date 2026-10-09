"""Every tool a description tells the agent to use must be one the server registers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from vultr_mcp.interface.compiler import compile_interface
from vultr_mcp.server import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERFACE_DIR = REPO_ROOT / "interface"

# A tool name in prose: the vultr_ prefix, then the family and the rest in snake case.
_TOOL_NAME = re.compile(r"\bvultr_[a-z0-9]+(?:_[a-z0-9]+)+\b")


def _references(tool) -> set[str]:
    # The input schema is serialized so parameter descriptions are searched too.
    text = tool.description + json.dumps(tool.input_schema)
    return set(_TOOL_NAME.findall(text))


def test_descriptions_only_name_registered_tools():
    interface = compile_interface(INTERFACE_DIR, load_spec())
    registered = {tool.name for tool in interface.tools}

    missing = {
        tool.name: sorted(_references(tool) - registered)
        for tool in interface.tools
        if _references(tool) - registered
    }

    assert not missing, f"descriptions name tools that are not registered: {missing}"


def test_the_raw_compute_tools_are_gone_or_curated():
    interface = compile_interface(INTERFACE_DIR, load_spec())
    curated = {tool.operation_id for tool in interface.tools}
    excluded = {operation.operation_id for operation in interface.excluded}
    declined = {operation.operation_id for operation in interface.declined}

    assert "list-instance-ipv6-reverse" in curated
    assert {"list-instance-private-networks", "get-cluster-availability"} <= excluded
    assert not {"list-instance-ipv6-reverse", "list-instance-private-networks", "get-cluster-availability"} & declined
