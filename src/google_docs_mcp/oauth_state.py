"""HMAC-signed, single-use OAuth state parameter.

Encodes the calling user's ``sub`` claim into the OAuth state param so
the ``/oauth/google/api/callback`` handler can recover *who* came back
from Google without needing a server-side state store.

**The CSRF / replay threat we mitigate.** A naive design would put
``user_id`` in the state plaintext. An attacker could:

1. Initiate their OWN OAuth flow against our server.
2. Edit the state in transit to swap in a victim's ``user_id``.
3. Complete consent on Google.
4. Our callback stores the ATTACKER's Google creds under the VICTIM's
   key — now the victim's MCP tool calls run against the attacker's
   Google account.

HMAC over ``sub.nonce.exp`` makes (2) impossible. The single-use
``NonceStore`` (shared with the signed-upload-URL plumbing) makes
state replay impossible: if an attacker observes a victim's state
in browser history / access logs and tries to redeem it later,
``consume()`` returns ``False`` and the callback rejects.

State on the wire is opaque-ish — embeds the ``sub`` (already a
public-ish identifier) but anyone tampering breaks the signature.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from .crypto import NonceStore

DEFAULT_TTL_SECONDS = 600  # 10 minutes — long enough for browser dance,
                           # short enough that a leaked URL is mostly dead.
MAX_TTL_SECONDS = 3600


def _canonical(sub_b64: str, nonce: str, exp: int) -> str:
    return f"{sub_b64}.{nonce}.{exp}"


def sign_state(user_id: str, signing_key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a fresh signed state token binding ``user_id`` to a TTL.

    Returns a ``.``-separated string: ``sub_b64.nonce.exp.sig``. Pass
    this verbatim as the ``state`` query param in the Google auth URL.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_TTL_SECONDS}], got {ttl_seconds}"
        )

    sub_b64 = base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")
    nonce = secrets.token_urlsafe(16)
    exp = int(time.time()) + ttl_seconds
    sig = _hmac(signing_key, _canonical(sub_b64, nonce, exp))
    return f"{sub_b64}.{nonce}.{exp}.{sig}"


def verify_state(
    state: str, signing_key: str, nonce_store: NonceStore
) -> tuple[bool, str | None, str | None]:
    """Validate + consume a state token.

    Returns ``(ok, user_id_if_ok, error_if_not_ok)``. On success the
    nonce is marked consumed atomically — a second call with the same
    state returns ``(False, None, "state already used")``.
    """
    if not state:
        return False, None, "state is empty"

    parts = state.split(".")
    if len(parts) != 4:
        return False, None, "state malformed (expected sub.nonce.exp.sig)"
    sub_b64, nonce, exp_str, sig = parts

    try:
        exp = int(exp_str)
    except ValueError:
        return False, None, "exp must be an integer"

    if exp <= int(time.time()):
        return False, None, "state has expired"

    expected = _hmac(signing_key, _canonical(sub_b64, nonce, exp))
    if not hmac.compare_digest(expected, sig):
        return False, None, "signature mismatch"

    if not nonce_store.consume(nonce, exp):
        return False, None, "state already used"

    try:
        # Restore the padding stripped at sign time before decoding.
        padded = sub_b64 + "=" * (-len(sub_b64) % 4)
        user_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, None, "sub_b64 not decodable"

    return True, user_id, None


def _hmac(key: str, message: str) -> str:
    return hmac.new(
        key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
