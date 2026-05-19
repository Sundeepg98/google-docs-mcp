"""Per-user state storage tests.

Covers the v1.1 multi-tenant storage abstraction. Guards against:
- silent data loss on merge-update (partial save blowing away other fields)
- cross-user leakage (one user's row visible to another)
- timestamp lies (created_at mutated, updated_at not bumped)
- typos in field names silently writing nothing
- schema not being auto-initialized on fresh deployments
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point user_store at a per-test SQLite file so tests don't bleed."""
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    # Also override data_dir so default_data_dir() doesn't touch the
    # real ~/.google-docs-mcp during test runs.
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield db_file


def test_get_state_returns_empty_dict_for_unknown_user():
    from google_docs_mcp.user_store import get_state
    assert get_state("user-does-not-exist") == {}


def test_save_then_get_roundtrip():
    from google_docs_mcp.user_store import get_state, save_state

    save_state(
        "user-1",
        {
            "apps_script_url": "https://script.google.com/macros/s/ABC/exec",
            "apps_script_script_id": "SCRIPT_ID_1",
        },
    )
    state = get_state("user-1")

    assert state["user_id"] == "user-1"
    assert state["apps_script_url"] == "https://script.google.com/macros/s/ABC/exec"
    assert state["apps_script_script_id"] == "SCRIPT_ID_1"
    assert isinstance(state["created_at"], int)
    assert isinstance(state["updated_at"], int)


def test_save_merges_existing_state_not_overwrites():
    """The killer guard: a partial update must NOT erase other fields."""
    from google_docs_mcp.user_store import get_state, save_state

    save_state(
        "user-2",
        {
            "apps_script_url": "https://script.google.com/macros/s/URLV1/exec",
            "apps_script_script_id": "S1",
        },
    )
    save_state("user-2", {"apps_script_deployment_id": "D1"})  # partial!

    state = get_state("user-2")
    assert state["apps_script_url"] == "https://script.google.com/macros/s/URLV1/exec", (
        "second save() with no apps_script_url field erased the first one — "
        "merge semantics are broken"
    )
    assert state["apps_script_script_id"] == "S1"
    assert state["apps_script_deployment_id"] == "D1"


def test_save_preserves_created_at_but_bumps_updated_at():
    from google_docs_mcp.user_store import get_state, save_state

    save_state("user-3", {"apps_script_url": "https://script.google.com/macros/s/U3/exec"})
    first = get_state("user-3")

    # Sleep just enough to guarantee a different unix timestamp.
    time.sleep(1.05)
    save_state("user-3", {"apps_script_script_id": "S2"})
    second = get_state("user-3")

    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]


def test_clear_removes_state():
    from google_docs_mcp.user_store import clear_state, get_state, save_state

    save_state("user-4", {"apps_script_url": "https://script.google.com/macros/s/U4/exec"})
    assert get_state("user-4") != {}

    clear_state("user-4")
    assert get_state("user-4") == {}


def test_clear_nonexistent_user_is_noop():
    """No exception when clearing a row that doesn't exist."""
    from google_docs_mcp.user_store import clear_state
    clear_state("user-never-existed")  # must not raise


def test_multiple_users_isolated():
    from google_docs_mcp.user_store import get_state, save_state

    save_state("alice", {"apps_script_url": "https://script.google.com/macros/s/ALICE/exec"})
    save_state("bob", {"apps_script_url": "https://script.google.com/macros/s/BOB/exec"})

    assert get_state("alice")["apps_script_url"] == "https://script.google.com/macros/s/ALICE/exec"
    assert get_state("bob")["apps_script_url"] == "https://script.google.com/macros/s/BOB/exec"


def test_unknown_field_raises_loudly():
    """Typos in field names must surface immediately, not silently no-op.

    Without this guard, ``save_state(uid, {"apps_script_ulr": ...})``
    would write nothing useful and you'd debug the consumer for hours.
    """
    from google_docs_mcp.user_store import save_state
    with pytest.raises(ValueError, match="Unknown user_state fields"):
        save_state("user-5", {"appz_scripit_url": "typo"})


