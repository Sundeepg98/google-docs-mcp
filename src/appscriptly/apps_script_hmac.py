"""Per-user HMAC for the Apps Script ``/exec`` Web App (v2.0c verify-path).

The restructure Web App is deployed ``access=ANYONE_ANONYMOUS`` because the
MCP server POSTs to ``/exec`` with no Google sign-in. URL secrecy alone is
therefore NOT an adequate access control — anyone who learns a user's
``/exec`` URL could otherwise POST ``{docId, splitTree}`` and mutate any
doc that user owns. This module is the single source of truth for the HMAC
scheme that closes that gap (THREAT_MODEL §4 row 5 / §9 row 1):

  * **PROVISION** — ``generate_hmac_key`` mints a 64-hex-char (256-bit) key;
    ``setup_apps_script`` persists it per user and templates it into the
    deployed ``restructure.gs`` via ``inject_hmac_into_source``.
  * **SIGN** — ``compute_signature`` produces the lowercase-hex
    ``HMAC-SHA256(key, "<timestamp>.<body>")`` the server sends as
    ``X-MCP-Signature`` alongside ``X-MCP-Timestamp`` (``docx_import``).
  * **VERIFY** — ``restructure.gs::_verifyHmac`` recomputes the same value
    with ``Utilities.computeHmacSha256Signature`` and constant-time-compares.

Signing the timestamp TOGETHER with the body binds them: a captured
``(body, signature)`` pair can't be replayed under a fresh timestamp, and a
stale timestamp is rejected by the Apps Script side's skew window.

The two sentinels here MUST match the literals declared in
``restructure.gs`` (``MCP_HMAC_KEY`` / ``MCP_HMAC_REQUIRED``); the fencing
test ``test_threat_model_claims_match_code`` pins the round-trip.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# Exact literals the deploy-time substitution replaces in restructure.gs.
# Kept as the runtime-assembled form (split across a concat) NOWHERE here —
# these are the *target* strings to find. restructure.gs declares them as
# ``var MCP_HMAC_KEY = '__MCP_HMAC_KEY__';`` etc.
_KEY_PLACEHOLDER = "__MCP_HMAC_KEY__"
_REQUIRED_PLACEHOLDER = "__MCP_HMAC_REQUIRED__"

# Header names the server sends and restructure.gs reads (case-insensitively).
SIGNATURE_HEADER = "X-MCP-Signature"
TIMESTAMP_HEADER = "X-MCP-Timestamp"


def generate_hmac_key() -> str:
    """Mint a fresh per-user HMAC-SHA256 key.

    ``secrets.token_hex(32)`` returns 32 bytes (256 bits) encoded as a
    64-character lowercase hex string — exactly what
    ``user_store._valid_apps_script_hmac_key`` validates and what the
    migration backfill (``scripts/migrate_existing_users.py``) produces.
    Centralized here so the runtime provisioning path and the migration
    path mint identically-shaped keys.
    """
    return secrets.token_hex(32)


def compute_signature(key: str, *, timestamp: str, body: str) -> str:
    """Return the lowercase-hex HMAC-SHA256 the server sends as the signature.

    Message construction MUST match ``restructure.gs::_verifyHmac`` exactly:
    ``timestamp + "." + body``. ``timestamp`` is the unix-seconds string sent
    in ``X-MCP-Timestamp``; ``body`` is the raw request body (the exact bytes
    POSTed, decoded as UTF-8 by the caller before hand-off here).

    Args:
        key: the per-user 64-hex-char key (as stored / as templated into
            the script).
        timestamp: unix-epoch-seconds as a string (the value of the
            ``X-MCP-Timestamp`` header — signed as-is so the Apps Script
            side can re-sign the identical string).
        body: the raw JSON request body as a ``str``.

    Returns:
        64-char lowercase hex digest.
    """
    mac = hmac.new(
        key.encode("utf-8"),
        (timestamp + "." + body).encode("utf-8"),
        hashlib.sha256,
    )
    return mac.hexdigest()


def inject_hmac_into_source(source: str, key: str) -> str:
    """Template a provisioned ``key`` into a copy of ``restructure.gs`` source.

    Replaces the two sentinels declared in the script:
      * ``__MCP_HMAC_KEY__`` → the 64-hex key;
      * ``__MCP_HMAC_REQUIRED__`` → ``true`` (so ``_verifyHmac`` enforces).

    Pure function — returns a new string, never mutates input. The result is
    what ``setup_apps_script`` pushes to Apps Script AND what it feeds to
    ``compute_content_hash`` (so the hash is stable across re-runs for the
    same user as long as the key is unchanged; a key rotation correctly
    triggers a fresh deploy).

    Raises:
        ValueError: the source does not contain the key placeholder (guards
            against a future edit silently dropping the sentinel and
            shipping an unauthenticated Web App), or ``key`` is falsy.
    """
    if not key:
        raise ValueError("Refusing to inject an empty HMAC key into the script.")
    if _KEY_PLACEHOLDER not in source:
        raise ValueError(
            f"restructure.gs source is missing the {_KEY_PLACEHOLDER!r} "
            "placeholder — the HMAC key cannot be templated in. The deployed "
            "Web App would be UNAUTHENTICATED. Restore the sentinel in "
            "restructure.gs (see apps_script_hmac.inject_hmac_into_source)."
        )
    return (
        source
        .replace(_KEY_PLACEHOLDER, key)
        .replace(_REQUIRED_PLACEHOLDER, "true")
    )
