"""RFC 8628 device authorization grant.

A unit test cannot stage the case this exists for -- a browser on one machine
approving for a client on another -- so these pin everything either side of
that hop: the code pair, the poll state machine, single-use and expiry, and
that a GET on the verification page never authorizes anything.

The approval leg is stubbed: these own the device grant, not FastMCP's OAuth.
"""

from __future__ import annotations

import time

import pytest
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from vultr_mcp.auth import _with_device_grant
from vultr_mcp.device_flow import (
    DEFAULT_INTERVAL,
    DEVICE_GRANT_TYPE,
    DeviceFlowHandlers,
    device_routes,
    new_user_code,
    normalize_user_code,
)

BASE_URL = "https://vultrmcp.com"


class StubToken:
    """Stands in for the OAuthToken the proxy returns from an exchange."""

    def model_dump(self, **_kwargs):
        return {
            "access_token": "tok-abc123",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "ref-xyz789",
        }


class StubProxy:
    """The narrow slice of OAuthProxy the device flow actually touches."""

    def __init__(self) -> None:
        self.clients: dict = {}
        self.authorized: list = []
        self.exchanged: list = []

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client):
        self.clients[client.client_id] = client

    async def authorize(self, client, params):
        self.authorized.append((client, params))
        return f"https://my.vultr.com/oauth/authorize?state={params.state}"

    async def load_authorization_code(self, client, code):
        return None if code == "bad-code" else {"code": code}

    async def exchange_authorization_code(self, client, code_obj):
        self.exchanged.append(code_obj)
        return StubToken()


@pytest.fixture
def handlers():
    return DeviceFlowHandlers(StubProxy(), MemoryStore(), base_url=BASE_URL)


async def _delegated(request):
    """Stands in for the proxy's own /token handler.

    A distinctive status so a test can prove a non-device grant reached it —
    and, with it, that the request body survived being inspected on the way.
    """
    return JSONResponse({"grant": "delegated"}, status_code=418)


@pytest.fixture
def client(handlers):
    # /token is not one of device_routes(): the grant is bolted onto the
    # proxy's existing token endpoint. Mount the real wrapper over a stub so
    # these tests cover the wrapper rather than the handler alone.
    routes = [
        *device_routes(handlers),
        Route("/token", _with_device_grant(_delegated, handlers), methods=["POST"]),
    ]
    return TestClient(Starlette(routes=routes))


def _start(client, client_id="openclaw-mcp"):
    resp = client.post("/device_authorization", data={"client_id": client_id})
    assert resp.status_code == 200
    return resp.json()


def _poll(client, device_code):
    return client.post(
        "/token",
        data={"grant_type": DEVICE_GRANT_TYPE, "device_code": device_code},
    )


# -- user codes ---------------------------------------------------------------


def test_user_code_avoids_confusable_characters():
    """These get read off a terminal and retyped into a browser."""
    for _ in range(200):
        code = new_user_code()
        assert len(code) == 9 and code[4] == "-"
        assert not set(code[:4] + code[5:]) & set("01OIL")


@pytest.mark.parametrize(
    "typed", ["BC3D-FG4H", "bc3d-fg4h", "bc3dfg4h", " BC3D FG4H ", "BC3D--FG4H"]
)
def test_user_code_normalization_is_forgiving(typed):
    assert normalize_user_code(typed) == "BC3DFG4H"


# -- the client's half --------------------------------------------------------


def test_device_authorization_returns_rfc8628_shape(client):
    body = _start(client)
    assert set(body) == {
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    }
    assert body["verification_uri"] == f"{BASE_URL}/device"
    assert body["user_code"] in body["verification_uri_complete"]
    assert body["interval"] == DEFAULT_INTERVAL
    assert body["expires_in"] > 0
    # The device code is the secret half and must not be guessable.
    assert len(body["device_code"]) >= 32


