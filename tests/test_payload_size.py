"""The root endpoint's tools/list must stay small enough for real clients.

The server has always *answered* the root correctly (HTTP 200, complete body),
but at 750KB / ~187k tokens of tool definitions clients refuse the listing —
which is why connecting to https://vultrmcp.com/ failed while /instances worked.
Nearly all of that was generated `outputSchema`, which agents don't need.

These tests pin the fix so the root endpoint can't silently regrow.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from vultr_mcp.server import create_server, load_spec

# Headroom over the ~265KB current root listing, well under the ~750KB that broke
# clients. If this trips, the tool surface grew — curate it, don't raise the cap.
MAX_ROOT_LISTING_BYTES = 400_000


@pytest.fixture(scope="module")
def spec():
    return load_spec()


async def _wire_listing(server) -> tuple[list, int]:
    """The tools/list payload as it actually goes over the wire."""
    async with Client(server) as client:
        tools = await client.list_tools()
    wire = json.dumps(
        [t.model_dump(exclude_none=True) for t in tools], separators=(",", ":")
    )
    return tools, len(wire)


async def test_root_listing_fits_client_budgets(spec):
    tools, size = await _wire_listing(create_server(spec))
    assert size < MAX_ROOT_LISTING_BYTES, (
        f"root tools/list is {size:,} bytes across {len(tools)} tools — "
        "large enough that MCP clients reject the listing"
    )


async def test_output_schemas_are_stripped_by_default(spec):
    """The single biggest contributor to the listing size stays off."""
    tools, _ = await _wire_listing(create_server(spec))
    with_schema = [t.name for t in tools if t.outputSchema]
    assert not with_schema, f"{len(with_schema)} tools still advertise outputSchema"


async def test_output_schemas_can_be_restored(spec, monkeypatch):
    """Opt back in via env, for clients that genuinely want structured output."""
    monkeypatch.setenv("VULTR_MCP_OUTPUT_SCHEMAS", "true")
    tools, size = await _wire_listing(create_server(spec))
    assert any(t.outputSchema for t in tools)
    assert size > MAX_ROOT_LISTING_BYTES, "restoring schemas should grow the listing"


async def test_tool_count_is_unchanged_by_the_size_fix(spec, monkeypatch):
    """Stripping outputSchema must not drop tools — only shrink their definitions."""
    slim, slim_size = await _wire_listing(create_server(spec))
    monkeypatch.setenv("VULTR_MCP_OUTPUT_SCHEMAS", "true")
    full, full_size = await _wire_listing(create_server(spec))
    assert {t.name for t in slim} == {t.name for t in full}
    assert slim_size < full_size / 2, "expected the listing to at least halve"
