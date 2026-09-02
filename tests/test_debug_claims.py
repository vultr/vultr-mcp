"""TEMPORARY — tests for the org-binding diagnostic. Delete with debug.py."""

from __future__ import annotations

import base64
import json

from vultr_mcp.debug import debug_claims_enabled, decode_claims, summarise


def _jwt(payload: dict) -> str:
    """A JWT-shaped string. Signature is irrelevant: nothing here verifies it."""

    def seg(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.not-a-real-signature"


def test_claims_are_decoded_without_the_signing_key():
    token = _jwt({"acctid": "acct-test-org", "sub": "user-123", "aud": "client-abc"})
    assert decode_claims(token)["acctid"] == "acct-test-org"


def test_a_raw_api_key_is_explained_rather_than_crashing():
    """DualTokenVerifier accepts raw keys, so this input really does arrive."""
    claims = decode_claims("NOTAJWTJUSTANAPIKEY")
    assert claims["error"] == "not a JWT"
    assert "no claims" in claims["note"]


def test_an_empty_token_says_so():
    assert "no token" in decode_claims("")["error"]


def test_garbage_payload_reports_instead_of_raising():
    assert "did not decode" in decode_claims("aaa.!!!not-base64!!!.ccc")["error"]


def test_credential_shaped_claims_are_masked():
    """Claim values are attacker-shaped; anything credential-named is hidden."""
    token = _jwt({"acctid": "acct-1", "client_secret": "hunter2", "api_key": "abc"})
    claims = decode_claims(token)
    assert claims["client_secret"] == "<redacted>"
    assert claims["api_key"] == "<redacted>"
    assert claims["acctid"] == "acct-1", "identifiers must survive masking"


def test_the_raw_token_never_appears_in_the_output():
    token = _jwt({"acctid": "acct-1"})
    assert token not in json.dumps(summarise(decode_claims(token)))


def test_a_long_claim_is_truncated():
    claims = decode_claims(_jwt({"jumbo": "x" * 400}))
    assert claims["jumbo"].endswith("(+144 chars)")


def test_a_missing_acctid_points_at_issuance():
    """The two causes need opposite fixes, so the reading must distinguish."""
    reading = summarise(decode_claims(_jwt({"sub": "u", "aud": "c"})))["reading"]
    assert "issuance" in reading
    assert "acctid" in summarise(decode_claims(_jwt({"sub": "u"})))["absent"]


def test_a_present_acctid_points_at_resolution():
    summary = summarise(decode_claims(_jwt({"acctid": "acct-1", "sub": "u"})))
    assert "authFromJWT" in summary["reading"]
    assert summary["decisive_claims"]["acctid"] == "acct-1"


def test_the_diagnostic_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("VULTR_MCP_DEBUG_CLAIMS", raising=False)
    assert debug_claims_enabled() is False
    monkeypatch.setenv("VULTR_MCP_DEBUG_CLAIMS", "true")
    assert debug_claims_enabled() is True


async def test_the_tool_is_absent_from_the_default_surface(monkeypatch):
    """A diagnostic that ships enabled is a diagnostic that leaks."""
    from fastmcp import Client

    from vultr_mcp.server import create_server

    monkeypatch.delenv("VULTR_MCP_DEBUG_CLAIMS", raising=False)
    async with Client(create_server()) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert "vultr_debug_token_claims" not in names
