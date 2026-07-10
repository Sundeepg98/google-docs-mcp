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

# Default upload-size cap baked into a signed URL when the caller doesn't
# override it. 50 MiB is Drive's docx-converter ceiling — uploading more
# than this can't succeed downstream anyway.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB

# Floor/ceiling for the per-URL ``max_bytes`` cap. The cap is now ENFORCED
# (the convert endpoint rejects bodies over it), so an out-of-range value
# is no longer a harmless advisory — it must be validated at both the mint
# and verify boundaries:
#   * floor (1 byte): a 0 / negative cap is nonsensical and, once
#     enforced, would reject every upload — almost certainly a bug or a
#     forged URL, not an intent. Reject rather than silently brick uploads.
#   * ceiling (100 MiB): an attacker who could mint or forge a URL with
#     ``max=<huge>`` would defeat the cap's purpose (memory-exhaustion
#     protection). 100 MiB sits comfortably above the 50 MiB default so
#     legitimate callers are unaffected; anything larger is out of policy.
#     Defense-in-depth: the HMAC already stops a third party forging
#     ``max``, but bounding it at the crypto layer means even a
#     mis-configured mint (or a future bug that lets max grow unbounded)
#     can't produce an unbounded-cap URL.
MIN_MAX_BYTES = 1
MAX_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB hard ceiling


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
    max_bytes: int = DEFAULT_MAX_BYTES,
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

    ``max_bytes`` is the upload-size cap baked into the signature and
    ENFORCED by the convert endpoint (over-cap → 413). Validated against
    ``[MIN_MAX_BYTES, MAX_MAX_BYTES]`` so a caller can't mint a URL whose
    cap is nonsensical (≤0 → bricks every upload) or unbounded (defeats
    the memory-exhaustion protection the cap exists for).
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must be a non-empty string")
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_TTL_SECONDS}], got {ttl_seconds}"
        )
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)  # True/False are ints in Python
        or max_bytes < MIN_MAX_BYTES
        or max_bytes > MAX_MAX_BYTES
    ):
        raise ValueError(
            f"max_bytes must be an int in "
            f"[{MIN_MAX_BYTES}, {MAX_MAX_BYTES}], got {max_bytes!r}"
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
    consume_nonce: bool = True,
) -> tuple[bool, str | None, int | None, str | None]:
    """Validate a signed-URL query-string and (by default) consume the nonce.

    ``signing_key`` is bytes — see ``sign_upload_url`` docstring for
    the v2.0b rationale (HKDF output isn't UTF-8 in general).

    Returns ``(ok, error_message_if_not_ok, max_bytes_int_if_ok,
    user_id_if_ok)``.

    ``user_id`` is the ``uid`` query param from the request URL. v2.1
    strictly requires it; a missing or empty ``uid`` is rejected before
    the HMAC compare even runs. The HMAC compare itself is over the
    canonical including user_id, so any tamper of ``uid`` after signing
    fails signature verification.

    ``consume_nonce=False`` (job-model, T1.1) verifies signature, expiry
    and bounds but leaves the nonce store untouched AND does not reject
    an already-redeemed nonce. The convert endpoint then consumes the
    nonce itself at JOB-CREATION time, so a request that fails
    validation before any work starts (bad form field, missing creds)
    never burns the single-use URL, and a retry whose nonce is already
    burned can still ATTACH to the job that burned it via the request
    fingerprint. Callers that keep the default get the historical
    verify-and-consume behavior unchanged.
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

    # Policy bound on the (now authenticated) cap. Checked AFTER the HMAC
    # so we only reason about a genuinely-signed value, and BEFORE the
    # nonce is consumed so an out-of-policy URL doesn't burn its one use.
    # ``sign_upload_url`` already enforces these bounds at mint time, so a
    # current-code URL always passes; this rejects a URL signed by older
    # code (pre-bounds) or one minted after the ceiling was tightened —
    # i.e. a value the enforcing endpoint should never have honored.
    if max_i < MIN_MAX_BYTES or max_i > MAX_MAX_BYTES:
        return (
            False,
            f"max out of bounds [{MIN_MAX_BYTES}, {MAX_MAX_BYTES}]",
            None,
            None,
        )

    if consume_nonce and not nonce_store.consume(nonce, exp_i):
        return False, "URL already used", None, None

    return True, None, max_i, user_id


# ---------------------------------------------------------------------
# Job-status URLs (T1.1 async job model)
# ---------------------------------------------------------------------

# Status URLs live much longer than upload URLs: a client that opted
# into async=1 (or got disconnected) may poll hours later, and the
# conversion result should stay collectible across a workday. 24h.
JOB_STATUS_TTL_SECONDS = 24 * 3600


def _job_status_canonical(job_id: str, exp: int) -> str:
    """Domain-tagged canonical for job-status signatures.

    The ``jobstatus.`` prefix separates this signature domain from the
    upload-URL canonical (``{exp}.{nonce}.{max}.{user_id}``) even though
    the two shapes could never collide today - explicit domain tags cost
    nothing and survive future shape changes. The user_id is NOT in the
    canonical: job_id is an unguessable uuid4 and the signature binds
    it, so possession of the signed URL (returned only to the
    authenticated uploader) is the capability.
    """
    return f"jobstatus.{job_id}.{exp}"


def sign_job_status_url(
    *,
    base_url: str,
    signing_key: bytes,
    job_id: str,
    ttl_seconds: int = JOB_STATUS_TTL_SECONDS,
) -> dict:
    """Mint a pre-signed, MULTI-use status URL for one convert job.

    Unlike upload URLs there is deliberately NO nonce: polling is the
    whole point, so the URL must verify any number of times until
    ``exp``. Returns ``{"status_url", "expires_at"}``.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be a non-empty string")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    exp = int(time.time()) + ttl_seconds
    sig = hmac.new(
        signing_key,
        _job_status_canonical(job_id, exp).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = urlencode({"exp": exp, "sig": sig})
    return {
        "status_url": f"{base_url.rstrip('/')}?{query}",
        "expires_at": exp,
    }


def verify_job_status_sig(
    *,
    signing_key: bytes,
    job_id: str,
    exp: str,
    sig: str,
) -> tuple[bool, str | None]:
    """Validate a job-status URL's signature + expiry. No nonce, multi-use.

    ``job_id`` comes from the request PATH (the middleware extracts it),
    so a tampered path breaks the HMAC exactly like a tampered query
    param. Returns ``(ok, error_message_if_not_ok)``.
    """
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False, "exp must be an integer"
    if exp_i <= int(time.time()):
        return False, "status URL has expired"
    expected = hmac.new(
        signing_key,
        _job_status_canonical(job_id, exp_i).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "signature mismatch"
    return True, None
