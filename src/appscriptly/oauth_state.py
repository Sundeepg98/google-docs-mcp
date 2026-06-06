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
import threading
import time

from .crypto import NonceStore

# Per-nonce PKCE code_verifier store. Kept server-side rather than
# encoded in the state token because putting the verifier in the URL
# (which already carries the code on the callback) would defeat
# PKCE's whole purpose — the protection comes from the verifier
# being secret AND held only by the legitimate client.
#
# In-process dict, same lifetime/semantics as NonceStore. Survives
# until: (a) the nonce is consumed via verify_state, or (b) the entry
# ages out via opportunistic eviction (next sign or verify call).
# If the Fly machine restarts between sign_state and the user's
# OAuth callback completing, the verifier is forgotten and that flow
# fails — acceptable since OAuth flows are short (TTL is 10 min).
_pending_verifiers: dict[str, tuple[str, int]] = {}  # nonce -> (verifier, exp)
_pending_lock = threading.Lock()


def _evict_expired_verifiers() -> None:
    """Drop verifier entries whose exp has passed. Cheap to call often."""
    now = int(time.time())
    with _pending_lock:
        stale = [n for n, (_, exp) in _pending_verifiers.items() if exp <= now]
        for n in stale:
            del _pending_verifiers[n]

DEFAULT_TTL_SECONDS = 600  # 10 minutes — long enough for browser dance,
                           # short enough that a leaked URL is mostly dead.
MAX_TTL_SECONDS = 3600


def _canonical(sub_b64: str, nonce: str, exp: int) -> str:
    return f"{sub_b64}.{nonce}.{exp}"


def sign_state(
    user_id: str,
    signing_key: bytes,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    code_verifier: str | None = None,
) -> str:
    """Mint a fresh signed state token binding ``user_id`` to a TTL.

    Returns a ``.``-separated string: ``sub_b64.nonce.exp.sig``. Pass
    this verbatim as the ``state`` query param in the Google auth URL.

    ``signing_key`` is bytes (matches ``keys.get_key("oauth_state")``'s
    return type). v2.0b strict-flip: HKDF returns raw 32-byte keys
    that aren't generally valid UTF-8 — the pre-flip ``.decode("utf-8")``
    round-trip at the call site crashed ~99.96% of derived-key
    deployments. Passing bytes through directly works for all key
    sources (override / shim / HKDF).

    If ``code_verifier`` is provided (PKCE), it's stored server-side
    under the generated nonce. ``verify_state`` will retrieve it on
    consume so the OAuth callback can pass it to ``Flow.fetch_token``.
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

    if code_verifier:
        _evict_expired_verifiers()
        with _pending_lock:
            _pending_verifiers[nonce] = (code_verifier, exp)

    sig = _hmac(signing_key, _canonical(sub_b64, nonce, exp))
    return f"{sub_b64}.{nonce}.{exp}.{sig}"


def verify_state(
    state: str, signing_key: bytes, nonce_store: NonceStore
) -> tuple[bool, str | None, str | None, str | None]:
    """Validate + consume a state token.

    Returns ``(ok, user_id, error, code_verifier)``. On success the
    nonce is marked consumed atomically and any PKCE ``code_verifier``
    stored at sign time is popped from the pending store and returned.
    A second call with the same state returns
    ``(False, None, "state already used", None)``.

    The 4-tuple shape is stable: when no PKCE was used, the 4th
    element is ``None`` — caller checks before using.
    """
    if not state:
        return False, None, "state is empty", None

    parts = state.split(".")
    if len(parts) != 4:
        return False, None, "state malformed (expected sub.nonce.exp.sig)", None
    sub_b64, nonce, exp_str, sig = parts

    try:
        exp = int(exp_str)
    except ValueError:
        return False, None, "exp must be an integer", None

    if exp <= int(time.time()):
        return False, None, "state has expired", None

    expected = _hmac(signing_key, _canonical(sub_b64, nonce, exp))
    if not hmac.compare_digest(expected, sig):
        return False, None, "signature mismatch", None

    if not nonce_store.consume(nonce, exp):
        return False, None, "state already used", None

    # Pop the PKCE verifier (if any) atomically alongside the nonce.
    with _pending_lock:
        verifier_entry = _pending_verifiers.pop(nonce, None)
    code_verifier = verifier_entry[0] if verifier_entry else None

    try:
        # Restore the padding stripped at sign time before decoding.
        padded = sub_b64 + "=" * (-len(sub_b64) % 4)
        user_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, None, "sub_b64 not decodable", None

    return True, user_id, None, code_verifier


def _hmac(key: bytes, message: str) -> str:
    return hmac.new(
        key, message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
