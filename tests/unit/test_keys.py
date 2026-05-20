"""HKDF key derivation tests — post-v2.0b strict-flip world.

Guards the v2.0b design intent:
- ``_BACK_COMPAT_RAW_MASTER`` is now the empty frozenset; every
  purpose falls through to HKDF derivation unless an env-var override
  is set (per v1.5.1).
- The 32-char length check on the master is enforced for every HKDF
  call (was previously bypassed by the shim path).
- ``is_shim_active()`` returns False throughout v2.x.
- Override path (``MCP_API_BEARER_KEY`` etc.) bypasses HKDF entirely;
  see v1.5.1 RUNBOOK §3.4 for the rotation procedure that path enables.

Some tests below intentionally invoke ``monkeypatch`` to re-populate
``_BACK_COMPAT_RAW_MASTER`` temporarily — these are the "shim still
works as a mechanism" tests, kept so a future minor version could
add a purpose back if needed without rebuilding the scaffolding.
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
# Shim path (mechanism) — post-v2.0b the shim set is empty, but the
# mechanism is still wired up in case a future minor adds a purpose
# back. These tests monkeypatch _BACK_COMPAT_RAW_MASTER to exercise
# that code path.
# ---------------------------------------------------------------------


@pytest.fixture
def populated_shim(monkeypatch):
    """Temporarily re-populate the shim set with all 3 purposes so
    pre-v2.0b mechanism tests still exercise the shim path."""
    from google_docs_mcp import keys as keys_mod

    monkeypatch.setattr(
        keys_mod,
        "_BACK_COMPAT_RAW_MASTER",
        frozenset({"api_bearer", "oauth_state", "signed_url"}),
    )
    yield


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_shim_returns_raw_master_bytes(long_master, populated_shim, purpose):
    """When the shim set contains a purpose, get_key returns raw master.
    Guards the mechanism even though v2.0b ships with the set empty."""
    from google_docs_mcp.keys import get_key

    result = get_key(purpose)
    assert result == long_master.encode("utf-8")
    assert isinstance(result, bytes)


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_shim_skips_length_check_for_short_master(
    short_master, populated_shim, purpose,
):
    """When a purpose IS in the shim set, the shim returns short masters
    without invoking the 32-char check. Empty-set in v2.0b means the
    length check now ALWAYS fires (covered by test_strict_flip_*)."""
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
def test_provenance_reports_raw_master_shim_when_in_shim_set(
    long_master, populated_shim, purpose,
):
    """When a purpose IS in the shim set, provenance reports raw_master_shim.
    With v2.0b's empty set, every purpose now reports hkdf_derived —
    see test_strict_flip_provenance_reports_hkdf_derived."""
    from google_docs_mcp.keys import key_provenance

    prov = key_provenance(purpose)
    assert prov.purpose == purpose
    assert prov.mechanism == "raw_master_shim"
    assert prov.master_len == 32


def test_is_shim_active_returns_false_after_strict_flip(long_master):
    """v2.0b: _BACK_COMPAT_RAW_MASTER is empty → is_shim_active() is False.
    Pre-flip (v1.x) this returned True."""
    from google_docs_mcp.keys import is_shim_active

    assert is_shim_active() is False


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


def test_shim_hit_counter_increments_on_shim_path(
    long_master, populated_shim, reset_shim_counters,
):
    """Each get_key() call through the shim path must bump the counter
    for that purpose by exactly 1.

    Post-v2.0b note: requires ``populated_shim`` because the live
    _BACK_COMPAT_RAW_MASTER is empty; only the mechanism is tested here."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    for _ in range(7):
        get_key("api_bearer")

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 7
    # Untouched purposes stay at 0.
    assert counters["oauth_state"] == 0
    assert counters["signed_url"] == 0


def test_shim_hit_counter_separate_per_purpose(
    long_master, populated_shim, reset_shim_counters,
):
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


def test_shim_hit_counter_is_threadsafe(
    long_master, populated_shim, reset_shim_counters,
):
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


