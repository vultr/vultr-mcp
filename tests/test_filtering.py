"""Phase 3: identity-category exclusions and category-endpoint scoping."""

from __future__ import annotations

import pytest
from fastmcp import Client

from vultr_mcp.server import (
    DEFAULT_EXCLUDED_CATEGORIES,
    all_categories,
    create_server,
    load_spec,
)

# Substrings that should only appear in tools from excluded identity
# categories — used to assert those tools are gone.
IDENTITY_TOOL_HINTS = ("iam_", "_iam", "scim", "organization", "api_key", "apikey")


async def _tool_names(server) -> list[str]:
    async with Client(server) as client:
        return [t.name for t in await client.list_tools()]


async def test_default_exclusions_drop_identity_tools():
    names = await _tool_names(create_server())
    joined = " ".join(names).lower()

    # No IAM/SCIM/org/user/api-key tools survive the default exclusions.
    for hint in ("scim", "role_trust", "roletrust", "list_users", "create_user"):
        assert hint not in joined, f"excluded-category tool leaked: matched '{hint}'"

    # But the infrastructure surface is intact.
    for hint in ("instance", "kubernetes", "dns", "firewall"):
        assert hint in joined, f"expected infra tool mentioning '{hint}'"


async def test_exclusions_disabled_restores_full_surface():
    # read_only pinned on both sides so this measures the category gap only.
    full = await _tool_names(create_server(exclude_categories=set(), read_only=False))
    trimmed = await _tool_names(create_server(read_only=False))
    assert len(full) > len(trimmed), "disabling exclusions should add tools back"

    # ~110-ish identity tools in the excluded categories; sanity-check the gap.
    assert len(full) - len(trimmed) > 80, (
        f"expected 80+ excluded tools, gap was {len(full) - len(trimmed)}"
    )


async def test_only_categories_scopes_to_one_endpoint():
    names = await _tool_names(create_server(only_categories={"instances"}))
    assert names, "instances-only server should still expose tools"

    joined = " ".join(names).lower()
    assert "instance" in joined
    # Nothing from other categories leaks in.
    for foreign in ("kubernetes", "dns_domain", "load_balancer", "database"):
        assert foreign not in joined, f"category endpoint leaked '{foreign}'"


async def test_only_categories_cannot_resurface_excluded_identity():
    # Even asking explicitly for 'iam', the identity exclusion wins.
    names = await _tool_names(create_server(only_categories={"iam"}))
    joined = " ".join(names).lower()
    assert "role" not in joined and "policy" not in joined, (
        "a category endpoint must not be able to expose an excluded identity tool"
    )


def test_slugify_produces_clean_url_paths():
    from vultr_mcp.app import slugify

    assert slugify("Container Registry") == "container-registry"
    assert slugify("private Networks") == "private-networks"
    assert slugify("CDNs") == "cdns"
    assert slugify("instances") == "instances"
    assert slugify("VPC2") == "vpc2"


def test_all_categories_matches_spec():
    cats = all_categories(load_spec())
    assert DEFAULT_EXCLUDED_CATEGORIES <= cats, (
        "every default-excluded category must exist as a real spec tag"
    )
    assert len(cats) > 35


async def test_no_category_is_unreviewed():
    """A tag nobody has decided about must fail the build, not ship.

    The default for a new OpenAPI tag is exposure, so a spec update can put a
    product area on the tool surface with nobody having looked. That is how
    `oauth` arrived with 24 client-management operations, and `logs` with an
    endpoint returning s3_secret_key. Both are now decided; this is what makes
    the next one impossible to miss.

    To fix a failure here: put the tag in DEFAULT_EXCLUDED_CATEGORIES if it is
    identity, credential, or otherwise not for agents, and in
    REVIEWED_CATEGORIES if it belongs on the surface.
    """
    from vultr_mcp.server import unreviewed_categories

    unreviewed = unreviewed_categories(load_spec())
    assert not unreviewed, (
        "new OpenAPI tag(s) nobody has assessed: "
        + ", ".join(sorted(unreviewed))
        + " — decide each one into DEFAULT_EXCLUDED_CATEGORIES or REVIEWED_CATEGORIES"
    )


async def test_reviewed_and_excluded_do_not_overlap():
    """A tag in both lists means one of them is a lie about the surface."""
    from vultr_mcp.server import DEFAULT_EXCLUDED_CATEGORIES, REVIEWED_CATEGORIES

    overlap = REVIEWED_CATEGORIES & DEFAULT_EXCLUDED_CATEGORIES
    assert not overlap, f"listed as both reviewed and excluded: {sorted(overlap)}"


async def test_reviewed_categories_still_exist_in_the_spec():
    """A reviewed tag the spec dropped is a stale decision worth pruning."""
    from vultr_mcp.server import REVIEWED_CATEGORIES

    stale = REVIEWED_CATEGORIES - all_categories(load_spec())
    assert not stale, f"reviewed tag(s) no longer in the spec: {sorted(stale)}"
