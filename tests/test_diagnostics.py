"""Upstream timing and fault attribution.

The questions these have to answer are "was it us or the API" and "where did
the time go". Both are derived, so the tests pin the derivation rather than the
plumbing: a wrong ``fault`` sends someone to the wrong team for a day.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from vultr_mcp import diagnostics
from vultr_mcp.diagnostics import (
    InstrumentedTransport,
    PathTemplates,
    fault,
    summarise,
)

SPEC_PATHS = [
    "/instances",
    "/instances/{instance-id}",
    "/instances/{instance-id}/ipv4",
    "/cdns/pull-zones/{pullzone-id}",
    "/cdns/pull-zones/{pullzone-id}/purge",
    "/domains/{dns-domain}/records/{record-id}",
]


@pytest.fixture
def templates():
    return PathTemplates(SPEC_PATHS)


@pytest.fixture
def collect():
    calls: list[dict] = []
    diagnostics.UPSTREAM_CALLS.set(calls)
    return calls


class TestPathTemplates:
    def test_ids_never_reach_the_log(self, templates):
        """The path is for grouping; the id in it names a customer's resource."""
        matched = templates.match("/instances/8a3ec1f2-4b5c-4d6e-9f01-23456789abcd/ipv4")
        assert matched == "/instances/{instance-id}/ipv4"
        assert "8a3ec1f2" not in matched

    def test_a_two_segment_collection_is_not_mistaken_for_an_id(self, templates):
        """No positional rule works: this puts the id at an even index and
        `pull-zones` at an odd one."""
        assert templates.match("/cdns/pull-zones/99/purge") == "/cdns/pull-zones/{pullzone-id}/purge"

    def test_a_literal_beats_a_parameter(self, templates):
        assert templates.match("/instances") == "/instances"

    def test_an_unmatched_path_still_drops_identifiers(self, templates):
        """Unmatched should not happen; if it does it must not be the leak."""
        matched = templates.match("/unknown/8a3ec1f2-4b5c-4d6e-9f01-23456789abcd")
        assert matched == "/unknown/{id}"

    def test_a_domain_name_is_an_identifier_too(self, templates):
        """`example.com` is as much customer data as a UUID is."""
        assert templates.match("/domains/example.com/records/12") == (
            "/domains/{dns-domain}/records/{record-id}"
        )


class TestTransport:
    async def _get(self, handler, collect, templates, path="/instances"):
        transport = InstrumentedTransport(httpx.MockTransport(handler), templates)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
            return await client.get(path)

    async def test_a_successful_call_is_timed_and_recorded(self, collect, templates):
        await self._get(lambda r: httpx.Response(200, json={"instances": []}), collect, templates)
        assert len(collect) == 1
        assert collect[0]["status"] == 200
        assert collect[0]["path"] == "/instances"
        assert collect[0]["method"] == "GET"
        assert collect[0]["duration_ms"] >= 0

    async def test_timing_covers_the_body_not_just_the_headers(self, collect, templates):
        """The production path streams. Body download parked in overhead_ms
        would read as server slowness when it is transfer, pointing diagnosis
        at the wrong half of the system."""

        class _Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(4):
                    await asyncio.sleep(0.01)
                    yield b"y" * 256

        await self._get(lambda r: httpx.Response(200, stream=_Slow()), collect, templates)
        assert collect[0]["response_bytes"] == 1024
        # Four 10ms chunks: headers-only timing would miss essentially all of it.
        assert collect[0]["duration_ms"] >= 30

    async def test_an_already_read_body_is_still_recorded(self, collect, templates):
        """Not every transport streams. One that hands back materialised content
        must not silently produce no record."""
        await self._get(
            lambda r: httpx.Response(200, json={"instances": [{"x": "y" * 500}]}),
            collect,
            templates,
        )
        assert collect[0]["response_bytes"] > 500

    async def test_an_upstream_error_status_is_recorded_not_raised(self, collect, templates):
        response = await self._get(lambda r: httpx.Response(503), collect, templates)
        assert response.status_code == 503
        assert collect[0]["status"] == 503

    async def test_an_unreachable_api_is_recorded(self, collect, templates):
        def boom(request):
            raise httpx.ConnectError("no route to host")

        with pytest.raises(httpx.ConnectError):
            await self._get(boom, collect, templates)
        assert collect[0]["error_type"] == "ConnectError"
        assert "status" not in collect[0]

    async def test_calls_outside_a_tool_are_not_attributed(self, templates):
        """Token exchange and discovery belong to no tool call, so they are
        dropped rather than charged to whichever tool ran last."""
        diagnostics.UPSTREAM_CALLS.set(None)
        transport = InstrumentedTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, json={})), templates
        )
        async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
            await client.get("/instances")
        assert diagnostics.UPSTREAM_CALLS.get() is None


class TestSummarise:
    def test_a_paginated_scan_is_visible_as_many_calls(self):
        """One tool call, eight pages: the count is the explanation for the
        duration, and without it the tool just looks slow."""
        calls = [
            {"path": "/instances", "status": 200, "duration_ms": 120.0} for _ in range(8)
        ]
        out = summarise(calls)
        assert out["upstream_calls"] == 8
        assert out["upstream_ms"] == 960.0
        assert out["upstream_paths"] == ["/instances"]

    def test_the_slowest_page_is_kept(self):
        calls = [
            {"path": "/instances", "status": 200, "duration_ms": 10.0},
            {"path": "/instances", "status": 200, "duration_ms": 4000.0},
        ]
        assert summarise(calls)["upstream_slowest_ms"] == 4000.0

    def test_the_worst_status_wins(self):
        """A scan whose last page 500s is a failure, however well it started."""
        calls = [
            {"path": "/instances", "status": 200, "duration_ms": 1.0},
            {"path": "/instances", "status": 500, "duration_ms": 1.0},
        ]
        assert summarise(calls)["upstream_status"] == 500

    def test_no_upstream_call_is_stated_not_omitted(self):
        assert summarise([])["upstream_calls"] == 0


class TestFault:
    def test_success_has_no_fault(self):
        assert fault("ok", [{"status": 200}]) is None

    def test_an_upstream_5xx_is_theirs(self):
        assert fault("error", [{"status": 503, "duration_ms": 1.0}]) == "upstream"

    def test_a_2xx_that_still_failed_is_ours(self):
        """The data arrived and we broke it on the way out -- shaping or
        serialisation. This is the case the error class alone cannot show."""
        assert fault("error", [{"status": 200, "duration_ms": 1.0}]) == "mcp"

    def test_failing_before_any_call_is_ours(self):
        """Validation or compilation: nothing was ever sent."""
        assert fault("error", []) == "mcp"

    def test_a_4xx_is_request_construction_not_an_upstream_outage(self):
        assert fault("error", [{"status": 400, "duration_ms": 1.0}]) == "request"

    def test_auth_is_separated_from_request_construction(self):
        """"Their credential is wrong" and "we built a bad request" send you to
        different places."""
        assert fault("error", [{"status": 401, "duration_ms": 1.0}]) == "auth"
        assert fault("error", [{"status": 403, "duration_ms": 1.0}]) == "auth"

    def test_a_transport_failure_is_the_api_being_unreachable(self):
        assert fault("error", [{"error_type": "ConnectTimeout"}]) == "unreachable"
