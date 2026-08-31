"""The interface layer: validation, compilation, runtime, and wiring.

The layer exists because generated tool definitions are ambiguous enough to get
picked wrongly (§5 of the product requirements doc: Claude reached for a VKE
tool when asked about a Compute Cluster). Its definitions are hand-reviewed but
drafted by a model against a 1.6MB spec, so the tests that matter most are the
ones proving a confidently invented field fails the build rather than the call.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastmcp import Client

from vultr_mcp.interface import runtime
from vultr_mcp.interface.scaffold import _snake
from vultr_mcp.interface.compiler import InterfaceError, compile_interface
from vultr_mcp.interface.tools import InterfaceTool
from vultr_mcp.server import create_server, load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERFACE_DIR = REPO_ROOT / "interface"

CLUSTER_TOOL = "vultr_compute_clusters_search"


@pytest.fixture(scope="module")
def spec():
    return load_spec()


@pytest.fixture(scope="module")
def compiled(spec):
    return compile_interface(INTERFACE_DIR, spec)


@pytest.fixture(scope="module")
def cluster_tool(compiled):
    return next(tool for tool in compiled.tools if tool.name == CLUSTER_TOOL)


def _cluster(label: str, **overrides):
    """A cluster shaped like the one in openapi.json's own example."""
    item = {
        "id": f"id-{label}",
        "region": "ewr",
        "label": label,
        "plan": "vbm-256c-3072gb-8-mi325x-aac-gpu",
        "min_pool_count": 1,
        "desired_pool_count": 2,
        "hostname": f"{label}-host",
        "status": "active",
        "state": "running",
        "date_created": "2026-01-15T10:30:00+00:00",
        "cluster_type": "fabric",
        "type": "cluster",
        "instances": [{"id": "a"}, {"id": "b"}],
        "vpc_networks": [{"id": "v", "description": "default"}],
    }
    item.update(overrides)
    return item


def _payload(*labels, total=None, next_cursor=""):
    clusters = [_cluster(label) for label in labels]
    return {
        "clusters": clusters,
        "meta": {
            "total": total if total is not None else len(clusters),
            "links": {"next": next_cursor, "prev": ""},
        },
    }


# --------------------------------------------------------------------------
# Validation: the shipped layer, and each way a drafted file can drift.
# --------------------------------------------------------------------------


def test_shipped_interface_validates(spec):
    from vultr_mcp.interface.validator import validate_manifest

    problems = validate_manifest(INTERFACE_DIR, spec)
    assert problems == [], "\n".join(str(problem) for problem in problems)