def test_empty_user_id_rejected():
    from google_docs_mcp.user_store import clear_state, get_state, save_state
    for fn_call in (
        lambda: get_state(""),
        lambda: save_state("", {"apps_script_url": "x"}),
        lambda: clear_state(""),
    ):
        with pytest.raises(ValueError, match="user_id is required"):
            fn_call()


def test_db_initialized_lazily_on_fresh_deployment(tmp_path, monkeypatch):
    """No setup ritual required — first call creates the schema."""
    fresh_db = tmp_path / "nested" / "subdir" / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(fresh_db))
    assert not fresh_db.exists()

    from google_docs_mcp.user_store import get_state
    get_state("any-user")

    assert fresh_db.exists()


def test_google_creds_dict_parses_when_present():
    from google_docs_mcp.user_store import (
        google_creds_dict, get_state, save_state,
    )
    creds_json = json.dumps(
        {"token": "abc", "refresh_token": "xyz", "scopes": ["docs"]}
    )
    save_state("user-6", {"google_creds_json": creds_json})

    parsed = google_creds_dict(get_state("user-6"))
    assert parsed == {"token": "abc", "refresh_token": "xyz", "scopes": ["docs"]}


def test_google_creds_dict_returns_none_when_absent():
    from google_docs_mcp.user_store import google_creds_dict
    assert google_creds_dict({}) is None
    assert google_creds_dict({"apps_script_url": "URL"}) is None


