"""Tests for the v2.0a user_state migration script.

Guards against:
- non-idempotent runs that re-key already-migrated users (breaking
  signature verification for users mid-flight)
- dry-run leaking writes to the DB
- the script producing keys that don't match the field validator
- silently running while the server is live (clobbering refresh writes)
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


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point user_store at a per-test SQLite file so tests don't bleed.

    Mirrors tests/unit/test_user_store.py — same env-var override path
    the script uses internally so we get the same file end-to-end.
    """
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    yield db_file


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
    from google_docs_mcp import user_store
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
    from google_docs_mcp import user_store
    path = user_store.db_path()
    cutoff = int(time.time()) - seconds_ago
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("UPDATE user_state SET updated_at = ?", (cutoff,))
    finally:
        conn.close()


def _read_key(user_id: str) -> str | None:
    from google_docs_mcp import user_store
    return user_store.get_state(user_id).get("apps_script_hmac_key")


def test_migrate_provisions_64_hex_key():
    """Fresh legacy user gets a key matching the field validator."""
    from google_docs_mcp.user_store import _valid_apps_script_hmac_key

    _seed_user(
        "user-legacy-1",
        apps_script_url="https://script.google.com/macros/s/ABC/exec",
    )
    _back_date_all()

    rc = mig.main([])
    assert rc == 0

    key = _read_key("user-legacy-1")
    assert key is not None, "migration did not write a key"
    assert _valid_apps_script_hmac_key(key), (
        f"migration wrote {key!r}, which fails the field validator — "
        "format is supposed to be 64-char lowercase hex"
    )


def test_migrate_idempotent():
    """Two runs of the script leave the same key. Second run is a no-op."""
    _seed_user(
        "user-id-2",
        apps_script_url="https://script.google.com/macros/s/X/exec",
    )
    _back_date_all()

    mig.main([])
    first_key = _read_key("user-id-2")
    assert first_key is not None

    # Second run — should detect existing key and skip.
    # Back-date again because the first run just bumped updated_at.
    _back_date_all()
    mig.main([])
    second_key = _read_key("user-id-2")

    assert first_key == second_key, (
        "migration re-keyed an already-migrated user — breaks signature "
        "verification for any user mid-flight"
    )


def test_migrate_dry_run_no_writes():
    """--dry-run reports what would change but doesn't touch the DB."""
    _seed_user(
        "user-dryrun",
        apps_script_url="https://script.google.com/macros/s/Y/exec",
    )
    _back_date_all()

    # Capture updated_at before run to verify it's unchanged.
    from google_docs_mcp import user_store
    before = user_store.get_state("user-dryrun")
    assert "apps_script_hmac_key" not in before, "seed leaked a key"

    rc = mig.main(["--dry-run"])
    assert rc == 0

    after = user_store.get_state("user-dryrun")
    assert "apps_script_hmac_key" not in after, (
        "--dry-run wrote a key to the DB — dry-run is supposed to be read-only"
    )
    assert after["updated_at"] == before["updated_at"], (
        "--dry-run bumped updated_at — dry-run is supposed to be read-only"
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

    rc = mig.main([])
    assert rc == 0

    after = _read_key("user-already")
    assert after == existing_key, (
        "migration overwrote an already-present key — should have been "
        "a no-op for this user"
    )


def test_migrate_fails_loud_on_recent_heartbeat():
    """If a row was updated in the last 60s, refuse to run."""
    _seed_user(
        "user-live",
        apps_script_url="https://script.google.com/macros/s/L/exec",
    )
    # Do NOT back-date — save_state set updated_at = now, so the
    # heartbeat check should refuse.

    with pytest.raises(SystemExit) as exc_info:
        mig.main([])

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

    rc = mig.main(["--user-id", "alice"])
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

    mig.main([])
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
    from google_docs_mcp import user_store
    user_store.get_state("triggers-init")

    rc = mig.main([])
    assert rc == 0