def _scratch_interface(tmp_path: Path, tool: dict | list) -> Path:
    """A scratch interface directory, sharing the real schema file."""
    tools = tool if isinstance(tool, list) else [tool]
    schema = json.loads(
        (INTERFACE_DIR / "schema" / "interface.schema.json").read_text(encoding="utf-8")
    )
    (tmp_path / "schema").mkdir()
    (tmp_path / "schema" / "interface.schema.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    (tmp_path / "interface.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "test",
                "schema": "schema/interface.schema.json",
                "product_areas": {"clusters": "clusters.yaml"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "clusters.yaml").write_text(
        yaml.safe_dump(
            {"product_area": "clusters", "family": "compute", "tools": tools}
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def definition() -> dict:
    """The shipped cluster tool, as a mutable starting point for drift tests."""
    document = yaml.safe_load((INTERFACE_DIR / "clusters.yaml").read_text(encoding="utf-8"))
    return copy.deepcopy(document["tools"][0])


def _problems(tmp_path, definition, spec) -> list[str]:
    from vultr_mcp.interface.validator import validate_manifest

    directory = _scratch_interface(tmp_path, definition)
    return [str(problem) for problem in validate_manifest(directory, spec)]


def test_unknown_operation_fails(tmp_path, definition, spec):
    definition["operation"] = "list-clusters-v2"
    assert any("not in openapi.json" in p for p in _problems(tmp_path, definition, spec))


def test_write_operation_mislabelled_read_fails(tmp_path, definition, spec):
    definition["operation"] = "delete-cluster"
    problems = _problems(tmp_path, definition, spec)
    assert any("which changes state" in p for p in problems)


def test_input_that_is_neither_mapped_nor_filtered_fails(tmp_path, definition, spec):
    # The most likely drafting error: an input the API has never heard of, with
    # no filter block, which would be silently dropped at runtime.
    definition["input"]["properties"]["hostname"] = {
        "type": "string",
        "description": "Cluster hostname.",
    }
    problems = _problems(tmp_path, definition, spec)
    assert any("is not a parameter of list-clusters" in p for p in problems)


def test_filter_on_field_the_api_does_not_return_fails(tmp_path, definition, spec):
    definition["input"]["properties"]["owner"] = {
        "type": "string",
        "description": "Who owns it.",
        "filter": {"field": "owner", "match": "equals"},
    }
    problems = _problems(tmp_path, definition, spec)
    assert any("does not return" in p for p in problems)


def test_include_of_unknown_field_fails(tmp_path, definition, spec):
    definition["output"]["include"].append("cost_per_month")
    problems = _problems(tmp_path, definition, spec)
    assert any("is not in the 200 response" in p for p in problems)


def test_invented_expression_function_fails(tmp_path, definition, spec):
    definition["computed"]["instance_count"]["from"] = "count(instances)"
    problems = _problems(tmp_path, definition, spec)
    # The schema pattern rejects it before the semantic pass even runs.
    assert problems


def test_expression_reading_absent_field_fails(tmp_path, definition, spec):
    definition["computed"]["instance_count"]["from"] = "length(nodes)"
    problems = _problems(tmp_path, definition, spec)
    assert any("does not return" in p for p in problems)


def test_computed_field_colliding_with_a_real_one_fails(tmp_path, definition, spec):
    definition["computed"]["hostname"] = {
        "type": "string",
        "description": "Duplicate of a real field.",
        "from": "label",
    }
    problems = _problems(tmp_path, definition, spec)
    assert any("already exists in the response" in p for p in problems)


def test_path_item_parameters_are_accepted(tmp_path, spec):
    """Most Vultr path params are declared on the path item, not the operation.

    Reading only ``operation.parameters`` would reject every by-id tool, so this
    pins that the whole path is considered.
    """
    tool = {
        "name": "vultr_compute_clusters_get",
        "access": "read",
        "enabled": True,
        "description": (
            "Gets one Vultr Compute Cluster by ID, including its instances and "
            "current state. Do not use this tool for VKE clusters."
        ),
        "operation": "get-cluster",
        "input": {
            "type": "object",
            "required": ["cluster_id"],
            "properties": {
                "cluster_id": {
                    "type": "string",
                    "description": "ID of the cluster to fetch.",
                    "maps_to": "cluster-id",
                }
            },
        },
    }
    assert _problems(tmp_path, tool, spec) == []


def test_required_api_parameter_with_no_input_fails(tmp_path, spec):
    tool = {
        "name": "vultr_compute_clusters_get",
        "access": "read",
        "enabled": True,
        "description": (
            "Gets one Vultr Compute Cluster by ID, including its instances and "
            "current state. Do not use this tool for VKE clusters."
        ),
        "operation": "get-cluster",
        "input": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Cluster label.",
                    "filter": {"field": "label", "match": "equals"},
                }
            },
        },
    }
    problems = _problems(tmp_path, tool, spec)
    assert any("which no input supplies" in p for p in problems)


def test_name_must_carry_the_declared_family(tmp_path, definition, spec):
    """The family prefix is what stops a name being mistakable for another product.

    vultr_clusters_search reads fine on its own and loses the distinction from
    VKE clusters, so the file's declared family is enforced rather than trusted.
    """
    definition["name"] = "vultr_clusters_search"
    problems = _problems(tmp_path, definition, spec)
    assert any("must start with 'vultr_compute_'" in p for p in problems)


