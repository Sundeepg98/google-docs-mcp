# STATUS NOTE: this script backfills `apps_script_hmac_key` for legacy
# users. As of v2.0c the key IS consumed at runtime — restructure.gs
# verifies the per-request signature (`Utilities.computeHmacSha256Signature`)
# and `_call_webapp` signs every POST. Backfilling now provides real
# runtime security uplift: a legacy user gains an authenticated /exec
# endpoint once they re-run gdocs_install_automation so their DEPLOYED
# script carries the key (see the re-run note below).
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

**Default is dry-run.** Invocations without ``--apply`` are read-only:
no schema mutation, no row writes, no ``updated_at`` bumps. This is
the safety default — operators have to opt in with ``--apply`` to
actually mutate the DB. The ``--force`` flag skips the heartbeat
check; use it only when you've confirmed the server is down some
other way (process check, Fly machine state) and you trust your
out-of-band evidence more than the in-DB heartbeat signal.

Users with ``apps_script_url`` set but no ``apps_script_hmac_key`` (i.e.
they had a v1.x deployment) must re-run ``gdocs_setup_apps_script`` after
this script completes — the freshly minted HMAC key needs to land in
their deployed Apps Script too, otherwise signature verification fails on
the first v2.0 request. The script prints the list at the end for
operator follow-up (email blast / status page / etc.).

Usage::

    # Default = dry-run: report what would change, write nothing.
    python scripts/migrate_existing_users.py

    # Apply: actually mutate the DB (writes the keys).
    python scripts/migrate_existing_users.py --apply

    # Migrate a single user only (debugging, re-runs). Still dry-run
    # without --apply.
    python scripts/migrate_existing_users.py --user-id 1234567890 --apply

    # Point at a different DB (e.g. a Fly volume snapshot).
    python scripts/migrate_existing_users.py --data-dir /mnt/fly-backup

    # Skip heartbeat check (use only when you've otherwise confirmed
    # the server is down; logs a loud WARNING).
    python scripts/migrate_existing_users.py --apply --force

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
        # Empty DB — no rows, no heartbeat to check. Safe to proceed.
        # Log so operators wondering whether the check ran see a trail.
        _log.info(
            "heartbeat check: no rows in user_state, nothing to race on",
        )
        return

    age = int(time.time()) - int(max_updated)
    if age < _HEARTBEAT_WINDOW_SECONDS:
        raise SystemExit(
            f"REFUSING TO MIGRATE: user_state has a row updated {age}s ago "
            f"(< {_HEARTBEAT_WINDOW_SECONDS}s window). The server appears "
            f"to be live. Stop the server, wait at least "
            f"{_HEARTBEAT_WINDOW_SECONDS}s, then re-run this script. "
            f"(Override with --force only if you've otherwise confirmed "
            f"the server is down.) "
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

    The per-user lock from ``credentials._user_lock`` is an in-process
    ``dict[str, threading.Lock]`` — it serializes only between threads
    inside THIS Python process. It does NOT coordinate across processes
    (a live server runs its own dict). Cross-process protection comes
    from the heartbeat check + the operator stopping the server before
    running. We take the lock anyway as defense-in-depth against a
    hypothetical multi-threaded migrate (none currently exists).
    """
    # Defer the import — keeps argparse --help / dry-run with empty DB
    # fast and lets the script stay runnable in stripped-down envs.
    from appscriptly.credentials import _user_lock

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
            "(v1.x -> v2.0). Default is DRY-RUN; pass --apply to write. "
            "Stop the server before running."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually mutate the DB (write keys, bump updated_at, run "
            "the schema ALTER TABLE if needed). Without this flag the "
            "script is read-only — touches nothing."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Skip the heartbeat check (which refuses to run if any row "
            "was updated in the last 60s). Use only when you've "
            "otherwise confirmed the server is down. Logs a loud "
            "WARNING when set."
        ),
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

    # ``--apply`` opts INTO writes. Default is dry-run — the safe default
    # for a one-shot migration script (operator must explicitly take
    # responsibility for mutating production state).
    dry_run = not args.apply

    if args.force:
        _log.warning(
            "--force passed: skipping the heartbeat check. Make sure "
            "the server is actually down — the check exists for a "
            "reason (refresh writes from a live server race the "
            "migration's UPDATE).",
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

    # Order matters here: we open SQLite raw and run the heartbeat
    # check FIRST, before any schema-mutating code. That way a dry-run
    # is genuinely read-only (no ALTER TABLE, no _ensure_initialized
    # side-effects) and a heartbeat-refused run aborts without having
    # touched the DB. ``_ensure_initialized`` runs only if we've
    # decided to actually write (apply mode, past the heartbeat check).
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row

        if not args.force:
            _assert_no_recent_heartbeat(conn)

        # In apply mode, schema-init NOW (after heartbeat clearance).
        # Done after opening our own conn — _ensure_initialized opens
        # its own short-lived connection internally and that's fine
        # under WAL with busy_timeout.
        if not dry_run:
            # Defer the import — keeps argparse --help fast and lets
            # the script stay runnable in stripped-down envs.
            from appscriptly import user_store
            user_store._ensure_initialized(db_path)

        rows = _select_target_rows(conn, args.user_id)
        if not rows:
            print(
                "No matching rows in user_state — nothing to migrate."
                if args.user_id is None
                else f"No row found for user_id={args.user_id!r}."
            )
            return 0

        outcomes = _migrate_all(rows, conn=conn, dry_run=dry_run)
        _report(outcomes, dry_run=dry_run)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
