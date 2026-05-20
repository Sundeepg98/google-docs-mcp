"""HKDF key derivation from MCP_BEARER_TOKEN master.

Per-purpose key separation: deriving distinct keys for ``api_bearer``,
``oauth_state``, and ``signed_url`` purposes from one master means
rotating the master invalidates all three atomically without per-purpose
key management.

v1.3.1 ships with a back-compat shim: the 3 purposes above STILL return
the raw master so existing claude.ai connectors + in-flight signed URLs
continue working unchanged. Operators see ``key_back_compat_shim_active:
true`` in ``gdocs_server_info`` (added in v1.4.0). The shim removes in
v2.0+ alongside the tool-consolidation Option B shim window.

Threat-model notes (see THREAT_MODEL §5 once that doc lands):

- The 32-char master length check is DEFERRED to first ``get_key()``
  call rather than module import. Short legacy tokens (pre-v1.3.1,
  often 16 chars) must boot the server cleanly and only fail loudly
  when a derived-key purpose is requested. The back-compat shim
  returns the raw master without invoking the length check at all.

- v1.3.1 ships the shim path active for all 3 purposes; no caller in
  v1.3.1 actually exercises the HKDF derived path. The derived code
  is present-but-inactive so v2.0's strict-flip is a small,
  reviewable change rather than a new-module introduction under
  time pressure.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

# v2.0b: shim removed. All 3 derived keys now go through HKDF unless
# overridden via MCP_API_BEARER_KEY / OAUTH_STATE_SIGNING_KEY /
# SIGNED_URL_SIGNING_KEY env vars (per v1.5.1).
#
# The symbol is preserved as an empty frozenset (rather than deleted)
# so importers — including is_shim_active(), key_provenance(), the
# get_key() conditional, and any external code that import-tests
# against its existence — continue to work without rewrite. The empty
# set means the `purpose in _BACK_COMPAT_RAW_MASTER` branch in
# get_key() never fires; every call falls through to HKDF derivation
# (or to the v1.5.1 env-var override path if set).
#
# DO NOT re-populate without major version bump + CHANGELOG note: any
# entry here resurrects mass-invalidation risk at the next removal.
_BACK_COMPAT_RAW_MASTER: frozenset[str] = frozenset()

# v1.5 observability: per-purpose hit counter for the shim path. Surfaced
# via gdocs_server_info().key_back_compat_shim_active_hits so operators
# can soak-test (deploy v1.5, wait 3+ days, verify counters are 0 for
# the last 24h) before shipping v2.0b's strict-flip. The shim removal
# would invalidate every key minted via the shim — this counter is the
# evidence that no such keys are actively in use.
#
# Process-local (intentionally — each replica reports its own count;
# the operator aggregates across replicas at read time). A threading
# Lock guards increments because Starlette+uvicorn can call into this
# from multiple worker threads.
_shim_hit_counter: dict[str, int] = {
    "api_bearer": 0,
    "oauth_state": 0,
    "signed_url": 0,
}
_shim_hit_counter_lock = threading.Lock()

# v1.5.1 (#28): per-purpose denominator counter. Counts EVERY successful
# get_key() call regardless of which path served the key (override /
# shim / HKDF). Without this denominator, "0 shim hits" is ambiguous:
# it could mean "everyone migrated off the shim" OR "nobody called
# get_key at all in this window". The preflight script asserts BOTH
# shim==0 AND total>=N before declaring it safe to ship v2.0b's
# strict-flip.
#
# Same threading model as _shim_hit_counter: process-local, lock-guarded.
_total_call_counter: dict[str, int] = {
    "api_bearer": 0,
    "oauth_state": 0,
    "signed_url": 0,
}
_total_call_counter_lock = threading.Lock()

# v2.6 (#48): per-purpose timestamp of the FIRST successful get_key() call
# in this process. ``None`` means the purpose has never been requested.
# Surfaced via ``gdocs_server_info().key_observability.first_call_age_seconds``
# as ``time.time() - first_call_at`` so operators can gate the v2.0b strict-
# flip on a soak window: "first call happened ≥1h30 ago AND the cumulative
# total since then is large enough" is the trustworthy signal.
#
# Without this, a freshly-restarted machine reports ``shim_hits=0`` AND
# ``totals=0`` after start-up, and the preflight gate can't tell that
# apart from "everyone has migrated off the shim, just no traffic yet."
# The first-call timestamp closes that gap.
#
# Process-local (each replica reports its own first-call) — same as the
# hit/total counters. Lock-guarded for the same Starlette+uvicorn-worker
# reason; the write is a tiny CAS under the lock, no measurable overhead.
_first_call_at: dict[str, float | None] = {
    "api_bearer": None,
    "oauth_state": None,
    "signed_url": None,
}
_first_call_at_lock = threading.Lock()

# HKDF info-context per purpose. Changing any string here invalidates
# the derived key for that purpose — same blast radius as rotating
# the master. Never change these once they leave the shim.
_HKDF_INFO: dict[str, bytes] = {
    "api_bearer": b"google-docs-mcp v1 api_bearer",
    "oauth_state": b"google-docs-mcp v1 oauth_state",
    "signed_url": b"google-docs-mcp v1 signed_url",
}

Purpose = Literal["api_bearer", "oauth_state", "signed_url"]

# v1.5.1: per-purpose env-var overrides. When set (and ≥32 chars), the
# override bytes are returned verbatim — bypassing BOTH the back-compat
# shim AND HKDF derivation. The RUNBOOK §3.4 documents these as the
# safe rotation path: an operator pins all 3 purposes to the current
# master BEFORE rotating MCP_BEARER_TOKEN, so derived keys don't move
# in lockstep with the master and invalidate every in-flight token.
#
# Removal from this map = breaking change; treat with same care as
# the HKDF info strings (changing either invalidates that purpose's
# in-flight tokens).
_OVERRIDE_ENV: dict[str, str] = {
    "api_bearer": "MCP_API_BEARER_KEY",
    "oauth_state": "OAUTH_STATE_SIGNING_KEY",
    "signed_url": "SIGNED_URL_SIGNING_KEY",
}

_MIN_MASTER_LEN = 32


@dataclass(frozen=True)
class KeyProvenance:
    """How a key was sourced — for ``gdocs_server_info`` introspection."""
    purpose: str
    mechanism: Literal["raw_master_shim", "hkdf_derived"]
    master_len: int


def _master() -> str:
    val = os.environ.get("MCP_BEARER_TOKEN")
    if not val:
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var is required for key derivation"
        )
    return val


def _validate_master_or_raise(master: str) -> None:
    """Enforce ≥32 chars. Called only when derivation is actually needed.

    NEVER call at module import — short legacy tokens must boot cleanly
    and only fail when a derived purpose is requested. The shim path
    skips this entirely.
    """
    if len(master) < _MIN_MASTER_LEN:
        raise RuntimeError(
            f"MCP_BEARER_TOKEN must be ≥{_MIN_MASTER_LEN} chars for "
            f"HKDF derivation (got {len(master)}). Either lengthen "
            f"the token or use a purpose currently in the back-compat "
            f"shim. See THREAT_MODEL.md §5 for rotation guidance."
        )


def _record_first_call(purpose: str) -> None:
    """Idempotently stamp the first-call timestamp for ``purpose``.

    v2.6 (#48). Called from every successful ``get_key()`` path (override,
    shim, HKDF-derived) so the timestamp reflects the first time the
    purpose was actually exercised in this process. Subsequent calls
    are no-ops — the timestamp pins the START of the soak window, not
    the most-recent call.

    Lock-guarded check-then-set so concurrent worker threads can't both
    see ``None`` and race to write different values.
    """
    with _first_call_at_lock:
        if _first_call_at.get(purpose) is None:
            _first_call_at[purpose] = time.time()


def _hkdf_sha256(master_bytes: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-Extract+Expand using HMAC-SHA256, salt='' for our use.

    Simple inline implementation — single dep on stdlib ``hmac`` +
    ``hashlib`` keeps ``keys.py`` import-free of cryptography for the
    shim path. The full ``cryptography`` lib is used elsewhere but
    avoiding the import here keeps boot fast and minimizes surface.

    Currently UNREACHED in v1.3.1 (all live purposes route through the
    shim) but verified via unit tests so v2.0's strict-flip is a
    pure-config change.
    """
    # Extract: PRK = HMAC-SHA256(salt=b"", IKM=master_bytes)
    prk = hmac.new(b"", master_bytes, hashlib.sha256).digest()
    # Expand: T(1) = HMAC-SHA256(PRK, info || 0x01)
    t = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return t[:length]