def test_declared_family_is_required(tmp_path, definition, spec):
    from vultr_mcp.interface.validator import validate_manifest

    directory = _scratch_interface(tmp_path, definition)
    document = yaml.safe_load((directory / "clusters.yaml").read_text(encoding="utf-8"))
    del document["family"]
    (directory / "clusters.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    assert [str(p) for p in validate_manifest(directory, spec)]


def test_a_broken_disabled_tool_warns_rather_than_failing(tmp_path, definition, spec):
    """A draft must not be able to stop the server from starting.

    Scaffolded definitions land disabled and unreviewed. They are never
    registered, so their problems cannot reach an agent -- but they are still
    reported, because a draft nobody looks at is how drift gets in.
    """
    from vultr_mcp.interface.validator import validate_manifest

    draft = copy.deepcopy(definition)
    draft["name"] = "vultr_compute_clusters_draft"
    draft["operation"] = "list-clusters-v2-does-not-exist"
    draft["enabled"] = False

    directory = _scratch_interface(tmp_path, [definition, draft])
    problems = validate_manifest(directory, spec)

    assert [p for p in problems if not p.is_error], "the draft should be reported"
    assert not [p for p in problems if p.is_error], "but not as a build failure"

    compiled = compile_interface(directory, spec)
    assert [tool.name for tool in compiled.tools] == ["vultr_compute_clusters_search"]


def test_the_same_problem_is_fatal_once_the_tool_is_enabled(tmp_path, definition, spec):
    draft = copy.deepcopy(definition)
    draft["name"] = "vultr_compute_clusters_draft"
    draft["operation"] = "list-clusters-v2-does-not-exist"
    draft["enabled"] = True

    directory = _scratch_interface(tmp_path, [definition, draft])
    with pytest.raises(InterfaceError):
        compile_interface(directory, spec)


async def test_declining_an_operation_does_not_hide_its_generated_tool():
    """Declining is bookkeeping for drift reports, not suppression.

    interface/instances.yaml declines the ipv4/ipv6 operations because
    vultr_compute_instances_get already answers them. That decision must not
    quietly remove capability from the served surface.
    """
    names = await _tool_names(create_server())
    assert "get_instance_ipv4" in names
    assert "get_instance_ipv6" in names


def test_declined_operations_are_compiled_with_their_reasons(compiled):
    declined = {entry.operation_id: entry for entry in compiled.declined}
    assert "list-instance-ipv6-reverse" in declined
    assert declined["list-instance-ipv6-reverse"].product_area == "instances"
    # The reason is for whoever revisits the decision, so it has to say why.
    assert "dns" in declined["list-instance-ipv6-reverse"].reason.lower()


def test_a_stale_decline_warns_without_failing(tmp_path, definition, spec):
    from vultr_mcp.interface.validator import validate_manifest

    directory = _scratch_interface(tmp_path, definition)
    document = yaml.safe_load((directory / "clusters.yaml").read_text(encoding="utf-8"))
    document["declined"] = {
        "get-cluster-that-was-removed": {
            "reason": "Recorded before the operation disappeared from the spec."
        }
    }
    (directory / "clusters.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    problems = validate_manifest(directory, spec)
    assert any("the decision is stale" in str(p) for p in problems)
    assert not [p for p in problems if p.is_error]


def test_declining_an_operation_that_also_has_a_tool_fails(tmp_path, definition, spec):
    from vultr_mcp.interface.validator import validate_manifest

    directory = _scratch_interface(tmp_path, definition)
    document = yaml.safe_load((directory / "clusters.yaml").read_text(encoding="utf-8"))
    document["declined"] = {
        "list-clusters": {"reason": "Contradicts the tool defined in this same file."}
    }
    (directory / "clusters.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    problems = validate_manifest(directory, spec)
    assert any("also has a tool" in str(p) for p in problems)
    assert [p for p in problems if p.is_error]


def test_compile_refuses_an_invalid_layer(tmp_path, definition, spec):
    definition["operation"] = "list-clusters-v2"
    directory = _scratch_interface(tmp_path, definition)
    with pytest.raises(InterfaceError):
        compile_interface(directory, spec)


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------


def test_compiled_tool_splits_parameters_from_filters(cluster_tool):
    passed = {plan.agent_name: plan.api_name for plan in cluster_tool.parameters}
    filtered = {plan.agent_name for plan in cluster_tool.filters}

    # GET /clusters accepts only per_page and cursor, so everything else has to
    # be applied here.
    assert passed == {"page_size": "per_page", "cursor": "cursor"}
    assert filtered == {"label", "region", "plan", "status", "state", "cluster_type"}


def test_input_schema_hides_layer_directives(cluster_tool):
    for prop in cluster_tool.input_schema["properties"].values():
        assert "maps_to" not in prop
        assert "filter" not in prop
    assert cluster_tool.input_schema["properties"]["page_size"]["default"] == 100


def test_output_plan_keeps_the_container_key(cluster_tool):
    assert cluster_tool.output.container_key == "clusters"
    assert cluster_tool.output.is_collection
    assert "clusters" in cluster_tool.output.envelope_include
    assert "label" in cluster_tool.output.item_include


def test_a_tool_without_an_output_block_still_filters(tmp_path, spec):
    """Finding the results does not depend on declaring how to trim them."""
    directory = _scratch_interface(
        tmp_path,
        {
            "name": "vultr_compute_clusters_search",
            "access": "read",
            "enabled": True,
            "description": (
                "Searches Vultr Compute Clusters by label. Do not use this tool "
                "for VKE or Object Storage clusters."
            ),
            "operation": "list-clusters",
            "input": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Text contained in the label.",
                        "filter": {"field": "label", "match": "contains_ci"},
                    }
                },
            },
        },
    )
    tool = compile_interface(directory, spec).tools[0]
    assert tool.output.container_key == "clusters"

    result = runtime.shape_response(_payload("alpha", "beta"), tool, {"label": "alph"})
    assert [item["label"] for item in result["clusters"]] == ["alpha"]
    # Nothing was trimmed, because nothing asked to be.
    assert "hostname" in result["clusters"][0]


