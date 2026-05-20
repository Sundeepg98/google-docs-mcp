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

**v2.1 M1a refactor.** The mechanism layer has been promoted to
``key_provider.py`` — Protocol + 3 adapters + LayeredKeyProvider
composite. This module remains the public facade: every pre-v2.1
function signature is preserved, and every internal symbol that tests
or external code monkeypatched (``_BACK_COMPAT_RAW_MASTER``,
``_reset_*_for_tests``) is preserved too. The refactor is purely
internal — no behavior change for callers. See ``key_provider.py``
module docstring for the Hex-style port-and-adapters rationale.

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

import os
from typing import Literal

from .key_provider import (
    HKDFKeyProvider,
    KeyProvenance,
    LayeredKeyProvider,
    build_default_provider,
    get_active_provider,
    set_key_provider,
)

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
#
# v2.1 NOTE: this symbol is read at call time by
# ``RawMasterShimKeyProvider`` (via ``build_default_provider``'s
# resolver closure). Tests that ``monkeypatch.setattr`` this to a
# non-empty frozenset still take effect for the next ``get_key()``
# call without needing to swap out the active provider.
_BACK_COMPAT_RAW_MASTER: frozenset[str] = frozenset()


# Constants preserved at module level for backward-compat (tests + other
# code import these directly). The duplication with ``key_provider.py``
# is intentional — those are the adapter-internal source of truth, these
# are the historical public surface.
_HKDF_INFO: dict[str, bytes] = {
    "api_bearer": b"google-docs-mcp v1 api_bearer",
    "oauth_state": b"google-docs-mcp v1 oauth_state",
    "signed_url": b"google-docs-mcp v1 signed_url",
}

_OVERRIDE_ENV: dict[str, str] = {
    "api_bearer": "MCP_API_BEARER_KEY",
    "oauth_state": "OAUTH_STATE_SIGNING_KEY",
    "signed_url": "SIGNED_URL_SIGNING_KEY",
}

Purpose = Literal["api_bearer", "oauth_state", "signed_url"]

_MIN_MASTER_LEN = 32


def _validate_master_or_raise(master: str) -> None:
    """Enforce ≥32 chars. Called only when derivation is actually needed.

    NEVER call at module import — short legacy tokens must boot cleanly
    and only fail when a derived purpose is requested. The shim path
    skips this entirely.

    **v2.1**: the actual enforcement is inside ``HKDFKeyProvider.get_key``;
    this helper is preserved for backward-compat with tests that import
    it directly. Same threshold, same error shape.
    """
    if len(master) < _MIN_MASTER_LEN:
        raise RuntimeError(
            f"MCP_BEARER_TOKEN must be ≥{_MIN_MASTER_LEN} chars for "
            f"HKDF derivation (got {len(master)}). Either lengthen "
            f"the token or use a purpose currently in the back-compat "
            f"shim. See THREAT_MODEL.md §5 for rotation guidance."
        )


# ---------------------------------------------------------------------
# Wire the default provider at import time.
# ---------------------------------------------------------------------


def _shim_set_resolver() -> frozenset[str]:
    """Read the current ``_BACK_COMPAT_RAW_MASTER`` set at call time.

    Lazily-bound so tests that ``monkeypatch.setattr("keys._BACK_COMPAT_RAW_MASTER", ...)``
    take effect immediately — no need to re-create the provider.
    """
    return _BACK_COMPAT_RAW_MASTER


# Build the default LayeredKeyProvider chain and register as the
# process-wide active provider. The chain matches the pre-v2.1
# resolution order: env override > shim > HKDF.
_default_provider: LayeredKeyProvider = build_default_provider(_shim_set_resolver)
set_key_provider(_default_provider)


# ---------------------------------------------------------------------
# Backward-compat: pre-v2.1 module-level counters + locks.
# ---------------------------------------------------------------------
# Pre-v2.1, ``keys.py`` owned the counters directly:
#     _shim_hit_counter, _shim_hit_counter_lock,
#     _total_call_counter, _total_call_counter_lock,
#     _first_call_at, _first_call_at_lock
# Test fixtures (conftest.py, test_isolated_db_fixture.py) reach into
# these by name. v2.1 moved the LIVE counters into LayeredKeyProvider,
# but we preserve the module-level symbols as thin live-view proxies
# so the existing test scaffolding keeps working without rewrites.
#
# The dicts below are the SAME objects as the provider's internal
# counters — mutating ``keys._shim_hit_counter[...]`` updates the
# provider's view. This is achieved by binding to the provider's
# internal dict attribute at import (since the default provider is
# constructed above).
_shim_hit_counter: dict[str, int] = _default_provider._shim_hits
_shim_hit_counter_lock = _default_provider._lock
_total_call_counter: dict[str, int] = _default_provider._totals
_total_call_counter_lock = _default_provider._lock
_first_call_at: dict[str, float | None] = _default_provider._first_call
_first_call_at_lock = _default_provider._lock


