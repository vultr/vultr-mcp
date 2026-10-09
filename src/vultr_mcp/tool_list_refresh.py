"""Tell connected clients to refetch the tool list after a release, without a reconnect.

The HTTP app runs stateless behind a round-robin ingress, so a deploy breaks no
connection -- but a client keeps the ``tools/list`` it fetched when it connected,
and after a release it offers removed tools, misses new ones and reads old
descriptions until the user reconnects. MCP's signal for this is
``notifications/tools/list_changed``. A stateless server has no session stream to
push it on, so it rides the response of the client's own ``tools/call``: sent with
the call's request id, the transport puts it on that call's response stream, and
Claude Code refetches the list straight away. Sent without the request id -- the
SDK's own ``send_tool_list_changed()`` -- it is silently dropped.

Each caller is told once per change to the tool list, not on every call: the
digest of the list it was last told about is kept per caller and endpoint, in the
shared Redis so every replica agrees. A call naming a tool that no longer exists
is told regardless, with an error that says why.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import mcp.types as types
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.utilities.logging import get_logger

from vultr_mcp.refresh_coalescer import Backend, MemoryBackend, RedisBackend, redis_from_env

logger = get_logger(__name__)

_PREFIX = "vultr-mcp:tools-seen"

# Longer than the 30-day refresh window, so a caller still connected is never forgotten.
SEEN_TTL_S = 45 * 24 * 3600

# Callers already confirmed current in this process, so a busy client costs one store read per release, not per call.
_CONFIRMED_CAP = 50_000

UNKNOWN_TOOL_MESSAGE = (
    "Unknown tool: {name!r}. The Vultr MCP server's tools changed in an update; "
    "your client has been asked to refresh its tool list. Check the current tools and try again."
)


def _caller_and_endpoint() -> tuple[str, str] | None:
    """Who is calling and on which endpoint, or None outside an authenticated HTTP request.

    A FastMCP proxy token names the client registration it was issued to, which
    survives every refresh; a raw API key has no such claim, so the key's own
    hash stands in. Both are hashed before use, so no credential is stored.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception:  # noqa: BLE001 - STDIO and tests have no HTTP request
        return None

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    bearer = auth[7:].strip()
    caller = "key:" + bearer
    parts = bearer.split(".")
    if len(parts) == 3:
        try:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
            if isinstance(claims, dict) and claims.get("client_id"):
                caller = "client:" + str(claims["client_id"])
        except Exception:  # noqa: BLE001 - an unreadable token keeps the hash
            pass
    return caller, request.url.path.rstrip("/") or "/"


def _seen_key(caller: str, endpoint: str) -> str:
    return f"{_PREFIX}:{hashlib.sha256(f'{endpoint}|{caller}'.encode()).hexdigest()}"


async def _tool_list_digest(server: Any) -> str:
    """A digest of what tools/list returns: names, descriptions and input schemas."""
    tools = await server.list_tools()
    listing = sorted(
        (tool.name, tool.description or "", json.dumps(tool.parameters or {}, sort_keys=True))
        for tool in tools
    )
    return hashlib.sha256(json.dumps(listing).encode()).hexdigest()


async def _send_list_changed(fastmcp_context: Any) -> None:
    """Put notifications/tools/list_changed on the current call's response stream."""
    request_context = fastmcp_context.request_context
    await fastmcp_context.session.send_notification(
        types.ServerNotification(types.ToolListChangedNotification()),
        related_request_id=request_context.request_id,
    )


class ToolListRefreshMiddleware(Middleware):
    """On a caller's first tool call after its tool list changed, tell it to refetch."""

    def __init__(self, store: Backend | None = None) -> None:
        super().__init__()
        self._store = store if store is not None else build_seen_store()
        self._digest: str | None = None
        self._confirmed: set[str] = set()

    async def on_call_tool(self, context, call_next):
        fastmcp_context = getattr(context, "fastmcp_context", None)
        identity = _caller_and_endpoint()
        told = False
        if fastmcp_context is not None and identity is not None:
            told = await self._tell_if_stale(fastmcp_context, _seen_key(*identity))

        try:
            return await call_next(context)
        except NotFoundError:
            name = getattr(context.message, "name", None)
            if fastmcp_context is not None and not told:
                try:
                    await _send_list_changed(fastmcp_context)
                except Exception:  # noqa: BLE001 - the error below still reaches the client
                    logger.warning("tool list refresh: could not notify for an unknown tool", exc_info=True)
            # A ToolError, because FastMCP re-wraps a NotFoundError in its own bare message.
            raise ToolError(UNKNOWN_TOOL_MESSAGE.format(name=name)) from None

    async def _tell_if_stale(self, fastmcp_context: Any, key: str) -> bool:
        """Send list_changed if this caller was last told about a different list; never fails the call."""
        try:
            if self._digest is None:
                self._digest = await _tool_list_digest(fastmcp_context.fastmcp)
            if key in self._confirmed:
                return False

            digest = self._digest.encode()
            if await self._store.get(key) != digest:
                await _send_list_changed(fastmcp_context)
                await self._store.put(key, digest, SEEN_TTL_S)
                told = True
            else:
                told = False

            if len(self._confirmed) >= _CONFIRMED_CAP:
                self._confirmed.clear()
            self._confirmed.add(key)
            return told
        except Exception:  # noqa: BLE001 - a store outage must not fail a tool call
            logger.warning("tool list refresh: check failed; the call goes ahead", exc_info=True)
            return False


_SHARED_STORE: Backend | None = None


def build_seen_store() -> Backend:
    """Redis when the deployment shares one, so every replica agrees; else in-process. One per process, shared by every endpoint's server."""
    global _SHARED_STORE
    if _SHARED_STORE is None:
        client = redis_from_env()
        _SHARED_STORE = MemoryBackend() if client is None else RedisBackend(client)
    return _SHARED_STORE