def test_pagination_is_detected_from_the_spec(cluster_tool):
    assert cluster_tool.pagination is not None
    assert cluster_tool.pagination.agent_page_param == "page_size"
    assert cluster_tool.pagination.api_page_param == "per_page"


# --------------------------------------------------------------------------
# Runtime: request building and shaping, no I/O.
# --------------------------------------------------------------------------


def test_build_request_renames_and_defaults(cluster_tool):
    path, query = runtime.build_request(cluster_tool, {})
    assert path == "/clusters"
    # The agent said nothing about paging, so the declared default is applied
    # here — MCP clients do not fill in JSON Schema defaults.
    assert query == {"per_page": 100}

    _, query = runtime.build_request(cluster_tool, {"page_size": 7})
    assert query == {"per_page": 7}


def test_build_request_substitutes_path_parameters(tmp_path, spec):
    from vultr_mcp.interface.compiler import compile_interface as compile_layer

    directory = _scratch_interface(
        tmp_path,
        {
            "name": "vultr_compute_clusters_get",
            "access": "read",
            "enabled": True,
            "description": (
                "Gets one Vultr Compute Cluster by ID. Do not use this tool for "
                "VKE clusters."
            ),
            "operation": "get-cluster",
            "input": {
                "type": "object",
                "required": ["cluster_id"],
                "properties": {
                    "cluster_id": {
                        "type": "string",
                        "description": "ID of the cluster.",
                        "maps_to": "cluster-id",
                    }
                },
            },
        },
    )
    tool = compile_layer(directory, spec).tools[0]
    path, query = runtime.build_request(tool, {"cluster_id": "abc/123"})
    assert path == "/clusters/abc%2F123"
    assert query == {}


def test_shaping_trims_fields_and_adds_computed(cluster_tool):
    result = runtime.shape_response(_payload("alpha"), cluster_tool, {})
    item = result["clusters"][0]

    assert item["instance_count"] == 2
    assert "hostname" not in item, "field outside include should be dropped"
    assert "vpc_networks" not in item
    assert item["label"] == "alpha"


def test_filtering_is_case_insensitive_on_label(cluster_tool):
    payload = _payload("Production GPU", "staging", "prod-backup")
    result = runtime.shape_response(payload, cluster_tool, {"label": "PROD"})

    assert [item["label"] for item in result["clusters"]] == [
        "Production GPU",
        "prod-backup",
    ]


def test_filters_are_anded(cluster_tool):
    payload = {
        "clusters": [
            _cluster("one", region="ewr", status="active"),
            _cluster("two", region="lax", status="active"),
            _cluster("three", region="ewr", status="pending"),
        ],
        "meta": {"total": 3, "links": {"next": "", "prev": ""}},
    }
    result = runtime.shape_response(
        payload, cluster_tool, {"region": "ewr", "status": "active"}
    )
    assert [item["label"] for item in result["clusters"]] == ["one"]


