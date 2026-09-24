"""One upstream refresh per refresh token, however many times it is presented.

Vultr rotates refresh tokens and treats a second use of one as theft: the loser
of the rotation race gets ``invalid_grant`` and the whole token family is
revoked -- including the successor the winner was just handed
(vultr.com ``classes/OIDC/Controller/Provider.php``, ``tokenFromRefreshToken``).
A client whose access token lapses with two requests in flight refreshes twice.
Behind two replicas those land on different pods, where FastMCP's refresh lock
(per process) never sees both, and the user is signed out within the hour.

So the first refresh of a token takes a lock, does the real exchange, and leaves
its result behind for a short window; any other refresh of the same token -- on
any pod -- collects that result instead of calling upstream again. Keyed by a
hash of the presented token and bound to its client_id: only whoever holds that
exact token can collect it, which is what they would have got by presenting it
first. The tokens are Fernet-encrypted at rest, like FastMCP's own stored tokens.

Across replicas this needs the shared Redis (``REDIS_HOST``). Without it, or
when Redis is unreachable, a refresh goes straight through -- the behaviour
before this existed, never worse.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Protocol

import anyio
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.utilities.logging import get_logger
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthToken

# FastMCP's logger, so these land in the pod log beside its own refresh lines.
logger = get_logger(__name__)

# How long a finished refresh stays collectable. Long enough for a duplicate
# that queued behind a slow request; short enough that a stolen token is not a
# standing way in.
RESULT_TTL_S = 60
# A failed refresh is shared too, so the duplicate fails fast and identically
# instead of repeating a doomed upstream call.
ERROR_TTL_S = 10
# The lock outlives any sane upstream refresh; if its holder dies, waiters stop
# waiting once it expires.
LOCK_TTL_MS = 30_000
WAIT_S = 20.0
POLL_S = 0.1

_PREFIX = "vultr-mcp:refresh"


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


# Delete the lock only if it is still ours: a holder that overran the TTL must
# not release the next holder's lock.
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
        """Scopes of a refresh of this token that is in flight or just finished.

        For ``load_refresh_token``: once the winner rotates, the old token is
        gone from FastMCP's store, and a duplicate would be refused before it
        ever reached ``exchange_refresh_token``. None means nothing to collect.
        """
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
        """``report`` hears how the refresh resolved, for the audit record:
        ``upstream`` (this call refreshed), ``shared_finished`` / ``shared_inflight``
        (a duplicate, served another call's result), or ``direct`` (no
        coalescing -- store down, or the other call vanished)."""
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


def build_refresh_coalescer(upstream_client_secret: str | None) -> RefreshCoalescer:
    """Redis when the deployment shares one (every replica agrees), else in-process."""
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    # Derived from the OAuth client secret, as FastMCP derives its own storage
    # key: stable across pods and restarts, never configured separately.
    material = upstream_client_secret or uuid.uuid4().hex
    fernet = Fernet(derive_jwt_key(high_entropy_material=material, salt="vultr-mcp-refresh-coalescer"))

    host = os.environ.get("REDIS_HOST")
    if not host:
        return RefreshCoalescer(MemoryBackend(), fernet)
    import redis.asyncio as redis

    client = redis.Redis(
        host=host,
        port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    return RefreshCoalescer(RedisBackend(client), fernet)
