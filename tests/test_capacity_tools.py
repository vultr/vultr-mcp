"""Reserved capacity tools, shaped from a response laid out the way api.vultr.com builds one."""

from __future__ import annotations

from pathlib import Path

import pytest

from vultr_mcp.interface import runtime
from vultr_mcp.interface.compiler import compile_interface
from vultr_mcp.server import load_spec

INTERFACE_DIR = Path(__file__).resolve().parent.parent / "interface"


@pytest.fixture(scope="module")
def tools():
    compiled = compile_interface(INTERFACE_DIR, load_spec())
    return {tool.name: tool for tool in compiled.tools if tool.name.startswith("vultr_compute_capacity_")}


def _server(server_id: str, region: str, allocation: dict) -> dict:
    return {
        "server_id": server_id,
        "server_type": "bare_metal" if allocation["type"] == "bare_metal" else "vm_host",
        "plans": ["vc2-1c-1gb"],
        "region": region,
        "location": {"id": region, "city": "Newark", "country": "US", "description": "New Jersey"},
        "hardware_specs": {"gpu": {"name": "", "vram_gb": 0, "count": 0}, "cpu": "2x EPYC", "memory": "512 GB",
                           "network": "2x 25 Gbps", "storage": "", "storage_disks": [], "total_storage_gb": 0},
        "status": "available" if allocation["type"] == "unallocated" else "active",
        "allocation": allocation,
        "rack": {"id": "rack-1"},
    }


def _payload() -> dict:
    instances = [
        {"id": "inst-1", "label": "web", "kvm": "https://my.vultr.com/subs/vps/novnc/api.php?data=SECRET"},
        {"id": "inst-2", "label": "db", "kvm": "https://my.vultr.com/subs/vps/novnc/api.php?data=SECRET"},
    ]
    return {
        "capacity": [
            _server("srv-host", "ewr", {"type": "instances", "instance_count": 2, "instances": instances}),
            _server("srv-metal", "lax", {"type": "bare_metal", "bare_metal": {"id": "bm-1", "default_password": "SECRET"}}),
            _server("srv-free", "ewr", {"type": "unallocated"}),
        ],
        "meta": {"total": 3, "links": {"next": "", "prev": ""}},
    }


def test_both_tools_compile(tools):
    assert set(tools) == {"vultr_compute_capacity_list", "vultr_compute_capacity_get"}


def test_what_runs_on_a_server_is_reduced_to_ids(tools):
    rows = runtime.shape_response(_payload(), tools["vultr_compute_capacity_list"], {})["capacity"]
    host, metal, free = rows
    assert (host["allocation_type"], host["instance_count"], host["instance_ids"]) == ("instances", 2, ["inst-1", "inst-2"])
    assert (metal["allocation_type"], metal["bare_metal_id"]) == ("bare_metal", "bm-1")
    assert free["allocation_type"] == "unallocated" and "instance_ids" not in free


def test_the_embedded_instances_never_reach_the_agent(tools):
    """Each embedded instance carries its console link; the list keeps only their IDs."""
    result = runtime.shape_response(_payload(), tools["vultr_compute_capacity_list"], {})
    assert "SECRET" not in repr(result)
    assert all("allocation" not in row for row in result["capacity"])


def test_filters_apply_to_the_reserved_servers(tools):
    rows = runtime.shape_response(_payload(), tools["vultr_compute_capacity_list"],
                                  {"region": "ewr", "status": "available"})["capacity"]
    assert [row["server_id"] for row in rows] == ["srv-free"]


def test_get_shapes_the_one_server_the_same_way(tools):
    payload = {"capacity": _payload()["capacity"][0]}
    shaped = runtime.shape_response(payload, tools["vultr_compute_capacity_get"], {})["capacity"]
    assert shaped["instance_ids"] == ["inst-1", "inst-2"]
    assert "SECRET" not in repr(shaped)