def test_filtered_response_reports_what_it_scanned(cluster_tool):
    payload = _payload("alpha", "beta", "gamma", total=3)
    result = runtime.shape_response(payload, cluster_tool, {"label": "alpha"})

    filtered = result["meta"]["filtered"]
    assert filtered == {"matched": 1, "scanned": 3, "complete": True}
    # The API's own total is left alone: it counts the collection, not the match.
    assert result["meta"]["total"] == 3


def test_unfiltered_response_is_not_annotated(cluster_tool):
    result = runtime.shape_response(_payload("alpha"), cluster_tool, {})
    assert "filtered" not in result["meta"]


def test_matches_beyond_page_size_are_truncated_and_flagged(cluster_tool):
    payload = _payload("prod-1", "prod-2", "prod-3")
    result = runtime.shape_response(payload, cluster_tool, {"label": "prod", "page_size": 2})

    assert len(result["clusters"]) == 2
    filtered = result["meta"]["filtered"]
    assert filtered["matched"] == 3
    assert filtered["complete"] is False
    assert "page_size" in filtered["note"]


def test_partial_scan_is_never_reported_as_complete(cluster_tool):
    payload = _payload("prod-1")
    result = runtime.shape_response(
        payload, cluster_tool, {"label": "prod"}, scan={"pages": 10, "complete": False}
    )
    filtered = result["meta"]["filtered"]
    assert filtered["complete"] is False
    assert filtered["pages_fetched"] == 10
    assert "partial count" in filtered["note"]


# --------------------------------------------------------------------------
# Runtime: the fetch policy, against a mocked API.
# --------------------------------------------------------------------------


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.vultr.example/v2", transport=httpx.MockTransport(handler)
    )


async def test_unfiltered_call_makes_one_request(cluster_tool):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload("alpha", next_cursor="more"))

    async with _client(handler) as client:
        result = await runtime.execute(cluster_tool, {"page_size": 5}, client)

    assert len(calls) == 1
    assert calls[0].url.params["per_page"] == "5"
    assert len(result["clusters"]) == 1


async def test_filtering_pages_through_the_collection(cluster_tool):
    pages = [
        _payload("alpha", "prod-1", next_cursor="page2"),
        _payload("beta", "prod-2", next_cursor=""),
    ]
    seen = []

    def handler(request):
        seen.append(request.url.params.get("cursor"))
        return httpx.Response(200, json=pages[len(seen) - 1])

    async with _client(handler) as client:
        result = await runtime.execute(cluster_tool, {"label": "prod"}, client)

    assert seen == [None, "page2"], "second page fetched with the returned cursor"
    assert [item["label"] for item in result["clusters"]] == ["prod-1", "prod-2"]
    filtered = result["meta"]["filtered"]
    assert filtered == {"matched": 2, "scanned": 4, "complete": True, "pages_fetched": 2}


async def test_paging_stops_at_the_cap_and_says_so(cluster_tool, monkeypatch):
    monkeypatch.setenv("VULTR_MCP_INTERFACE_MAX_PAGES", "2")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload("prod-1", next_cursor="always-more"))

    async with _client(handler) as client:
        result = await runtime.execute(cluster_tool, {"label": "prod"}, client)

    assert len(calls) == 2
    assert result["meta"]["filtered"]["complete"] is False
    assert "partial" in result["meta"]["filtered"]["note"]


