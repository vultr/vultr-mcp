"""One upstream refresh per refresh token, however many times and on whichever pod it is presented.

Vultr treats a reused refresh token as theft and revokes the whole token family, so the first
refresh takes a lock and leaves its (encrypted) result for duplicates to collect, keyed by a hash of
the token and bound to its client_id. Needs the shared Redis across replicas; without it, or when
Redis is down, refreshes go straight through.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Protocol

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.utilities.logging import get_logger
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthToken

logger = get_logger(__name__)

# How long a finished refresh, or a failed one, stays collectable by duplicates.
RESULT_TTL_S = 60
ERROR_TTL_S = 10
LOCK_TTL_MS = 30_000
WAIT_S = 20.0
POLL_S = 0.1

_PREFIX = "vultr-mcp:refresh"

# Waits between attempts when Vultr's token endpoint fails transiently; its top-of-hour 502s fail in ~90 ms and clear within seconds.
UPSTREAM_RETRY_DELAYS_S = (1.0, 3.0)
# Gateway failures, which answer before the refresh token is spent, unlike a timeout after the request was sent.
TRANSIENT_STATUSES = frozenset({502, 503, 504})
# The owner bound to an upstream result: only this proxy ever holds an upstream refresh token.
_UPSTREAM_OWNER = "upstream"


# How each upstream refresh in this request resolved, for the audit record of the refresh that asked for it.
UPSTREAM_RESOLUTION: ContextVar[list[str] | None] = ContextVar("vultr_upstream_resolution", default=None)


def _resolved(how: str) -> None:
    seen = UPSTREAM_RESOLUTION.get()
    if seen is not None:
        seen.append(how)


class UpstreamUnavailable(Exception):
    """Vultr's token endpoint failed transiently on every attempt; the grant and its refresh token are untouched."""

    # The OAuth error a client is given for it, so it keeps its tokens and retries instead of signing in again.
    error = "temporarily_unavailable"


