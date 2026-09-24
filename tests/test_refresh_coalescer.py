"""One upstream refresh per refresh token.

The failure these guard against was seen in production on 2026-09-23 19:05:21:
a client refreshed twice at once, the two requests reached different pods, one
got new tokens and the other ``Invalid refresh token`` -- and because Vultr
revokes the whole token family on reuse, the winner's new refresh token died
too, and the user had to sign in again. The tests pin that a second refresh of
the same token never becomes a second upstream call.
"""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from vultr_mcp import refresh_coalescer as rc
from vultr_mcp.refresh_coalescer import MemoryBackend, RedisBackend, RefreshCoalescer, token_key

FERNET = Fernet(Fernet.generate_key())


def _token(n: int) -> OAuthToken:
    return OAuthToken(access_token=f"access-{n}", token_type="Bearer", expires_in=3600,
                      refresh_token=f"refresh-{n}", scope="read")


class _Upstream:
    """Counts the refreshes that would have reached Vultr."""

    def __init__(self, delay: float = 0.2, fail: bool = False) -> None:
        self.calls = 0
        self.delay = delay
        self.fail = fail

    async def __call__(self) -> OAuthToken:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TokenError("invalid_grant", "Upstream refresh failed: Invalid refresh token")
        return _token(self.calls)


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(rc, "POLL_S", 0.01)


def _coalescer(backend=None) -> RefreshCoalescer:
    return RefreshCoalescer(backend or MemoryBackend(), FERNET)


# -- the coalescer on its own --------------------------------------------------


async def test_concurrent_refreshes_make_one_upstream_call():
    c, up = _coalescer(), _Upstream()
    a, b = await asyncio.gather(c.run("k", "client", ["read"], up), c.run("k", "client", ["read"], up))
    assert up.calls == 1
    assert a == b


async def test_a_late_duplicate_gets_the_same_tokens():
    """A duplicate that queued behind a slow request, arriving after the winner finished."""
    c, up = _coalescer(), _Upstream(delay=0)
    first = await c.run("k", "client", ["read"], up)
    again = await c.run("k", "client", ["read"], up)
    assert up.calls == 1
    assert again == first


async def test_a_different_token_is_refreshed_on_its_own():
    c, up = _coalescer(), _Upstream(delay=0)
    await c.run("k1", "client", ["read"], up)
    await c.run("k2", "client", ["read"], up)
    assert up.calls == 2


async def test_another_client_never_collects_the_result():
    """Bound to client_id as well as the token: the result is only ever what
    the presenting client would have got by going first."""
    c, up = _coalescer(), _Upstream(delay=0)
    await c.run("k", "client-a", ["read"], up)
    await c.run("k", "client-b", ["read"], up)
    assert up.calls == 2


async def test_a_failed_refresh_fails_the_duplicate_the_same_way_without_retrying():
    c, up = _coalescer(), _Upstream(fail=True)
    results = await asyncio.gather(
        c.run("k", "client", ["read"], up), c.run("k", "client", ["read"], up), return_exceptions=True
    )
    assert up.calls == 1
    assert all(isinstance(r, TokenError) and r.error == "invalid_grant" for r in results)


async def test_a_vanished_lock_holder_does_not_strand_the_waiter():
    """The holder died without an answer: once its lock is gone, refresh directly."""
    backend = MemoryBackend()
    c, up = _coalescer(backend), _Upstream(delay=0)
    await backend.acquire("vultr-mcp:refresh:k:lock", b'{"client_id":"client","scopes":["read"]}', 150)
    token = await c.run("k", "client", ["read"], up)
    assert up.calls == 1
    assert token == _token(1)


async def test_an_unreachable_store_falls_back_to_a_direct_refresh():
    class _Down:
        async def get(self, key):
            raise ConnectionError("redis down")

        put = acquire = release = get

    c, up = _coalescer(_Down()), _Upstream(delay=0)
    assert await c.run("k", "client", ["read"], up) == _token(1)
    assert up.calls == 1


async def test_tokens_are_encrypted_at_rest():
    backend = MemoryBackend()
    c = _coalescer(backend)
    await c.run("k", "client", ["read"], _Upstream(delay=0))
    stored = await backend.get("vultr-mcp:refresh:k:result")
    assert b"access-1" not in stored and b"refresh-1" not in stored


async def test_claimed_scopes_cover_in_flight_and_finished_refreshes():
    c, up = _coalescer(), _Upstream(delay=0.2)
    assert await c.claimed_scopes("k", "client") is None
    task = asyncio.create_task(c.run("k", "client", ["read"], up))
    await asyncio.sleep(0.05)
    assert await c.claimed_scopes("k", "client") == ["read"]  # in flight
    await task
    assert await c.claimed_scopes("k", "client") == ["read"]  # finished
    assert await c.claimed_scopes("k", "someone-else") is None