async def test_an_explicit_cursor_disables_auto_paging(cluster_tool):
    """A cursor means the agent is walking pages itself; don't scan under it."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload("prod-1", next_cursor="next"))

    async with _client(handler) as client:
        result = await runtime.execute(
            cluster_tool, {"label": "prod", "cursor": "abc"}, client
        )

    assert len(calls) == 1
    assert calls[0].url.params["cursor"] == "abc"
    # One page of a longer collection is not a complete search, and saying so
    # is the difference between a right answer and a confident wrong count.
    assert result["meta"]["filtered"]["complete"] is False


async def test_api_errors_carry_the_api_message(cluster_tool):
    def handler(request):
        return httpx.Response(403, json={"error": "Forbidden: read-only key"})

    async with _client(handler) as client:
        with pytest.raises(runtime.VultrAPIError) as caught:
            await runtime.execute(cluster_tool, {}, client)

    assert "403" in str(caught.value)
    assert "read-only key" in str(caught.value)


async def test_tool_run_returns_the_shaped_payload(cluster_tool):
    def handler(request):
        return httpx.Response(200, json=_payload("alpha"))

    async with _client(handler) as client:
        tool = InterfaceTool.build(cluster_tool, client)
        result = await tool.run({})

    body = json.loads(result.content[0].text)
    assert body["clusters"][0]["instance_count"] == 2


async def test_calling_through_a_client_returns_shaped_json(cluster_tool):
    """The whole path an agent takes: list, call, read the result."""
    from fastmcp import FastMCP

    def handler(request):
        assert request.url.params["per_page"] == "500", "filtered search scans"
        return httpx.Response(200, json=_payload("prod-1", "staging"))

    async with _client(handler) as http_client:
        server = FastMCP("interface-test")
        server.add_tool(InterfaceTool.build(cluster_tool, http_client))
        async with Client(server) as client:
            result = await client.call_tool(CLUSTER_TOOL, {"label": "prod"})

    body = json.loads(result.content[0].text)
    assert [item["label"] for item in body["clusters"]] == ["prod-1"]
    assert body["meta"]["filtered"]["scanned"] == 2


# --------------------------------------------------------------------------
# Scaffolding: drafts a human reviews, never a surface anyone serves.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def index(spec):
    from vultr_mcp.interface.spec_index import SpecIndex

    return SpecIndex.load(spec)


def test_every_tag_scaffolds_into_something_that_validates(tmp_path, spec, index):
    """The whole point of deriving from the spec: it cannot be wrong about it.

    Run over all 41 tags rather than a sample, because the failures worth
    catching are the odd ones -- an operation with no parameters at all, a
    response field named after a date, a description containing a colon.
    """
    from vultr_mcp.interface.scaffold import scaffold_area, slug
    from vultr_mcp.interface.validator import validate_manifest

    (tmp_path / "schema").mkdir()
    (tmp_path / "schema" / "interface.schema.json").write_text(
        (INTERFACE_DIR / "schema" / "interface.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    tags = sorted({tag for op in index.operations.values() for tag in op.tags})
    areas = {}
    for tag in tags:
        drafted = scaffold_area(tag, "compute", index)
        if not drafted.tool_count:
            continue
        area = slug(tag)
        (tmp_path / f"{area}.yaml").write_text(drafted.text, encoding="utf-8")
        yaml.safe_load(drafted.text)  # must parse
        areas[area] = f"{area}.yaml"

    (tmp_path / "interface.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "scaffold-test",
                "schema": "schema/interface.schema.json",
                "product_areas": areas,
            }
        ),
        encoding="utf-8",
    )

    problems = validate_manifest(tmp_path, spec)
    assert not [p for p in problems if p.is_error], "\n".join(
        str(p) for p in problems if p.is_error
    )


def test_scaffolded_tools_are_disabled_with_stub_descriptions(index):
    from vultr_mcp.interface.scaffold import scaffold_area

    document = yaml.safe_load(scaffold_area("instances", "compute", index).text)
    for tool in document["tools"]:
        assert tool["enabled"] is False
        assert "TODO review" in tool["description"]


def test_scaffolding_withholds_credential_shaped_fields(index):
    """GET /instances returns a live password and a console link.

    A draft that put those in the include list would eventually have one
    accepted, so they are commented out and have to be added back deliberately.
    """
    from vultr_mcp.interface.scaffold import scaffold_area

    drafted = scaffold_area("instances", "compute", index)
    assert set(drafted.withheld_fields) == {"default_password", "kvm"}

    document = yaml.safe_load(drafted.text)
    for tool in document["tools"]:
        include = (tool.get("output") or {}).get("include") or []
        assert "default_password" not in include
        assert "kvm" not in include


def test_scaffolded_names_follow_the_family_convention(index):
    from vultr_mcp.interface.scaffold import tool_name

    names = {
        operation.operation_id: tool_name("compute", "instances", operation)
        for operation in index.operations.values()
        if operation.operation_id in ("list-instances", "get-instance", "get-instance-bandwidth")
    }
    # The same names we arrived at by hand for the two tools that exist.
    assert names["list-instances"] == "vultr_compute_instances_search"
    assert names["get-instance"] == "vultr_compute_instances_get"
    assert names["get-instance-bandwidth"] == "vultr_compute_instances_bandwidth_get"


def test_descriptions_with_yaml_metacharacters_survive(index):
    """'Filter upgrade by type: - all (applications, os, plans)' is a real one."""
    from vultr_mcp.interface.scaffold import scaffold_area

    document = yaml.safe_load(scaffold_area("instances", "compute", index).text)
    upgrades = next(
        tool for tool in document["tools"] if tool["operation"] == "get-instance-upgrades"
    )
    assert "type:" in upgrades["input"]["properties"]["type"]["description"]


# --------------------------------------------------------------------------
# Drift: what the spec grew that nobody has looked at.
# --------------------------------------------------------------------------


def test_drift_reports_only_covered_areas(spec):
    """A report over the whole spec would say 519 uncovered, which is noise."""
    from vultr_mcp.interface.drift import detect_drift

    report = detect_drift(INTERFACE_DIR, spec)
    assert {area.product_area for area in report.areas} == {"clusters", "instances"}


def test_declined_operations_are_not_reported_as_unreviewed(spec):
    """The whole reason the declined marker exists.

    A report listing every operation somebody already decided about would bury
    anything genuinely new. The exact counts belong to the spec and are free to
    change; what must hold is that a decline keeps an operation out of the
    report and a non-decline does not.
    """
    from vultr_mcp.interface.drift import detect_drift

    report = detect_drift(INTERFACE_DIR, spec)
    instances = next(a for a in report.areas if a.product_area == "instances")

    unreviewed = {ref.operation_id for ref in instances.unreviewed_reads}
    declared = yaml.safe_load(
        (INTERFACE_DIR / "instances.yaml").read_text(encoding="utf-8")
    )["declined"]

    assert instances.declined == len(declared)
    for operation_id in declared:
        assert operation_id not in unreviewed
    # ipv4 is deliberately NOT declined: it returns every address on the
    # instance, where the instance object carries only main_ip.
    assert "get-instance-ipv4" in unreviewed


def test_a_new_operation_in_a_covered_area_is_reported(tmp_path, definition, spec):
    from vultr_mcp.interface.drift import detect_drift

    directory = _scratch_interface(tmp_path, definition)
    report = detect_drift(directory, spec)
    clusters = next(a for a in report.areas if a.product_area == "clusters")

    assert clusters.served == 1
    # Everything else carrying the clusters tag is unreviewed, by definition.
    # Named rather than enumerated: the spec may add more, and that is the
    # report doing its job rather than a test breaking.
    unreviewed = {ref.operation_id for ref in clusters.unreviewed_reads}
    assert "get-cluster" in unreviewed
    assert "list-clusters" not in unreviewed, "the served operation must be excluded"
    assert not report.is_clean


def test_a_drafted_tool_counts_as_reviewed_not_unreviewed(tmp_path, definition, spec):
    """A scaffolded draft is somebody's work in progress, not a blind spot."""
    from vultr_mcp.interface.drift import detect_drift

    draft = copy.deepcopy(definition)
    draft["name"] = "vultr_compute_clusters_get"
    draft["operation"] = "get-cluster"
    draft["enabled"] = False
    draft["input"] = {
        "type": "object",
        "required": ["cluster_id"],
        "properties": {
            "cluster_id": {
                "type": "string",
                "description": "ID of the cluster.",
                "maps_to": "cluster-id",
            }
        },
    }
    draft.pop("output", None)
    draft.pop("computed", None)

    directory = _scratch_interface(tmp_path, [definition, draft])
    clusters = next(
        a for a in detect_drift(directory, spec).areas if a.product_area == "clusters"
    )

    assert clusters.served == 1
    assert clusters.drafted == 1
    assert "get-cluster" not in {ref.operation_id for ref in clusters.unreviewed_reads}