def get_key(purpose: Purpose) -> bytes:
    """Return key bytes for ``purpose``. Override / shim / HKDF-derived.

    Resolution order (first match wins):
      1. Per-purpose env override (e.g. ``MCP_API_BEARER_KEY``) — v1.5.1+,
         the safe-rotation path per RUNBOOK §3.4. Bypasses shim AND HKDF.
      2. Back-compat shim — purposes in ``_BACK_COMPAT_RAW_MASTER`` return
         the raw master bytes. Increments ``_shim_hit_counter`` for the
         soak-test telemetry (v1.5+).
      3. HKDF-derived — runs ``_hkdf_sha256(master, info)`` for the purpose.
         Enforces ≥32-char master.

    Master length check happens HERE, not at import, and only when the
    HKDF derivation path is actually exercised. Override and shim paths
    skip it.

    Every successful call (any path) increments the per-purpose total-call
    counter (v1.5.1, #28) so the preflight script can tell "zero shim hits
    because nobody called" apart from "zero shim hits because everyone
    migrated."

    Raises ``ValueError`` for unknown purposes (defensive — purposes
    are a closed set; an unknown one is a typo, never a runtime case).
    Raises ``RuntimeError`` if the override is set but shorter than 32
    chars (operator intent was clear; failing loud beats silent fallback).
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")

    # 1. Per-purpose override — explicit operator choice, highest precedence.
    # Read it BEFORE invoking _master() so an operator can use overrides
    # without setting MCP_BEARER_TOKEN at all. NOTE: override hits do NOT
    # touch the shim counter — they're not shim usage; conflating them
    # would lie to operators reading the preflight telemetry.
    override = os.environ.get(_OVERRIDE_ENV[purpose])
    if override:
        if len(override) < _MIN_MASTER_LEN:
            raise RuntimeError(
                f"{_OVERRIDE_ENV[purpose]} must be ≥{_MIN_MASTER_LEN} chars "
                f"(got {len(override)}). Set a longer value or unset the "
                f"override env var to fall back to the master."
            )
        with _total_call_counter_lock:
            _total_call_counter[purpose] = (
                _total_call_counter.get(purpose, 0) + 1
            )
        _record_first_call(purpose)
        return override.encode("utf-8")

    master = _master()

    if purpose in _BACK_COMPAT_RAW_MASTER:
        # Shim: return raw master bytes. No length check.
        # v1.5: instrument every shim-path hit so operators can confirm
        # zero active usage before v2.0b's strict-flip ships.
        with _shim_hit_counter_lock:
            _shim_hit_counter[purpose] = _shim_hit_counter.get(purpose, 0) + 1
        with _total_call_counter_lock:
            _total_call_counter[purpose] = (
                _total_call_counter.get(purpose, 0) + 1
            )
        _record_first_call(purpose)
        return master.encode("utf-8")

    # Derived path: enforce master length first.
    _validate_master_or_raise(master)
    derived = _hkdf_sha256(master.encode("utf-8"), _HKDF_INFO[purpose])
    with _total_call_counter_lock:
        _total_call_counter[purpose] = _total_call_counter.get(purpose, 0) + 1
    _record_first_call(purpose)
    return derived


def key_provenance(purpose: Purpose) -> KeyProvenance:
    """Report how a key WOULD be sourced for ``purpose`` — without deriving.

    Used by ``gdocs_server_info`` (v1.4.0+) to surface shim state
    without invoking the actual key material into a tool response.
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")
    master = _master()
    if purpose in _BACK_COMPAT_RAW_MASTER:
        return KeyProvenance(
            purpose=purpose, mechanism="raw_master_shim",
            master_len=len(master),
        )
    return KeyProvenance(
        purpose=purpose, mechanism="hkdf_derived",
        master_len=len(master),
    )


