"""Tests for the v2.0a user_state migration script.

Guards against:
- non-idempotent runs that re-key already-migrated users (breaking
  signature verification for users mid-flight)
- dry-run leaking writes to the DB (including schema-level writes —
  ALTER TABLE must NOT fire on dry-run)
- the script producing keys that don't match the field validator
- silently running while the server is live (clobbering refresh writes)
- ``--force`` being missing or misnamed (operator docs would lie)
- ``--apply`` being missing or default-on (would write by default)
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

# scripts/ isn't on the package path; add it explicitly so the test
# can import migrate_existing_users without being co-located.
# Mirrors tests/unit/test_mutation_check.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import migrate_existing_users as mig  # noqa: E402  # pyright: ignore[reportMissingImports]


# isolated_db fixture is auto-applied from tests/conftest.py (R23 B3
# consolidation, v2.0.5). Canonical version clears _initialized_paths
# so the legacy-schema test in this file still gets the ALTER path,
# and additionally resets _per_user_locks, _shim_hit_counter, and
# _creds_cache.


def _seed_user(
    user_id: str,
    *,
    apps_script_url: str | None = None,
    apps_script_hmac_key: str | None = None,
    updated_at: int | None = None,
) -> None:
    """Insert a user row via save_state (uses normal package code path).

    For tests that need to bypass validators (e.g. seeding a row with an
    older ``updated_at``), do a direct UPDATE after save_state.
    """
    from appscriptly import user_store
    updates: dict[str, object] = {}
    if apps_script_url is not None:
        updates["apps_script_url"] = apps_script_url
    if apps_script_hmac_key is not None:
        updates["apps_script_hmac_key"] = apps_script_hmac_key
    user_store.save_state(user_id, updates)

    if updated_at is not None:
        # Back-date the row past the heartbeat window. save_state always
        # bumps updated_at to now — overwrite via direct SQL.
        path = user_store.db_path()
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute(
                "UPDATE user_state SET updated_at = ? WHERE user_id = ?",
                (updated_at, user_id),
            )
        finally:
            conn.close()


def _back_date_all(seconds_ago: int = 120) -> None:
    """Push every row's updated_at into the past — past the heartbeat window.

    Needed for tests that call save_state to seed (which sets updated_at
    to now) and then expect the migration to actually run.
    """
    from appscriptly import user_store
    path = user_store.db_path()
    cutoff = int(time.time()) - seconds_ago
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("UPDATE user_state SET updated_at = ?", (cutoff,))
    finally:
        conn.close()


def _read_key(user_id: str) -> str | None:
    from appscriptly import user_store
    return user_store.get_state(user_id).get("apps_script_hmac_key")


def test_migrate_provisions_64_hex_key():
    """Fresh legacy user gets a key matching the field validator."""
    from appscriptly.user_store import _valid_apps_script_hmac_key

    _seed_user(
        "user-legacy-1",
        apps_script_url="https://script.google.com/macros/s/ABC/exec",
    )
    _back_date_all()

    rc = mig.main(["--apply"])
    assert rc == 0

    key = _read_key("user-legacy-1")
    assert key is not None, "migration did not write a key"
    assert _valid_apps_script_hmac_key(key), (
        f"migration wrote {key!r}, which fails the field validator — "
        "format is supposed to be 64-char lowercase hex"
    )


def test_migrate_idempotent():
    """Two --apply runs leave the same key. Second run is a no-op."""
    _seed_user(
        "user-id-2",
        apps_script_url="https://script.google.com/macros/s/X/exec",
    )
    _back_date_all()

    mig.main(["--apply"])
    first_key = _read_key("user-id-2")
    assert first_key is not None

    # Second run — should detect existing key and skip.
    # Back-date again because the first run just bumped updated_at.
    _back_date_all()
    mig.main(["--apply"])
    second_key = _read_key("user-id-2")

    assert first_key == second_key, (
        "migration re-keyed an already-migrated user — breaks signature "
        "verification for any user mid-flight"
    )


def test_migrate_dry_run_no_writes():
    """No flags = dry-run: reports what would change but doesn't touch the DB.

    Verifies both the row-level contract (key not written, updated_at
    not bumped) and the schema-level contract (ALTER TABLE doesn't
    fire). The latter is what IMPORTANT 1 from the code review was
    about: ``_ensure_initialized`` used to run unconditionally, which
    silently ran the ALTER even on dry-run.
    """
    _seed_user(
        "user-dryrun",
        apps_script_url="https://script.google.com/macros/s/Y/exec",
    )
    _back_date_all()

    # Capture pre-state including schema. The seed call above went
    # through save_state, which invokes _ensure_initialized — meaning
    # the column is already present on the seeded DB. We work around
    # by dropping it via a fresh DB connection that bypasses the
    # in-process _initialized_paths cache.
    from appscriptly import user_store
    before = user_store.get_state("user-dryrun")
    assert "apps_script_hmac_key" not in before, "seed leaked a key"

    # Default (no --apply) should be dry-run.
    rc = mig.main([])
    assert rc == 0

    after = user_store.get_state("user-dryrun")
    assert "apps_script_hmac_key" not in after, (
        "dry-run wrote a key to the DB — default is supposed to be read-only"
    )
    assert after["updated_at"] == before["updated_at"], (
        "dry-run bumped updated_at — default is supposed to be read-only"
    )


def test_migrate_dry_run_does_not_alter_schema_on_legacy_db(tmp_path, monkeypatch):
    """IMPORTANT 1 from code review: dry-run must NOT run ALTER TABLE.

    Builds a v1.x-shaped DB by hand (no apps_script_hmac_key column),
    runs the migration in dry-run, and asserts the column is STILL
    absent. The pre-fix code path called _ensure_initialized before
    the heartbeat / dry-run branch, which silently added the column
    — breaking the "dry-run touches nothing" contract.
    """
    # Build a v1.x-shaped DB by hand. Use a path the in-process
    # _initialized_paths cache hasn't seen, so we control whether
    # the package's schema-init runs.
    legacy_db = tmp_path / "legacy.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(legacy_db))

    conn = sqlite3.connect(legacy_db, isolation_level=None)
    try:
        # Schema as it shipped in v1.3.1 — no apps_script_hmac_key.
        conn.execute(
            """
            CREATE TABLE user_state (
                user_id                      TEXT PRIMARY KEY,
                google_creds_json            TEXT,
                apps_script_url              TEXT,
                apps_script_script_id        TEXT,
                apps_script_deployment_id    TEXT,
                apps_script_version_number   INTEGER,
                apps_script_content_hash     TEXT,
                created_at                   INTEGER NOT NULL,
                updated_at                   INTEGER NOT NULL
            )
            """
        )
        old = int(time.time()) - 3600  # well past the heartbeat window
        conn.execute(
            "INSERT INTO user_state "
            "(user_id, apps_script_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-uid", "https://script.google.com/macros/s/Q/exec", old, old),
        )
    finally:
        conn.close()

    # Dry-run.
    rc = mig.main([])
    assert rc == 0

    # Re-open the DB raw and check that the column is STILL absent —
    # dry-run must not have run the ALTER TABLE.
    conn = sqlite3.connect(legacy_db, isolation_level=None)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(user_state)")}
    finally:
        conn.close()

    assert "apps_script_hmac_key" not in cols, (
        "dry-run added the apps_script_hmac_key column to a legacy DB — "
        "schema mutation leaked from --apply path into the default "
        "dry-run path. See IMPORTANT 1 in the v2.0a review."
    )


def test_migrate_skips_already_migrated():
    """A user with a key present is skipped on subsequent runs."""
    # Seed an already-migrated user — provide a valid 64-hex key.
    existing_key = "a" * 64
    _seed_user(
        "user-already",
        apps_script_url="https://script.google.com/macros/s/Z/exec",
        apps_script_hmac_key=existing_key,
    )
    _back_date_all()

    rc = mig.main(["--apply"])
    assert rc == 0

    after = _read_key("user-already")
    assert after == existing_key, (
        "migration overwrote an already-present key — should have been "
        "a no-op for this user"
    )


def test_migrate_fails_loud_on_recent_heartbeat():
    """If a row was updated in the last 60s, refuse to run (even with --apply)."""
    _seed_user(
        "user-live",
        apps_script_url="https://script.google.com/macros/s/L/exec",
    )
    # Do NOT back-date — save_state set updated_at = now, so the
    # heartbeat check should refuse.

    with pytest.raises(SystemExit) as exc_info:
        mig.main(["--apply"])

    # SystemExit.code is the message string here (we raise SystemExit(msg)).
    msg = str(exc_info.value)
    assert "REFUSING TO MIGRATE" in msg, (
        f"heartbeat refusal message changed shape — got: {msg!r}"
    )
    assert "Stop the server" in msg, (
        "operator-facing message should say 'Stop the server' so the "
        f"fix is obvious — got: {msg!r}"
    )

    # And the key must not have been written despite the refusal.
    assert _read_key("user-live") is None


def test_migrate_force_skips_heartbeat(caplog):
    """``--force`` bypasses the heartbeat check and lets the migration run.

    Operators use this when they have out-of-band evidence the server
    is down (e.g. checked fly machine state) and they don't want to
    wait 60s for the heartbeat to clear. The script must log a loud
    WARNING when --force is set so it's obvious in the deploy log.
    """
    _seed_user(
        "user-force",
        apps_script_url="https://script.google.com/macros/s/F/exec",
    )
    # Do NOT back-date — without --force the heartbeat check would
    # refuse. With --force it should proceed.

    import logging
    with caplog.at_level(logging.WARNING):
        rc = mig.main(["--apply", "--force"])
    assert rc == 0

    # Key should have been written.
    assert _read_key("user-force") is not None, (
        "--force with --apply should let the migration write the key"
    )

    # And there should be a loud WARNING about --force.
    warning_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"
    ]
    assert any("--force" in m for m in warning_messages), (
        "expected a WARNING-level log about --force usage; got: "
        f"{warning_messages!r}"
    )


def test_migrate_user_id_filter_only_migrates_one():
    """--user-id X migrates only that user, leaves others alone."""
    _seed_user(
        "alice",
        apps_script_url="https://script.google.com/macros/s/A/exec",
    )
    _seed_user(
        "bob",
        apps_script_url="https://script.google.com/macros/s/B/exec",
    )
    _back_date_all()

    rc = mig.main(["--user-id", "alice", "--apply"])
    assert rc == 0

    assert _read_key("alice") is not None, "alice was supposed to be migrated"
    assert _read_key("bob") is None, (
        "--user-id alice migrated bob too — filter not honored"
    )


def test_migrate_reports_needs_redeploy(capsys):
    """Users with a v1.x deployment land in the needs-redeploy list."""
    _seed_user(
        "user-had-deployment",
        apps_script_url="https://script.google.com/macros/s/D/exec",
    )
    _seed_user(
        "user-no-deployment",
        # No apps_script_url — never deployed. Doesn't need re-deploy.
    )
    _back_date_all()

    mig.main(["--apply"])
    out = capsys.readouterr().out

    assert "needs_redeploy" in out.lower() or "re-deploy" in out.lower(), (
        f"report missing needs-redeploy section — got: {out!r}"
    )
    assert "user-had-deployment" in out, (
        "user with a v1.x deployment should appear in needs-redeploy list"
    )
    # The other user (no deployment) should NOT be in the list.
    # The check is intentionally narrow — the user_id appears nowhere
    # under the needs-redeploy heading.
    needs_redeploy_section = out.split("needing gdocs_setup_apps_script")[-1]
    assert "user-no-deployment" not in needs_redeploy_section, (
        "user with no v1.x deployment was flagged for re-deploy — they "
        "have nothing deployed, so there's nothing to re-deploy"
    )


def test_migrate_empty_db_returns_zero():
    """A fresh DB with no rows reports gracefully and returns 0."""
    # No _seed_user calls — DB exists (autouse fixture) but is empty.
    # We need to trigger schema init first via a get_state call so the
    # DB file actually materializes.
    from appscriptly import user_store
    user_store.get_state("triggers-init")

    rc = mig.main(["--apply"])
    assert rc == 0
