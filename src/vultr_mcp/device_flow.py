"""RFC 8628 device authorization grant, layered over the OAuth proxy.

The authorization-code flow needs the browser and the client on one machine,
because the client listens on its own loopback redirect. An agent on a Vultr
instance reached over SSH breaks that: the browser is on the operator's laptop,
so ``127.0.0.1`` names two hosts and the callback is refused. Here the client
polls instead of listening, and nothing has to reach back to it.

The approval leg is an ordinary authorization-code flow driven from ``/device``
against ``proxy.authorize()``, so consent, the upstream redirect and the IdP
callback all run unchanged -- only the way a human says yes is different.

State shares the proxy's key-value store, which must be Redis across replicas:
the pod that completes an approval is rarely the pod being polled.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Collection name in the shared key-value store. Distinct from the proxy's own
# collections so device state can be inspected or flushed independently.
COLLECTION = "vultr-device-flow"

# RFC 8628 section 6.1 wants a user code that survives being read aloud and
# retyped. Digits and uppercase letters minus everything that gets confused:
# 0/O, 1/I/L, plus vowels so a random draw can't spell something unfortunate.
USER_CODE_ALPHABET = "BCDFGHJKMNPQRSTVWXZ23456789"
USER_CODE_LENGTH = 8

# 15 minutes is long enough to find a browser, short enough that an
# unattended pending code is not worth attacking.
DEFAULT_EXPIRES_IN = 900
# The floor the client is told to respect between polls, in seconds.
DEFAULT_INTERVAL = 5

# Stable id for the internal client the approval leg runs as. The device's own
# client never takes part in that leg -- it has a loopback redirect_uri, which
# is the whole problem -- so approval runs as this one, whose redirect_uri is
# ours and is publicly reachable.
DEVICE_CLIENT_ID = "vultr-mcp-device-flow"


def _now() -> int:
    return int(time.time())


def new_user_code() -> str:
    """A fresh user code, displayed as ``XXXX-XXXX``.

    ~38 bits over the restricted alphabet. That is deliberately far below the
    device code's entropy: it is protected by a 15-minute TTL, single use, and
    the fact that guessing one only reaches a consent screen an attacker still
    has to authenticate through as the victim.
    """
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(value: str) -> str:
    """Fold a typed code to its lookup form.

    People retype these from a terminal into a browser, so accept lowercase,
    missing or extra dashes, and stray whitespace.
    """
    return "".join(ch for ch in (value or "").upper() if ch in USER_CODE_ALPHABET)


def _device_key(device_code: str) -> str:
    """Storage key for a device code.

    Hashed so that a dump of the store -- a Redis inspection, a log of keys --
    does not hand over usable device codes. The code itself is the secret the
    client presents; the store only needs to recognise it.
    """
    return "d:" + hashlib.sha256(device_code.encode()).hexdigest()


def _user_key(user_code: str) -> str:
    return "u:" + normalize_user_code(user_code)


def _txn_key(state: str) -> str:
    return "t:" + state


def _pkce_pair() -> tuple[str, str]:
    """A PKCE verifier/challenge pair for the internal approval leg."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _error(code: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