def is_transient(exc: BaseException) -> bool:
    """A failure that never reached Vultr's refresh logic, so presenting the same refresh token again is safe."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUSES
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


async def refresh_with_retries(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """One upstream refresh, retried briefly on a transient failure and then reported as ``UpstreamUnavailable``."""
    for delay in (*UPSTREAM_RETRY_DELAYS_S, None):
        try:
            return await call()
        except Exception as exc:
            if not is_transient(exc):
                raise
            if delay is None:
                logger.warning("upstream refresh still failing after retries: %s", exc)
                raise UpstreamUnavailable(str(exc)) from exc
            logger.warning("upstream refresh failed transiently (%s); retrying in %.0fs", exc, delay)
            await anyio.sleep(delay)
    raise AssertionError("the final attempt always returns or raises")


class Backend(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def put(self, key: str, value: bytes, ttl_s: int) -> None: ...
    async def acquire(self, key: str, value: bytes, ttl_ms: int) -> bool: ...
    async def release(self, key: str, owner_value: bytes) -> None: ...


class MemoryBackend:
    """One process: tests, local runs, a single replica."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float]] = {}

    def _live(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        if item[1] < time.monotonic():
            del self._data[key]
            return None
        return item[0]

    async def get(self, key: str) -> bytes | None:
        return self._live(key)

    async def put(self, key: str, value: bytes, ttl_s: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl_s)

    async def acquire(self, key: str, value: bytes, ttl_ms: int) -> bool:
        if self._live(key) is not None:
            return False
        self._data[key] = (value, time.monotonic() + ttl_ms / 1000)
        return True

    async def release(self, key: str, owner_value: bytes) -> None:
        if self._live(key) == owner_value:
            del self._data[key]


# Delete the lock only if it is still ours.
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisBackend:
    """Every replica, through the Redis the OAuth store already uses."""

    def __init__(self, client: Any) -> None:
        self._r = client

    async def get(self, key: str) -> bytes | None:
        return await self._r.get(key)

    async def put(self, key: str, value: bytes, ttl_s: int) -> None:
        await self._r.set(key, value, ex=ttl_s)

    async def acquire(self, key: str, value: bytes, ttl_ms: int) -> bool:
        return bool(await self._r.set(key, value, nx=True, px=ttl_ms))

    async def release(self, key: str, owner_value: bytes) -> None:
        await self._r.eval(_RELEASE, 1, key, owner_value)


def token_key(refresh_token: str) -> str:
    """Never the token itself: a hash, the same way FastMCP keys its store."""
    return hashlib.sha256(refresh_token.encode()).hexdigest()


class RefreshCoalescer:
    def __init__(self, backend: Backend, fernet: Fernet) -> None:
        self._backend = backend
        self._fernet = fernet

    # -- storage, each step failing soft --------------------------------------

    async def _result(self, key: str, client_id: str) -> dict[str, Any] | None:
        try:
            raw = await self._backend.get(f"{_PREFIX}:{key}:result")
        except Exception as exc:  # noqa: BLE001 - a store outage must not fail a refresh
            logger.warning("refresh coalescer: result read failed (%s)", type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            entry = json.loads(self._fernet.decrypt(raw))
        except (InvalidToken, ValueError):
            return None
        return entry if entry.get("client_id") == client_id else None

    async def _lock(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._backend.get(f"{_PREFIX}:{key}:lock")
        except Exception:  # noqa: BLE001
            return None
        return json.loads(raw) if raw else None

    async def _store(self, key: str, entry: dict[str, Any], ttl_s: int) -> None:
        try:
            await self._backend.put(
                f"{_PREFIX}:{key}:result", self._fernet.encrypt(json.dumps(entry).encode()), ttl_s
            )
        except Exception as exc:  # noqa: BLE001 - the caller still gets its tokens
            logger.warning("refresh coalescer: result write failed (%s)", type(exc).__name__)

    @staticmethod
    def _unpack(entry: dict[str, Any]) -> OAuthToken:
        if "error" in entry:
            code, description = entry["error"]
            raise TokenError(code, description)
        return OAuthToken.model_validate(entry["token"])

    # -- the two things the proxy asks ----------------------------------------

    async def claimed_scopes(self, key: str, client_id: str) -> list[str] | None:
        """Scopes of an in-flight or just-finished refresh of this token, or None if there is nothing to collect."""
        entry = await self._result(key, client_id)
        if entry is not None and "token" in entry:
            return entry["scopes"]
        lock = await self._lock(key)
        if lock is not None and lock.get("client_id") == client_id:
            return lock["scopes"]
        return None

    async def run(
        self,
        key: str,
        client_id: str,
        scopes: list[str],
        exchange: Callable[[], Awaitable[OAuthToken]],
        report: Callable[[str], None] = lambda _: None,
    ) -> OAuthToken:
        """Run or collect a client refresh; ``report`` hears upstream, shared_finished, shared_inflight or direct."""
        entry = await self._result(key, client_id)
        if entry is not None:
            logger.info("refresh coalescer: served a duplicate refresh from a finished one")
            report("shared_finished")
            return self._unpack(entry)

        owner = json.dumps({"owner": uuid.uuid4().hex, "client_id": client_id, "scopes": scopes}).encode()
        lock_key = f"{_PREFIX}:{key}:lock"
        try:
            acquired = await self._backend.acquire(lock_key, owner, LOCK_TTL_MS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh coalescer unavailable (%s); refreshing directly", type(exc).__name__)
            report("direct")
            return await exchange()

        if acquired:
            report("upstream")
            try:
                try:
                    token = await exchange()
                except TokenError as exc:
                    await self._store(key, {"client_id": client_id, "error": [exc.error, exc.error_description]}, ERROR_TTL_S)
                    raise
                granted = token.scope.split() if token.scope else scopes
                await self._store(
                    key, {"client_id": client_id, "scopes": granted, "token": token.model_dump(mode="json")}, RESULT_TTL_S
                )
                return token
            finally:
                try:
                    await self._backend.release(lock_key, owner)
                except Exception:  # noqa: BLE001 - it expires on its own
                    pass

        # Someone else is refreshing this token: wait for their answer.
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            await anyio.sleep(POLL_S)
            entry = await self._result(key, client_id)
            if entry is not None:
                logger.info("refresh coalescer: served a concurrent duplicate refresh")
                report("shared_inflight")
                return self._unpack(entry)
            if await self._lock(key) is None:
                break  # holder gone without an answer (died, or its write failed)
        logger.warning("refresh coalescer: no result from the concurrent refresh; refreshing directly")
        report("direct")
        return await exchange()

    async def run_upstream(self, key: str, call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """One Vultr refresh per upstream refresh token, shared by every path and pod; only successes are shared."""
        upstream_key = f"upstream:{key}"
        entry = await self._result(upstream_key, _UPSTREAM_OWNER)
        if entry is not None:
            logger.info("refresh coalescer: served an upstream refresh another path just made")
            _resolved("shared_finished")
            return entry["response"]

        owner = json.dumps({"owner": uuid.uuid4().hex, "client_id": _UPSTREAM_OWNER}).encode()
        lock_key = f"{_PREFIX}:{upstream_key}:lock"
        try:
            acquired = await self._backend.acquire(lock_key, owner, LOCK_TTL_MS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh coalescer unavailable (%s); refreshing upstream directly", type(exc).__name__)
            _resolved("direct")
            return await call()

        if acquired:
            _resolved("upstream")
            try:
                response = await call()
                await self._store(upstream_key, {"client_id": _UPSTREAM_OWNER, "response": response}, RESULT_TTL_S)
                return response
            finally:
                try:
                    await self._backend.release(lock_key, owner)
                except Exception:  # noqa: BLE001 - it expires on its own
                    pass

        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            await anyio.sleep(POLL_S)
            entry = await self._result(upstream_key, _UPSTREAM_OWNER)
            if entry is not None:
                logger.info("refresh coalescer: served a concurrent upstream refresh")
                _resolved("shared_inflight")
                return entry["response"]
            if await self._lock(upstream_key) is None:
                break
        _resolved("direct")
        return await call()


class CoalescedUpstreamClient:
    """The upstream OAuth client, refreshing each upstream token once across pods and riding out brief Vultr outages."""

    def __init__(self, inner: Any, coalescer: RefreshCoalescer) -> None:
        self._inner = inner
        self._coalescer = coalescer

    async def refresh_token(self, url: str, refresh_token: str | None = None, **kwargs: Any) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            return await self._inner.refresh_token(url, refresh_token=refresh_token, **kwargs)

        if not refresh_token:
            return await attempt()
        return await self._coalescer.run_upstream(token_key(refresh_token), lambda: refresh_with_retries(attempt))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def build_refresh_coalescer(upstream_client_secret: str | None) -> RefreshCoalescer:
    """Redis when the deployment shares one (every replica agrees), else in-process."""
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    # Derived from the OAuth client secret, so it is stable across pods and restarts.
    material = upstream_client_secret or uuid.uuid4().hex
    fernet = Fernet(derive_jwt_key(high_entropy_material=material, salt="vultr-mcp-refresh-coalescer"))

    client = redis_from_env()
    return RefreshCoalescer(MemoryBackend() if client is None else RedisBackend(client), fernet)


def redis_from_env() -> Any | None:
    """The shared Redis client when REDIS_HOST is set, else None; short timeouts so an outage degrades rather than stalls."""
    host = os.environ.get("REDIS_HOST")
    if not host:
        return None
    import redis.asyncio as redis

    return redis.Redis(
        host=host,
        port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=2,
        socket_connect_timeout=2,
    )