def test_a_stale_reference_is_reported_as_stale(tmp_path, definition, spec):
    from vultr_mcp.interface.drift import detect_drift

    definition["operation"] = "list-clusters-that-no-longer-exists"
    directory = _scratch_interface(tmp_path, definition)
    report = detect_drift(directory, spec)

    assert report.stale == 1
    clusters = next(a for a in report.areas if a.product_area == "clusters")
    assert clusters.stale == ("list-clusters-that-no-longer-exists",)


def test_deprecated_operations_are_flagged_as_decline_candidates(spec, index):
    """Deprecation is the spec's claim, not ours, so the report should surface it.

    Which operations are deprecated is the spec's business and changes between
    releases -- list-instance-vpc2 was flagged here until Vultr retired it
    outright -- so this checks the flagging against whatever the spec currently
    says rather than against a remembered list.
    """
    from vultr_mcp.interface.drift import detect_drift, format_report

    report = detect_drift(INTERFACE_DIR, spec)
    instances = next(a for a in report.areas if a.product_area == "instances")

    expected = {
        operation.operation_id
        for operation in index.operations.values()
        if "instances" in operation.tags
        and operation.is_deprecated
        and not operation.is_write
    }
    reported = {ref.operation_id for ref in instances.deprecated_unreviewed}
    assert expected, "the spec should still deprecate something under instances"
    assert expected <= reported

    rendered = format_report(report)
    assert "[deprecated]" in rendered
    assert "the claim is the spec's" in rendered


