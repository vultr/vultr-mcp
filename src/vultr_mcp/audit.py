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
    """Write one record as a JSON line on stdout.

    stdout because the cluster already ships it; JSON because the consumer is a
    log pipeline rather than a person. Flushed so a record survives a pod that
    is about to be killed -- the case where it matters most.
    """
    try:
        sys.stdout.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        sys.stdout.flush()
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