def is_shim_active() -> bool:
    """True if any purpose currently routes through the raw-master shim.

    Consumed by ``gdocs_server_info`` (v1.4.0+) so operators see the
    shim is active before v2.0's strict-flip deploy. Currently always
    True in v1.3.1; flips to False per-purpose as v2.0 removes
    entries from ``_BACK_COMPAT_RAW_MASTER``.
    """
    return bool(_BACK_COMPAT_RAW_MASTER)


def get_shim_hit_counters() -> dict[str, int]:
    """Snapshot of per-purpose shim-path hit counters (v1.5+).

    Returns a copy so callers can't mutate the live counter dict.
    Surfaced via ``gdocs_server_info().key_back_compat_shim_active_hits``
    — operators verify zero active usage in the last soak window before
    v2.0b's strict-flip ships, which would invalidate any in-flight key
    minted via the shim.

    Process-local: each replica reports its own count. Aggregate across
    replicas at read time.
    """
    with _shim_hit_counter_lock:
        return dict(_shim_hit_counter)


def _reset_shim_hit_counters_for_tests() -> None:
    """For tests only — reset all per-purpose shim hit counters to zero.

    Underscore-prefixed and named ``_for_tests`` because production
    code must never reset the counter (doing so would lie to operators
    about shim usage during the v2.0b soak window).
    """
    with _shim_hit_counter_lock:
        for purpose in _shim_hit_counter:
            _shim_hit_counter[purpose] = 0