def test_a_tool_on_a_deprecated_operation_is_reported(tmp_path, definition, spec, index):
    """Hand-authoring a name for something Vultr is retiring is worth knowing."""
    from vultr_mcp.interface.drift import detect_drift, format_report

    # Any deprecated read the spec currently carries will do.
    victim = next(
        operation
        for operation in index.operations.values()
        if operation.is_deprecated
        and not operation.is_write
        and not operation.has_request_body
    )
    definition["name"] = "vultr_compute_deprecated_thing_get"
    definition["operation"] = victim.operation_id
    definition["input"] = {
        "type": "object",
        "required": [_snake(p.name) for p in victim.parameters.values() if p.location == "path"],
        "properties": {
            _snake(parameter.name): {
                "type": "string",
                "description": f"Path parameter {parameter.name}.",
                "maps_to": parameter.name,
            }
            for parameter in victim.parameters.values()
            if parameter.location == "path"
        },
    }
    definition.pop("output", None)
    definition.pop("computed", None)

    directory = _scratch_interface(tmp_path, definition)
    report = detect_drift(directory, spec)

    assert report.deprecated_in_use == 1
    assert "DEPRECATED but covered by a tool here" in format_report(report)


def test_writes_are_counted_separately_from_reads(spec):
    """Read-only is the shipped posture, so an unreviewed write is not urgent."""
    from vultr_mcp.interface.drift import detect_drift, format_report

    report = detect_drift(INTERFACE_DIR, spec)
    assert report.unreviewed_writes > report.unreviewed_reads

    quiet = format_report(report)
    assert "create-instance" not in quiet
    assert "create-instance" in format_report(report, show_writes=True)


# --------------------------------------------------------------------------
# Wiring: the interface tool replaces the generated one.
# --------------------------------------------------------------------------


async def _tool_names(server) -> list[str]:
    async with Client(server) as client:
        return [tool.name for tool in await client.list_tools()]


async def test_interface_tool_replaces_its_generated_counterpart():
    names = await _tool_names(create_server())
    assert CLUSTER_TOOL in names
    assert "list_clusters" not in names, "one operation must not yield two tools"


async def test_interface_can_be_switched_off():
    names = await _tool_names(create_server(use_interface=False))
    assert CLUSTER_TOOL not in names
    assert "list_clusters" in names


async def test_interface_read_tool_survives_read_only_mode():
    assert CLUSTER_TOOL in await _tool_names(create_server(read_only=True))


async def test_category_exclusions_still_apply_to_interface_tools():
    # The layer must not be a way around the category gate: excluding clusters
    # drops the hand-authored tool too, not just the generated ones.
    names = await _tool_names(create_server(exclude_categories={"clusters"}))
    assert CLUSTER_TOOL not in names


async def test_missing_interface_directory_is_not_fatal(tmp_path):
    names = await _tool_names(create_server(interface_dir=tmp_path))
    assert "list_clusters" in names
