"""One structured record per tool call and sign-in, for audit.

Metadata only: argument names, never values; result counts, never content, since responses carry
what the interface layer shapes away. Auditing never breaks a call: every field and the emission
itself are guarded.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.middleware import Middleware

from vultr_mcp import diagnostics
from vultr_mcp.suspension import SUSPENDED_REASON_CLAIM

# The consenting user and the account the call resolves against; nothing else from the token.
IDENTITY_CLAIMS = ("sub", "acctid")


def audit_enabled() -> bool:
    """On unless explicitly disabled; an audit trail that defaults off is not one."""
    return os.environ.get("VULTR_MCP_AUDIT_LOG", "true").lower() not in ("0", "false", "no")


def audit_dir() -> str:
    """Directory for a file copy of the records for a log shipper to tail, or "" for the console only."""
    return os.environ.get("VULTR_MCP_AUDIT_DIR", "").strip()


def audit_file_max_bytes() -> int:
    """Size past which a file is abandoned for the console only, so an undrained file cannot fill its volume."""
    try:
        return max(1, int(os.environ.get("VULTR_MCP_AUDIT_FILE_MAX_MB", "256"))) * 1024 * 1024
    except ValueError:
        return 256 * 1024 * 1024


# Which replica wrote a record; HOSTNAME is the pod name in k8s.
POD = os.environ.get("HOSTNAME") or None

# One file per event type, since the record shapes share almost no fields; None where opening failed.
_writers: dict[str, int | None] = {}


def _writer(event: str) -> int | None:
    """An O_APPEND descriptor for one event type, opened once; None if unconfigured or unopenable.

    Not a logging handler: rotating handlers seek and rename under a tailer, while one whole-line
    write on an O_APPEND descriptor never seeks and always lands at the current end.
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
            # Group-writable, for a tailer that shares the pod's fsGroup and truncates what it shipped.
            0o660,
        )
    except Exception:  # noqa: BLE001
        fd = None

    _writers[event] = fd
    return fd


def _identity() -> dict[str, Any]:
    """The auth method, plus the OAuth subject and account when the token carries them (a raw API key has none)."""
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
        # A held session: Vultr has suspended the grant, and the call was answered with its reason.
        if claims.get(SUSPENDED_REASON_CLAIM):
            out["suspended"] = claims[SUSPENDED_REASON_CLAIM]
        return out
    except Exception:  # noqa: BLE001 - auditing must not break a call
        return {"auth_method": "unknown"}


def _endpoint() -> dict[str, Any]:
    """The path and Host the call came in on, without the query string; empty under STDIO."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        return {
            "endpoint": request.url.path.rstrip("/") or "/",
            "host": request.headers.get("host"),
        }
    except Exception:  # noqa: BLE001 - no HTTP request in scope
        return {}


def _result_size(result: Any) -> int | None:
    """How many items came back, never what they were."""
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
    """Exception class names down the cause chain, since FastMCP wraps every failure in ToolError; never messages."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(names) < limit and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return names


def _console():
    """stdout over HTTP, stderr otherwise, since a STDIO server's stdout is the MCP protocol channel."""
    if os.environ.get("VULTR_MCP_TRANSPORT", "stdio").lower() == "http":
        return sys.stdout
    return sys.stderr


def emit(record: dict[str, Any]) -> None:
    """Write one record as a JSON line to the console (flushed), and to a file when configured; neither failure affects the other."""
    if POD and "pod" not in record:
        record = {**record, "pod": POD}

    try:
        line = json.dumps(record, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return

    try:
        console = _console()
        console.write(line + "\n")
        console.flush()
    except Exception:  # noqa: BLE001
        pass

    try:
        fd = _writer(str(record.get("event", "record")))
        if fd is not None and os.fstat(fd).st_size < audit_file_max_bytes():
            # One write per whole line, so concurrent lines never interleave.
            os.write(fd, (line + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass


def upstream_detail_enabled() -> bool:
    """Per-upstream-call records, on by default; VULTR_MCP_AUDIT_UPSTREAM=false turns off the higher-volume half."""
    return os.environ.get("VULTR_MCP_AUDIT_UPSTREAM", "true").lower() not in ("0", "false", "no")


class AuditMiddleware(Middleware):
    """Emit one ``mcp.tool_call`` record per tool invocation."""

    def __init__(self) -> None:
        super().__init__()
        # Created empty at boot: a tailer that finds a file already holding lines starts from its end.
        _writer("mcp.tool_call")
        if upstream_detail_enabled():
            _writer("mcp.upstream_call")
        _writer(AUTH_EVENT)

    async def on_call_tool(self, context, call_next):
        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        # A fresh list per call, bound before the tool runs, collects its upstream calls.
        upstream: list[dict[str, Any]] = []
        diagnostics.UPSTREAM_CALLS.set(upstream)
        record: dict[str, Any] = {
            "event": "mcp.tool_call",
            "ts": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
        }

        try:
            record["tool"] = getattr(context.message, "name", None)
            # Names only, never values.
            arguments = getattr(context.message, "arguments", None)
            if isinstance(arguments, dict):
                record["argument_names"] = sorted(arguments)
            record.update(_identity())
            record.update(_endpoint())

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

        # Time this server spent, as distinct from time spent waiting on Vultr.
        upstream_ms = record.get("upstream_ms")
        if upstream_ms is not None:
            record["overhead_ms"] = round(record["duration_ms"] - upstream_ms, 1)

        blame = diagnostics.fault(record.get("outcome", "error"), upstream)
        # An unknown argument fails before any upstream call, but it is the caller's fault, not ours.
        if "ArgumentError" in record.get("error_chain", ()):
            blame = "caller"
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
    """Emit, swallowing anything that goes wrong."""
    try:
        emit(record)
    except Exception:  # noqa: BLE001
        pass


# -- sign-in and token refresh --------------------------------------------------

AUTH_EVENT = "mcp.auth"


def emit_auth(
    action: str, outcome: str, client_id: str | None, started: float, **fields: Any
) -> None:
    """One ``mcp.auth`` record for a sign-in, a refresh, or a rejected bearer; never a token.

    ``action``: authorize, code_exchange, refresh, transparent_refresh or access.
    ``outcome``: ok, error, refused, held or rejected.
    """
    if not audit_enabled():
        return
    record: dict[str, Any] = {
        "event": AUTH_EVENT,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "outcome": outcome,
        "client_id": client_id,
        # CIMD clients identify by a URL, registered (DCR) clients by an opaque id.
        "client_kind": "cimd" if (client_id or "").startswith("https://") else "registered",
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    record.update({k: v for k, v in fields.items() if v is not None})
    _emit_safely(record)
