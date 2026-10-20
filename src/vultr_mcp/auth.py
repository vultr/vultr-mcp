"""OAuth, via FastMCP's OAuthProxy.

Vultr's OIDC provider needs a pre-registered confidential client, while MCP clients expect Dynamic
Client Registration. The proxy offers DCR downstream, issues clients its own JWT, and swaps it for
the stored upstream Vultr token on each request. Enabled by VULTR_OIDC_ENABLED=true.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from vultr_mcp.audit import emit_auth
from vultr_mcp.diagnostics import scrub_identifiers
from vultr_mcp.device_flow import (
    DEVICE_GRANT_TYPE,
    DeviceFlowHandlers,
    device_flow_enabled,
    device_routes,
)
from vultr_mcp.loopback_handoff import app_return_page, completion_page, is_app_link, is_loopback_target
from vultr_mcp.refresh_coalescer import (
    CoalescedUpstreamClient,
    UPSTREAM_RESOLUTION,
    UpstreamUnavailable,
    build_refresh_coalescer,
    token_key,
)
from vultr_mcp.server import USER_AGENT
from vultr_mcp.suspension import (
    HELD_REFRESH_RESPONSE,
    SUSPENDED_MESSAGE_CLAIM,
    SUSPENDED_REASON_CLAIM,
    SUSPENSION_GRACE_S,
    SUSPENSION_SEEN,
    SuspensionAwareClient,
    UpstreamSuspended,
    unverified_claims,
)

# The proxy's own access token lifetime; each use is still checked against Vultr's hourly token.
ACCESS_TOKEN_LIFETIME_S = 24 * 3600

# What a client is told when sign-in fails in the browser after it began, instead of waiting until it times out.
SIGN_IN_FAILED_MESSAGE = "Signing in to Vultr did not complete. Please try connecting again."


def _error_code(exc: BaseException) -> str:
    return getattr(exc, "error", None) or type(exc).__name__


def _error_fields(exc: BaseException) -> dict[str, Any]:
    """The OAuth error code and description the client was told."""
    fields: dict[str, Any] = {"error": _error_code(exc)}
    if isinstance(exc, TokenError) and exc.error_description:
        fields["error_description"] = scrub_identifiers(exc.error_description)[:300]
    elif str(exc):
        fields["error_description"] = scrub_identifiers(str(exc))[:300]
    return fields


def loopback_handoff_enabled() -> bool:
    """On unless explicitly disabled; the escape hatch is for debugging only."""
    return os.environ.get("VULTR_MCP_LOOPBACK_HANDOFF", "true").lower() not in (
        "0",
        "false",
        "no",
    )


def _looks_like_jwt(token: str) -> bool:
    """Three base64url segments = a JWT; anything else is a raw API key."""
    return token.count(".") == 2 and all(token.split("."))


def _api_key_access_token(token: str) -> AccessToken:
    """Wrap an opaque bearer as an AccessToken; api.vultr.com decides whether it is valid."""
    return AccessToken(
        token=token,
        client_id="vultr-api-key",
        scopes=[],
        claims={"auth_method": "api_key"},
    )


class DualTokenVerifier(TokenVerifier):
    """Accept Vultr OIDC JWTs (verified against the JWKS) and raw API keys (left to api.vultr.com)."""

    def __init__(self, jwt_verifier: TokenVerifier) -> None:
        super().__init__(
            base_url=getattr(jwt_verifier, "base_url", None),
            required_scopes=getattr(jwt_verifier, "required_scopes", None),
        )
        self._jwt = jwt_verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        if _looks_like_jwt(token):
            return await self._jwt.verify_token(token)
        return _api_key_access_token(token)


class VultrOAuthProxy(OAuthProxy):
    """OAuthProxy that also accepts raw Vultr API keys, which the base class 401s, and the device grant."""

    def __init__(self, *args, **kwargs) -> None:
        # Shared with the device grant, since the pod that approves is rarely the pod being polled.
        self._shared_storage = kwargs.get("client_storage")
        # Vultr revokes a token family when a refresh token is reused, so concurrent refreshes become one call across pods.
        self._refresh_coalescer = build_refresh_coalescer(kwargs.get("upstream_client_secret"))
        super().__init__(*args, **kwargs)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not _looks_like_jwt(token):
            return _api_key_access_token(token)
        seen: list[UpstreamSuspended] = []
        reset = SUSPENSION_SEEN.set(seen)
        try:
            access = await super().load_access_token(token)
        finally:
            SUSPENSION_SEEN.reset(reset)
        if access is not None:
            return access
        if seen:
            return await self._held_access(token, seen[-1])
        await self._audit_rejection(token)
        return None

    async def _audit_rejection(self, token: str) -> None:
        """Why a proxy token got a 401, which is the moment a client may decide to sign in again."""
        started = time.perf_counter()
        client_id = None
        try:
            payload = self.jwt_issuer.verify_token(token)
            client_id = payload.get("client_id")
            mapping = await self._jti_mapping_store.get(key=payload["jti"])
            if not mapping:
                reason = "session_not_found"
            elif not await self._upstream_token_store.get(key=mapping.upstream_token_id):
                reason = "upstream_tokens_not_found"
            else:
                reason = "upstream_token_rejected"
        except Exception as exc:  # noqa: BLE001 - classifying the rejection must never fail the request
            reason = "token_expired" if "expired" in str(exc).lower() else "token_invalid"
        emit_auth("access", "rejected", client_id, started, error=reason)

    async def _try_transparent_refresh(self, upstream_token_set: Any) -> Any:
        """FastMCP's refresh inside a request whose upstream token lapsed, which clients never see unless it fails."""
        started = time.perf_counter()
        resolved: list[str] = []
        reset = UPSTREAM_RESOLUTION.set(resolved)
        try:
            refreshed = await super()._try_transparent_refresh(upstream_token_set)
        except Exception as exc:
            emit_auth("transparent_refresh", "error", upstream_token_set.client_id, started,
                      resolved_by=resolved[0] if resolved else None, **_error_fields(exc))
            raise
        finally:
            UPSTREAM_RESOLUTION.reset(reset)
        emit_auth("transparent_refresh", "ok", upstream_token_set.client_id, started,
                  resolved_by=resolved[0] if resolved else None)
        return refreshed

    # One cookie per pending sign-in: FastMCP's single shared cookie loses entries when sign-ins overlap.
    def _entry_cookie_name(self, base_name: str, key: str) -> str:
        return self._cookie_name(f"{base_name}_{hashlib.sha256(key.encode()).hexdigest()[:16]}")

    def _signed_entries(self, request: Request, base_name: str) -> list[Any]:
        prefix = self._cookie_name(f"{base_name}_")
        entries: list[Any] = []
        for name, raw in request.cookies.items():
            if not name.startswith(prefix):
                continue
            payload = self._verify_cookie(raw)
            if not payload:
                continue
            try:
                entries.append(json.loads(base64.b64decode(payload.encode()).decode()))
            except Exception:  # noqa: BLE001 - a malformed cookie is simply not an entry
                continue
        return entries

    def _set_entry_cookie(self, response: Any, base_name: str, key: str, value: Any, max_age: int) -> None:
        payload = base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()
        response.set_cookie(
            self._entry_cookie_name(base_name, key),
            self._sign_cookie(payload),
            max_age=max_age,
            secure=self._is_https,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def _read_consent_bindings(self, request: Request) -> dict[str, str]:
        bindings = super()._read_consent_bindings(request)
        for entry in self._signed_entries(request, "MCP_CONSENT_BINDING"):
            if isinstance(entry, dict) and entry.get("txn") and entry.get("token"):
                bindings[str(entry["txn"])] = str(entry["token"])
        return bindings

    def _set_consent_binding_cookie(self, request: Request, response: Any, txn_id: str, consent_token: str) -> None:
        self._set_entry_cookie(response, "MCP_CONSENT_BINDING", txn_id, {"txn": txn_id, "token": consent_token}, 15 * 60)

    def _clear_consent_binding_cookie(self, request: Request, response: Any, txn_id: str) -> None:
        response.set_cookie(
            self._entry_cookie_name("MCP_CONSENT_BINDING", txn_id),
            "",
            max_age=0,
            secure=self._is_https,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def _decode_list_cookie(self, request: Request, base_name: str) -> list[str]:
        values = super()._decode_list_cookie(request, base_name)
        if base_name == "MCP_CONSENT_STATE":
            values += [str(entry) for entry in self._signed_entries(request, base_name) if isinstance(entry, str)]
        return values

    def _set_list_cookie(self, response: Any, base_name: str, value_b64: str, max_age: int) -> None:
        if base_name != "MCP_CONSENT_STATE":
            super()._set_list_cookie(response, base_name, value_b64, max_age)
            return
        payload = self._verify_cookie(value_b64)
        tokens = json.loads(base64.b64decode(payload.encode()).decode()) if payload else []
        for token in tokens:
            self._set_entry_cookie(response, base_name, str(token), str(token), max_age)

    def _create_upstream_oauth_client(self) -> Any:
        # Vultr evaluates Use MCP's conditions at refresh too, so the refresh must carry the User-Agent the API calls do.
        client = super()._create_upstream_oauth_client()
        client.headers["User-Agent"] = USER_AGENT
        return client

    @asynccontextmanager
    async def _upstream_oauth_client(self) -> AsyncIterator[Any]:
        async with super()._upstream_oauth_client() as client:
            yield SuspensionAwareClient(CoalescedUpstreamClient(client, self._refresh_coalescer))

    async def _held_access(self, token: str, suspended: UpstreamSuspended) -> AccessToken | None:
        """The session behind a proxy token Vultr refused as suspended, while the grace lasts; SuspendedTransport answers its calls."""
        try:
            payload = self.jwt_issuer.verify_token(token)
            upstream = await self._upstream_for_jti(payload["jti"])
        except Exception:  # noqa: BLE001 - an unreadable session is simply not held
            return None
        if upstream is None:
            return None
        held_until = upstream.expires_at + SUSPENSION_GRACE_S
        if time.time() >= held_until:
            return None
        claims: dict[str, Any] = {
            **unverified_claims(upstream.access_token),
            SUSPENDED_MESSAGE_CLAIM: suspended.message,
            SUSPENDED_REASON_CLAIM: suspended.reason,
        }
        if payload.get("upstream_claims"):
            claims["upstream_claims"] = payload["upstream_claims"]
        return AccessToken(
            token=upstream.access_token,
            client_id=str(payload.get("client_id") or upstream.client_id or ""),
            scopes=upstream.scope.split() if upstream.scope else [],
            expires_at=int(min(payload.get("exp", held_until), held_until)),
            claims=claims,
        )

    async def _upstream_for_jti(self, jti: str) -> Any:
        mapping = await self._jti_mapping_store.get(key=jti)
        if not mapping:
            return None
        return await self._upstream_token_store.get(key=mapping.upstream_token_id)

    async def _exchange_holding_suspension(
        self, exchange, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str], held: list[str]
    ) -> OAuthToken:
        """A client refresh; if Vultr refuses it as suspended, new proxy tokens over the unchanged stored grant."""
        try:
            return await exchange(client, refresh_token, scopes)
        except TokenError as exc:
            # FastMCP reports every failed upstream refresh as invalid_grant; a Vultr outage must not tell the client its session is dead.
            if isinstance(exc.__cause__, UpstreamUnavailable):
                raise UpstreamUnavailable(str(exc.__cause__)) from exc.__cause__
            suspended = exc.__cause__
            if not isinstance(suspended, UpstreamSuspended):
                raise
            upstream = await self._upstream_for_refresh(refresh_token.token)
            if upstream is None or not upstream.refresh_token or time.time() >= upstream.expires_at + SUSPENSION_GRACE_S:
                raise
            reset = HELD_REFRESH_RESPONSE.set({
                "access_token": upstream.access_token,
                "token_type": upstream.token_type or "Bearer",
                "expires_in": int(upstream.expires_at - time.time()),
                "refresh_token": upstream.refresh_token,
            })
            try:
                token = await exchange(client, refresh_token, scopes)
            finally:
                HELD_REFRESH_RESPONSE.reset(reset)
            held.append(suspended.reason)
            return token

    async def _upstream_for_refresh(self, refresh_token: str) -> Any:
        try:
            payload = self.jwt_issuer.verify_token(refresh_token, expected_token_use="refresh")
            return await self._upstream_for_jti(payload["jti"])
        except Exception:  # noqa: BLE001 - an unreadable refresh token is simply not held
            return None

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        started = time.perf_counter()
        try:
            url = await super().authorize(client, params)
        except Exception as exc:
            emit_auth("authorize", "error", client.client_id, started, error=_error_code(exc))
            raise
        emit_auth("authorize", "ok", client.client_id, started)
        return url

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        started = time.perf_counter()
        try:
            token = await super().exchange_authorization_code(client, authorization_code)
        except Exception as exc:
            emit_auth("code_exchange", "error", client.client_id, started, **_error_fields(exc))
            raise
        emit_auth("code_exchange", "ok", client.client_id, started, **await self._account_of(token))
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        # A duplicate of an in-flight or just-finished refresh, whose token the winner already rotated out of the store.
        started = time.perf_counter()
        scopes = await self._refresh_coalescer.claimed_scopes(
            token_key(refresh_token), client.client_id or ""
        )
        if scopes is not None:
            return RefreshToken(token=refresh_token, client_id=client.client_id or "", scopes=scopes)
        found = await super().load_refresh_token(client, refresh_token)
        if found is None:
            emit_auth("refresh", "refused", client.client_id, started, error="invalid_grant",
                      error_description="refresh token not found (rotated, expired or revoked)")
        return found

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        started = time.perf_counter()
        resolved: list[str] = []
        held: list[str] = []
        # Bound here: zero-argument super() does not work inside the lambda.
        exchange = super().exchange_refresh_token
        try:
            token = await self._refresh_coalescer.run(
                token_key(refresh_token.token),
                client.client_id or "",
                scopes,
                lambda: self._exchange_holding_suspension(exchange, client, refresh_token, scopes, held),
                report=resolved.append,
            )
        except Exception as exc:
            emit_auth("refresh", "error", client.client_id, started,
                      resolved_by=resolved[0] if resolved else None, **_error_fields(exc))
            raise
        if held:
            emit_auth("refresh", "held", client.client_id, started,
                      resolved_by=resolved[0] if resolved else None, suspended=held[0])
            return token
        emit_auth("refresh", "ok", client.client_id, started,
                  resolved_by=resolved[0] if resolved else None, **await self._account_of(token))
        return token

    async def _account_of(self, token: OAuthToken) -> dict[str, Any]:
        """acctid and sub behind a token just issued, resolved the way a request's bearer is."""
        try:
            access = await OAuthProxy.load_access_token(self, token.access_token)
        except Exception:  # noqa: BLE001 - auditing never fails a sign-in
            return {}
        claims = getattr(access, "claims", None) or {}
        return {k: claims[k] for k in ("acctid", "sub") if claims.get(k) is not None}

    async def _handle_idp_callback(self, request: Request):
        """Hand the code back without assuming the loopback port exists (see ``loopback_handoff``)."""
        response = await super()._handle_idp_callback(request)
        if _is_browser_failure(response):
            response = await self._failure_to_client(request.query_params.get("state"), response)
        if not loopback_handoff_enabled():
            return response
        target = response.headers.get("location", "") if response is not None else ""
        # Errors keep the base class's redirect so the client sees the OAuth error.
        if target and "code=" in target and is_loopback_target(target):
            return completion_page(target)
        if target and "code=" in target and is_app_link(target):
            return app_return_page(target)
        return response

    async def _submit_consent(self, request: Request) -> Any:
        form = await request.form()
        response = await super()._submit_consent(request)
        if _is_browser_failure(response):
            response = await self._failure_to_client(str(form.get("txn_id", "")), response)
        return response

    async def _failure_to_client(self, txn_id: str | None, response: Any) -> Any:
        """A sign-in that failed after the client was known, sent to its registered redirect URI as an OAuth error.

        FastMCP shows these as a browser page, so the client never hears and waits until it times out.
        """
        if not txn_id:
            return response
        try:
            transaction = await self._transaction_store.get(key=txn_id)
        except Exception:  # noqa: BLE001 - without the transaction the browser page is all there is
            return response
        if transaction is None or not self._validate_client_redirect_uri(transaction.client_redirect_uri):
            return response
        params = {
            "error": "server_error" if response.status_code >= 500 else "access_denied",
            "error_description": SIGN_IN_FAILED_MESSAGE,
        }
        if transaction.client_state:
            params["state"] = transaction.client_state
        emit_auth("authorize", "error", transaction.client_id, time.perf_counter(), error=params["error"],
                  error_description=f"HTTP {response.status_code} returned to the client instead of a browser page")
        separator = "&" if "?" in transaction.client_redirect_uri else "?"
        return RedirectResponse(f"{transaction.client_redirect_uri}{separator}{urlencode(params)}", status_code=302)

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Proxy routes, with /token wrapped and, when enabled, the device grant's routes and metadata."""
        routes = [
            _replace_endpoint(route, _TemporarilyUnavailableToken(route.endpoint))
            if isinstance(route, Route) and route.path == "/token"
            else route
            for route in super().get_routes(mcp_path)
        ]
        if not device_flow_enabled():
            return routes

        handlers = DeviceFlowHandlers(
            self, self._shared_storage, base_url=str(self.base_url).rstrip("/")
        )

        patched: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/token":
                patched.append(
                    Route(
                        path=route.path,
                        endpoint=_with_device_grant(route.endpoint, handlers),
                        methods=route.methods,
                        name=route.name,
                        include_in_schema=route.include_in_schema,
                    )
                )
            elif isinstance(route, Route) and route.path.startswith(
                "/.well-known/oauth-authorization-server"
            ):
                patched.append(
                    Route(
                        path=route.path,
                        endpoint=_advertising_device_grant(route.endpoint, self.base_url),
                        methods=route.methods,
                        name=route.name,
                        include_in_schema=route.include_in_schema,
                    )
                )
            else:
                patched.append(route)

        return patched + device_routes(handlers)


def _replace_endpoint(route: Route, endpoint) -> Route:
    return Route(
        path=route.path,
        endpoint=endpoint,
        methods=route.methods,
        name=route.name,
        include_in_schema=route.include_in_schema,
    )


# RFC 6749 defines temporarily_unavailable only for /authorize; at /token a 503 with Retry-After is what clients retry instead of signing in again.
_TEMPORARILY_UNAVAILABLE = {
    "error": UpstreamUnavailable.error,
    "error_description": "Vultr sign-in is briefly unavailable. Your session is intact; retry the refresh shortly.",
}


class _TemporarilyUnavailableToken:
    """``/token``, answering 503 when Vultr's token endpoint is down instead of letting FastMCP call the session dead."""

    def __init__(self, original) -> None:
        self._original = original

    async def __call__(self, scope, receive, send) -> None:
        try:
            await _invoke(self._original, scope, receive, send)
        except UpstreamUnavailable:
            response = JSONResponse(
                _TEMPORARILY_UNAVAILABLE,
                status_code=503,
                headers={"Retry-After": "30", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)


def _is_browser_failure(response: Any) -> bool:
    """An error page shown in the browser, which the client waiting on its callback never sees."""
    return response is not None and response.status_code >= 400 and not isinstance(response, RedirectResponse)


def _is_asgi_endpoint(endpoint) -> bool:
    """True for an ASGI app (FastMCP's cors_middleware routes), False for a ``(Request) -> Response`` handler."""
    try:
        return len(inspect.signature(endpoint).parameters) == 3
    except (TypeError, ValueError):
        return False


async def _invoke(endpoint, scope, receive, send) -> None:
    """Call a route endpoint of either shape as an ASGI app."""
    if _is_asgi_endpoint(endpoint):
        await endpoint(scope, receive, send)
        return
    response = await endpoint(Request(scope, receive))
    await response(scope, receive, send)


async def _drain(receive) -> bytes:
    """Read a complete request body off the ASGI receive channel."""
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return body


def _replay(body: bytes):
    """A receive channel that hands back an already-consumed body, for the handler delegated to."""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


class _DeviceGrantToken:
    """``/token``, answering device-code polls before delegating; an object so Starlette routes it as ASGI."""

    def __init__(self, original, handlers: DeviceFlowHandlers) -> None:
        self._original = original
        self._handlers = handlers

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await _invoke(self._original, scope, receive, send)
            return

        body = await _drain(receive)
        handled = await self._handlers.token_grant(Request(scope, receive=_replay(body)))
        if handled is not None:
            await handled(scope, receive, send)
            return
        await _invoke(self._original, scope, _replay(body), send)


def _with_device_grant(original, handlers: DeviceFlowHandlers) -> _DeviceGrantToken:
    return _DeviceGrantToken(original, handlers)


class _DeviceGrantMetadata:
    """Authorization-server metadata, patched to advertise the device grant."""

    def __init__(self, original, base_url) -> None:
        self._original = original
        self._base_url = str(base_url).rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await _invoke(self._original, scope, receive, send)
            return

        start: dict = {}
        chunks: list[bytes] = []

        async def capture(message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            else:
                await send(message)

        await _invoke(self._original, scope, receive, capture)
        raw = b"".join(chunks)

        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            # Not JSON (an error page, a CORS preflight): pass it on.
            if start:
                await send(start)
            await send({"type": "http.response.body", "body": raw})
            return

        doc["device_authorization_endpoint"] = f"{self._base_url}/device_authorization"
        grants = list(doc.get("grant_types_supported") or [])
        if DEVICE_GRANT_TYPE not in grants:
            grants.append(DEVICE_GRANT_TYPE)
        doc["grant_types_supported"] = grants

        payload = json.dumps(doc).encode()
        headers = [
            (k, v)
            for k, v in start.get("headers", [])
            if k.lower() not in (b"content-length", b"content-type")
        ]
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(payload)).encode()))

        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