def test_get_shim_hit_counters_returns_copy(
    long_master, populated_shim, reset_shim_counters,
):
    """Mutating the returned dict must not corrupt the live counter."""
    from google_docs_mcp.keys import get_key, get_shim_hit_counters

    get_key("api_bearer")
    snapshot = get_shim_hit_counters()
    snapshot["api_bearer"] = 999_999  # mutate the snapshot

    # Live counter is unaffected.
    assert get_shim_hit_counters()["api_bearer"] == 1


def test_shim_hit_counter_unaffected_by_unknown_purpose(
    long_master, populated_shim, reset_shim_counters,
):
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
    long_master, monkeypatch, populated_shim, reset_all_counters,
):
    """Override hits are NOT shim hits — the shim counter must stay 0
    when the override path is taken. Conflating them would lie to
    operators reading the preflight telemetry.

    Post-v2.0b note: uses ``populated_shim`` so the test exercises a
    real branch decision (override-vs-shim) rather than override-vs-
    HKDF; either branch keeps the shim counter at 0, but the populated
    case is the historically interesting one."""
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


def test_env_override_empty_string_falls_through_to_hkdf(
    long_master, monkeypatch, reset_all_counters,
):
    """An empty-string override is treated as 'not set' so an operator
    can effectively un-set an override without restarting just to
    clear the env. (Matches the ``if override:`` truthiness check.)

    Post-v2.0b: with the shim removed, fall-through lands on HKDF
    derivation instead of returning the raw master. Pre-flip this
    test asserted master-bytes + shim_counter==1; post-flip we
    assert HKDF-derived bytes + shim_counter==0."""
    from google_docs_mcp.keys import (
        _HKDF_INFO, _hkdf_sha256, get_key, get_shim_hit_counters,
    )

    monkeypatch.setenv("MCP_API_BEARER_KEY", "")
    result = get_key("api_bearer")

    expected = _hkdf_sha256(
        long_master.encode("utf-8"), _HKDF_INFO["api_bearer"],
    )
    assert result == expected
    assert result != long_master.encode("utf-8")
    # Empty override means no override path; shim is empty so it
    # didn't run either — counter stays 0.
    assert get_shim_hit_counters()["api_bearer"] == 0


# ---------------------------------------------------------------------
# v1.5.1 (#28): total-call denominator counter
# ---------------------------------------------------------------------


def test_total_call_counter_increments_on_shim_path(
    long_master, populated_shim, reset_all_counters,
):
    """Every successful get_key() call through the shim must bump the
    total-call counter for that purpose.

    Post-v2.0b note: requires ``populated_shim`` because the live
    _BACK_COMPAT_RAW_MASTER is empty; this asserts the counter still
    increments when a future minor re-adds a purpose."""
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
    """Even on the HKDF derived path, totals must increment. After
    v2.0b's strict-flip this is the DEFAULT path (no monkeypatch
    needed) but the explicit setattr is kept for defensive clarity
    against any future change to _BACK_COMPAT_RAW_MASTER."""
    from google_docs_mcp import keys as keys_mod
    from google_docs_mcp.keys import get_key, get_total_call_counters

    # Temporarily empty the shim set so 'api_bearer' takes the
    # derived path. Restore after. Post-v2.0b: this is a no-op
    # (the live set is already empty) but the explicit override
    # remains useful as a guard against accidental re-population.
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


# ---------------------------------------------------------------------
# v2.0b strict-flip — the breaking change
# ---------------------------------------------------------------------


def test_strict_flip_shim_is_empty():
    """v2.0b ships with _BACK_COMPAT_RAW_MASTER = frozenset(). Symbol
    preserved for importers; the empty set is what makes get_key route
    every purpose through HKDF."""
    from google_docs_mcp.keys import _BACK_COMPAT_RAW_MASTER

    assert _BACK_COMPAT_RAW_MASTER == frozenset(), (
        "v2.0b regression: _BACK_COMPAT_RAW_MASTER must be empty. Any "
        "entry resurrects mass-invalidation risk at the next removal."
    )
    assert isinstance(_BACK_COMPAT_RAW_MASTER, frozenset)