class DeviceFlowStore:
    """Device-grant state on top of the proxy's key-value store.

    Three record shapes share one collection: ``d:<sha256(device_code)>`` holds
    the pending authorization, ``u:<user_code>`` and ``t:<state>`` point at it
    for the verification page and the callback. Every write carries a TTL, so
    an abandoned flow simply stops existing.
    """

    def __init__(self, storage: Any, *, ttl: int = DEFAULT_EXPIRES_IN) -> None:
        self._storage = storage
        self._ttl = ttl

    async def _get(self, key: str) -> dict[str, Any] | None:
        return await self._storage.get(key=key, collection=COLLECTION)

    async def _put(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None:
        await self._storage.put(
            key=key, value=value, collection=COLLECTION, ttl=ttl or self._ttl
        )

    async def _delete(self, key: str) -> None:
        await self._storage.delete(key=key, collection=COLLECTION)

    async def create(self, *, client_id: str, scope: str | None) -> dict[str, Any]:
        device_code = secrets.token_urlsafe(32)
        user_code = new_user_code()
        record = {
            "device_code": device_code,
            "user_code": user_code,
            "client_id": client_id,
            "scope": scope,
            "status": "pending",
            "created_at": _now(),
            "expires_at": _now() + self._ttl,
            "last_polled_at": 0,
        }
        await self._put(_device_key(device_code), record)
        await self._put(_user_key(user_code), {"device_code": device_code})
        return record

    async def by_device_code(self, device_code: str) -> dict[str, Any] | None:
        return await self._get(_device_key(device_code))

    async def by_user_code(self, user_code: str) -> dict[str, Any] | None:
        pointer = await self._get(_user_key(user_code))
        if not pointer:
            return None
        return await self.by_device_code(pointer["device_code"])

    async def save(self, record: dict[str, Any]) -> None:
        # Keep the remaining lifetime rather than extending it on every write;
        # a flow's clock starts when it is created, not when it is touched.
        remaining = max(1, record["expires_at"] - _now())
        await self._put(_device_key(record["device_code"]), record, ttl=remaining)

    async def link_transaction(self, state: str, device_code: str) -> None:
        await self._put(_txn_key(state), {"device_code": device_code})

    async def by_transaction(self, state: str) -> dict[str, Any] | None:
        pointer = await self._get(_txn_key(state))
        if not pointer:
            return None
        return await self.by_device_code(pointer["device_code"])

    async def consume(self, record: dict[str, Any]) -> None:
        """Retire a completed flow. Device codes are strictly single use."""
        await self._delete(_device_key(record["device_code"]))
        await self._delete(_user_key(record["user_code"]))


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------

_PAGE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #10151c; --muted: #5a6572;
  --card: #f6f8fa; --line: #d7dde5; --accent: #0057d9;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1419; --fg: #e8edf3; --muted: #97a3b2;
          --card: #171d25; --line: #2a333e; --accent: #5b9dff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 24px; }
.card { width: 100%; max-width: 420px; background: var(--card);
  border: 1px solid var(--line); border-radius: 12px; padding: 28px; }
h1 { margin: 0 0 6px; font-size: 19px; }
p { margin: 0 0 18px; color: var(--muted); }
input[type=text] { width: 100%; padding: 12px 14px; font-size: 22px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: 2px;
  text-align: center; text-transform: uppercase; border: 1px solid var(--line);
  border-radius: 8px; background: var(--bg); color: var(--fg); }
button { width: 100%; margin-top: 14px; padding: 12px; font-size: 15px;
  font-weight: 600; border: 0; border-radius: 8px;
  background: var(--accent); color: #fff; cursor: pointer; }
.err { color: #c8343f; margin: 0 0 14px; font-size: 14px; }
@media (prefers-color-scheme: dark) { .err { color: #ff7b85; } }
.ok { font-size: 40px; line-height: 1; margin-bottom: 10px; }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{_PAGE_CSS}</style>"
        f'<div class="card">{body}</div>',
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _prompt_page(user_code: str = "", error: str | None = None) -> HTMLResponse:
    err = f'<p class="err">{error}</p>' if error else ""
    return _page(
        "Connect a device",
        "<h1>Connect a device</h1>"
        "<p>Enter the code shown in your terminal.</p>"
        f"{err}"
        '<form method="post" autocomplete="off">'
        f'<input type="text" name="user_code" value="{user_code}" '
        'placeholder="XXXX-XXXX" autofocus spellcheck="false">'
        "<button type=submit>Continue</button>"
        "</form>",
        status=400 if error else 200,
    )


def _done_page() -> HTMLResponse:
    return _page(
        "Device connected",
        '<div class="ok">&#10003;</div>'
        "<h1>Device connected</h1>"
        "<p>You can close this window and return to your terminal.</p>",
    )


def _failed_page(message: str) -> HTMLResponse:
    return _page("Could not connect", f"<h1>Could not connect</h1><p>{message}</p>", status=400)


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------


class DeviceFlowHandlers:
    """The four endpoints, bound to one proxy instance.

    A class rather than closures so the internal client is resolved once, the
    store is shared, and tests can drive a handler directly without standing
    up routing.
    """

    def __init__(self, proxy: Any, storage: Any, *, base_url: str) -> None:
        self._proxy = proxy
        self._base_url = base_url.rstrip("/")
        self.store = DeviceFlowStore(storage)

    @property
    def _redirect_uri(self) -> str:
        return f"{self._base_url}/device/callback"

    async def _device_client(self):
        """The internal client the approval leg runs as, registered on demand.

        The device's own client cannot be used here: it registered a loopback
        redirect_uri, which is precisely what this flow exists to avoid. This
        one redirects to us, and the token it yields is an ordinary proxy
        token whose upstream credential belongs to the user who approved --
        which is all the caller needs.
        """
        from mcp.shared.auth import OAuthClientInformationFull

        existing = await self._proxy.get_client(DEVICE_CLIENT_ID)
        if existing is not None:
            return existing

        client = OAuthClientInformationFull(
            client_id=DEVICE_CLIENT_ID,
            client_secret=None,
            redirect_uris=[AnyUrl(self._redirect_uri)],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        await self._proxy.register_client(client)
        return client

    # -- 1. the client asks for a pair of codes -------------------------------

    async def authorization(self, request: Request) -> Response:
        """``POST /device_authorization`` -- RFC 8628 sections 3.1 and 3.2."""
        try:
            form = await request.form()
        except Exception:
            form = {}

        client_id = str(form.get("client_id") or "").strip()
        if not client_id:
            return _error("invalid_request", "client_id is required")

        scope = str(form.get("scope") or "").strip() or None
        record = await self.store.create(client_id=client_id, scope=scope)

        verification_uri = f"{self._base_url}/device"
        query = urlencode({"user_code": record["user_code"]})
        return JSONResponse(
            {
                "device_code": record["device_code"],
                "user_code": record["user_code"],
                "verification_uri": verification_uri,
                "verification_uri_complete": f"{verification_uri}?{query}",
                "expires_in": record["expires_at"] - _now(),
                "interval": DEFAULT_INTERVAL,
            },
            headers={"Cache-Control": "no-store"},
        )

    # -- 2. the human approves in whatever browser they have ------------------

    async def verification(self, request: Request) -> Response:
        """``GET|POST /device`` -- enter the code, then confirm.

        GET only ever renders the form, even when ``?user_code=`` is present.
        Approving takes a deliberate POST, so that merely following a link --
        from a chat message, or a mail client prefetching it -- cannot
        authorize anything.
        """
        if request.method == "GET":
            return _prompt_page(request.query_params.get("user_code", ""))

        form = await request.form()
        typed = normalize_user_code(str(form.get("user_code") or ""))
        if not typed:
            return _prompt_page(error="Enter the code from your terminal.")

        record = await self.store.by_user_code(typed)
        if record is None:
            return _prompt_page(typed, error="That code is not valid. Check it and try again.")
        if record["status"] != "pending":
            return _failed_page("That code has already been used.")
        if record["expires_at"] <= _now():
            return _failed_page("That code has expired. Start again from your terminal.")

        # Hand off to the ordinary authorization-code flow. Consent, the
        # upstream Vultr redirect and the IdP callback all run untouched.
        from mcp.server.auth.provider import AuthorizationParams

        state = secrets.token_urlsafe(24)
        verifier, challenge = _pkce_pair()
        record["status"] = "authorizing"
        record["code_verifier"] = verifier
        await self.store.save(record)
        await self.store.link_transaction(state, record["device_code"])

        client = await self._device_client()
        params = AuthorizationParams(
            state=state,
            scopes=record["scope"].split() if record.get("scope") else None,
            code_challenge=challenge,
            redirect_uri=AnyUrl(self._redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=None,
        )
        target = await self._proxy.authorize(client, params)
        return RedirectResponse(target, status_code=302)

    # -- 3. the approval lands back here --------------------------------------

    async def callback(self, request: Request) -> Response:
        """``GET /device/callback`` -- turn the proxy's code into a token."""
        state = request.query_params.get("state", "")
        record = await self.store.by_transaction(state)
        if record is None:
            return _failed_page(
                "This approval link has expired. Start again from your terminal."
            )

        denied = request.query_params.get("error")
        if denied:
            record["status"] = "denied"
            record["error"] = denied
            await self.store.save(record)
            return _failed_page("Authorization was declined.")

        code = request.query_params.get("code", "")
        if not code:
            return _failed_page("No authorization code was returned.")

        client = await self._device_client()
        code_obj = await self._proxy.load_authorization_code(client, code)
        if code_obj is None:
            return _failed_page("That authorization code is no longer valid.")

        token = await self._proxy.exchange_authorization_code(client, code_obj)

        record["status"] = "complete"
        record["token"] = token.model_dump(mode="json", exclude_none=True)
        await self.store.save(record)
        return _done_page()

    # -- 4. the client's poll picks it up -------------------------------------

    async def token_grant(self, request: Request) -> Response | None:
        """Handle a device-code poll at ``/token``.

        Returns ``None`` when the request is some other grant, so the caller
        delegates to the proxy's own token handler untouched.
        """
        try:
            form = await request.form()
        except Exception:
            return None
        if str(form.get("grant_type") or "") != DEVICE_GRANT_TYPE:
            return None

        device_code = str(form.get("device_code") or "").strip()
        if not device_code:
            return _error("invalid_request", "device_code is required")

        record = await self.store.by_device_code(device_code)
        # An unknown code and an expired one are deliberately indistinguishable:
        # the TTL deletes the record, so there is nothing left to tell apart.
        if record is None:
            return _error("expired_token", "The device code has expired or is unknown.")

        if record["expires_at"] <= _now():
            await self.store.consume(record)
            return _error("expired_token", "The device code has expired.")

        # RFC 8628 section 3.5: a client polling faster than the interval it
        # was given is told to slow down rather than answered.
        now = _now()
        if record["last_polled_at"] and now - record["last_polled_at"] < DEFAULT_INTERVAL:
            record["last_polled_at"] = now
            await self.store.save(record)
            return _error("slow_down", "Polling too frequently.")
        record["last_polled_at"] = now

        status = record["status"]
        if status == "denied":
            await self.store.consume(record)
            return _error("access_denied", "The request was declined.")
        if status != "complete":
            await self.store.save(record)
            return _error("authorization_pending", "Waiting for the user to approve.")

        token = record["token"]
        await self.store.consume(record)
        return JSONResponse(token, headers={"Cache-Control": "no-store"})


def device_routes(handlers: DeviceFlowHandlers) -> list[Route]:
    """The routes to append to the proxy's own."""
    return [
        Route("/device_authorization", handlers.authorization, methods=["POST"]),
        Route("/device", handlers.verification, methods=["GET", "POST"]),
        Route("/device/callback", handlers.callback, methods=["GET"]),
    ]


def device_flow_enabled() -> bool:
    """On unless explicitly disabled -- this is the headless-host path."""
    return os.environ.get("VULTR_MCP_DEVICE_FLOW", "true").lower() not in ("0", "false", "no")
