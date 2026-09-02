"""TEMPORARY diagnostic: what account does the OAuth token actually name?

Why this exists
---------------
An OAuth user who consents while their console is on one org gets a token that
resolves, at api.vultr.com, to the org that OWNS the OAuth application. Verified
live 2026-09-02: consenting from a personal test-org, ``vultr_account_get``
returns the Vultr RnD org owner with ``acls: [root, ...]``.

Two candidate causes, and they need opposite fixes:

  issuance     the token is minted carrying the wrong account -- the `acctid`
               claim is stamped from the consent session's primary membership
               rather than the console's active org. Fix belongs at consent.
  resolution   the token carries the RIGHT `acctid`, and the request side
               ignores it. ``OIDCAuthMiddleware::authFromJWT`` resolves the
               account solely from ``cached_dbobject_records_b64->Account``,
               the *issuer's* cached account, with no `acctid` handling at all.
               Fix belongs in the middleware.

Both fixes were written and both were reverted, because nobody had looked at a
real token's claims. This is the cheapest place to look: the MCP server holds
the upstream Vultr token already, so it can read the claims without a platform
deploy.

What it does NOT do
-------------------
Never returns the raw token -- that is a live credential, and the whole point is
to inspect claims rather than hand one around. Signature is not verified either,
because it does not need to be: FastMCP's JWTVerifier already validated this
token against the provider JWKS before any tool could run. This only decodes
what was already trusted.

Delete this module once the org binding is fixed.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

# Claims worth redacting if a provider ever adds them. The interesting claims
# (acctid, sub, aud, iss) are identifiers, not secrets -- but a token payload is
# attacker-controlled shape, so anything credential-named is masked rather than
# assumed harmless.
_SENSITIVE = ("secret", "password", "key", "token", "credential", "assertion")

# Fields we specifically want to see, called out so a reader of the output knows
# which ones answer the question rather than having to guess.
DECISIVE_CLAIMS = ("acctid", "sub", "aud", "iss", "client_id", "scope")


def debug_claims_enabled() -> bool:
    """Off unless explicitly switched on. This is a diagnostic, not a feature."""
    return os.environ.get("VULTR_MCP_DEBUG_CLAIMS", "false").lower() in (
        "true",
        "1",
        "yes",
    )


def _b64url_decode(segment: str) -> bytes:
    """Decode a JWT segment, restoring the padding JWTs strip."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, or an explanation of why there isn't one.

    Deliberately tolerant: a raw Vultr API key is not a JWT and reaching here
    with one is a real possibility (DualTokenVerifier accepts both), so that
    case gets a plain answer rather than an exception.
    """
    if not token:
        return {"error": "no token on this request"}

    parts = token.split(".")
    if len(parts) != 3:
        return {
            "error": "not a JWT",
            "note": (
                "three dot-separated segments expected; this looks like a raw "
                "Vultr API key, which carries no claims. Connect over OAuth to "
                "inspect a token."
            ),
            "segments": len(parts),
        }

    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as error:  # noqa: BLE001 - report, never raise, in a diagnostic
        return {"error": f"payload did not decode: {type(error).__name__}: {error}"}

    if not isinstance(payload, dict):
        return {"error": f"payload is {type(payload).__name__}, expected an object"}

    return {key: _mask(key, value) for key, value in payload.items()}


def _mask(key: str, value: Any) -> Any:
    """Mask credential-shaped claims; truncate anything unreasonably long."""
    if any(word in key.lower() for word in _SENSITIVE):
        return "<redacted>"
    if isinstance(value, str) and len(value) > 256:
        return value[:256] + f"... (+{len(value) - 256} chars)"
    return value


def summarise(claims: dict[str, Any]) -> dict[str, Any]:
    """Claims plus a plain reading of what they mean for the org-binding bug."""
    if "error" in claims:
        return {"claims": claims}

    present = {name: claims.get(name) for name in DECISIVE_CLAIMS if name in claims}
    missing = [name for name in DECISIVE_CLAIMS if name not in claims]

    if "acctid" not in claims:
        reading = (
            "No `acctid` claim at all. The middleware could not honour it even "
            "if it tried, so the fix has to be at issuance: stamp the "
            "consenting user's active org into the token."
        )
    else:
        reading = (
            "An `acctid` claim IS present. Compare it against the org you "
            "consented from. If it names YOUR org, the token is minted "
            "correctly and the bug is in OIDCAuthMiddleware::authFromJWT, "
            "which resolves the account from the issuer's cached record and "
            "never reads acctid -- a request-side fix. If it names the app "
            "owner's org, the token is minted wrong and the fix is at consent."
        )

    return {
        "decisive_claims": present,
        "absent": missing,
        "reading": reading,
        "all_claims": claims,
    }
