"""KeyProvider Port + Adapters tests (v2.1 M1a POC).

Exercises the new Hex-style port shape in isolation from ``keys.py``:

- Protocol conformance for all 3 production adapters + InMemoryKeyProvider
- LayeredKeyProvider resolution order + counter discipline
- with_key_provider context manager (test-injection ergonomics)
- Provenance reporting WITHOUT touching key bytes (observability)
"""
from __future__ import annotations

import threading

import pytest

from appscriptly.key_provider import (
    EnvOverrideKeyProvider,
    HKDFKeyProvider,
    InMemoryKeyProvider,
    KeyProvenance,
    KeyProvider,
    LayeredKeyProvider,
    RawMasterShimKeyProvider,
    build_default_provider,
    get_active_provider,
    set_key_provider,
    with_key_provider,
)


# ---------------------------------------------------------------------
# Protocol conformance — every adapter satisfies KeyProvider at runtime
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory",
    [
        lambda: EnvOverrideKeyProvider(),
        lambda: RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"})),
        lambda: HKDFKeyProvider(),
        lambda: InMemoryKeyProvider({"api_bearer": b"x" * 32}),
        lambda: LayeredKeyProvider([HKDFKeyProvider()]),
    ],
)
def test_adapter_satisfies_key_provider_protocol(adapter_factory):
    adapter = adapter_factory()
    assert isinstance(adapter, KeyProvider)


# ---------------------------------------------------------------------
# EnvOverrideKeyProvider
# ---------------------------------------------------------------------


def test_env_override_returns_none_when_not_set(monkeypatch):
    monkeypatch.delenv("MCP_API_BEARER_KEY", raising=False)
    adapter = EnvOverrideKeyProvider()
    assert adapter.get_key("api_bearer") is None
    assert adapter.provenance("api_bearer") is None


def test_env_override_returns_bytes_when_set(monkeypatch):
    monkeypatch.setenv("MCP_API_BEARER_KEY", "x" * 32)
    adapter = EnvOverrideKeyProvider()
    assert adapter.get_key("api_bearer") == b"x" * 32
    prov = adapter.provenance("api_bearer")
    assert prov is not None
    assert prov.mechanism == "env_override"
    assert prov.master_len == 32


def test_env_override_rejects_short_value(monkeypatch):
    monkeypatch.setenv("MCP_API_BEARER_KEY", "short")
    adapter = EnvOverrideKeyProvider()
    with pytest.raises(RuntimeError, match="≥32"):
        adapter.get_key("api_bearer")


# ---------------------------------------------------------------------
# RawMasterShimKeyProvider
# ---------------------------------------------------------------------


def test_shim_returns_master_when_purpose_in_shim_set(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "short-master")
    adapter = RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"}))
    assert adapter.get_key("api_bearer") == b"short-master"
    # Shim path skips the 32-char length check (v1.x compat).
    prov = adapter.provenance("api_bearer")
    assert prov is not None
    assert prov.mechanism == "raw_master_shim"


