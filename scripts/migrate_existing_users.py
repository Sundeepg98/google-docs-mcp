"""v2.0a migration — backfill ``apps_script_hmac_key`` for legacy user rows.

Pre-v2.0 the MCP server signed requests to each user's Apps Script Web App
with a single shared key. v2.0+ flips to a per-user HMAC-SHA256 key stored
on the user_state row (``apps_script_hmac_key`` — 64-char lowercase hex,
generated via ``secrets.token_hex(32)``). Existing rows have no key yet
because the column didn't exist in v1.x.

This one-shot script reads every row from ``user_state.db`` and writes a
freshly generated key to any row missing one. Idempotent — re-running it
is a no-op once every row has a key (or is intentionally pruned).

**STOP THE SERVER BEFORE RUNNING.** This script asserts that no row in
user_state was touched in the last 60 seconds (heartbeat check). If the
server is live, OAuth callbacks or token-refresh writes will bump
``updated_at`` and the migration refuses to run. Coordinating with a
live server is out of scope — graceful-shutdown then migrate is the
supported path. (See issue #13 for the rollout playbook.)

Users with ``apps_script_url`` set but no ``apps_script_hmac_key`` (i.e.
they had a v1.x deployment) must re-run ``gdocs_setup_apps_script`` after
this script completes — the freshly minted HMAC key needs to land in
their deployed Apps Script too, otherwise signature verification fails on
the first v2.0 request. The script prints the list at the end for
operator follow-up (email blast / status page / etc.).

Usage::

    # Dry run — report what would change, write nothing.
    python scripts/migrate_existing_users.py --dry-run

    # Migrate every row in the default data dir's DB.
    python scripts/migrate_existing_users.py

    # Migrate a single user only (debugging, re-runs).
    python scripts/migrate_existing_users.py --user-id 1234567890

    # Point at a different DB (e.g. a Fly volume snapshot).
    python scripts/migrate_existing_users.py --data-dir /mnt/fly-backup

    # Verbose — per-row decisions logged to stderr.
    python scripts/migrate_existing_users.py -v
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

_log = logging.getLogger("migrate_existing_users")

# Heartbeat window — if any user_state row was updated this recently,
# assume the server is live and refuse to run. Generous enough that a
# real graceful shutdown (which can take ~10s for in-flight requests +
# Fly's grace period) clears the window comfortably; tight enough that
# the operator notices stale processes rather than waiting all day.
_HEARTBEAT_WINDOW_SECONDS = 60


def _resolve_db_path(data_dir: Path | None) -> Path:
    """Resolve the SQLite DB path the same way ``user_store.db_path()`` does.

    Mirror — not import — to keep the script runnable without sourcing
    the package (handy for ops-only environments). The
    ``GOOGLE_DOCS_USER_STORE_PATH`` env override still wins so tests
    and CI can point at a tmp file.
    """
    override = os.environ.get("GOOGLE_DOCS_USER_STORE_PATH")
    if override:
        return Path(override)
    if data_dir is not None:
        return data_dir / "user_state.db"
    # Default — same as auth.default_data_dir() for the operator account.
    return Path.home() / ".google-docs-mcp" / "user_state.db"


def _assert_no_recent_heartbeat(conn: sqlite3.Connection) -> None:
    """Refuse to run if any row was updated in the last 60s.

    Coordinating with a live server is out of scope for this script —
    we'd need a shared lock dance that v1.x doesn't have. Easier to
    just refuse, document "stop the server first," and trust the
    operator to follow the playbook.
    """
    row = conn.execute("SELECT MAX(updated_at) FROM user_state").fetchone()
    max_updated = row[0] if row else None
    if max_updated is None:
        # Empty DB. Safe to proceed — nothing to race on.
        return

    age = int(time.time()) - int(max_updated)
    if age < _HEARTBEAT_WINDOW_SECONDS:
        raise SystemExit(
            f"REFUSING TO MIGRATE: user_state has a row updated {age}s ago "
            f"(< {_HEARTBEAT_WINDOW_SECONDS}s window). The server appears "
            f"to be live. Stop the server, wait at least "
            f"{_HEARTBEAT_WINDOW_SECONDS}s, then re-run this script. "
            f"See scripts/migrate_existing_users.py docstring."
        )


def _select_target_rows(
    conn: sqlite3.Connection, only_user_id: str | None,
) -> list[sqlite3.Row]:
    """Return rows that need consideration.

    Filters by ``user_id`` if given, else returns every row. Idempotency
    is enforced later (per-row check on ``apps_script_hmac_key``) rather
    than at the SELECT step — the per-row branch keeps the verbose
    output honest (we log "skipped: already has key" for each).
    """
    if only_user_id:
        rows = conn.execute(
            "SELECT * FROM user_state WHERE user_id = ?", (only_user_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM user_state").fetchall()
    return list(rows)


def _generate_hmac_key() -> str:
    """Mint a fresh per-user HMAC-SHA256 key.

    ``secrets.token_hex(32)`` returns 32 bytes (256 bits) encoded as a
    64-character lowercase hex string — exactly matches the validator
    in ``user_store._valid_apps_script_hmac_key``. Centralized here so
    the format definition lives in one place.
    """
    return secrets.token_hex(32)


def _migrate_user(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    dry_run: bool,
) -> tuple[str, bool]:
    """Return (decision, needs_redeploy) for one user.

    decision ∈ {"skipped:already_migrated", "would_migrate", "migrated"}.
    ``needs_redeploy`` is True if the user had an Apps Script deployment
    before this run — their deployed script doesn't know the new HMAC
    key yet, so they need to re-run gdocs_setup_apps_script.
    """
    # Defensive — we may be running against a DB written before the
    # column existed. If get-by-column-name raises, treat as absent.
    try:
        existing_key = row["apps_script_hmac_key"]
    except (IndexError, KeyError):
        existing_key = None

    user_id = row["user_id"]
    had_deployment = bool(row["apps_script_url"])

    if existing_key:
        return "skipped:already_migrated", False

    if dry_run:
        return "would_migrate", had_deployment

    new_key = _generate_hmac_key()
    # Use direct SQL rather than user_store.save_state so the script can
    # run against a DB whose schema doesn't yet have the column registered
    # with the package's validators (e.g. migrating from a snapshot taken
    # before package upgrade). ``user_store._ensure_initialized`` was
    # called via the import in main() before we got here, so the ALTER
    # TABLE has already run if needed.
    now = int(time.time())
    conn.execute(
        "UPDATE user_state SET apps_script_hmac_key = ?, updated_at = ? "
        "WHERE user_id = ?",
        (new_key, now, user_id),
    )
    return "migrated", had_deployment


def _migrate_all(
    rows: Iterable[sqlite3.Row], *, conn: sqlite3.Connection, dry_run: bool,
) -> dict[str, list[str]]:
    """Iterate rows, take per-user lock, run migration, collect outcomes.

    Per-user lock acquired via ``credentials._user_lock`` — defensive
    against the (unsupported) "server is live and you ignored the
    heartbeat refusal somehow" case. If the lock is in our process
    (typical), it's a near-no-op; if another process holds the row,
    SQLite's busy_timeout in the connection serializes the actual write.
    """
    # Defer the import — keeps argparse --help / --dry-run with empty DB
    # fast and lets the script stay runnable in stripped-down envs.
    from google_docs_mcp.credentials import _user_lock

    outcomes: dict[str, list[str]] = {
        "migrated": [],
        "skipped:already_migrated": [],
        "would_migrate": [],
        "needs_redeploy": [],
    }
    for row in rows:
        user_id = row["user_id"]
        with _user_lock(user_id):
            decision, needs_redeploy = _migrate_user(
                conn, row, dry_run=dry_run,
            )
        outcomes[decision].append(user_id)
        if needs_redeploy:
            outcomes["needs_redeploy"].append(user_id)
        _log.debug(
            "user_id=%s decision=%s needs_redeploy=%s",
            user_id[:8], decision, needs_redeploy,
        )
    return outcomes


def _report(outcomes: dict[str, list[str]], *, dry_run: bool) -> None:
    """Print a human-readable summary to stdout.

    The needs_redeploy list is the operationally critical bit — it's
    the audience for the post-migration ``gdocs_setup_apps_script``
    nudge email/banner.
    """
    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"\n=== migrate_existing_users — {mode} ===\n")
    if dry_run:
        n = len(outcomes["would_migrate"])
        print(f"Would migrate: {n} user(s)")
    else:
        n = len(outcomes["migrated"])
        print(f"Migrated: {n} user(s)")
    print(f"Skipped (already migrated): {len(outcomes['skipped:already_migrated'])}")

    redeploy = outcomes["needs_redeploy"]
    print(f"\nUsers needing gdocs_setup_apps_script re-deploy: {len(redeploy)}")
    if redeploy:
        print(
            "  (They had a v1.x Apps Script deployment — its deployed code "
            "doesn't know the freshly minted HMAC key. Until they re-run "
            "gdocs_setup_apps_script, v2.0+ requests will fail signature "
            "verification.)"
        )
        for uid in redeploy:
            print(f"  - {uid}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_existing_users",
        description=(
            "Backfill apps_script_hmac_key for legacy user_state rows "
            "(v1.x -> v2.0). Stop the server before running."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change; write nothing.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Migrate only this user_id (debugging / re-runs).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Data dir containing user_state.db. Defaults to "
            "~/.google-docs-mcp (or whatever GOOGLE_DOCS_USER_STORE_PATH "
            "points at)."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Per-row decisions logged to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    db_path = _resolve_db_path(args.data_dir)
    if not db_path.exists():
        # No DB = no rows to migrate. Treat as success (idempotent in
        # the "nothing to do" sense) so the script is safe to run on a
        # fresh host as part of an automated deploy pipeline.
        _log.warning(
            "user_state DB does not exist at %s — nothing to migrate.",
            db_path,
        )
        return 0

    # Force the package's schema-init path to run so the column exists
    # before we try to UPDATE it. Imports happen here (not at top) so
    # --help works without the package installed.
    from google_docs_mcp import user_store
    user_store._ensure_initialized(db_path)

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row

        _assert_no_recent_heartbeat(conn)

        rows = _select_target_rows(conn, args.user_id)
        if not rows:
            print(
                "No matching rows in user_state — nothing to migrate."
                if args.user_id is None
                else f"No row found for user_id={args.user_id!r}."
            )
            return 0

        outcomes = _migrate_all(rows, conn=conn, dry_run=args.dry_run)
        _report(outcomes, dry_run=args.dry_run)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
