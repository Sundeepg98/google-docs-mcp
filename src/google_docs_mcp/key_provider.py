"""Hex-style Port + Adapters for key derivation (v2.1 / M1a POC).

The pre-v2.1 ``keys.py`` was a single module with three resolution
mechanisms (env override / back-compat shim / HKDF) interleaved in one
function. This module promotes that into a proper Port + Adapters
shape:

- ``KeyProvider`` Protocol — the port (interface every adapter satisfies).
- ``EnvOverrideKeyProvider`` — adapter for the per-purpose env-var path.
- ``RawMasterShimKeyProvider`` — adapter for the v1.x back-compat shim.
- ``HKDFKeyProvider`` — adapter for the v2.0b HKDF-derived path.
- ``LayeredKeyProvider`` — composite that walks providers in order
  until one returns a key. Production wires this as
  ``[Env, Shim, HKDF]`` to match the historical resolution order.
- ``InMemoryKeyProvider`` — test-only adapter; returns deterministic
  bytes from a dict without touching os.environ. Replaces the
  fragile ``monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)`` pattern
  in unit tests.

**Design rationale (M1a POC).** This is the first of several Hex
foundation refactors (M1b CredentialStore, M2 GoogleAPIClient, M3
per-service folders, M4 @workspace_tool rename). KeyProvider was
chosen as the POC because:

1. It has 3 real mechanisms today, so the Protocol surface gets
   stress-tested by 3 concrete adapters from day one.
2. ``KeyProvenance`` already exists as a port-shaped value object —
   the Protocol can return it without inventing new types.
3. Observability is load-bearing (the v2.0b strict-flip preflight
   gates on shim_hit / total_call / first_call telemetry). If the
   Protocol surface accommodates those cross-cutting concerns, the
   pattern is good enough to copy for M1b/M2.

**Backward compatibility.** ``keys.py`` keeps every public function
signature (``get_key``, ``key_provenance``, ``is_shim_active``,
``get_shim_hit_counters``, ``get_total_call_counters``,
``get_first_call_timestamps``, ``_reset_*_for_tests``). The facade
delegates to a module-level ``_default_provider: KeyProvider``. Tests
that previously monkeypatched ``_BACK_COMPAT_RAW_MASTER`` still work
because the symbol is preserved in ``keys.py`` and ``RawMasterShimKeyProvider``
reads it at call time (not at construction).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol, runtime_checkable

Purpose = Literal["api_bearer", "oauth_state", "signed_url"]

_MIN_MASTER_LEN = 32


@dataclass(frozen=True)
class KeyProvenance:
    """How a key was sourced — for ``gdocs_server_info`` introspection.

    Pre-v2.1 ``mechanism`` was ``"raw_master_shim" | "hkdf_derived"``.
    v2.1 adds ``"env_override"`` to make the override path observable
    (previously it was indistinguishable from "shim" in the provenance
    surface, even though the get_key resolution clearly separated them).
    """
    purpose: str
    mechanism: Literal["raw_master_shim", "hkdf_derived", "env_override"]
    master_len: int


@runtime_checkable
class KeyProvider(Protocol):
    """Port for per-purpose key derivation.

    Concrete adapters return either ``bytes`` (a key) or ``None`` (this
    provider doesn't serve this purpose; the layered composite should
    try the next provider in the chain). ``provenance`` returns a
    description of how the key WOULD be sourced — without actually
    deriving, so it's safe to call from observability endpoints that
    must not log key material.
    """

    def get_key(self, purpose: Purpose) -> bytes | None:
        """Return key bytes for ``purpose``, or ``None`` to defer to the
        next provider in a layered chain."""
        ...

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        """Describe how a key WOULD be sourced. ``None`` means this
        provider has no opinion (defer to the next in the chain).
        Must NOT derive or log the actual key bytes."""
        ...


# ---------------------------------------------------------------------
# Adapter 1: Env-var override (highest precedence — operator's explicit
# pin for safe-rotation per RUNBOOK §3.4).
# ---------------------------------------------------------------------


_OVERRIDE_ENV: dict[str, str] = {
    "api_bearer": "MCP_API_BEARER_KEY",
    "oauth_state": "OAUTH_STATE_SIGNING_KEY",
    "signed_url": "SIGNED_URL_SIGNING_KEY",
}


class EnvOverrideKeyProvider:
    """Adapter: returns per-purpose env-var override bytes if set.

    Reads the env var lazily on every call (matches v1.5.1+ behavior —
    operators set the var on a deploy, restart the process, and the
    next call picks it up without re-importing). Raises RuntimeError
    if the override is set but shorter than 32 chars (operator intent
    was clear; failing loud beats silent fallback).
    """

    def get_key(self, purpose: Purpose) -> bytes | None:
        env_name = _OVERRIDE_ENV.get(purpose)
        if env_name is None:
            return None  # unknown purpose — let HKDF raise
        override = os.environ.get(env_name)
        if not override:
            return None  # not set — defer to next provider
        if len(override) < _MIN_MASTER_LEN:
            raise RuntimeError(
                f"{env_name} must be ≥{_MIN_MASTER_LEN} chars "
                f"(got {len(override)}). Set a longer value or unset the "
                f"override env var to fall back to the master."
            )
        return override.encode("utf-8")

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        env_name = _OVERRIDE_ENV.get(purpose)
        if env_name is None:
            return None
        override = os.environ.get(env_name)
        if not override:
            return None
        # master_len reports the override length (the "master" from this
        # provider's perspective). Documented in v2.1 release notes —
        # pre-v2.1 surfaced 0 here, which was useless to operators.
        return KeyProvenance(
            purpose=purpose, mechanism="env_override", master_len=len(override),
        )


# ---------------------------------------------------------------------
# Adapter 2: Back-compat shim (v1.x — empty in v2.0b+).
# ---------------------------------------------------------------------


class RawMasterShimKeyProvider:
    """Adapter: returns raw master bytes for purposes in the shim set.

    The shim set is passed at construction (a frozenset of purpose
    strings). Production wires this to ``keys._BACK_COMPAT_RAW_MASTER``
    so the facade-level monkeypatch pathway still works for tests that
    inject a non-empty shim set.

    Lazy resolution: reads ``MCP_BEARER_TOKEN`` from os.environ at call
    time, not construction. This matches the v1.x behavior — the env
    var can be set after the module imports.
    """

    def __init__(self, shim_set_resolver) -> None:
        """``shim_set_resolver`` is a callable returning the current set
        of purposes that should be served by the shim. Using a callable
        (rather than a frozenset constant) lets ``keys._BACK_COMPAT_RAW_MASTER``
        monkeypatching at test time take effect at call time."""
        self._shim_set_resolver = shim_set_resolver

    def _shim_set(self) -> frozenset[str]:
        return self._shim_set_resolver()

    def get_key(self, purpose: Purpose) -> bytes | None:
        if purpose not in self._shim_set():
            return None
        master = os.environ.get("MCP_BEARER_TOKEN")
        if not master:
            # Shim asked but master missing — fall through so HKDF can
            # surface the clearer "MCP_BEARER_TOKEN required" error.
            return None
        # Shim path skips the length check by design (v1.x compat).
        return master.encode("utf-8")

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        if purpose not in self._shim_set():
            return None
        master = os.environ.get("MCP_BEARER_TOKEN", "")
        return KeyProvenance(
            purpose=purpose, mechanism="raw_master_shim", master_len=len(master),
        )


# ---------------------------------------------------------------------
# Adapter 3: HKDF-derived (v2.0b production path).
# ---------------------------------------------------------------------


_HKDF_INFO: dict[str, bytes] = {
    "api_bearer": b"google-docs-mcp v1 api_bearer",
    "oauth_state": b"google-docs-mcp v1 oauth_state",
    "signed_url": b"google-docs-mcp v1 signed_url",
}


def _hkdf_sha256(master_bytes: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-Extract+Expand using HMAC-SHA256, salt='' for our use.

    Single inline implementation — keeps ``key_provider.py`` import-free
    of cryptography for the import-fast-path. Matches pre-v2.1 ``keys._hkdf_sha256``
    byte-for-byte; reproduced here to keep the adapter self-contained.
    """
    prk = hmac.new(b"", master_bytes, hashlib.sha256).digest()
    t = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return t[:length]


