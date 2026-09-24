"""Upstream timing and fault attribution.

The questions these have to answer are "was it us or the API" and "where did
the time go". Both are derived, so the tests pin the derivation rather than the
plumbing: a wrong ``fault`` sends someone to the wrong team for a day.
"""

from __future__ import annotations

import asyncio
import zlib

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


# -- upstream error bodies ----------------------------------------------------


def test_error_snippet_keeps_the_message_and_truncates():
    """Enough of the upstream's words to identify the failure, no more."""
    from vultr_mcp.diagnostics import MAX_UPSTREAM_ERROR_CHARS, error_snippet

    assert error_snippet(b'{"error":"Unable to retrieve VPCs","status":500}') == (
        '{"error":"Unable to retrieve VPCs","status":500}'
    )
    assert len(error_snippet(b"x" * 5000)) == MAX_UPSTREAM_ERROR_CHARS
    # A body that is not valid UTF-8 must not take the record down with it.
    assert error_snippet(b"\xff\xfe bad") != ""


async def test_a_compressed_response_records_its_encoding():
    """A size is only evidence if you know what it measures.

    The streamed path counts wire bytes and the buffered path counts decoded
    ones, so the same payload reads at two sizes. Without the encoding beside
    it, a small number looks like a small payload -- which is exactly the
    inference a bug report turned on.
    """
    import httpx

    from vultr_mcp import diagnostics

    calls: list[dict] = []
    diagnostics.UPSTREAM_CALLS.set(calls)

    def handler(request):
        import gzip

        body = gzip.compress(b'{"bandwidth":{}}')
        return httpx.Response(200, content=body, headers={"content-encoding": "gzip"})

    transport = diagnostics.InstrumentedTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(base_url="https://api.vultr.example/v2", transport=transport) as c:
        await c.get("/instances/abc/bandwidth")

    assert calls[-1]["response_encoding"] == "gzip"


class _Chunks(httpx.AsyncByteStream):
    """A streamed body, delivered in fixed-size pieces like the wire does."""

    def __init__(self, body: bytes, size: int = 7) -> None:
        self._body, self._size = body, size

    async def __aiter__(self):
        for i in range(0, len(self._body), self._size):
            yield self._body[i : i + self._size]


async def _streamed_error(status, body, encoding):
    from vultr_mcp import diagnostics

    calls: list[dict] = []
    diagnostics.UPSTREAM_CALLS.set(calls)
    headers = {"content-encoding": encoding} if encoding else {}
    transport = diagnostics.InstrumentedTransport(
        httpx.MockTransport(lambda r: httpx.Response(status, stream=_Chunks(body), headers=headers))
    )
    async with httpx.AsyncClient(base_url="https://api.vultr.example/v2", transport=transport) as c:
        response = await c.get("/instances/abc/vpcs")
    return response, calls[-1]


async def test_a_gzipped_streamed_error_records_its_message():
    """The production failure: four 403s were recorded as gzip bytes -- the
    upstream_error of each began U+FFFD 0x08 -- so what the API said was lost."""
    import gzip

    message = b'{"error":"Unauthorized IP address: 203.0.113.9","status":403}'
    response, record = await _streamed_error(403, gzip.compress(message), "gzip")

    assert record["upstream_error"] == message.decode()
    assert record["response_encoding"] == "gzip"
    # The caller's view is untouched: httpx still decodes the body for it.
    assert response.json()["status"] == 403


async def test_a_deflated_streamed_error_records_its_message():
    message = b'{"error":"Unable to retrieve VPCs","status":500}'
    _, record = await _streamed_error(500, zlib.compress(message), "deflate")
    assert record["upstream_error"] == message.decode()


async def test_only_the_head_of_a_long_compressed_error_is_needed():
    """Only the first bytes are kept, so decoding must work on a truncated stream."""
    import gzip
    import random

    from vultr_mcp.diagnostics import MAX_UPSTREAM_ERROR_CHARS

    rng = random.Random(7)
    noise = "".join(rng.choice("abcdefghij0123456789") for _ in range(20_000))
    body = gzip.compress(f'{{"error":"Upstream exploded","trace":"{noise}"}}'.encode())
    assert len(body) > MAX_UPSTREAM_ERROR_CHARS * 4  # truly truncated on capture

    _, record = await _streamed_error(502, body, "gzip")
    assert record["upstream_error"].startswith('{"error":"Upstream exploded"')
    assert len(record["upstream_error"]) <= MAX_UPSTREAM_ERROR_CHARS


async def test_an_encoding_it_cannot_undo_is_named_not_stored_as_noise():
    _, record = await _streamed_error(403, b"\x8b\x0b\x80\x7b\x22\x65", "br")
    assert record["upstream_error"] == "(br-encoded body, not decoded)"


async def test_a_plain_streamed_error_is_unchanged():
    _, record = await _streamed_error(404, b'{"error":"Invalid resource ID","status":404}', None)
    assert record["upstream_error"] == '{"error":"Invalid resource ID","status":404}'
