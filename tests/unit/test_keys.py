"""HKDF key derivation + back-compat shim tests (v1.3.1).

Guards the v1.3.1 design intent:
- All 3 covered purposes return the raw master via shim — preserves
  back-compat with v1.x bearer tokens, in-flight signed URLs, and
  OAuth states.
- The 32-char length check is DEFERRED to first derivation call, not
  applied at module import or in the shim path. Short legacy tokens
  must boot the server cleanly.
- ``is_shim_active()`` returns True throughout v1.3.x; flips per-purpose
  in v2.0+ as the shim removes.

The HKDF derived-path code is unreached in v1.3.1 (no callers) but
tested here so v2.0's strict-flip is a small, reviewable change.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def long_master(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    yield "x" * 32


@pytest.fixture
def short_master(monkeypatch):
    """A 16-char legacy token — pre-v1.3.1 deployments use these."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "y" * 16)
    yield "y" * 16


@pytest.fixture
def no_master(monkeypatch):
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)


# ---------------------------------------------------------------------
# Shim path — all 3 covered purposes return raw master
# ---------------------------------------------------------------------


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_shim_returns_raw_master_bytes(long_master, purpose):
    from google_docs_mcp.keys import get_key

    result = get_key(purpose)
    assert result == long_master.encode("utf-8")
    assert isinstance(result, bytes)


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_shim_skips_length_check_for_short_master(short_master, purpose):
    """Short legacy tokens must boot cleanly — the shim returns them
    without invoking the 32-char check."""
    from google_docs_mcp.keys import get_key

    result = get_key(purpose)
    assert result == short_master.encode("utf-8")
    assert len(result) == 16  # specifically the short length


# ---------------------------------------------------------------------
# Length check DEFERRED to derivation; never at import
# ---------------------------------------------------------------------


def test_module_imports_with_short_master(short_master):
    """Importing keys.py with a 16-char master must succeed."""
    import importlib
    import google_docs_mcp.keys as keys_mod
    importlib.reload(keys_mod)  # force re-import
    # Bare import path works; only the deferred-derivation check fires later.


def test_module_imports_with_no_master(no_master):
    """Importing keys.py with no MCP_BEARER_TOKEN set must succeed.

    The env-var check fires only when get_key() is called, not at import.
    """
    import importlib
    import google_docs_mcp.keys as keys_mod
    importlib.reload(keys_mod)


# ---------------------------------------------------------------------
# Unknown purpose rejection
# ---------------------------------------------------------------------


def test_unknown_purpose_raises_valueerror(long_master):
    from google_docs_mcp.keys import get_key

    with pytest.raises(ValueError, match="Unknown key purpose"):
        get_key("not_a_real_purpose")  # type: ignore[arg-type]


def test_provenance_unknown_purpose_raises(long_master):
    from google_docs_mcp.keys import key_provenance

    with pytest.raises(ValueError, match="Unknown key purpose"):
        key_provenance("oops")  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Master env var requirement
# ---------------------------------------------------------------------


def test_get_key_raises_when_master_unset(no_master):
    from google_docs_mcp.keys import get_key

    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        get_key("api_bearer")


# ---------------------------------------------------------------------
# Provenance + shim-active reporting
# ---------------------------------------------------------------------


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_provenance_reports_raw_master_shim_for_covered_purposes(
    long_master, purpose,
):
    from google_docs_mcp.keys import key_provenance

    prov = key_provenance(purpose)
    assert prov.purpose == purpose
    assert prov.mechanism == "raw_master_shim"
    assert prov.master_len == 32


def test_is_shim_active_returns_true_in_v1_3_1(long_master):
    """In v1.3.1, all 3 purposes route through the shim."""
    from google_docs_mcp.keys import is_shim_active

    assert is_shim_active() is True


# ---------------------------------------------------------------------
# HKDF derived path — unreached in v1.3.1, tested for v2.0 strict-flip
# ---------------------------------------------------------------------


def test_hkdf_helper_produces_32_byte_key(long_master):
    """Direct test of the HKDF primitive (bypasses the shim gate)."""
    from google_docs_mcp.keys import _hkdf_sha256

    out = _hkdf_sha256(b"x" * 32, b"google-docs-mcp v1 api_bearer")
    assert isinstance(out, bytes)
    assert len(out) == 32


def test_hkdf_deterministic_for_same_inputs(long_master):
    from google_docs_mcp.keys import _hkdf_sha256

    a = _hkdf_sha256(b"master-32-bytes-aaaaaaaaaaaaaaaa", b"info-1")
    b = _hkdf_sha256(b"master-32-bytes-aaaaaaaaaaaaaaaa", b"info-1")
    assert a == b


def test_hkdf_different_info_produces_different_key(long_master):
    """Per-purpose info strings MUST yield distinct keys."""
    from google_docs_mcp.keys import _hkdf_sha256, _HKDF_INFO

    master_bytes = b"master-32-bytes-aaaaaaaaaaaaaaaa"
    key_a = _hkdf_sha256(master_bytes, _HKDF_INFO["api_bearer"])
    key_b = _hkdf_sha256(master_bytes, _HKDF_INFO["oauth_state"])
    key_c = _hkdf_sha256(master_bytes, _HKDF_INFO["signed_url"])
    assert key_a != key_b
    assert key_b != key_c
    assert key_a != key_c


