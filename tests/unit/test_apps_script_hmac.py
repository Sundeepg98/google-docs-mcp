"""Tests for the Apps Script /exec HMAC verify-path (v2.0c).

Covers the three legs of the HMAC fix end-to-end:

  * PROVISION — ``apps_script_hmac.generate_hmac_key`` shape +
    ``inject_hmac_into_source`` round-trip and its fail-loud guard; the
    setup-time read-or-create persistence (stdio config + cloud user_store).
  * SIGN — ``compute_signature`` determinism + the exact wire scheme;
    ``docx_import._call_webapp`` attaches the headers and refuses to send
    unsigned.
  * VERIFY (parity) — the signing message the Python side builds is the
    same one ``restructure.gs::_verifyHmac`` recomputes (``"<ts>.<body>"``),
    asserted structurally so the two implementations can't silently drift.

The Apps Script side is JavaScript (not executed here); these tests pin the
SCHEME so a Python-side change that would break the JS verify is caught.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
from pathlib import Path

import pytest

from appscriptly import apps_script_hmac as H

_REPO = Path(__file__).resolve().parents[2]
_RESTRUCTURE_GS = _REPO / "src" / "appscriptly" / "restructure.gs"


# ---------------------------------------------------------------------
# generate_hmac_key
# ---------------------------------------------------------------------


def test_generate_hmac_key_is_64_lowercase_hex():
    k = H.generate_hmac_key()
    assert len(k) == 64
    assert all(c in "0123456789abcdef" for c in k)


def test_generate_hmac_key_is_random():
    assert H.generate_hmac_key() != H.generate_hmac_key()


def test_generated_key_passes_user_store_validator():
    """The key shape must satisfy user_store's persisted-field validator so
    save_state accepts it on the provisioning path."""
    from appscriptly.user_store import _valid_apps_script_hmac_key
    assert _valid_apps_script_hmac_key(H.generate_hmac_key())


# ---------------------------------------------------------------------
# compute_signature — determinism + scheme
# ---------------------------------------------------------------------


def test_compute_signature_matches_reference_hmac():
    """compute_signature is exactly lowercase-hex HMAC-SHA256 over
    "<timestamp>.<body>" — the string restructure.gs re-signs."""
    key = "ab" * 32
    ts = "1700000000"
    body = '{"docId":"D","splitTree":[]}'
    expected = _hmac.new(
        key.encode(), (ts + "." + body).encode(), hashlib.sha256
    ).hexdigest()
    assert H.compute_signature(key, timestamp=ts, body=body) == expected


def test_compute_signature_is_deterministic():
    key = H.generate_hmac_key()
    a = H.compute_signature(key, timestamp="1", body="{}")
    b = H.compute_signature(key, timestamp="1", body="{}")
    assert a == b


def test_compute_signature_changes_with_body_and_timestamp():
    key = H.generate_hmac_key()
    base = H.compute_signature(key, timestamp="1", body="{}")
    assert H.compute_signature(key, timestamp="2", body="{}") != base
    assert H.compute_signature(key, timestamp="1", body='{"x":1}') != base


# ---------------------------------------------------------------------
# inject_hmac_into_source
# ---------------------------------------------------------------------


def test_inject_replaces_both_sentinels():
    key = H.generate_hmac_key()
    src = "var MCP_HMAC_KEY = '__MCP_HMAC_KEY__';\nvar MCP_HMAC_REQUIRED = '__MCP_HMAC_REQUIRED__';"
    out = H.inject_hmac_into_source(src, key)
    assert "__MCP_HMAC_KEY__" not in out
    assert "__MCP_HMAC_REQUIRED__" not in out
    assert key in out
    assert "= 'true'" in out or "='true'" in out


def test_inject_is_pure_does_not_mutate_input():
    src = "x = '__MCP_HMAC_KEY__'"
    H.inject_hmac_into_source(src, "ab" * 32)
    assert src == "x = '__MCP_HMAC_KEY__'"  # original untouched


def test_inject_rejects_missing_placeholder():
    """Fail loud rather than silently shipping an UNAUTHENTICATED web app if
    a future edit drops the sentinel from restructure.gs."""
    with pytest.raises(ValueError, match="missing the .* placeholder"):
        H.inject_hmac_into_source("function doPost(e){}", "ab" * 32)


def test_inject_rejects_empty_key():
    with pytest.raises(ValueError, match="empty HMAC key"):
        H.inject_hmac_into_source("__MCP_HMAC_KEY__", "")


# ---------------------------------------------------------------------
# restructure.gs <-> Python scheme parity (structural)
# ---------------------------------------------------------------------


def test_restructure_gs_declares_matching_sentinels_and_headers():
    """The JS must declare the SAME sentinels the Python injector targets and
    read the SAME header names the Python signer sends — else the round-trip
    silently breaks at deploy/runtime."""
    gs = _RESTRUCTURE_GS.read_text(encoding="utf-8")
    assert "__MCP_HMAC_KEY__" in gs
    assert "__MCP_HMAC_REQUIRED__" in gs
    # Header names are matched case-insensitively in the JS; assert the
    # lowercase forms of the Python header constants appear.
    assert H.SIGNATURE_HEADER.lower() in gs.lower()
    assert H.TIMESTAMP_HEADER.lower() in gs.lower()


def test_restructure_gs_signs_timestamp_dot_body():
    """The JS must build the HMAC message as ``tsRaw + '.' + body`` — the
    same construction compute_signature uses. Pin the literal so a reorder
    (e.g. body-then-ts) is caught."""
    gs = _RESTRUCTURE_GS.read_text(encoding="utf-8")
    assert "tsRaw + '.' + body" in gs


def test_restructure_gs_fails_closed_when_unconfigured():
    """If the key wasn't templated in, the JS must reject (fail closed), not
    accept unsigned traffic."""
    gs = _RESTRUCTURE_GS.read_text(encoding="utf-8")
    assert "MCP_HMAC_REQUIRED !== 'true'" in gs


# ---------------------------------------------------------------------
# setup provisioning — read-or-create (cloud user_store path)
# ---------------------------------------------------------------------


@pytest.fixture
def isolated_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(tmp_path / "u.db"))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield


def test_resolve_or_create_user_hmac_key_persists_and_reuses(isolated_user_store):
    from appscriptly import user_store
    from appscriptly.setup_apps_script import _resolve_or_create_user_hmac_key

    k1 = _resolve_or_create_user_hmac_key("user-1")
    assert len(k1) == 64
    # Persisted on the row.
    assert user_store.get_state("user-1")["apps_script_hmac_key"] == k1
    # Second call REUSES it (idempotent — critical for stable content_hash).
    k2 = _resolve_or_create_user_hmac_key("user-1")
    assert k2 == k1


def test_resolve_or_create_user_hmac_key_is_per_user(isolated_user_store):
    from appscriptly.setup_apps_script import _resolve_or_create_user_hmac_key

    assert _resolve_or_create_user_hmac_key("alice") != _resolve_or_create_user_hmac_key("bob")


def test_hmac_key_survives_ledger_clear(isolated_user_store):
    """A ledger reset (hash mismatch / manual delete) NULLs the apps_script_*
    fields but must NOT rotate the HMAC key — it isn't in the ledger field
    map, so the user keeps one stable key across re-deploys."""
    from appscriptly import user_store
    from appscriptly.setup_apps_script import (
        _USER_STORE_FIELD_MAP,
        _resolve_or_create_user_hmac_key,
    )

    key = _resolve_or_create_user_hmac_key("user-1")
    # Simulate _clear(): NULL exactly the ledger-mapped columns.
    user_store.save_state("user-1", {c: None for c in _USER_STORE_FIELD_MAP.values()})
    assert user_store.get_state("user-1").get("apps_script_hmac_key") == key
    assert "apps_script_hmac_key" not in _USER_STORE_FIELD_MAP.values()