class HKDFKeyProvider:
    """Adapter: derives per-purpose keys from MCP_BEARER_TOKEN via HKDF-SHA256.

    Enforces ≥32-char master. Reads ``MCP_BEARER_TOKEN`` at call time
    so the env var can be set after the module imports.

    This is the production path post-v2.0b strict-flip — when neither
    env override nor shim apply, every key derives from the master here.
    Same HKDF info strings as pre-v2.1 ``keys._HKDF_INFO``; changing
    them is a key-rotation event (invalidates every in-flight key for
    that purpose).
    """

    def get_key(self, purpose: Purpose) -> bytes | None:
        if purpose not in _HKDF_INFO:
            return None  # unknown purpose — facade raises ValueError
        master = os.environ.get("MCP_BEARER_TOKEN")
        if not master:
            raise RuntimeError(
                "MCP_BEARER_TOKEN env var is required for key derivation"
            )
        if len(master) < _MIN_MASTER_LEN:
            raise RuntimeError(
                f"MCP_BEARER_TOKEN must be ≥{_MIN_MASTER_LEN} chars for "
                f"HKDF derivation (got {len(master)}). Either lengthen "
                f"the token or use a purpose currently in the back-compat "
                f"shim. See THREAT_MODEL.md §5 for rotation guidance."
            )
        return _hkdf_sha256(master.encode("utf-8"), _HKDF_INFO[purpose])

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        if purpose not in _HKDF_INFO:
            return None
        master = os.environ.get("MCP_BEARER_TOKEN", "")
        return KeyProvenance(
            purpose=purpose, mechanism="hkdf_derived", master_len=len(master),
        )