def _advertising_device_grant(original, base_url) -> _DeviceGrantMetadata:
    return _DeviceGrantMetadata(original, base_url)


def _env(key: str) -> str | None:
    val = os.environ.get(key)
    return val if val else None


def _enabled() -> bool:
    return os.environ.get("VULTR_OIDC_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class UpstreamEndpoints:
    """Resolved Vultr authorization-server endpoints."""

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str


def discover_endpoints(provider_id: str, *, ssl_verify: bool = True) -> UpstreamEndpoints:
    """The provider's OIDC discovery document; the JWKS lives under a different UUID than the provider."""
    api_base = os.environ.get("VULTR_API_BASE_URL", "https://api.vultr.com/v2").rstrip("/")
    url = f"{api_base}/oidc/provider/{provider_id}/.well-known/openid-configuration"
    resp = httpx.get(url, timeout=10.0, verify=ssl_verify)
    resp.raise_for_status()
    doc = resp.json()
    return UpstreamEndpoints(
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        jwks_uri=doc["jwks_uri"],
        issuer=doc["issuer"],
    )


def build_client_storage():
    """OAuth state store: Redis when REDIS_HOST is set (required with several replicas), else in memory."""
    redis_host = os.environ.get("REDIS_HOST")
    if not redis_host:
        from key_value.aio.stores.memory import MemoryStore

        return MemoryStore()

    from key_value.aio.stores.redis import RedisStore

    return RedisStore(
        host=redis_host,
        port=int(os.environ.get("REDIS_PORT", "6379")),
    )


def build_auth(
    *,
    endpoints: UpstreamEndpoints | None = None,
    client_storage=None,
) -> OAuthProxy | None:
    """Construct the OAuthProxy from env, or None when OIDC is disabled.

    endpoints:
        Pre-resolved upstream endpoints; None fetches discovery.
    client_storage:
        OAuth state store; None builds one from the environment.
    """
    if not _enabled():
        return None

    provider_id = _env("VULTR_OIDC_PROVIDER_ID")
    client_id = _env("VULTR_OAUTH_CLIENT_ID")
    client_secret = _env("VULTR_OAUTH_CLIENT_SECRET")
    resource_url = (os.environ.get("MCP_RESOURCE_URL", "https://mcp.vultr.com")).rstrip("/")
    ssl_verify = os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

    missing = [
        name
        for name, val in (
            ("VULTR_OIDC_PROVIDER_ID", provider_id),
            ("VULTR_OAUTH_CLIENT_ID", client_id),
            ("VULTR_OAUTH_CLIENT_SECRET", client_secret),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "VULTR_OIDC_ENABLED=true but missing required vars: " + ", ".join(missing)
        )

    if endpoints is None:
        endpoints = discover_endpoints(provider_id, ssl_verify=ssl_verify)

    if client_storage is None:
        client_storage = build_client_storage()

    # Vultr puts the OAuth client_id in `aud`, not the resource URL.
    audience = _env("VULTR_OIDC_AUDIENCE") or client_id

    jwt_verifier = JWTVerifier(
        jwks_uri=endpoints.jwks_uri,
        issuer=endpoints.issuer,
        audience=audience,
        algorithm="RS256",
    )
    token_verifier = DualTokenVerifier(jwt_verifier)

    return VultrOAuthProxy(
        upstream_authorization_endpoint=endpoints.authorization_endpoint,
        upstream_token_endpoint=endpoints.token_endpoint,
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=token_verifier,
        base_url=resource_url,
        forward_pkce=True,
        client_storage=client_storage,
        fastmcp_access_token_expiry_seconds=ACCESS_TOKEN_LIFETIME_S,
    )