@pytest.mark.parametrize("purpose", ["api_bearer", "oauth_state", "signed_url"])
def test_strict_flip_get_key_derives_via_hkdf_when_no_override(
    long_master, purpose,
):
    """Post-flip: get_key returns HKDF-derived bytes, NOT the raw master."""
    from google_docs_mcp.keys import _HKDF_INFO, _hkdf_sha256, get_key

    result = get_key(purpose)
    expected = _hkdf_sha256(long_master.encode("utf-8"), _HKDF_INFO[purpose])

    assert result == expected
    # Defensively confirm it's NOT the raw master — the whole point of v2.0b.
    assert result != long_master.encode("utf-8")
    assert len(result) == 32


def test_strict_flip_get_key_rejects_short_master_for_all_purposes(short_master):
    """Pre-flip, the shim let short tokens through for the 3 covered
    purposes. Post-flip, ALL purposes require ≥32-char master because
    every path goes through HKDF."""
    from google_docs_mcp.keys import get_key

    for purpose in ("api_bearer", "oauth_state", "signed_url"):
        with pytest.raises(RuntimeError, match="≥32 chars"):
            get_key(purpose)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "purpose,env_var",
    [
        ("api_bearer", "MCP_API_BEARER_KEY"),
        ("oauth_state", "OAUTH_STATE_SIGNING_KEY"),
        ("signed_url", "SIGNED_URL_SIGNING_KEY"),
    ],
)
def test_strict_flip_override_still_works(
    long_master, monkeypatch, purpose, env_var,
):
    """v1.5.1's env-var override path must continue to function post-flip.

    With the shim gone, overrides are the ONLY way to pin a purpose to
    a specific key — required for the safe-rotation procedure documented
    in RUNBOOK §3.4. Tightened from the forward-compat version (which
    accepted either override bytes OR HKDF bytes) now that v1.5.1 has
    merged: we now assert the override bytes are returned, period.
    """
    from google_docs_mcp.keys import get_key

    override = "Z" * 40  # distinct from long_master ('x' * 32)
    monkeypatch.setenv(env_var, override)

    result = get_key(purpose)  # type: ignore[arg-type]

    assert result == override.encode("utf-8"), (
        f"override env var {env_var} did not win for purpose {purpose!r}; "
        f"v1.5.1 override path is broken or has been removed"
    )
    # Sanity: definitely NOT what HKDF would have produced.
    assert result != long_master.encode("utf-8")


def test_strict_flip_increments_total_call_counter_not_shim_hit_counter(
    long_master, reset_all_counters,
):
    """After the flip, get_key() calls must NOT touch the shim counter
    (because the shim set is empty), but v1.5.1's total-call counter
    MUST increment so the preflight signal stays meaningful.

    Tightened from the forward-compat version (which had a try/except
    ImportError around get_total_call_counters) now that v1.5.1 is in
    main and the denominator is guaranteed to exist.
    """
    from google_docs_mcp.keys import (
        get_key,
        get_shim_hit_counters,
        get_total_call_counters,
    )

    for _ in range(5):
        get_key("api_bearer")

    counters = get_shim_hit_counters()
    assert counters["api_bearer"] == 0, (
        "shim counter incremented post-flip — strict-flip is broken: "
        "_BACK_COMPAT_RAW_MASTER must be empty AND get_key's shim "
        "conditional must check it"
    )
    assert counters["oauth_state"] == 0
    assert counters["signed_url"] == 0

    totals = get_total_call_counters()
    assert totals["api_bearer"] == 5, (
        "v1.5.1 total-call counter didn't increment on HKDF path — "
        "denominator is broken; preflight script will see no traffic"
    )
    # Untouched purposes stay at 0.
    assert totals["oauth_state"] == 0
    assert totals["signed_url"] == 0
