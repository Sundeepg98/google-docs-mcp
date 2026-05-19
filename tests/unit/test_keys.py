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
