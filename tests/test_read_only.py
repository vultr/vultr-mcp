"""The server ships a read-only tool surface, with no switch to change it.

No deployment, hosted or self-hosted, can hand an agent a tool that provisions,
mutates, or destroys infrastructure. These tests pin that and the one
behavioural exception (a POST that only lists).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from vultr_mcp.server import (
    READ_ONLY_METHOD_OVERRIDES,
    WRITE_METHODS,
    create_server,
    load_spec,
)

# Prefixes FastMCP derives from Vultr's non-GET operationIds. Any of these
# surviving in read-only mode means a write leaked through.
WRITE_TOOL_PREFIXES = (
    "create_",
    "delete_",
    "update_",
    "patch_",
    "put_",
    "attach_",
    "detach_",
    "halt_",
    "reboot_",
    "reinstall_",
    "restore_",
    "start_",
    "destroy_",
)


@pytest.fixture(scope="module")
def spec():
    return load_spec()


async def _tool_names(server) -> list[str]:
    async with Client(server) as client:
        return [t.name for t in await client.list_tools()]


async def test_default_surface_is_read_only(spec):
    names = await _tool_names(create_server(spec))
    leaked = [n for n in names if n.startswith(WRITE_TOOL_PREFIXES)]
    assert not leaked, f"write tools exposed on the default surface: {leaked}"

    # And the read surface is genuinely intact, not just empty.
    assert len(names) > 150, f"read surface collapsed to {len(names)} tools"

    # The interface layer is serving. Naming a generated tool here does not
    # work any more: as the layer covers an area its hand-authored tools
    # replace the generated ones, so every such canary eventually disappears by
    # design -- get_account, then list_regions and list_plans, each in turn.
    assert "vultr_account_get" in names

    # from_openapi still populates the surface, pinned to an operation that
    # cannot stop being generated: list-marketplace-app-variables is declined,
    # and declining deliberately keeps the generated tool.
    assert "list_marketplace_app_variables" in names

    joined = " ".join(names)

    # Instances are still reachable, under whichever name owns them: the
    # interface layer replaces the generated list_instances with a
    # hand-authored tool, so pinning the generated name here would fail the
    # moment a product area is covered rather than when a read tool goes
    # missing.
    assert "list_instances" in joined or "vultr_compute_instances_list" in joined


@pytest.mark.parametrize("value", ["true", "1", "yes"])
async def test_the_removed_write_switch_does_nothing(spec, monkeypatch, value):
    """A self-hosted deployment still setting VULTR_MCP_WRITES_ENABLED gets no write tools."""
    monkeypatch.setenv("VULTR_MCP_WRITES_ENABLED", value)
    names = await _tool_names(create_server(spec))
    leaked = [n for n in names if n.startswith(WRITE_TOOL_PREFIXES)]
    assert not leaked, f"the old write switch restored write tools: {leaked}"


async def test_surface_is_bounded_by_the_get_operations(spec):
    """Only GETs (plus the read-only overrides) can become tools."""
    names = await _tool_names(create_server(spec))
    get_ops = sum(
        1
        for path_item in spec["paths"].values()
        for method, op in path_item.items()
        if method == "get" and isinstance(op, dict)
    )
    assert len(names) < get_ops + len(READ_ONLY_METHOD_OVERRIDES), "the surface can't exceed the GET count + overrides"


async def test_credential_minting_options_routes_are_dropped(spec):
    """Vultr's two OPTIONS routes create Docker credentials despite the verb."""
    names = await _tool_names(create_server(spec))
    for tool in (
        "create_registry_docker_credentials",
        "create_registry_kubernetes_docker_credentials",
    ):
        assert tool not in names, f"{tool} mints credentials — not read-only"


async def test_read_only_post_override_survives(spec):
    """POST /databases/{id}/alerts only lists alerts, so it stays."""
    names = await _tool_names(create_server(spec))
    assert "list_service_alerts" in names


def test_overrides_point_at_real_spec_operations(spec):
    """An override whose path stops matching the spec would silently no-op."""
    import re

    for method, pattern in READ_ONLY_METHOD_OVERRIDES:
        matches = [
            path
            for path, item in spec["paths"].items()
            if re.search(pattern, path) and method.lower() in item
        ]
        assert matches, f"read-only override {method} {pattern} matches no spec operation"


async def test_writes_served_over_get_stay_off_the_surface(spec):
    """A GET that changes state is removed by its interface exclusion, since its method reads as safe."""
    names = await _tool_names(create_server(spec))
    assert not [n for n in names if "purge" in n.lower()], "a cache purge leaked onto the read-only surface"


def test_get_is_not_treated_as_a_write():
    assert "GET" not in WRITE_METHODS


async def test_category_endpoints_are_read_only_too(spec):
    """Scoping to a category must not reopen the write surface."""
    names = await _tool_names(create_server(spec, only_categories={"instances"}))
    leaked = [n for n in names if n.startswith(WRITE_TOOL_PREFIXES)]
    assert not leaked, f"category endpoint exposed write tools: {leaked}"
    assert names, "instances endpoint should still expose read tools"


async def test_identity_exclusions_still_apply(spec):
    """Read-only is a second gate, not a replacement for the identity one."""
    names = " ".join(await _tool_names(create_server(spec))).lower()
    assert "scim" not in names and "list_users" not in names