def test_concurrent_writes_to_different_users_dont_corrupt():
    """SQLite WAL + per-connection isolation should handle parallel writes
    to distinct user_ids cleanly. If this fails, the storage layer is
    not multi-tenant-safe."""
    from google_docs_mcp.user_store import get_state, save_state

    def worker(uid: str) -> None:
        for i in range(10):
            # Validator requires real GAS-style URLs; encode uid+i into the
            # deployment-id slot (kept alphanumeric so it matches the regex).
            tag = uid.replace("-", "") + str(i)
            save_state(uid, {"apps_script_url": f"https://script.google.com/macros/s/{tag}/exec"})

    threads = [
        threading.Thread(target=worker, args=(f"user-thread-{i}",))
        for i in range(5)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    for i in range(5):
        uid = f"user-thread-{i}"
        state = get_state(uid)
        tag = uid.replace("-", "") + "9"
        assert state["apps_script_url"] == f"https://script.google.com/macros/s/{tag}/exec"  # last write wins


# ---------------------------------------------------------------------------
# v1.4.0a -- _FIELD_VALIDATORS (issue #11)
# Defense-in-depth guard for persisted fields. Validators are invoked by
# save_state (raise on invalid) and get_state (drop+log on invalid).
# ---------------------------------------------------------------------------


def test_valid_gas_url_accepts_canonical_exec():
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url("https://script.google.com/macros/s/ABC123/exec") is True


def test_valid_gas_url_accepts_dev_path():
    """/dev is the head-deployment endpoint, also legitimate."""
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url("https://script.google.com/macros/s/ABC123/dev") is True


def test_valid_gas_url_accepts_deployment_id_with_underscores_and_hyphens():
    """Real GAS deployment IDs include _ and - characters."""
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url(
        "https://script.google.com/macros/s/AKfycb_X-Y_Z-abc123/exec"
    ) is True


def test_valid_gas_url_rejects_http():
    """HTTPS-only: an http:// URL would silently downgrade transport security."""
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url("http://script.google.com/macros/s/ABC123/exec") is False


def test_valid_gas_url_rejects_non_google():
    """Reject look-alike hosts -- attacker-controlled origin is the threat model."""
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url("https://evil.com/macros/s/x/exec") is False
    assert _valid_gas_url("https://script.google.com.evil.com/macros/s/x/exec") is False


def test_valid_gas_url_rejects_malformed_path():
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url("https://script.google.com/wrong/path") is False
    assert _valid_gas_url("https://script.google.com/macros/s/ABC123/run") is False  # not exec|dev
    assert _valid_gas_url("https://script.google.com/macros/s//exec") is False  # empty deploy id


def test_valid_gas_url_rejects_non_string():
    from google_docs_mcp.user_store import _valid_gas_url
    assert _valid_gas_url(None) is False
    assert _valid_gas_url(123) is False
    assert _valid_gas_url({"url": "x"}) is False
    assert _valid_gas_url("") is False


def test_save_state_raises_on_invalid_gas_url():
    """Bad value at the write boundary must abort the write loudly."""
    from google_docs_mcp.user_store import get_state, save_state

    with pytest.raises(ValueError, match="apps_script_url"):
        save_state("user-bad", {"apps_script_url": "http://bad"})

    # And the row must not exist -- failed validation is all-or-nothing.
    assert get_state("user-bad") == {}


def test_save_state_error_message_names_field_and_value():
    from google_docs_mcp.user_store import save_state
    with pytest.raises(ValueError) as exc:
        save_state("user-bad-2", {"apps_script_url": "https://evil.com/x"})
    msg = str(exc.value)
    assert "apps_script_url" in msg
    assert "_FIELD_VALIDATORS" in msg  # points the operator at the registry


def test_save_state_accepts_valid_gas_url():
    """Happy path: a valid URL writes cleanly and roundtrips."""
    from google_docs_mcp.user_store import get_state, save_state
    good = "https://script.google.com/macros/s/HAPPY/exec"
    save_state("user-good", {"apps_script_url": good})
    assert get_state("user-good")["apps_script_url"] == good


def test_save_state_allows_none_to_blank_validated_field():
    """A caller explicitly clearing a validated field with None must succeed --
    validators only veto bad non-None values, not the absence of a value."""
    from google_docs_mcp.user_store import save_state
    # Should not raise.
    save_state("user-clear", {"apps_script_url": None})


def test_get_state_drops_invalid_gas_url_with_warn(isolated_db, caplog):
    """Seed the DB with an invalid URL via raw SQL (simulating a row from
    a pre-validator install or external tampering), then assert get_state
    drops the field AND emits a WARNING."""
    from google_docs_mcp.user_store import get_state, _ensure_initialized

    # Trigger schema creation so the table exists before we INSERT.
    _ensure_initialized(isolated_db)

    now = int(time.time())
    conn = sqlite3.connect(isolated_db, isolation_level=None, timeout=30)
    try:
        conn.execute(
            "INSERT INTO user_state "
            "(user_id, apps_script_url, apps_script_script_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user-tampered", "http://attacker.example/x", "SCRIPT_OK", now, now),
        )
    finally:
        conn.close()

    with caplog.at_level(logging.WARNING, logger="google_docs_mcp.user_store"):
        state = get_state("user-tampered")

    assert "apps_script_url" not in state, "validator-failed field must be dropped"
    # Other fields on the same row must survive untouched.
    assert state["apps_script_script_id"] == "SCRIPT_OK"
    assert state["user_id"] == "user-tampered"

    # Must emit a WARNING -- silent drops would mask data loss in production.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("apps_script_url" in r.getMessage() for r in warnings), (
        f"expected a WARNING mentioning apps_script_url, got: "
        f"{[r.getMessage() for r in warnings]}"
    )


def test_get_state_does_not_drop_valid_persisted_url(isolated_db, caplog):
    """Negative control: a valid persisted URL must NOT be dropped or
    trigger a warning -- guards against an over-eager validator regression."""
    from google_docs_mcp.user_store import get_state, save_state

    save_state(
        "user-ok",
        {"apps_script_url": "https://script.google.com/macros/s/OK1/exec"},
    )

    with caplog.at_level(logging.WARNING, logger="google_docs_mcp.user_store"):
        state = get_state("user-ok")

    assert state["apps_script_url"] == "https://script.google.com/macros/s/OK1/exec"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_save_state_unaffected_for_other_fields():
    """Fields without a registered validator must write through normally.
    Regression guard: don't accidentally validate-everything."""
    from google_docs_mcp.user_store import get_state, save_state

    save_state(
        "user-other",
        {
            "apps_script_script_id": "arbitrary-string-no-validator",
            "apps_script_deployment_id": "also-arbitrary",
            "apps_script_version_number": 42,
        },
    )
    state = get_state("user-other")
    assert state["apps_script_script_id"] == "arbitrary-string-no-validator"
    assert state["apps_script_deployment_id"] == "also-arbitrary"
    assert state["apps_script_version_number"] == 42