def test_the_key_is_a_hash_not_the_token():
    assert token_key("secret-refresh") != "secret-refresh"
    assert len(token_key("secret-refresh")) == 64


# -- two pods sharing Redis ------------------------------------------------------


class _FakeRedis:
    """The four commands RedisBackend uses, with Redis's semantics for them."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[bytes, float]] = {}

    def _live(self, key):
        import time

        item = self.data.get(key)
        if item and item[1] < time.monotonic():
            del self.data[key]
            return None
        return item[0] if item else None

    async def get(self, key):
        return self._live(key)

    async def set(self, key, value, ex=None, px=None, nx=False):
        import time

        if nx and self._live(key) is not None:
            return None
        ttl = ex if ex is not None else (px / 1000 if px is not None else 1e9)
        self.data[key] = (value, time.monotonic() + ttl)
        return True

    async def eval(self, script, numkeys, key, owner):
        assert script == rc._RELEASE  # the compare-and-delete, emulated
        if self._live(key) == owner:
            del self.data[key]
            return 1
        return 0


async def test_two_pods_sharing_redis_make_one_upstream_call():
    """The production shape: the two refreshes land on different replicas."""
    shared = _FakeRedis()
    pod_a, pod_b = _coalescer(RedisBackend(shared)), _coalescer(RedisBackend(shared))
    up = _Upstream()
    a, b = await asyncio.gather(pod_a.run("k", "client", ["read"], up), pod_b.run("k", "client", ["read"], up))
    assert up.calls == 1
    assert a == b


async def test_a_lock_is_released_only_by_its_owner():
    shared = _FakeRedis()
    backend = RedisBackend(shared)
    assert await backend.acquire("lock", b"mine", 30_000)
    await backend.release("lock", b"not-mine")
    assert await shared.get("lock") == b"mine"
    await backend.release("lock", b"mine")
    assert await shared.get("lock") is None


# -- through VultrOAuthProxy, as the token handler calls it ----------------------


def _proxy(monkeypatch):
    from vultr_mcp.auth import build_auth

    from tests.test_auth import FAKE, _enable_env  # noqa: PLC0415 - reuse the fixtures

    _enable_env(monkeypatch)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    return build_auth(endpoints=FAKE)


async def test_the_proxy_turns_a_cross_pod_race_into_one_upstream_refresh(monkeypatch):
    """Two replicas, one refresh token presented to both at once, then once more
    after the winner has rotated it out of the store -- all served one refresh."""
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    up = _Upstream()
    live = {"refresh-0"}  # FastMCP's store: the winner deletes the old token

    async def base_load(self, client, refresh_token):
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=["read"]) if refresh_token in live else None

    async def base_exchange(self, client, refresh_token, scopes):
        token = await up()
        live.discard(refresh_token.token)
        return token

    monkeypatch.setattr(OAuthProxy, "load_refresh_token", base_load)
    monkeypatch.setattr(OAuthProxy, "exchange_refresh_token", base_exchange)

    pod_a, pod_b = _proxy(monkeypatch), _proxy(monkeypatch)
    shared = MemoryBackend()  # stands in for Redis
    pod_a._refresh_coalescer._backend = shared
    pod_b._refresh_coalescer._backend = shared
    client = OAuthClientInformationFull(client_id="client", redirect_uris=["http://127.0.0.1:9/cb"])

    async def refresh(pod):
        # What mcp's TokenHandler does for grant_type=refresh_token.
        loaded = await pod.load_refresh_token(client, "refresh-0")
        assert loaded is not None, "refused before reaching the exchange"
        return await pod.exchange_refresh_token(client, loaded, loaded.scopes)

    a, b = await asyncio.gather(refresh(pod_a), refresh(pod_b))
    late = await refresh(pod_b)  # the old token is gone from the store by now

    assert up.calls == 1
    assert a == b == late


# -- how each refresh resolved, as the audit record will say ---------------------


async def test_the_coalescer_reports_how_each_refresh_resolved():
    c, up = _coalescer(), _Upstream()
    first, second = [], []
    await asyncio.gather(
        c.run("k", "client", ["read"], up, report=first.append),
        c.run("k", "client", ["read"], up, report=second.append),
    )
    late = []
    await c.run("k", "client", ["read"], up, report=late.append)
    assert sorted(first + second) == ["shared_inflight", "upstream"]
    assert late == ["shared_finished"]


async def test_a_store_outage_is_reported_as_a_direct_refresh():
    class _Down:
        async def get(self, key):
            raise ConnectionError("redis down")

        put = acquire = release = get

    seen = []
    await _coalescer(_Down()).run("k", "client", ["read"], _Upstream(delay=0), report=seen.append)
    assert seen == ["direct"]


# -- the mcp.auth records the proxy emits ----------------------------------------


@pytest.fixture
def auth_records(monkeypatch):
    """Every mcp.auth record, as audit.emit would write it."""
    from vultr_mcp import audit

    records: list[dict] = []
    monkeypatch.setattr(audit, "emit", lambda r: records.append(r) if r.get("event") == "mcp.auth" else None)
    monkeypatch.setenv("VULTR_MCP_AUDIT_LOG", "true")
    return records


def _stub_base(monkeypatch, up, live, claims=None, fail=False):
    """Stand in for FastMCP's own refresh machinery beneath the overrides."""
    from fastmcp.server.auth.auth import AccessToken
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    async def base_load(self, client, refresh_token):
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=["read"]) if refresh_token in live else None

    async def base_exchange(self, client, refresh_token, scopes):
        token = await up()
        live.discard(refresh_token.token)
        return token

    async def base_access(self, token):
        return AccessToken(token=token, client_id="client", scopes=["read"], claims=claims or {})

    monkeypatch.setattr(OAuthProxy, "load_refresh_token", base_load)
    monkeypatch.setattr(OAuthProxy, "exchange_refresh_token", base_exchange)
    monkeypatch.setattr(OAuthProxy, "load_access_token", base_access)


