"""HMAC-signed single-use upload URLs.

The MCP tool ``get_signed_upload_url`` mints these; the
``/api/convert`` REST endpoint accepts them as an alternative to the
``Authorization: Bearer`` header. The signing key is the same
``MCP_BEARER_TOKEN`` already provisioned for header-based auth.

URL shape::

    /api/convert?exp=<unix_seconds>&nonce=<uuid4>&max=<bytes>&sig=<hex_hmac>

``sig`` is HMAC-SHA256 over the canonical string ``{exp}.{nonce}.{max}``.
The server validates: signature matches, ``exp`` is in the future, and
the nonce hasn't been redeemed yet (single-use, in-process LRU).
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


def _canonical(exp: int, nonce: str, max_bytes: int) -> str:
    return f"{exp}.{nonce}.{max_bytes}"


def sign_upload_url(
    *,
    base_url: str,
    signing_key: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a fresh signed upload URL.

    Returns ``{"url", "expires_at", "max_bytes", "nonce"}``.
    """
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_TTL_SECONDS}], got {ttl_seconds}"
        )
    exp = int(time.time()) + ttl_seconds
    nonce = secrets.token_urlsafe(16)
    canonical = _canonical(exp, nonce, max_bytes)
    sig = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = urlencode(
        {"exp": exp, "nonce": nonce, "max": max_bytes, "sig": sig}
    )
    return {
        "url": f"{base_url.rstrip('/')}?{query}",
        "expires_at": exp,
        "max_bytes": max_bytes,
        "nonce": nonce,
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
    signing_key: str,
    exp: str,
    nonce: str,
    max_bytes: str,
    sig: str,
    nonce_store: NonceStore,
) -> tuple[bool, str | None, int | None]:
    """Validate a signed-URL query-string and consume the nonce.

    Returns ``(ok, error_message_if_not_ok, max_bytes_int_if_ok)``.
    """
    try:
        exp_i = int(exp)
        max_i = int(max_bytes)
    except (TypeError, ValueError):
        return False, "exp and max must be integers", None

    if exp_i <= int(time.time()):
        return False, "URL has expired", None

    canonical = _canonical(exp_i, nonce, max_i)
    expected = hmac.new(
        signing_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "signature mismatch", None

    if not nonce_store.consume(nonce, exp_i):
        return False, "URL already used", None

    return True, None, max_i