def test_validate_master_raises_on_short_token():
    """The length-check helper itself, tested in isolation."""
    from google_docs_mcp.keys import _validate_master_or_raise

    with pytest.raises(RuntimeError, match="≥32 chars"):
        _validate_master_or_raise("too-short")


def test_validate_master_accepts_exactly_32_chars():
    from google_docs_mcp.keys import _validate_master_or_raise

    _validate_master_or_raise("x" * 32)  # no raise


# ---------------------------------------------------------------------
# v1.5 observability: shim-path hit counter
# ---------------------------------------------------------------------


@pytest.fixture
def reset_shim_counters():
    """Zero the shim hit counters before each counter test."""
    from google_docs_mcp.keys import _reset_shim_hit_counters_for_tests

    _reset_shim_hit_counters_for_tests()
    yield
    _reset_shim_hit_counters_for_tests()


def test_shim_hit_counter_increments_on_shim_path(long_master, reset_shim_counters):
    """Each get_key() call through the shim path must bump the counter
    for that purpose by exactly 1."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    for _ in range(7):
        get_key("api_bearer")

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 7
    # Untouched purposes stay at 0.
    assert counters["oauth_state"] == 0
    assert counters["signed_url"] == 0


def test_shim_hit_counter_separate_per_purpose(long_master, reset_shim_counters):
    """Counters track per-purpose, not aggregate."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    for _ in range(3):
        get_key("api_bearer")
    for _ in range(5):
        get_key("oauth_state")
    for _ in range(2):
        get_key("signed_url")

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 3
    assert counters["oauth_state"] == 5
    assert counters["signed_url"] == 2


def test_shim_hit_counter_is_threadsafe(long_master, reset_shim_counters):
    """Concurrent get_key() calls from multiple threads must not lose
    increments — the threading.Lock is load-bearing under Starlette+uvicorn."""
    import threading
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    n_threads = 16
    calls_per_thread = 200
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()  # maximize contention
        for _ in range(calls_per_thread):
            get_key("api_bearer")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == n_threads * calls_per_thread


def test_get_shim_hit_counters_returns_copy(long_master, reset_shim_counters):
    """Mutating the returned dict must not corrupt the live counter."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    get_key("api_bearer")
    snapshot = get_shim_hit_counters()
    snapshot["api_bearer"] = 999_999  # mutate the snapshot

    # Live counter is unaffected.
    assert get_shim_hit_counters()["api_bearer"] == 1


def test_shim_hit_counter_unaffected_by_unknown_purpose(long_master, reset_shim_counters):
    """ValueError-raising calls must NOT touch the counter."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    with pytest.raises(ValueError):
        get_key("not_a_real_purpose")  # type: ignore[arg-type]

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 0
    assert counters["oauth_state"] == 0
    assert counters["signed_url"] == 0


# ---------------------------------------------------------------------
# v1.5.1: per-purpose env-var overrides
# ---------------------------------------------------------------------


@pytest.fixture
def reset_all_counters():
    """Zero BOTH the shim hit counter AND the total-call counter
    before/after each test. Used by tests that touch get_key() and
    need a clean denominator."""
    from google_docs_mcp.keys import (
        _reset_shim_hit_counters_for_tests,
        _reset_total_call_counters_for_tests,
    )

    _reset_shim_hit_counters_for_tests()
    _reset_total_call_counters_for_tests()
    yield
    _reset_shim_hit_counters_for_tests()
    _reset_total_call_counters_for_tests()


def test_env_override_returns_override_bytes_not_shim(
    long_master, monkeypatch, reset_all_counters,
):
    """When MCP_API_BEARER_KEY is set ≥32 chars, get_key('api_bearer')
    must return THOSE bytes — not the master, not an HKDF-derived value."""
    from google_docs_mcp.keys import get_key

    override = "Z" * 40  # distinct from long_master ('x' * 32)
    monkeypatch.setenv("MCP_API_BEARER_KEY", override)

    result = get_key("api_bearer")
    assert result == override.encode("utf-8")
    # Sanity: definitely NOT the master.
    assert result != long_master.encode("utf-8")


@pytest.mark.parametrize(
    "purpose,env_var",
    [
        ("api_bearer", "MCP_API_BEARER_KEY"),
        ("oauth_state", "OAUTH_STATE_SIGNING_KEY"),
        ("signed_url", "SIGNED_URL_SIGNING_KEY"),
    ],
)
def test_env_override_works_for_all_three_purposes(
    long_master, monkeypatch, reset_all_counters, purpose, env_var,
):
    """All 3 purposes must honor their respective override env vars."""
    from google_docs_mcp.keys import get_key

    override = ("Q" * 32) + purpose  # purpose-specific to confirm routing
    monkeypatch.setenv(env_var, override)

    assert get_key(purpose) == override.encode("utf-8")  # type: ignore[arg-type]


