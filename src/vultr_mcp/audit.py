"""One structured record per tool call, for audit.

Why this exists
---------------
Nothing else records what the MCP did. Vultr's audit log covers account and
configuration changes, so a read-only surface leaves no trace there at all --
which makes "every action is logged" untrue rather than merely incomplete.
Every tool call already funnels through FastMCP's middleware chain, so one
``on_call_tool`` hook covers the generated surface and the interface layer
alike.

Metadata only, deliberately
---------------------------
Argument *names* are recorded, never their values; result *cardinality*, never
its content. Tool responses carry the very things the interface layer shapes
away -- kubeconfigs, load balancer private keys, VKE user-data -- so a log that
captured payloads would rebuild, in the logging pipeline, the disclosure the
shaping exists to prevent. Capturing payloads should be a separate decision
with its own retention and access rules, not a default.

What a record answers: who called which tool, as what identity, from which
client, when, how long it took, and whether it worked. What it cannot answer:
what the data was.

Failure policy
--------------
Auditing never breaks a call. Every field is gathered defensively and the whole
emission is guarded: a logging bug must not turn a working tool into an error.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.middleware import Middleware

from vultr_mcp import diagnostics

# The id of the tool call currently in flight, so the upstream request made on
# its behalf can carry the same one. A contextvar rather than an argument
# because the call site is httpx's auth hook, several frames below the tool and
# with no route to pass anything down -- and because each concurrent call needs
# its own value.
CURRENT_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vultr_mcp_request_id", default=None
)

# Sent on every upstream call. Namespaced rather than the conventional
# X-Request-Id so it cannot be confused with an id the platform already
# assigns, and so a row carrying it is unambiguously MCP-originated.
REQUEST_ID_HEADER = "X-Vultr-MCP-Request-Id"

# Claims worth recording from a verified OAuth token. `sub` is the consenting
# user and `acctid` the account the call resolves against -- together they are
# the answer to "who did this", and both are already validated by the time a
# tool runs. Everything else in the token stays out.
IDENTITY_CLAIMS = ("sub", "acctid")


def audit_enabled() -> bool:
    """On unless explicitly disabled; an audit trail that defaults off is not one."""
    return os.environ.get("VULTR_MCP_AUDIT_LOG", "true").lower() not in ("0", "false", "no")


def audit_dir() -> str:
    """Directory for the shipped copy of the records, or "" for stdout only.

    stdout is how a person reads what a pod is doing; it is not a place a
    collector can read from without the cluster in between, and it dies with
    the pod. A file is what a log shipper tails, and the shipper is a sidecar
    that knows nothing about this process beyond the path.
    """
    return os.environ.get("VULTR_MCP_AUDIT_DIR", "").strip()


def audit_file_max_bytes() -> int:
    """Size past which the file is abandoned for stdout only.

    Clicktail collapses shipped bytes off the front of the file, so while it
    runs the file stays near empty. A file this large means nothing is draining
    it, and the choice is to stop feeding it or to let an emptyDir fill until
    the pod is evicted. Losing lines is the clicktail design's own stance.
    """
    try:
        return max(1, int(os.environ.get("VULTR_MCP_AUDIT_FILE_MAX_MB", "256"))) * 1024 * 1024
    except ValueError:
        return 256 * 1024 * 1024


# Which replica wrote a record. Both pods feed the same ClickHouse table, and
# nothing else in a record tells them apart. HOSTNAME is the pod name in k8s.
POD = os.environ.get("HOSTNAME") or None

# One file per event type, because the two record shapes share almost no
# columns and clicktail maps each file to its own table (log_<filename>).
# Values are raw file descriptors, or None where opening failed.
_writers: dict[str, int | None] = {}


def _writer(event: str) -> int | None:
    """An O_APPEND descriptor for one event type, opened once.

    Not a logging handler, deliberately. Clicktail collapses consumed bytes
    off the front of the file while we write, and its README is explicit that
    a writer which seeks and then writes will corrupt it. Rotating handlers
    seek to measure size and rename the file out from under the tailer --
    and a renamed ``.log.1`` is never watched, so its lines never ship. One
    ``write()`` of a whole line on an O_APPEND descriptor never seeks, never
    leaves a partial line, and always lands at the current end.

    Returns None when no directory is configured or the file cannot be opened
    -- a log sink that takes the server down with it is worse than none.
    """
    directory = audit_dir()
    if not directory:
        return None
    if event in _writers:
        return _writers[event]

    fd: int | None = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(
            os.path.join(directory, f"{event.replace('.', '_')}.log"),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            # Group-writable: the tailer shares the pod's fsGroup, and
            # collapsing a file is a write.
            0o660,
        )
    except Exception:  # noqa: BLE001
        fd = None

    _writers[event] = fd
    return fd


def _close_writers() -> None:
    """Close every open descriptor. For tests, and for a clean shutdown."""
    for fd in _writers.values():
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    _writers.clear()


def _identity() -> dict[str, Any]:
    """Who the caller is, from the verified access token.

    Returns the auth method always, and the OAuth subject and account when the
    credential carries them. A raw API key resolves to no identity here at all
    -- it is opaque until api.vultr.com resolves it -- so those records are
    attributable only to the fact that a key was used.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is None:
            return {"auth_method": "none"}

        claims = getattr(token, "claims", None) or {}
        out: dict[str, Any] = {
            "auth_method": claims.get("auth_method", "oauth"),
            "client_id": getattr(token, "client_id", None),
        }
        for claim in IDENTITY_CLAIMS:
            if claims.get(claim) is not None:
                out[claim] = claims[claim]
        return out
    except Exception:  # noqa: BLE001 - auditing must not break a call
        return {"auth_method": "unknown"}


