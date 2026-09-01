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

# Headroom over the current root listing (~75KB read-only, ~265KB with writes),
# well under the ~750KB that broke clients. If this trips, the tool surface
# grew — curate it, don't raise the cap.
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


@pytest.mark.parametrize("read_only", [True, False])
async def test_root_listing_fits_client_budgets(spec, read_only):
    """Both surfaces must fit — the read-only default and the write surface
    that org-level opt-in will hand out."""
    tools, size = await _wire_listing(create_server(spec, read_only=read_only))
    assert size < MAX_ROOT_LISTING_BYTES, (
        f"root tools/list is {size:,} bytes across {len(tools)} tools — "
        "large enough that MCP clients reject the listing"
    )


async def test_output_schemas_are_stripped_by_default(spec):
    """The single biggest contributor to the listing size stays off."""
    tools, _ = await _wire_listing(create_server(spec))
    with_schema = [t.name for t in tools if t.outputSchema]
    assert not with_schema, f"{len(with_schema)} tools still advertise outputSchema"


def _schema_bytes(tools) -> int:
    """How much of a listing is outputSchema, as it is serialised on the wire."""
    return sum(
        len(json.dumps(tool.outputSchema, separators=(",", ":")))
        for tool in tools
        if tool.outputSchema
    )


async def test_output_schemas_can_be_restored(spec, monkeypatch):
    """Opt back in via env, for clients that genuinely want structured output."""
    _, stripped_size = await _wire_listing(create_server(spec))
    monkeypatch.setenv("VULTR_MCP_OUTPUT_SCHEMAS", "true")
    tools, size = await _wire_listing(create_server(spec))
    assert any(tool.outputSchema for tool in tools)
    assert size > stripped_size, "restoring schemas should grow the listing"


async def test_stripping_removes_output_schemas_and_nothing_else(spec, monkeypatch):
    """Stripping outputSchema must not drop tools — only shrink their definitions.

    This used to assert the listing at least halved, which held while almost
    every tool was generated. Hand-authored tools carry no outputSchema in
    either listing, so each one added moves that ratio without anything
    regressing; the threshold measured how much of the surface the interface
    layer owns, not whether the size fix works.

    What the fix actually claims is narrower and does not drift: the two
    listings hold the same tools, and every byte of the difference between them
    is outputSchema. The absolute ceiling that made this matter is pinned by
    test_root_listing_fits_client_budgets.
    """
    slim, slim_size = await _wire_listing(create_server(spec))
    monkeypatch.setenv("VULTR_MCP_OUTPUT_SCHEMAS", "true")
    full, full_size = await _wire_listing(create_server(spec))

    assert {tool.name for tool in slim} == {tool.name for tool in full}

    schemas = _schema_bytes(full)
    growth = full_size - slim_size
    # Each restored schema also costs its own `,"outputSchema":` key; nothing
    # else may account for the difference.
    overhead = 18 * sum(1 for tool in full if tool.outputSchema)
    assert schemas <= growth <= schemas + overhead, (
        f"listing grew by {growth:,} bytes but outputSchema accounts for "
        f"{schemas:,} — something other than schemas changed"
    )