def test_env_override_rejects_short_value(
    long_master, monkeypatch, reset_all_counters,
):
    """A short override (<32 chars) must fail loud — operator intent was
    explicit; silent fallback would hide a misconfiguration."""
    from google_docs_mcp.keys import get_key

    monkeypatch.setenv("MCP_API_BEARER_KEY", "too-short")

    with pytest.raises(RuntimeError, match="MCP_API_BEARER_KEY.*≥32 chars"):
        get_key("api_bearer")


def test_env_override_bypasses_shim_counter(
    long_master, monkeypatch, reset_all_counters,
):
    """Override hits are NOT shim hits — the shim counter must stay 0
    when the override path is taken. Conflating them would lie to
    operators reading the preflight telemetry."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    monkeypatch.setenv("MCP_API_BEARER_KEY", "Z" * 40)
    for _ in range(10):
        get_key("api_bearer")

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 0, (
        "override path leaked into the shim counter — that breaks the "
        "v2.0b preflight signal"
    )
    # Other purposes unchanged either way.
    assert counters["oauth_state"] == 0
    assert counters["signed_url"] == 0


def test_env_override_empty_string_falls_through_to_shim(
    long_master, monkeypatch, reset_all_counters,
):
    """An empty-string override is treated as 'not set' so an operator
    can effectively un-set an override without restarting just to
    clear the env. (Matches the ``if override:`` truthiness check.)"""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    monkeypatch.setenv("MCP_API_BEARER_KEY", "")
    result = get_key("api_bearer")

    # Falls through to the shim, returning the master.
    assert result == long_master.encode("utf-8")
    assert get_shim_hit_counters()["api_bearer"] == 1


# ---------------------------------------------------------------------
# v1.5.1 (#28): total-call denominator counter
# ---------------------------------------------------------------------


def test_total_call_counter_increments_on_shim_path(
    long_master, reset_all_counters,
):
    """Every successful get_key() call through the shim must bump the
    total-call counter for that purpose."""
    from google_docs_mcp.keys import get_key, get_total_call_counters

    for _ in range(7):
        get_key("api_bearer")

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 7
    assert totals["oauth_state"] == 0
    assert totals["signed_url"] == 0


def test_total_call_counter_increments_on_override_path(
    long_master, monkeypatch, reset_all_counters,
):
    """Override path must also count toward totals — the denominator
    spans ALL paths, not just shim."""
    from google_docs_mcp.keys import (
        get_key, get_total_call_counters, get_shim_hit_counters,
    )

    monkeypatch.setenv("MCP_API_BEARER_KEY", "Z" * 40)
    for _ in range(5):
        get_key("api_bearer")

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 5
    # Shim counter unaffected, totals counter bumped — exactly the
    # signal the preflight script needs.
    assert get_shim_hit_counters()["api_bearer"] == 0


def test_total_call_counter_increments_on_derived_path(
    long_master, monkeypatch, reset_all_counters,
):
    """Even for the (currently unreached) HKDF derived path, totals
    must increment. Future-proofs the preflight signal post-v2.0b
    when shim entries start coming out of _BACK_COMPAT_RAW_MASTER."""
    from google_docs_mcp import keys as keys_mod
    from google_docs_mcp.keys import get_key, get_total_call_counters

    # Temporarily empty the shim set so 'api_bearer' takes the
    # derived path. Restore after.
    original = keys_mod._BACK_COMPAT_RAW_MASTER
    monkeypatch.setattr(keys_mod, "_BACK_COMPAT_RAW_MASTER", frozenset())
    try:
        for _ in range(4):
            get_key("api_bearer")
    finally:
        monkeypatch.setattr(keys_mod, "_BACK_COMPAT_RAW_MASTER", original)

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 4


def test_total_call_counter_unaffected_by_unknown_purpose(
    long_master, reset_all_counters,
):
    """ValueError-raising calls must NOT touch the total-call counter
    either — same invariant as the shim counter."""
    from google_docs_mcp.keys import get_key, get_total_call_counters

    with pytest.raises(ValueError):
        get_key("not_a_real_purpose")  # type: ignore[arg-type]

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 0
    assert totals["oauth_state"] == 0
    assert totals["signed_url"] == 0


def test_total_call_counter_unaffected_by_short_override(
    long_master, monkeypatch, reset_all_counters,
):
    """An override that fails the length check must NOT count as a
    successful call — the failure is a misconfiguration, not traffic."""
    from google_docs_mcp.keys import get_key, get_total_call_counters

    monkeypatch.setenv("MCP_API_BEARER_KEY", "too-short")

    with pytest.raises(RuntimeError):
        get_key("api_bearer")

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 0


def test_get_total_call_counters_returns_copy(
    long_master, reset_all_counters,
):
    """Mutating the returned dict must not corrupt the live counter."""
    from google_docs_mcp.keys import get_key, get_total_call_counters

    get_key("api_bearer")
    snapshot = get_total_call_counters()
    snapshot["api_bearer"] = 999_999

    assert get_total_call_counters()["api_bearer"] == 1
