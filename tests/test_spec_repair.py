"""Repairs for defects in the published spec that would stop it parsing or resolving."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from vultr_mcp.interface.spec_index import SpecIndex
from vultr_mcp.spec_repair import sanitize_spec

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def _spec(path: str, op: dict, path_params: list | None = None) -> dict:
    item: dict = {"get": op}
    if path_params is not None:
        item["parameters"] = path_params
    return {"openapi": "3.0.0", "paths": {path: item}, "components": {"schemas": {}}}


def test_nested_security_requirements_are_flattened():
    spec = sanitize_spec(_spec("/a", {"security": [[{"API Key": []}]]}))
    assert spec["paths"]["/a"]["get"]["security"] == [{"API Key": []}]


def test_null_security_is_dropped_rather_than_guessed():
    spec = sanitize_spec(_spec("/a", {"security": None}))
    assert "security" not in spec["paths"]["/a"]["get"]


def test_file_relative_refs_become_the_path_placeholder_they_name():
    path = "/vpcs/{vpc-id}/nat-gateway/{nat-gateway-id}/virtual-ip/{virtual-ip-id}/backend-ip/{backend-ip-id}"
    refs = ["../shared/parameters.yaml#/vpc-id", "./parameters.yaml#/nat-gateway-id",
            "./parameters.yaml#/nat-gateway-virtual-ip-id", "./parameters.yaml#/nat-gateway-vip-backend-ip-id"]
    spec = sanitize_spec(_spec(path, {"parameters": [{"$ref": ref} for ref in refs]}))
    params = spec["paths"][path]["get"]["parameters"]
    assert [(p["name"], p["in"], p["required"]) for p in params] == [
        ("vpc-id", "path", True), ("nat-gateway-id", "path", True),
        ("virtual-ip-id", "path", True), ("backend-ip-id", "path", True),
    ]


def test_file_relative_paging_refs_become_the_query_parameters_the_api_takes():
    spec = sanitize_spec(_spec("/a", {"parameters": [
        {"$ref": "../shared/parameters.yaml#/per-page"}, {"$ref": "../shared/parameters.yaml#/cursor"},
    ]}))
    params = spec["paths"]["/a"]["get"]["parameters"]
    assert [(p["name"], p["in"], p["schema"]["type"]) for p in params] == [
        ("per_page", "query", "integer"), ("cursor", "query", "string"),
    ]


def test_refs_it_cannot_place_are_left_for_the_parser_to_reject():
    ambiguous = {"$ref": "./parameters.yaml#/thing-id"}
    spec = sanitize_spec(_spec("/a/{other-id}", {"parameters": [ambiguous]}))
    assert spec["paths"]["/a/{other-id}"]["get"]["parameters"] == [ambiguous]


def test_path_level_parameters_are_repaired_too():
    spec = sanitize_spec(_spec("/v/{vpc-id}", {}, path_params=[{"$ref": "../shared/parameters.yaml#/vpc-id"}]))
    assert spec["paths"]["/v/{vpc-id}"]["parameters"][0]["name"] == "vpc-id"


def test_repairs_are_idempotent_on_the_real_spec():
    once = sanitize_spec(json.loads(SPEC_PATH.read_text(encoding="utf-8")))
    assert sanitize_spec(copy.deepcopy(once)) == once


def test_the_real_spec_has_no_unresolvable_refs_once_repaired():
    spec = sanitize_spec(json.loads(SPEC_PATH.read_text(encoding="utf-8")))
    external: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#/"):
                external.append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    assert external == []


def test_the_interface_layer_sees_the_repaired_spec():
    """The validator and drift index the spec themselves, so they must resolve it as the server does."""
    index = SpecIndex.load(json.loads(SPEC_PATH.read_text(encoding="utf-8")))
    params = index.get("list-nat-gateways").raw["parameters"]
    assert all("$ref" not in param for param in params)