# ---------------------------------------------------------------------
# Composite: layered chain with built-in observability.
# ---------------------------------------------------------------------


class LayeredKeyProvider:
    """Composite: walks providers in order, returns first non-None key.

    Production wires ``[EnvOverrideKeyProvider, RawMasterShimKeyProvider,
    HKDFKeyProvider]`` — matches pre-v2.1 ``keys.get_key()`` resolution
    order exactly.

    **Observability lives here, not in the adapters.** Each adapter
    stays focused on "where does this key come from"; the layered
    composite is the single place that records "a key was actually
    served" telemetry (shim_hit / total_call / first_call). This keeps
    the metrics centralised so the v2.0b strict-flip preflight gate
    has one place to read.

    The 3 counters mirror pre-v2.1 ``keys._shim_hit_counter`` /
    ``_total_call_counter`` / ``_first_call_at`` semantically — same
    purposes-as-keys, same process-local + lock-guarded discipline.
    Refer to ``keys.py`` module docstring for the operator-side
    rationale (preflight gate, soak window, etc.).
    """

    def __init__(self, providers: list[KeyProvider]) -> None:
        self._providers = providers
        # Per-purpose counters. Initialised lazily as purposes are seen
        # so adding a 4th purpose later doesn't require re-touching
        # this constructor.
        self._shim_hits: dict[str, int] = {p: 0 for p in _HKDF_INFO}
        self._totals: dict[str, int] = {p: 0 for p in _HKDF_INFO}
        self._first_call: dict[str, float | None] = {p: None for p in _HKDF_INFO}
        self._lock = threading.Lock()

    def get_key(self, purpose: Purpose) -> bytes | None:
        for provider in self._providers:
            key = provider.get_key(purpose)
            if key is None:
                continue
            self._record_call(purpose, provider)
            return key
        return None

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        for provider in self._providers:
            prov = provider.provenance(purpose)
            if prov is not None:
                return prov
        return None

    def _record_call(self, purpose: str, provider: KeyProvider) -> None:
        """Bump the relevant counters for a successful key resolution.

        Shim hits only count when the SHIM provider was the one that
        served the key — env-override and HKDF do not increment the
        shim counter (pre-v2.1 behavior — operators reading the
        preflight telemetry rely on this).
        """
        with self._lock:
            self._totals[purpose] = self._totals.get(purpose, 0) + 1
            if isinstance(provider, RawMasterShimKeyProvider):
                self._shim_hits[purpose] = self._shim_hits.get(purpose, 0) + 1
            if self._first_call.get(purpose) is None:
                self._first_call[purpose] = time.time()

    # ------------- Observability accessors (snapshots) ----------------

    def shim_hit_counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._shim_hits)

    def total_call_counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)

    def first_call_timestamps(self) -> dict[str, float | None]:
        with self._lock:
            return dict(self._first_call)

    # ------------- Test-only reset hooks ------------------------------

    def _reset_shim_hits(self) -> None:
        with self._lock:
            for k in self._shim_hits:
                self._shim_hits[k] = 0

    def _reset_totals(self) -> None:
        with self._lock:
            for k in self._totals:
                self._totals[k] = 0

    def _reset_first_call(self) -> None:
        with self._lock:
            for k in self._first_call:
                self._first_call[k] = None


