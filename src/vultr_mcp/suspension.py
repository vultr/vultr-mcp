"""Keep a suspended Vultr grant's session alive, so restoring access needs no reconnect.

Vultr suspends rather than revokes a grant when MCP access is lost. For ``SUSPENSION_GRACE_S`` the
proxy keeps such a session: tool calls answer with Vultr's reason, refreshes reissue proxy tokens
over the same grant, and each request still asks Vultr first, so restored access applies at once.
"""

from __future__ import annotations

import base64
import json
from contextvars import ContextVar
from typing import Any

import httpx

# The refusals Vultr gives a suspended MCP grant, matched exactly so any other refusal stays terminal.
SUSPENSION_REASONS: dict[str, str] = {
    "The Vultr MCP server is not available to the public yet.": "mcp_unavailable",
    "Access Denied. MCP access is not enabled for your organization. "
    "Ask an administrator of your organization to enable it in your organization's settings.": "org_disabled",
    "MCP access is not currently granted to your Vultr user. "
    "An administrator of your Vultr organization can restore it, and this connection resumes when they do.": "user_access_removed",
}

# How long after the session last had working access a suspension keeps it alive.
SUSPENSION_GRACE_S = 24 * 3600

# Claims on a held session's AccessToken: the message tool calls answer with, and its short reason for the audit log.
SUSPENDED_MESSAGE_CLAIM = "vultr_suspended"
SUSPENDED_REASON_CLAIM = "vultr_suspended_reason"


class UpstreamSuspended(Exception):
    """Vultr refused an upstream refresh because the grant is suspended."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


def suspension_in(exc: BaseException) -> UpstreamSuspended | None:
    """The suspension Vultr reported in a refused refresh, or None for any other failure."""
    texts = [str(exc)] + [str(getattr(exc, attr, "") or "") for attr in ("error", "description", "error_description")]
    for message, reason in SUSPENSION_REASONS.items():
        if any(message in text for text in texts):
            return UpstreamSuspended(message, reason)
    return None


# Set per request by the proxy, so the upstream client wrapper can report a suspension back through FastMCP's own handling.
SUSPENSION_SEEN: ContextVar[list[UpstreamSuspended] | None] = ContextVar("vultr_suspension_seen", default=None)

# Set only while the proxy re-issues a held session's tokens: the token response the wrapper returns instead of calling Vultr.
HELD_REFRESH_RESPONSE: ContextVar[dict[str, Any] | None] = ContextVar("vultr_held_refresh_response", default=None)


class SuspensionAwareClient:
    """The upstream OAuth client, reporting Vultr's suspension refusals as ``UpstreamSuspended``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def refresh_token(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        held = HELD_REFRESH_RESPONSE.get()
        if held is not None:
            return dict(held)
        try:
            return await self._inner.refresh_token(*args, **kwargs)
        except Exception as exc:
            suspended = suspension_in(exc)
            if suspended is None:
                raise
            seen = SUSPENSION_SEEN.get()
            if seen is not None:
                seen.append(suspended)
            raise suspended from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def unverified_claims(token: str) -> dict[str, Any]:
    """A JWT's payload without verifying it: only for a token the proxy itself stored."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - not a JWT, or not ours to read
        return {}


def current_suspension() -> str | None:
    """Vultr's suspension message when this request's session is held, else None."""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001 - no request in scope
        return None
    claims = getattr(token, "claims", None) or {}
    message = claims.get(SUSPENDED_MESSAGE_CLAIM)
    return message if isinstance(message, str) else None


class SuspendedTransport(httpx.AsyncBaseTransport):
    """Answers a held session's API calls with Vultr's suspension refusal instead of sending its expired token."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        message = current_suspension()
        if message is not None:
            return httpx.Response(403, json={"error": message}, request=request)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