def get_total_call_counters() -> dict[str, int]:
    """Snapshot of per-purpose ``get_key()`` total-call counters (v1.5.1+).

    Returns a copy so callers can't mutate the live counter dict.
    Surfaced via ``gdocs_server_info().key_call_totals`` — the
    denominator for the shim-hit telemetry. Without this, "shim_hits=0"
    is ambiguous (no traffic vs. full migration); with it, the preflight
    script asserts ``shim_hits==0 AND totals>=N`` before declaring it
    safe to ship v2.0b's strict-flip.

    Process-local: each replica reports its own count. Aggregate across
    replicas at read time.
    """
    with _total_call_counter_lock:
        return dict(_total_call_counter)


def _reset_total_call_counters_for_tests() -> None:
    """For tests only — reset all per-purpose total-call counters to zero.

    Underscore-prefixed and named ``_for_tests`` because production
    code must never reset this counter (it pairs with the shim counter
    as the denominator for v2.0b preflight gating).
    """
    with _total_call_counter_lock:
        for purpose in _total_call_counter:
            _total_call_counter[purpose] = 0


def get_first_call_timestamps() -> dict[str, float | None]:
    """Snapshot of per-purpose first-call timestamps (v2.6, #48).

    Each value is the unix-epoch seconds of the FIRST successful
    ``get_key(purpose)`` call in this process, or ``None`` if the
    purpose has not been requested. Returns a copy so callers can't
    mutate the live dict.

    Surfaced via
    ``gdocs_server_info().key_observability.first_call_age_seconds``
    (as ``time.time() - timestamp`` so the field is monotonically
    increasing while the process runs). Operators use this with the
    preflight script to gate the v2.0b strict-flip on a 1h30 soak
    window: a fresh restart reports zero hits AND zero totals, but
    a non-None first_call_at says "yes, we've had real traffic since
    boot, the zero shim hits are meaningful."

    Process-local: each replica reports its own first-call. The
    operator aggregates across replicas at read time (taking the
    MIN-non-None across replicas as the cluster-wide first-call,
    since the soak window starts from the earliest call anywhere).
    """
    with _first_call_at_lock:
        return dict(_first_call_at)


def _reset_first_call_timestamps_for_tests() -> None:
    """For tests only — reset all per-purpose first-call timestamps.

    Underscore-prefixed because production code must never reset
    these (doing so would lie to operators about how long the soak
    window has actually been running).
    """
    with _first_call_at_lock:
        for purpose in _first_call_at:
            _first_call_at[purpose] = None
