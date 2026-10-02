"""The server tells the model what its tools are for.

Clients that defer tool loading show the model only tool names until it
searches. Without instructions, an agent asked to lock down SSH on a firewall
group never searched ~190 Vultr tools and went looking for a cloud CLI instead.
The instructions in `initialize` are what the model sees before any tool loads.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from vultr_mcp.server import create_server, load_spec


@pytest.fixture(scope="module")
def spec():
    return load_spec()


async def _instructions(server) -> str:
    async with Client(server) as client:
        return client.initialize_result.instructions or ""


async def test_instructions_say_the_tools_manage_the_vultr_account(spec):
    instructions = await _instructions(create_server(spec, read_only=True))
    assert "Vultr cloud account" in instructions
    # Named so a request about one of them leads the model to search here.
    for product in ("instances", "firewall groups", "DNS", "Kubernetes"):
        assert product in instructions


@pytest.mark.parametrize(
    ("read_only", "expected"),
    [(True, "read-only"), (False, "change the account immediately")],
)
async def test_instructions_state_the_write_posture(spec, read_only, expected):
    instructions = await _instructions(create_server(spec, read_only=read_only))
    assert expected in instructions


async def test_a_category_endpoint_names_only_what_it_serves(spec):
    instructions = await _instructions(
        create_server(spec, read_only=True, only_categories={"dns", "firewall"})
    )
    assert "covering: dns, firewall" in instructions
    assert "Kubernetes" not in instructions
