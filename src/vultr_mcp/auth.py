"""Phase 4 — OAuth via FastMCP OAuthProxy.

Vultr's OIDC provider is a confidential, non-DCR authorization server: every
token exchange requires a pre-registered client_id + secret. MCP clients
(claude.ai, ChatGPT) expect Dynamic Client Registration and refuse to store a
secret. ``OAuthProxy`` bridges the two: it presents a DCR-compliant interface
to MCP clients while using *our* one approved client's credentials upstream.

Token model: the ``token_verifier`` validates Vultr's own RS256 access tokens
against the provider JWKS, so the proxy forwards Vultr's token through to the
client. The client then sends that same Vultr token back on each request —
which is exactly what ``PerRequestVultrAuth`` forwards to api.vultr.com. No
separate token-exchange step is needed (api.vultr.com accepts the OIDC access
token directly; verified 2026-07, a 403 role-trust — not a 401 — came back).

Enable with VULTR_OIDC_ENABLED=true plus the VULTR_OAUTH_* / VULTR_OIDC_*
vars. When disabled, ``build_auth`` returns None and the server runs with the
raw-API-key / header-forwarding path only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier


def _looks_like_jwt(token: str) -> bool:
    """Three base64url segments = a JWT; anything else is a raw API key."""
    return token.count(".") == 2 and all(token.split("."))


class DualTokenVerifier(TokenVerifier):
    """Accept BOTH Vultr OIDC JWTs and raw Vultr API keys, concurrently.

    FastMCP's default auth would 401 any non-JWT bearer, which would break the
    header/API-key path whenever OAuth is enabled. This mirrors the PHP
    server's VultrAuth: JWT-shaped tokens are verified against Vultr's JWKS
    (OAuth path); opaque tokens are accepted as raw Vultr API keys and left for
    api.vultr.com to validate downstream (it is the real authority on keys).
    Both paths forward their bearer via PerRequestVultrAuth, so a bad key still
    fails at Vultr with a 401 — the MCP just doesn't gate on it.

    Note: with OAuth enabled, a raw key must arrive as ``Authorization:
    Bearer <key>`` (FastMCP extracts the token from that header before this
    verifier runs). ``X-Vultr-API-Key`` only applies in the no-auth-layer mode.
    """

    def __init__(self, jwt_verifier: TokenVerifier) -> None:
        # Inherit base_url / required_scopes / resource_server_url from the
        # wrapped verifier so the auth layer sees a fully-formed verifier.
        super().__init__(
            base_url=getattr(jwt_verifier, "base_url", None),
            required_scopes=getattr(jwt_verifier, "required_scopes", None),
        )
        self._jwt = jwt_verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        if _looks_like_jwt(token):
            return await self._jwt.verify_token(token)
        # Opaque bearer -> treat as a raw Vultr API key; Vultr validates it.
        return AccessToken(
            token=token,
            client_id="vultr-api-key",
            scopes=[],
            claims={"auth_method": "api_key"},
        )


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
    """Fetch the provider's OIDC discovery document for authoritative URLs.

    Using discovery (rather than hardcoding) transparently handles the
    provider-vs-issuer split — the JWKS lives under a different UUID than the
    provider — and picks up the /oauth/authorize endpoint fix automatically.
    """
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
    """OAuth state store for DCR registrations, auth codes, and tokens.

    Multi-replica deployments MUST share this — an auth code issued by one pod
    is redeemed on another. Uses Redis when REDIS_HOST is set (the VKE case),
    else in-memory (single-replica / local). Mirrors the PHP session store.
    """
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
        Pre-resolved upstream endpoints (tests pass these to skip the network
        discovery fetch). Production leaves this None -> discovery is fetched.
    client_storage:
        Persistent store for DCR client registrations + auth codes. Pass a
        Redis-backed store for multi-replica deployments; None = in-memory
        (single replica only).
    """
    if not _enabled():
        return None

    provider_id = _env("VULTR_OIDC_PROVIDER_ID")
    client_id = _env("VULTR_OAUTH_CLIENT_ID")
    client_secret = _env("VULTR_OAUTH_CLIENT_SECRET")
    resource_url = (os.environ.get("MCP_RESOURCE_URL", "https://vultrmcp.com")).rstrip("/")
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

    # Vultr stamps the OAuth client_id into `aud` (verified from a real token),
    # not the resource URL. Override with VULTR_OIDC_AUDIENCE if that changes.
    audience = _env("VULTR_OIDC_AUDIENCE") or client_id

    jwt_verifier = JWTVerifier(
        jwks_uri=endpoints.jwks_uri,
        issuer=endpoints.issuer,
        audience=audience,
        algorithm="RS256",
    )
    # Wrap so raw Vultr API keys keep working alongside OAuth at all times.
    token_verifier = DualTokenVerifier(jwt_verifier)

    return OAuthProxy(
        upstream_authorization_endpoint=endpoints.authorization_endpoint,
        upstream_token_endpoint=endpoints.token_endpoint,
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=token_verifier,
        base_url=resource_url,
        forward_pkce=True,
        client_storage=client_storage,
    )