CLIENT = OAuthClientInformationFull(client_id="client", redirect_uris=["http://127.0.0.1:9/cb"])


async def _refresh(pod, token="refresh-0"):
    loaded = await pod.load_refresh_token(CLIENT, token)
    if loaded is None:
        return None
    return await pod.exchange_refresh_token(CLIENT, loaded, loaded.scopes)


async def test_a_race_is_recorded_as_one_upstream_refresh_and_its_shared_duplicates(monkeypatch, auth_records):
    up = _Upstream()
    _stub_base(monkeypatch, up, {"refresh-0"}, claims={"acctid": 6716887, "sub": "user-1"})
    pod_a, pod_b = _proxy(monkeypatch), _proxy(monkeypatch)
    pod_b._refresh_coalescer._backend = pod_a._refresh_coalescer._backend

    await asyncio.gather(_refresh(pod_a), _refresh(pod_b))
    await _refresh(pod_b)

    refreshes = [r for r in auth_records if r["action"] == "refresh"]
    assert [r["outcome"] for r in refreshes] == ["ok", "ok", "ok"]
    assert sorted(r["resolved_by"] for r in refreshes) == ["shared_finished", "shared_inflight", "upstream"]
    # The account, as the tool-call records name it; never a token.
    assert all(r["acctid"] == 6716887 and r["sub"] == "user-1" for r in refreshes)
    assert not any("access-" in str(r) or "refresh-" in str(r) for r in refreshes)


async def test_an_unknown_refresh_token_is_recorded_as_refused(monkeypatch, auth_records):
    _stub_base(monkeypatch, _Upstream(delay=0), live=set())
    assert await _refresh(_proxy(monkeypatch), "refresh-gone") is None
    assert auth_records[-1]["action"] == "refresh"
    assert auth_records[-1]["outcome"] == "refused"
    assert auth_records[-1]["error"] == "invalid_grant"


async def test_a_failed_refresh_records_what_vultr_said(monkeypatch, auth_records):
    _stub_base(monkeypatch, _Upstream(delay=0, fail=True), {"refresh-0"})
    with pytest.raises(TokenError):
        await _refresh(_proxy(monkeypatch))
    record = auth_records[-1]
    assert (record["action"], record["outcome"], record["resolved_by"]) == ("refresh", "error", "upstream")
    assert "Invalid refresh token" in record["error_description"]


async def test_sign_ins_are_recorded(monkeypatch, auth_records):
    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    _stub_base(monkeypatch, _Upstream(delay=0), set(), claims={"acctid": 42})

    async def base_authorize(self, client, params):
        return "https://my.vultr.com/oauth/authorize?x=1"

    async def base_code(self, client, code):
        return _token(9)

    monkeypatch.setattr(OAuthProxy, "authorize", base_authorize)
    monkeypatch.setattr(OAuthProxy, "exchange_authorization_code", base_code)
    pod = _proxy(monkeypatch)
    await pod.authorize(CLIENT, None)
    await pod.exchange_authorization_code(CLIENT, None)
    assert [(r["action"], r["outcome"]) for r in auth_records] == [("authorize", "ok"), ("code_exchange", "ok")]
    assert auth_records[-1]["acctid"] == 42
    assert auth_records[0]["client_kind"] == "registered"


def test_auditing_off_emits_nothing(monkeypatch):
    from vultr_mcp import audit

    seen = []
    monkeypatch.setattr(audit, "emit", seen.append)
    monkeypatch.setenv("VULTR_MCP_AUDIT_LOG", "false")
    audit.emit_auth("refresh", "ok", "client", 0.0)
    assert seen == []