def _result_size(result: Any) -> int | None:
    """How many items came back, never what they were.

    Cardinality is what makes a record useful after the fact -- "this listed
    400 instances" reads differently from "this listed one" -- and it discloses
    nothing about content.
    """
    try:
        content = getattr(result, "content", None)
        if isinstance(content, list):
            return len(content)
        if isinstance(result, list):
            return len(result)
    except Exception:  # noqa: BLE001
        pass
    return None


def error_types(exc: BaseException, limit: int = 4) -> list[str]:
    """The exception class names, following the cause chain.

    FastMCP wraps every tool failure in ``ToolError``, so the outermost type
    alone says only "a tool failed". The chain distinguishes an upstream HTTP
    error from a validation failure from a bug. Class names only: the messages
    quote API responses and can carry the data this log must not hold.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(names) < limit and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return names


def emit(record: dict[str, Any]) -> None:
    """Write one record as a JSON line: always stdout, and a file when asked.

    stdout because the cluster already ships it; JSON because the consumer is a
    log pipeline rather than a person. Flushed so a record survives a pod that
    is about to be killed -- the case where it matters most.

    The file is the same line again, for a shipper to tail. Both, not one: the
    file is what outlives the pod, and stdout is what someone reads at 2am
    without a query engine. Neither failing may disturb the other, or the tool
    call that produced the record.
    """
    if POD and "pod" not in record:
        record = {**record, "pod": POD}

    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return

    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass

    try:
        fd = _writer(str(record.get("event", "record")))
        if fd is not None and os.fstat(fd).st_size < audit_file_max_bytes():
            # One call, whole line: the tailer reads complete lines and holds
            # partials, but a line split across two writes can interleave with
            # another thread's line between them.
            os.write(fd, (line + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass


def upstream_detail_enabled() -> bool:
    """Per-upstream-call records, on by default.

    The aggregate on the tool record answers most questions; the detail answers
    the rest -- which of eight scan pages was the slow one, which path 500'd.
    Separable because it is the higher-volume half.
    """
    return os.environ.get("VULTR_MCP_AUDIT_UPSTREAM", "true").lower() not in ("0", "false", "no")


class AuditMiddleware(Middleware):
    """Emit one ``mcp.tool_call`` record per tool invocation."""

    def __init__(self) -> None:
        super().__init__()
        # Create the files now, empty, rather than on the first record. A tailer
        # that finds a file already holding lines starts from its end -- clicktail
        # does, measured -- so a file created by the first call after a rollout
        # loses that call. Boot is seconds; the tailer is ready well inside it.
        _writer("mcp.tool_call")
        if upstream_detail_enabled():
            _writer("mcp.upstream_call")
        _writer(AUTH_EVENT)

    async def on_call_tool(self, context, call_next):
        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        # Bound before the tool runs so every upstream call it makes lands here.
        # A fresh list per call, and per task, so concurrent calls never merge.
        upstream: list[dict[str, Any]] = []
        diagnostics.UPSTREAM_CALLS.set(upstream)
        CURRENT_REQUEST_ID.set(request_id)
        record: dict[str, Any] = {
            "event": "mcp.tool_call",
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
        }

        try:
            record["tool"] = getattr(context.message, "name", None)
            # Names only. An argument value can be a label, an ID, or a
            # free-text filter the caller chose -- none of it belongs here.
            arguments = getattr(context.message, "arguments", None)
            if isinstance(arguments, dict):
                record["argument_names"] = sorted(arguments)
            record.update(_identity())

            ctx = getattr(context, "fastmcp_context", None)
            if ctx is not None:
                for field in ("client_id", "session_id"):
                    try:
                        value = getattr(ctx, field, None)
                        if value:
                            record[f"mcp_{field}"] = value
                    except Exception:  # noqa: BLE001 - accessors can raise off-session
                        pass
        except Exception:  # noqa: BLE001
            pass

        try:
            result = await call_next(context)
        except Exception as exc:
            record["outcome"] = "error"
            record["error_type"] = type(exc).__name__
            record["error_chain"] = error_types(exc)
            _finish(record, started, upstream)
            raise

        record["outcome"] = "ok"
        size = _result_size(result)
        if size is not None:
            record["result_items"] = size
        _finish(record, started, upstream)
        return result


def _finish(record: dict[str, Any], started: float, upstream: list[dict[str, Any]]) -> None:
    """Close out a tool record and emit it, with its upstream detail."""
    try:
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record.update(diagnostics.summarise(upstream))

        # The whole point of the split: time this server spent, as distinct from
        # time it spent waiting. A large overhead_ms is ours to explain.
        upstream_ms = record.get("upstream_ms")
        if upstream_ms is not None:
            record["overhead_ms"] = round(record["duration_ms"] - upstream_ms, 1)

        blame = diagnostics.fault(record.get("outcome", "error"), upstream)
        if blame is not None:
            record["fault"] = blame
    except Exception:  # noqa: BLE001 - auditing must not break a call
        pass

    _emit_safely(record)

    if not upstream_detail_enabled():
        return
    for call in upstream:
        _emit_safely(
            {
                "event": "mcp.upstream_call",
                "ts": datetime.now(timezone.utc).isoformat(),
                "request_id": record.get("request_id"),
                "tool": record.get("tool"),
                **call,
            }
        )


def _emit_safely(record: dict[str, Any]) -> None:
    """Emit, swallowing anything that goes wrong.

    ``emit`` guards its own writes, but this guards the call itself: a bug
    introduced in emission -- or in whatever replaces it later -- must not turn
    a working tool into a failed one. Auditing is never worth breaking a call.
    """
    try:
        emit(record)
    except Exception:  # noqa: BLE001
        pass


# -- sign-in and token refresh --------------------------------------------------

AUTH_EVENT = "mcp.auth"


def emit_auth(
    action: str, outcome: str, client_id: str | None, started: float, **fields: Any
) -> None:
    """One ``mcp.auth`` record: a sign-in starting or completing, or a refresh.

    Tool-call records exist only once a client holds a working token, so they
    cannot show the moment a user is made to sign in again -- which is what the
    eval harness reported, and what pod logs keep for barely a day. ``action``
    is ``authorize``, ``code_exchange`` or ``refresh``; ``outcome`` is ``ok``,
    ``error`` or ``refused``. Never a token: the client, how a refresh was
    resolved, the error the client was given, and the account once known.
    """
    if not audit_enabled():
        return
    record: dict[str, Any] = {
        "event": AUTH_EVENT,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "outcome": outcome,
        "client_id": client_id,
        # CIMD clients (Claude.ai and the like) identify by a URL; DCR clients
        # by an opaque id they registered with us.
        "client_kind": "cimd" if (client_id or "").startswith("https://") else "registered",
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    record.update({k: v for k, v in fields.items() if v is not None})
    _emit_safely(record)