def test_device_authorization_requires_client_id(client):
    resp = client.post("/device_authorization", data={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_poll_before_approval_is_pending(client):
    body = _start(client)
    resp = _poll(client, body["device_code"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "authorization_pending"


def test_unknown_device_code_is_expired(client):
    resp = _poll(client, "never-issued")
    assert resp.json()["error"] == "expired_token"


def test_rapid_polling_is_told_to_slow_down(client):
    body = _start(client)
    assert _poll(client, body["device_code"]).json()["error"] == "authorization_pending"
    # Immediately again, inside the advertised interval.
    assert _poll(client, body["device_code"]).json()["error"] == "slow_down"


@pytest.mark.anyio
async def test_expired_code_is_rejected_and_cleared(handlers, client):
    body = _start(client)
    record = await handlers.store.by_device_code(body["device_code"])
    record["expires_at"] = int(time.time()) - 1
    await handlers.store.save(record)

    assert _poll(client, body["device_code"]).json()["error"] == "expired_token"
    assert await handlers.store.by_device_code(body["device_code"]) is None


@pytest.mark.parametrize("grant", ["refresh_token", "authorization_code", ""])
def test_non_device_grants_are_delegated(client, grant):
    """Every other grant must reach the proxy's own handler untouched.

    Inspecting the form consumes the request body, so this also pins the
    replay: the delegated handler has to still be able to read it.
    """
    resp = client.post("/token", data={"grant_type": grant, "code": "abc"})
    assert resp.status_code == 418
    assert resp.json() == {"grant": "delegated"}


# -- the human's half ---------------------------------------------------------


def test_get_verification_page_renders_form(client):
    resp = client.get("/device")
    assert resp.status_code == 200
    assert "Enter the code" in resp.text


def test_get_with_user_code_does_not_authorize(client, handlers):
    """Following a link must never approve anything — RFC 8628 section 5.4.

    A prefetching mail client or a chat preview fetching the completion URL
    would otherwise authorize a device nobody consented to.
    """
    body = _start(client)
    resp = client.get("/device", params={"user_code": body["user_code"]})
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert not handlers._proxy.authorized  # nothing was started


def test_bad_user_code_reprompts(client):
    resp = client.post("/device", data={"user_code": "ZZZZ-ZZZZ"})
    assert resp.status_code == 400
    assert "not valid" in resp.text


def test_approval_redirects_into_the_authorization_code_flow(client, handlers):
    body = _start(client)
    resp = client.post(
        "/device", data={"user_code": body["user_code"]}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://my.vultr.com/oauth/authorize")

    # It ran as the internal client, whose redirect_uri is ours and reachable —
    # not the device's loopback, which is the entire problem being solved.
    client_used, params = handlers._proxy.authorized[0]
    assert str(params.redirect_uri) == f"{BASE_URL}/device/callback"
    assert client_used.client_id == "vultr-mcp-device-flow"


def test_callback_with_unknown_state_is_rejected(client):
    resp = client.get("/device/callback", params={"code": "x", "state": "nope"})
    assert resp.status_code == 400


# -- end to end ---------------------------------------------------------------


def _approve(client, user_code):
    """Drive the human half and return the state the proxy was handed."""
    resp = client.post("/device", data={"user_code": user_code}, follow_redirects=False)
    return resp.headers["location"].split("state=")[1]


def test_happy_path_hands_the_token_to_the_polling_client(client):
    body = _start(client)
    state = _approve(client, body["user_code"])

    done = client.get("/device/callback", params={"code": "vd-good", "state": state})
    assert done.status_code == 200
    assert "Device connected" in done.text

    resp = _poll(client, body["device_code"])
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "tok-abc123"


def test_device_code_is_single_use(client):
    body = _start(client)
    state = _approve(client, body["user_code"])
    client.get("/device/callback", params={"code": "vd-good", "state": state})

    assert _poll(client, body["device_code"]).status_code == 200
    # The record is gone, so a replay is indistinguishable from an expired one.
    assert _poll(client, body["device_code"]).json()["error"] == "expired_token"


def test_user_code_cannot_be_reused_after_approval(client):
    body = _start(client)
    _approve(client, body["user_code"])
    resp = client.post("/device", data={"user_code": body["user_code"]})
    assert resp.status_code == 400
    assert "already been used" in resp.text


def test_denied_authorization_reaches_the_client(client):
    body = _start(client)
    state = _approve(client, body["user_code"])

    denied = client.get("/device/callback", params={"error": "access_denied", "state": state})
    assert denied.status_code == 400

    resp = _poll(client, body["device_code"])
    assert resp.json()["error"] == "access_denied"


def test_failed_code_exchange_does_not_complete_the_flow(client):
    body = _start(client)
    state = _approve(client, body["user_code"])

    resp = client.get("/device/callback", params={"code": "bad-code", "state": state})
    assert resp.status_code == 400
    assert "no longer valid" in resp.text