def _provider() -> LayeredKeyProvider:
    """Return the active provider as a LayeredKeyProvider.

    The active provider can be swapped via ``with_key_provider(...)``
    for tests; that swap may install a non-Layered provider (e.g. a
    bare ``InMemoryKeyProvider``). The observability accessors below
    (``get_shim_hit_counters``, etc.) require a Layered instance —
    they fall back to the module-level ``_default_provider`` when a
    non-Layered provider is active. Tests that care about counters
    should inject via the layered composite explicitly.
    """
    active = get_active_provider()
    if isinstance(active, LayeredKeyProvider):
        return active
    return _default_provider


# ---------------------------------------------------------------------
# Facade — public API preserved bit-for-bit from pre-v2.1
# ---------------------------------------------------------------------


def get_key(purpose: Purpose) -> bytes:
    """Return key bytes for ``purpose``. Override / shim / HKDF-derived.

    Resolution order (first match wins):
      1. Per-purpose env override (e.g. ``MCP_API_BEARER_KEY``) — v1.5.1+,
         the safe-rotation path per RUNBOOK §3.4. Bypasses shim AND HKDF.
      2. Back-compat shim — purposes in ``_BACK_COMPAT_RAW_MASTER`` return
         the raw master bytes. Increments shim-path hit counter for the
         soak-test telemetry (v1.5+).
      3. HKDF-derived — runs HKDF-SHA256 for the purpose. Enforces ≥32-char
         master.

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

    **v2.1**: implementation delegates to the active ``KeyProvider``
    (the layered ``[Env, Shim, HKDF]`` chain by default). Behavior is
    byte-identical to pre-v2.1; see ``key_provider.py`` for the port shape.
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")
    provider = get_active_provider()
    key = provider.get_key(purpose)
    if key is None:
        # Defensive: a layered provider should always serve a known
        # purpose (HKDF is the unconditional fallback). If we land
        # here it means a test injected a partial InMemoryKeyProvider
        # that doesn't cover this purpose — surface clearly.
        raise RuntimeError(
            f"Active KeyProvider returned no key for purpose {purpose!r}. "
            f"If you're injecting an InMemoryKeyProvider, ensure it "
            f"includes this purpose."
        )
    return key


def key_provenance(purpose: Purpose) -> KeyProvenance:
    """Report how a key WOULD be sourced for ``purpose`` — without deriving.

    Used by ``gdocs_server_info`` (v1.4.0+) to surface shim state
    without invoking the actual key material into a tool response.
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")
    provider = get_active_provider()
    prov = provider.provenance(purpose)
    if prov is None:
        # Active provider doesn't cover this purpose. Fall back to the
        # default layered chain so observability never returns None.
        prov = _default_provider.provenance(purpose)
    if prov is None:
        # Defensive — HKDFKeyProvider in the default chain always
        # provides provenance for known purposes. Reaching here means
        # the constants got out of sync between modules.
        master = os.environ.get("MCP_BEARER_TOKEN", "")
        return KeyProvenance(
            purpose=purpose, mechanism="hkdf_derived", master_len=len(master),
        )
    # Pre-v2.1 contract: only the two mechanisms "raw_master_shim" /
    # "hkdf_derived" appeared. v2.1 adds "env_override" but legacy
    # callers (e.g. /info endpoint serializer) may still type-check
    # against the old union. The new value is additive; not a breaking
    # change.
    return prov


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
    return _provider().shim_hit_counters()


def _reset_shim_hit_counters_for_tests() -> None:
    """For tests only — reset all per-purpose shim hit counters to zero.

    Underscore-prefixed and named ``_for_tests`` because production
    code must never reset the counter (doing so would lie to operators
    about shim usage during the v2.0b soak window).
    """
    _provider()._reset_shim_hits()


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
    return _provider().total_call_counters()


def _reset_total_call_counters_for_tests() -> None:
    """For tests only — reset all per-purpose total-call counters to zero.

    Underscore-prefixed and named ``_for_tests`` because production
    code must never reset this counter (it pairs with the shim counter
    as the denominator for v2.0b preflight gating).
    """
    _provider()._reset_totals()


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
    return _provider().first_call_timestamps()


def _reset_first_call_timestamps_for_tests() -> None:
    """For tests only — reset all per-purpose first-call timestamps.

    Underscore-prefixed because production code must never reset
    these (doing so would lie to operators about how long the soak
    window has actually been running).
    """
    _provider()._reset_first_call()


# Re-export internal helper used by the test suite via direct import.
# Pre-v2.1 ``_hkdf_sha256`` lived in this module; v2.1 moved it to
# ``key_provider`` but tests + the HKDFKeyProvider both reference the
# function. Re-export keeps the historical import path working.
from .key_provider import _hkdf_sha256  # noqa: E402, F401 — re-export for test compat
