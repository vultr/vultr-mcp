"""Per-upstream-call timing and fault attribution.

Why this is separate from audit.py
----------------------------------
The audit record answers "who did what". This answers "why was it slow" and
"whose fault was the failure" -- different questions, different retention, and
a much higher volume. They share an emitter and a request id so a diagnostic
record can be joined back to the call that caused it, and nothing more.

The two numbers that matter
---------------------------
A tool call is not one API call. The interface layer's pagination scan walks up
to ``max_auto_pages()`` pages to satisfy a single client-side filter, so "this
tool took nine seconds" can mean one slow request or eight ordinary ones.
Splitting ``duration_ms`` into ``upstream_ms`` and ``overhead_ms``, and counting
the calls, is what separates a slow API from a slow server.

Attribution
-----------
``fault`` exists because a 500 out of the MCP says nothing about where the
failure was. An upstream 5xx is the API's; a 2xx that still ends in a tool error
is ours, always, because the data arrived and we broke it on the way out; a 4xx
is usually our request construction, which looks like a caller error and is not.
Deriving this once, here, beats re-deriving it from a stack trace at the moment
someone is asking.
"""

from __future__ import annotations

import contextvars
import re
import time
from typing import Any, Iterable

import httpx

# Upstream calls made on behalf of the tool call currently in flight. Bound per
# tool call by the audit middleware; a list, mutated in place, so a sub-task
# that inherits a copy of the context still appends to the same record.
UPSTREAM_CALLS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "vultr_mcp_upstream_calls", default=None
)

_UUID = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")


class PathTemplates:
    """Maps a concrete request path back to its OpenAPI template.

    A logged path has to be groupable -- ``/instances/{instance-id}/ipv4`` is
    something you can count and compare, ``/instances/8a3.../ipv4`` is not --
    and it must not carry the id itself, which names a specific customer
    resource.

    Matched against the spec rather than guessed: no positional rule works here,
    because a collection can be two segments deep
    (``/cdns/pull-zones/{pullzone-id}``) and templated segments land on both odd
    and even indices. The spec already knows, so ask it.
    """

    def __init__(self, paths: Iterable[str]) -> None:
        self._root: dict[str, Any] = {}
        for path in paths:
            node = self._root
            for segment in [s for s in path.split("/") if s]:
                key = "*" if segment.startswith("{") else segment
                node = node.setdefault(key, {})
            node["$"] = path

    def match(self, path: str) -> str:
        segments = [s for s in path.split("/") if s]
        found = self._walk(self._root, segments, 0)
        return found if found is not None else redact_unknown(segments)

    def _walk(self, node: dict[str, Any], segments: list[str], i: int) -> str | None:
        if i == len(segments):
            return node.get("$")
        # Literal before parameter: a real segment always beats a wildcard that
        # would also accept it, which is how the spec itself disambiguates.
        for key in (segments[i], "*"):
            child = node.get(key)
            if child is not None:
                found = self._walk(child, segments, i + 1)
                if found is not None:
                    return found
        return None


def redact_unknown(segments: list[str]) -> str:
    """Fallback for a path no template matched: keep the shape, drop anything
    that could be an identifier.

    An unmatched path should not happen. If one does, it must not be the thing
    that puts a customer id in the log.
    """
    out = []
    for segment in segments:
        if _UUID.match(segment) or segment.isdigit() or "." in segment or len(segment) > 24:
            out.append("{id}")
        else:
            out.append(segment)
    return "/" + "/".join(out)


def record_upstream(entry: dict[str, Any]) -> None:
    """Attribute one upstream call to the tool call in flight, if there is one.

    Calls made outside a tool -- token exchange, discovery -- bind no list and
    are dropped rather than attributed to whichever tool ran last.
    """
    calls = UPSTREAM_CALLS.get()
    if calls is not None:
        calls.append(entry)


class InstrumentedTransport(httpx.AsyncBaseTransport):
    """Times every upstream request, whichever surface made it.

    Wrapping the transport rather than the two call sites is deliberate: the
    generated surface and the interface layer share one ``AsyncClient``, so this
    is the single place that sees both, and it cannot be bypassed by a call site
    added later.

    Timing runs to stream close, not to response headers. Headers-only would
    park body download time in ``overhead_ms``, where a large list response
    would read as server overhead when it is transfer -- pointing diagnosis at
    the wrong half of the system.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport, templates: PathTemplates | None = None
    ) -> None:
        self._inner = inner
        self._templates = templates

    def _template(self, request: httpx.Request) -> str:
        path = request.url.path
        if self._templates is not None:
            return self._templates.match(path)
        return redact_unknown([s for s in path.split("/") if s])

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "method": request.method,
            "path": self._template(request),
        }

        try:
            response = await self._inner.handle_async_request(request)
        except Exception as exc:
            entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            entry["error_type"] = type(exc).__name__
            record_upstream(entry)
            raise

        entry["status"] = response.status_code

        # A transport that hands back an already-materialised body has nothing
        # left to time, and wrapping its stream would record nothing at all --
        # httpx never iterates a response whose content is set. Finish here
        # instead of depending on that invariant holding.
        if hasattr(response, "_content"):
            entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            entry["response_bytes"] = len(response._content)
            record_upstream(entry)
            return response

        response.stream = _TimedStream(response.stream, started, entry)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class _TimedStream(httpx.AsyncByteStream):
    """Passes the body through untouched, finalising the timing when it ends.

    Recording on close as well as on exhaustion covers the response nobody reads
    to completion: an error path that closes early still produces a record, with
    the time it actually consumed.
    """

    def __init__(self, inner: Any, started: float, entry: dict[str, Any]) -> None:
        self._inner = inner
        self._started = started
        self._entry = entry
        self._bytes = 0
        self._done = False

    async def __aiter__(self):
        async for chunk in self._inner:
            self._bytes += len(chunk)
            yield chunk
        self._finish()

    async def aclose(self) -> None:
        self._finish()
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self._entry["duration_ms"] = round((time.perf_counter() - self._started) * 1000, 1)
        self._entry["response_bytes"] = self._bytes
        record_upstream(self._entry)


def summarise(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the upstream calls of one tool call into the fields worth keeping."""
    if not calls:
        return {"upstream_calls": 0}

    timed = [c["duration_ms"] for c in calls if "duration_ms" in c]
    statuses = [c["status"] for c in calls if "status" in c]

    out: dict[str, Any] = {
        "upstream_calls": len(calls),
        "upstream_paths": sorted({c["path"] for c in calls if "path" in c}),
    }
    if timed:
        out["upstream_ms"] = round(sum(timed), 1)
        out["upstream_slowest_ms"] = max(timed)
    if statuses:
        # The worst status is the one worth surfacing: a scan whose eighth page
        # 500s is a failure, however well the first seven went.
        out["upstream_status"] = max(statuses)
    return out


def fault(outcome: str, calls: list[dict[str, Any]]) -> str | None:
    """Whose failure this was, from what upstream did or did not say."""
    if outcome == "ok":
        return None
    if not calls:
        # Nothing was ever sent: validation, compilation, or auth resolution.
        return "mcp"

    last = calls[-1]
    if "error_type" in last:
        return "unreachable"

    status = last.get("status")
    if status is None:
        return "mcp"
    if status >= 500:
        return "upstream"
    if status in (401, 403):
        return "auth"
    if status >= 400:
        # The API rejected what we sent. Usually parameter mapping, which reads
        # as a caller error and is not one.
        return "request"
    # Upstream said 2xx and the tool still failed: shaping, serialisation, ours.
    return "mcp"
