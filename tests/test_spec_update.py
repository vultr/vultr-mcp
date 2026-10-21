"""Reading the spec out of vultr.com/api and describing what an update changes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from vultr_mcp.interface.spec_update import (
    SpecExtractionError,
    check_plausible,
    diff_specs,
    dump_spec,
    extract_redoc_spec,
    format_summary,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERFACE_DIR = REPO_ROOT / "interface"
SPEC_PATH = REPO_ROOT / "openapi.json"


def _spec(paths=None, schemas=None):
    return {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
            "paths": paths or {}, "components": {"schemas": schemas or {}}}


def _page(state) -> str:
    return f"<html><script>const __redoc_state = {json.dumps(state)};</script></html>"


def test_extracts_the_spec_from_redoc_state():
    spec = _spec({"/a": {"get": {"operationId": "get-a"}}})
    assert extract_redoc_spec(_page({"menu": {}, "spec": {"data": spec}})) == spec


@pytest.mark.parametrize("html", [
    "<html>no state here</html>",
    _page({"spec": {}}),
    _page({"spec": {"data": {"swagger": "2.0", "paths": {"/a": {}}}}}),
    _page({"spec": {"data": {"openapi": "3.0.0", "paths": {}}}}),
])
def test_refuses_a_page_without_a_usable_spec(html):
    with pytest.raises(SpecExtractionError):
        extract_redoc_spec(html)


def test_refuses_a_spec_that_lost_most_of_its_operations():
    old = _spec({f"/p{i}": {"get": {"operationId": f"op{i}"}} for i in range(10)})
    new = _spec({"/p0": {"get": {"operationId": "op0"}}})
    with pytest.raises(SpecExtractionError):
        check_plausible(new, old)
    check_plausible(old, new)


def test_dump_matches_how_openapi_json_is_kept():
    text = SPEC_PATH.read_text(encoding="utf-8")
    assert dump_spec(json.loads(text)) == text


def test_reports_added_removed_and_changed_operations():
    old = _spec({
        "/a": {"get": {"operationId": "get-a", "tags": ["x"]}},
        "/b": {"get": {"operationId": "get-b", "tags": ["x"]}},
    })
    new = _spec({
        "/a": {"get": {"operationId": "get-a", "tags": ["x"], "parameters": [{"name": "q", "in": "query"}]}},
        "/c": {"get": {"operationId": "get-c", "tags": ["fresh", "x"]}},
    })
    diff = diff_specs(old, new)
    assert [op.operation_id for op in diff.added] == ["get-c"]
    assert [op.operation_id for op in diff.removed] == ["get-b"]
    assert diff.changed == {"get-a": ("parameters",)}
    assert diff.new_tags == ("fresh",)
    assert diff_specs(old, new, assessed_tags={"fresh"}).new_tags == ()


def test_path_level_parameters_count_as_part_of_each_operation():
    old = _spec({"/a": {"get": {"operationId": "get-a"}}})
    new = _spec({"/a": {"parameters": [{"name": "id", "in": "path"}], "get": {"operationId": "get-a"}}})
    assert diff_specs(old, new).changed == {"get-a": ("parameters",)}


def test_marks_component_properties_added_removed_and_changed():
    old = _spec(schemas={"thing": {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}}})
    new = _spec(schemas={"thing": {"properties": {"a": {"type": "integer"}, "c": {"type": "string"}}}})
    assert diff_specs(old, new).components == {"schemas/thing": ("~a", "-b", "+c")}


def test_names_reviewed_tools_whose_schemas_changed_through_a_ref_chain():
    old = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    new = copy.deepcopy(old)
    # cluster is reached from list-clusters' response, so a property change there touches the list tool.
    new["components"]["schemas"]["cluster"]["properties"]["added_for_test"] = {"type": "string"}
    diff = diff_specs(old, new, interface_dir=INTERFACE_DIR)
    assert diff.components == {"schemas/cluster": ("+added_for_test",)}
    assert "vultr_compute_clusters_list" in diff.tools_touched
    assert "schemas/cluster" in diff.tools_touched["vultr_compute_clusters_list"]


def test_names_reviewed_tools_whose_operation_was_removed():
    old = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    new = copy.deepcopy(old)
    del new["paths"]["/clusters"]["get"]
    diff = diff_specs(old, new, interface_dir=INTERFACE_DIR)
    assert diff.tools_touched["vultr_compute_clusters_list"] == "list-clusters was removed"


def test_summary_leads_with_the_tools_to_recheck():
    old = _spec({"/a": {"get": {"operationId": "get-a"}}})
    new = _spec({"/a": {"get": {"operationId": "get-a", "description": "new"}}})
    summary = format_summary(diff_specs(old, new), source="https://example.test/api/",
                             checks=[("Interface validation", False, "boom")])
    assert "1 changed" in summary
    assert "- `get-a`: description" in summary
    assert "❌ Interface validation" in summary and "boom" in summary
