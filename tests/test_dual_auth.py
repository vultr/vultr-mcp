"""DualTokenVerifier: OAuth JWTs and raw API keys accepted concurrently."""

from __future__ import annotations

import pytest

from vultr_mcp.auth import DualTokenVerifier, _looks_like_jwt


class _FakeJWT:
    """Stand-in JWT verifier: 'good.jwt.sig' is valid, everything else fails."""

    async def verify_token(self, token):
        if token == "good.jwt.sig":
            from fastmcp.server.auth.auth import AccessToken

            return AccessToken(token=token, client_id="oauth", scopes=["openid"])
        return None


def test_jwt_shape_detection():
    assert _looks_like_jwt("aaa.bbb.ccc")
    assert not _looks_like_jwt("VULTRKEY1234567890ABCDEF")  # opaque key
    assert not _looks_like_jwt("aa..bb")  # empty middle segment


async def test_valid_jwt_takes_oauth_path():
    v = DualTokenVerifier(_FakeJWT())
    tok = await v.verify_token("good.jwt.sig")
    assert tok is not None and tok.client_id == "oauth"


async def test_invalid_jwt_is_rejected_not_passed_as_key():
    # A JWT-shaped token that fails verification must NOT fall through to the
    # raw-key path (that would let forged/expired OAuth tokens in).
    v = DualTokenVerifier(_FakeJWT())
    assert await v.verify_token("bad.jwt.sig") is None


async def test_raw_api_key_accepted():
    v = DualTokenVerifier(_FakeJWT())
    tok = await v.verify_token("ABCDEF1234567890RAWVULTRKEY")
    assert tok is not None
    assert tok.claims["auth_method"] == "api_key"
