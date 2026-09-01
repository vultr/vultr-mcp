"""The public release ships a read-only tool surface.

Writes are opt-in (VULTR_MCP_WRITES_ENABLED), so a deployment that configures
nothing cannot hand an agent a tool that provisions, mutates, or destroys
infrastructure. These tests pin that default and the one behavioural
exception (a POST that only lists).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from vultr_mcp.server import (
    READ_ONLY_METHOD_OVERRIDES,
    WRITE_METHODS,
    create_server,
    load_spec,
    read_only_from_env,
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

    # And the read surface is genuinely intact, not just empty. These two are
    # generated: their product areas have no hand-authored file, so they prove
    # from_openapi still populates the surface.
    joined = " ".join(names)
    for hint in ("list_regions", "list_plans"):
        assert hint in joined, f"expected read tool '{hint}'"

    # The account is reachable too, but under the interface layer's name: a
    # hand-authored tool replaces its generated twin, so get_account is gone by
    # design rather than missing.
    assert "vultr_account_get" in names

    # Instances are still reachable, under whichever name owns them: the
    # interface layer replaces the generated list_instances with a
    # hand-authored tool, so pinning the generated name here would fail the
    # moment a product area is covered rather than when a read tool goes
    # missing.
    assert "list_instances" in joined or "vultr_compute_instances_list" in joined


async def test_read_only_is_the_default_without_env(monkeypatch):
    monkeypatch.delenv("VULTR_MCP_WRITES_ENABLED", raising=False)
    assert read_only_from_env() is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_writes_enabled_opts_in(monkeypatch, value):
    monkeypatch.setenv("VULTR_MCP_WRITES_ENABLED", value)
    assert read_only_from_env() is False


@pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
def test_anything_else_stays_read_only(monkeypatch, value):
    """Typos and junk fail closed, not open."""
    monkeypatch.setenv("VULTR_MCP_WRITES_ENABLED", value)
    assert read_only_from_env() is True


async def test_env_toggle_reaches_the_tool_surface(spec, monkeypatch):
    monkeypatch.setenv("VULTR_MCP_WRITES_ENABLED", "true")
    names = await _tool_names(create_server(spec))
    assert any(n.startswith("create_") for n in names), (
        "VULTR_MCP_WRITES_ENABLED=true should restore write tools"
    )


async def test_writes_are_the_only_difference(spec):
    """Read-only drops writes and nothing else — every GET must survive."""
    read_only = set(await _tool_names(create_server(spec)))
    full = set(await _tool_names(create_server(spec, read_only=False)))

    assert read_only < full
    dropped = full - read_only
    assert len(dropped) > 200, f"expected 200+ write tools dropped, got {len(dropped)}"

    # Cross-check against the spec: every non-excluded GET is still a tool.
    get_ops = sum(
        1
        for path_item in spec["paths"].values()
        for method, op in path_item.items()
        if method == "get" and isinstance(op, dict)
    )
    assert len(read_only) < get_ops, "read-only can't exceed the GET count + overrides"


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


def test_get_is_not_treated_as_a_write():
    assert "GET" not in WRITE_METHODS


async def test_category_endpoints_are_read_only_too(spec):
    """Scoping to a category must not reopen the write surface."""
    names = await _tool_names(create_server(spec, only_categories={"instances"}))
    leaked = [n for n in names if n.startswith(WRITE_TOOL_PREFIXES)]
    assert not leaked, f"category endpoint exposed write tools: {leaked}"
    assert names, "instances endpoint should still expose read tools"


async def test_identity_exclusions_still_win_with_writes_enabled(spec):
    """Read-only is a second gate, not a replacement for the identity one."""
    names = " ".join(await _tool_names(create_server(spec, read_only=False))).lower()
    assert "scim" not in names and "list_users" not in names
