"""HMAC-signed single-use upload URLs.

The MCP tool ``get_signed_upload_url`` mints these; the
``/api/convert`` REST endpoint accepts them as an alternative to the
``Authorization: Bearer`` header. The signing key is the same
``MCP_BEARER_TOKEN`` already provisioned for header-based auth.

URL shape (v2.1 — user_id bound)::

    /api/convert?exp=<unix>&nonce=<uuid4>&max=<bytes>&uid=<user_id>&sig=<hex_hmac>

``sig`` is HMAC-SHA256 over the canonical string
``{exp}.{nonce}.{max}.{user_id}``.

The server validates: signature matches, ``exp`` is in the future, the
nonce hasn't been redeemed yet (single-use, in-process LRU), AND the
``uid`` from the URL identifies the user whose Google credentials
(and Apps Script Web App URL) are used to service the request. Closes
the v1.x/v2.0 deferral where ``/api/convert`` always wrote into the
operator's Drive regardless of who minted the URL.

**Schema cutoff:** v2.1 strictly rejects URLs without ``uid``.
Pre-v2.1 signed URLs become invalid on deploy; they have a 10-minute
default TTL, so the practical impact is limited to the deploy window
and in-flight sandbox uploads will simply mint a fresh URL.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlencode

# Default per-URL TTL — short enough that a leaked URL is mostly useless,
# long enough for the model to hand the URL to the sandbox and for the
# sandbox to do a multipart upload over a slow link.
DEFAULT_TTL_SECONDS = 600  # 10 minutes
MAX_TTL_SECONDS = 3600  # 1 hour ceiling


def _canonical(exp: int, nonce: str, max_bytes: int, user_id: str) -> str:
    """v2.1 canonical: include user_id so a signed URL is bound to one tenant.

    Without user_id in the canonical, an attacker who possessed any
    valid signed URL could in principle re-purpose it to write into a
    different user's Drive once the server learned to do per-user
    auth — and even pre-v2.1, the URL was operator-bound de facto.
    Binding user_id at the crypto layer makes the property explicit
    and verifiable at the middleware boundary, before any handler
    runs.
    """
    return f"{exp}.{nonce}.{max_bytes}.{user_id}"


def sign_upload_url(
    *,
    base_url: str,
    signing_key: bytes,
    user_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a fresh signed upload URL bound to ``user_id``.

    Returns ``{"url", "expires_at", "max_bytes", "nonce", "user_id"}``.

    ``signing_key`` is bytes (matches ``keys.get_key("signed_url")``'s
    return type). v2.0b strict-flip: HKDF returns raw 32-byte keys
    that aren't generally valid UTF-8 — passing them as ``str`` (with
    a ``.decode("utf-8")`` round-trip at the call site) would crash
    ~99.96% of derived-key deployments. Passing bytes through to
    ``hmac.new`` directly avoids the round-trip and is correct for
    all key sources (override / shim / HKDF).

    ``user_id`` MUST identify the user whose Google credentials should
    service the eventual ``/api/convert`` POST. The server-side verify
    refuses to dispatch a request whose URL was signed for a different
    user (because the canonical wouldn't match and HMAC would fail).
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must be a non-empty string")
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_TTL_SECONDS}], got {ttl_seconds}"
        )
    exp = int(time.time()) + ttl_seconds
    nonce = secrets.token_urlsafe(16)
    canonical = _canonical(exp, nonce, max_bytes, user_id)
    sig = hmac.new(
        signing_key,
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = urlencode(
        {"exp": exp, "nonce": nonce, "max": max_bytes, "uid": user_id, "sig": sig}
    )
    return {
        "url": f"{base_url.rstrip('/')}?{query}",
        "expires_at": exp,
        "max_bytes": max_bytes,
        "nonce": nonce,
        "user_id": user_id,
    }


class NonceStore:
    """In-process single-use nonce tracker with time-bounded eviction.

    Adequate for single-user low-volume workloads (the user is the only
    caller). If the Fly machine restarts between URL issuance and
    consumption, the nonce is forgotten and the URL becomes unusable —
    acceptable since URLs are short-lived anyway.
    """

    def __init__(self) -> None:
        self._consumed: dict[str, int] = {}  # nonce -> exp
        self._lock = threading.Lock()

    def consume(self, nonce: str, exp: int) -> bool:
        """Atomically mark a nonce as consumed.

        Returns True if the consume succeeded (first use), False if the
        nonce was already redeemed. Side effect: opportunistically
        evicts expired entries.
        """
        now = int(time.time())
        with self._lock:
            # Opportunistic eviction — cheap, bounded by lock-hold time.
            stale = [n for n, e in self._consumed.items() if e <= now]
            for n in stale:
                del self._consumed[n]
            if nonce in self._consumed:
                return False
            self._consumed[nonce] = exp
            return True


def verify_signed_params(
    *,
    signing_key: bytes,
    exp: str,
    nonce: str,
    max_bytes: str,
    sig: str,
    user_id: str | None,
    nonce_store: NonceStore,
) -> tuple[bool, str | None, int | None, str | None]:
    """Validate a signed-URL query-string and consume the nonce.

    ``signing_key`` is bytes — see ``sign_upload_url`` docstring for
    the v2.0b rationale (HKDF output isn't UTF-8 in general).

    Returns ``(ok, error_message_if_not_ok, max_bytes_int_if_ok,
    user_id_if_ok)``.

    ``user_id`` is the ``uid`` query param from the request URL. v2.1
    strictly requires it; a missing or empty ``uid`` is rejected before
    the HMAC compare even runs. The HMAC compare itself is over the
    canonical including user_id, so any tamper of ``uid`` after signing
    fails signature verification.
    """
    if not user_id:
        # v2.1 strict cutoff — pre-v2.1 URLs lack uid and are rejected.
        return False, "missing 'uid' query param (v2.1+ required)", None, None
    try:
        exp_i = int(exp)
        max_i = int(max_bytes)
    except (TypeError, ValueError):
        return False, "exp and max must be integers", None, None

    if exp_i <= int(time.time()):
        return False, "URL has expired", None, None

    canonical = _canonical(exp_i, nonce, max_i, user_id)
    expected = hmac.new(
        signing_key,
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "signature mismatch", None, None

    if not nonce_store.consume(nonce, exp_i):
        return False, "URL already used", None, None

    return True, None, max_i, user_id