def test_shim_defers_when_purpose_not_in_set(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    adapter = RawMasterShimKeyProvider(lambda: frozenset())  # empty shim
    assert adapter.get_key("api_bearer") is None
    assert adapter.provenance("api_bearer") is None


def test_shim_set_resolver_called_at_call_time(monkeypatch):
    """The resolver MUST be invoked on every call so monkeypatching
    ``keys._BACK_COMPAT_RAW_MASTER`` mid-test takes effect immediately."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    shim_set = frozenset()
    adapter = RawMasterShimKeyProvider(lambda: shim_set)
    assert adapter.get_key("api_bearer") is None
    shim_set = frozenset({"api_bearer"})
    assert adapter.get_key("api_bearer") == b"x" * 32


# ---------------------------------------------------------------------
# HKDFKeyProvider
# ---------------------------------------------------------------------


def test_hkdf_derives_per_purpose_distinct_keys(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    adapter = HKDFKeyProvider()
    k1 = adapter.get_key("api_bearer")
    k2 = adapter.get_key("oauth_state")
    k3 = adapter.get_key("signed_url")
    assert k1 and k2 and k3
    assert k1 != k2 != k3, "HKDF info strings must produce distinct keys"
    # 32-byte HKDF output is the contract.
    assert len(k1) == len(k2) == len(k3) == 32


def test_hkdf_rejects_short_master(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "short")
    adapter = HKDFKeyProvider()
    with pytest.raises(RuntimeError, match="≥32"):
        adapter.get_key("api_bearer")


def test_hkdf_raises_when_master_missing(monkeypatch):
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    adapter = HKDFKeyProvider()
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN"):
        adapter.get_key("api_bearer")


# ---------------------------------------------------------------------
# LayeredKeyProvider — resolution order + counter discipline
# ---------------------------------------------------------------------


def test_layered_first_match_wins(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    monkeypatch.setenv("MCP_API_BEARER_KEY", "y" * 32)  # env override
    chain = LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"})),
        HKDFKeyProvider(),
    ])
    assert chain.get_key("api_bearer") == b"y" * 32  # env wins


def test_layered_falls_through_to_hkdf(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    monkeypatch.delenv("MCP_API_BEARER_KEY", raising=False)
    chain = LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(lambda: frozenset()),  # empty shim
        HKDFKeyProvider(),
    ])
    key = chain.get_key("api_bearer")
    assert key is not None
    assert len(key) == 32
    # HKDF output is bytes, NOT the raw master.
    assert key != b"x" * 32


def test_layered_shim_hit_counter_only_increments_on_shim(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    monkeypatch.setenv("MCP_API_BEARER_KEY", "y" * 32)
    chain = LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"})),
        HKDFKeyProvider(),
    ])
    # Env override serves: shim should NOT increment.
    chain.get_key("api_bearer")
    assert chain.shim_hit_counters()["api_bearer"] == 0
    # Total still counts.
    assert chain.total_call_counters()["api_bearer"] == 1


def test_layered_shim_hit_counter_increments_when_shim_serves(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    monkeypatch.delenv("MCP_API_BEARER_KEY", raising=False)
    chain = LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"})),
        HKDFKeyProvider(),
    ])
    chain.get_key("api_bearer")
    assert chain.shim_hit_counters()["api_bearer"] == 1
    assert chain.total_call_counters()["api_bearer"] == 1


def test_layered_first_call_timestamp_stamps_on_first_only(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    chain = LayeredKeyProvider([HKDFKeyProvider()])
    assert chain.first_call_timestamps()["api_bearer"] is None
    chain.get_key("api_bearer")
    first_ts = chain.first_call_timestamps()["api_bearer"]
    assert first_ts is not None
    chain.get_key("api_bearer")
    # Second call must NOT bump the first-call timestamp.
    assert chain.first_call_timestamps()["api_bearer"] == first_ts


def test_layered_counter_increments_are_threadsafe(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    chain = LayeredKeyProvider([HKDFKeyProvider()])
    threads = [threading.Thread(target=chain.get_key, args=("api_bearer",))
               for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert chain.total_call_counters()["api_bearer"] == 50


def test_layered_provenance_reports_first_provider_with_opinion(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    monkeypatch.setenv("MCP_API_BEARER_KEY", "y" * 32)
    chain = LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"})),
        HKDFKeyProvider(),
    ])
    prov = chain.provenance("api_bearer")
    assert prov is not None
    assert prov.mechanism == "env_override"


def test_layered_provenance_does_not_bump_counters(monkeypatch):
    """provenance() is the introspection API — calling it must NOT
    incur a get_key() side-effect. /info endpoint relies on this."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    chain = LayeredKeyProvider([HKDFKeyProvider()])
    chain.provenance("api_bearer")
    assert chain.total_call_counters()["api_bearer"] == 0
    assert chain.first_call_timestamps()["api_bearer"] is None


# ---------------------------------------------------------------------
# InMemoryKeyProvider — test-injection ergonomics
# ---------------------------------------------------------------------


def test_inmemory_returns_configured_bytes():
    adapter = InMemoryKeyProvider({"signed_url": b"\x01" * 32})
    assert adapter.get_key("signed_url") == b"\x01" * 32


def test_inmemory_returns_none_for_unconfigured_purpose():
    adapter = InMemoryKeyProvider({"signed_url": b"\x01" * 32})
    assert adapter.get_key("api_bearer") is None


def test_inmemory_provenance_reports_configured_mechanism():
    adapter = InMemoryKeyProvider(
        {"api_bearer": b"x" * 32},
        mechanism="env_override",
    )
    prov = adapter.provenance("api_bearer")
    assert prov is not None
    assert prov.mechanism == "env_override"
    assert prov.master_len == 32


# ---------------------------------------------------------------------
# with_key_provider — injection ergonomics + restore-on-exit
# ---------------------------------------------------------------------


def test_with_key_provider_swaps_active(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    from appscriptly import keys  # triggers default provider init
    injected = InMemoryKeyProvider({"api_bearer": b"INJECTED-" + b"x" * 23})
    with with_key_provider(injected):
        assert get_active_provider() is injected
        assert keys.get_key("api_bearer") == b"INJECTED-" + b"x" * 23
    # After exit: default restored.
    assert get_active_provider() is not injected
    restored = keys.get_key("api_bearer")
    assert restored != b"INJECTED-" + b"x" * 23


def test_with_key_provider_restores_on_exception(monkeypatch):
    """A test failure inside the with-block must NOT leak the injection
    into subsequent tests."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    from appscriptly import keys  # triggers default provider init
    injected = InMemoryKeyProvider({"api_bearer": b"\xff" * 32})
    before = get_active_provider()
    with pytest.raises(RuntimeError, match="boom"):
        with with_key_provider(injected):
            assert get_active_provider() is injected
            raise RuntimeError("boom")
    assert get_active_provider() is before


# ---------------------------------------------------------------------
# build_default_provider — production-default chain
# ---------------------------------------------------------------------


def test_build_default_provider_returns_layered_chain():
    chain = build_default_provider(lambda: frozenset())
    assert isinstance(chain, LayeredKeyProvider)
    # The chain is 3 adapters long in the documented order.
    assert len(chain._providers) == 3
    assert isinstance(chain._providers[0], EnvOverrideKeyProvider)
    assert isinstance(chain._providers[1], RawMasterShimKeyProvider)
    assert isinstance(chain._providers[2], HKDFKeyProvider)


# ---------------------------------------------------------------------
# KeyProvenance value object
# ---------------------------------------------------------------------


def test_keyprovenance_is_frozen():
    prov = KeyProvenance(purpose="api_bearer", mechanism="hkdf_derived", master_len=32)
    with pytest.raises(Exception):  # FrozenInstanceError
        prov.master_len = 99  # type: ignore[misc]


# ---------------------------------------------------------------------
# Defensive branches: defer-on-unknown-purpose + missing-master shim path
# ---------------------------------------------------------------------


def test_env_override_returns_none_for_unknown_purpose():
    """Defensive: a purpose not in the override-env map (typo, future
    addition without registration) must defer, not crash."""
    adapter = EnvOverrideKeyProvider()
    # type:ignore — we deliberately pass a string outside Purpose Literal
    assert adapter.get_key("not_a_real_purpose") is None  # type: ignore[arg-type]
    assert adapter.provenance("not_a_real_purpose") is None  # type: ignore[arg-type]


def test_shim_defers_when_master_env_missing(monkeypatch):
    """Defensive: shim purpose in set but master missing → defer to next
    provider rather than crash; HKDF will then surface the clearer
    "MCP_BEARER_TOKEN required" error."""
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    adapter = RawMasterShimKeyProvider(lambda: frozenset({"api_bearer"}))
    assert adapter.get_key("api_bearer") is None


def test_hkdf_returns_none_for_unknown_purpose():
    """Defensive: facade gates this, but the adapter itself must defer
    on unknown rather than KeyError on the _HKDF_INFO lookup."""
    adapter = HKDFKeyProvider()
    assert adapter.get_key("not_a_real_purpose") is None  # type: ignore[arg-type]
    assert adapter.provenance("not_a_real_purpose") is None  # type: ignore[arg-type]


def test_keys_facade_observability_falls_back_when_non_layered_active(
    monkeypatch,
):
    """When a test injects a bare ``InMemoryKeyProvider`` (no counters),
    the facade's observability accessors must fall back to the default
    provider's counters — not crash. Exercises ``_provider()``'s
    isinstance check in ``keys.py``."""
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)
    from appscriptly import keys

    bare = InMemoryKeyProvider({"api_bearer": b"x" * 32})
    with with_key_provider(bare):
        # These don't crash; they return the DEFAULT provider's counters
        # (which are 0 / None since no get_key call has hit the default
        # provider during this block).
        shim = keys.get_shim_hit_counters()
        totals = keys.get_total_call_counters()
        firsts = keys.get_first_call_timestamps()

    assert isinstance(shim, dict) and "api_bearer" in shim
    assert isinstance(totals, dict) and "api_bearer" in totals
    assert isinstance(firsts, dict) and "api_bearer" in firsts


# ---------------------------------------------------------------------
# HKDF byte-equality golden-value regression (v2.1.1 / Hex-spec deferral)
# ---------------------------------------------------------------------


def test_hkdf_byte_equality_golden_value():
    """Pin the exact 32-byte HKDF-SHA256 output for a fixed IKM + info.

    PR #88 replaced the inline ``hmac.new(b"", ikm, hashlib.sha256)`` +
    ``hmac.new(prk, info + b"\\x01", hashlib.sha256)`` HKDF
    implementation with ``cryptography.hazmat.primitives.kdf.hkdf.HKDF``
    (to clear a CodeQL false-positive). The swap was manually verified
    byte-identical, but that verification wasn't pinned by a test —
    until now.

    The golden value below was computed once against the current
    implementation. Any future change that drifts the bytes (a
    parameter substitution, an algorithm swap, an info-string typo)
    fails this test loudly, forcing the author to either:

    1. Confirm the drift is intentional and run a key-rotation event
       (every in-flight key for the affected purpose becomes invalid),
       OR
    2. Revert the change.

    Either way, the contract — "byte-stable HKDF output across
    implementation swaps" — is enforced at CI time, not at deploy
    time when shim_hit telemetry would notice.
    """
    from appscriptly.key_provider import _hkdf_sha256

    # Fixed inputs: 32 bytes of 'A', the live api_bearer info string.
    ikm = b"A" * 32
    info = b"google-docs-mcp v1 api_bearer"
    expected = bytes.fromhex(
        "6203ba051ab36bbdc14a27d9cca5dc3d04a60329ee3c890c64548bc1be7542b3"
    )

    actual = _hkdf_sha256(ikm, info)
    assert actual == expected, (
        f"HKDF output drifted from PR #88's pinned value.\n"
        f"  expected: {expected.hex()}\n"
        f"  actual:   {actual.hex()}\n"
        f"If this is intentional, also bump the key-rotation event in "
        f"CHANGELOG and confirm operator-side override-pinning per "
        f"RUNBOOK §3.4."
    )