# ---------------------------------------------------------------------
# Test-only adapter: InMemoryKeyProvider
# ---------------------------------------------------------------------


class InMemoryKeyProvider:
    """Deterministic test fixture: serves a fixed mapping.

    Replaces the brittle ``monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 32)``
    pattern in unit tests. Example::

        with with_key_provider(InMemoryKeyProvider({
            "signed_url": b"deterministic-test-key-32-chars!",
        })):
            ...test body...

    Unknown purposes return ``None`` (which the layered composite would
    fall through; standalone use raises ValueError via the facade).
    """

    def __init__(
        self,
        keys: dict[str, bytes],
        mechanism: Literal["raw_master_shim", "hkdf_derived", "env_override"] = "hkdf_derived",
    ) -> None:
        self._keys = dict(keys)
        # Explicit annotation so pyright preserves the Literal type
        # through assignment — without it, assignment widens to ``str``
        # and ``KeyProvenance(mechanism=self._mechanism)`` reports an
        # arg-type error at the call site.
        self._mechanism: Literal["raw_master_shim", "hkdf_derived", "env_override"] = mechanism

    def get_key(self, purpose: Purpose) -> bytes | None:
        return self._keys.get(purpose)

    def provenance(self, purpose: Purpose) -> KeyProvenance | None:
        if purpose not in self._keys:
            return None
        return KeyProvenance(
            purpose=purpose, mechanism=self._mechanism,
            master_len=len(self._keys[purpose]),
        )


# ---------------------------------------------------------------------
# Module-level default + injection ergonomics
# ---------------------------------------------------------------------


def build_default_provider(shim_set_resolver) -> LayeredKeyProvider:
    """Construct the production-default layered chain.

    Order: env-override > shim > HKDF. Matches pre-v2.1 ``keys.get_key()``
    resolution exactly. ``shim_set_resolver`` is a callable returning
    the current ``frozenset`` of purposes the shim should serve —
    bound to ``keys._BACK_COMPAT_RAW_MASTER`` in production so tests
    that monkeypatch the symbol still take effect.
    """
    return LayeredKeyProvider([
        EnvOverrideKeyProvider(),
        RawMasterShimKeyProvider(shim_set_resolver),
        HKDFKeyProvider(),
    ])


# Process-wide active provider. Production wires this to
# ``build_default_provider(...)`` from ``keys.py`` at import time.
# Tests swap it via ``set_key_provider()`` or the ``with_key_provider``
# context manager.
_active_provider: KeyProvider | None = None
_provider_lock = threading.Lock()


def get_active_provider() -> KeyProvider:
    """Return the currently active provider. Raises if not yet set.

    ``keys.py`` import-time wiring calls ``set_key_provider(...)`` so
    this only raises if someone imports ``key_provider`` directly
    without ``keys`` ever loading — which would be a packaging bug.
    """
    if _active_provider is None:
        raise RuntimeError(
            "No active KeyProvider — import google_docs_mcp.keys first, "
            "or call set_key_provider() explicitly in a test."
        )
    return _active_provider


def set_key_provider(provider: KeyProvider) -> KeyProvider | None:
    """Replace the active provider. Returns the previous (for restore).

    Tests that want a clean injection should prefer ``with_key_provider``
    over raw set + manual restore; this helper exists for the rare case
    where the context-manager idiom doesn't fit (e.g. session-scoped
    pytest fixtures with cleanup in finalizers).
    """
    global _active_provider
    with _provider_lock:
        previous = _active_provider
        _active_provider = provider
    return previous


@contextmanager
def with_key_provider(provider: KeyProvider) -> Iterator[KeyProvider]:
    """Temporarily swap the active provider within a ``with`` block.

    Example::

        from google_docs_mcp.key_provider import (
            InMemoryKeyProvider, with_key_provider,
        )
        from google_docs_mcp import keys

        with with_key_provider(InMemoryKeyProvider({
            "signed_url": b"\\x01" * 32,
        })):
            assert keys.get_key("signed_url") == b"\\x01" * 32

    Restores the prior provider on exit — including on exceptions —
    so a test failure in the body doesn't leak the injection into
    subsequent tests.
    """
    previous = set_key_provider(provider)
    try:
        yield provider
    finally:
        # Restore even if the prior was None — set_key_provider(None)
        # would re-raise on the next get_active_provider() call. That's
        # the right behaviour: if a test runs before keys.py has
        # initialised the default, we shouldn't paper over it.
        global _active_provider
        with _provider_lock:
            _active_provider = previous
