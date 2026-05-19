"""Tests for the v2.3 admin-only ``gdocs_admin_audit`` forensic tool.

Guards against:
- Admin surface accidentally enabled when ``MCP_ADMIN_TOKEN`` is unset
  (would expose the audit primitive to any caller)
- Naive ``==`` token comparison instead of ``hmac.compare_digest``
  (would leak the env token via response-timing side channel)
- ``since_hours`` accepting values that would balloon the response or
  pass nonsense (negative, zero, >1 week, bool, str)
- ``user_id`` leaking into logs at full length (PII / Google sub claim)
- The audit query missing rows in-window or including rows out-of-window
"""
from __future__ import annotations

import logging
import time

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point user_store at a per-test SQLite file so tests don't bleed.

    Mirrors tests/unit/test_user_store.py — same env-var override path
    user_store reads internally.
    """
    db_file = tmp_path / "user_state.db"
    monkeypatch.setenv("GOOGLE_DOCS_USER_STORE_PATH", str(db_file))
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))

    # Reset the package's in-process init cache so the new tmp DB
    # actually gets schema-init treatment rather than being skipped
    # due to a cache hit from a previous test's path.
    from google_docs_mcp import user_store
    user_store._initialized_paths.clear()

    yield db_file


@pytest.fixture(autouse=True)
def clean_admin_env(monkeypatch):
    """Make sure MCP_ADMIN_TOKEN is unset by default in every test.

    Tests that need it set use ``monkeypatch.setenv`` explicitly so the
    "set vs unset" axis is always visible in the test body — never an
    accident of env-var leakage from the host shell or a prior test.
    """
    monkeypatch.delenv("MCP_ADMIN_TOKEN", raising=False)


# ---------- helpers ---------------------------------------------------------

def _seed_user(user_id: str, *, updated_at: int | None = None) -> None:
    """Insert a user_state row. If ``updated_at`` given, back-/forward-date it."""
    from google_docs_mcp import user_store
    user_store.save_state(user_id, {})  # row with no fields; just timestamps
    if updated_at is not None:
        import sqlite3
        path = user_store.db_path()
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute(
                "UPDATE user_state SET updated_at = ? WHERE user_id = ?",
                (updated_at, user_id),
            )
        finally:
            conn.close()


def _call(admin_token, user_id, since_hours=24):
    """Invoke gdocs_admin_audit through its underlying function.

    FastMCP wraps tool functions; ``.fn`` exposes the original callable
    without the wrapper so we can assert on raw return values + raises
    instead of fighting the wire-protocol layer.
    """
    from google_docs_mcp.server import gdocs_admin_audit
    fn = getattr(gdocs_admin_audit, "fn", gdocs_admin_audit)
    return fn(admin_token, user_id, since_hours)


# ---------- gating: env + token --------------------------------------------

def test_admin_audit_rejects_without_env_token():
    """No MCP_ADMIN_TOKEN env var = admin surface disabled."""
    with pytest.raises(ToolError, match="admin disabled"):
        _call("anything", "user-1", 24)


def test_admin_audit_rejects_bad_token(monkeypatch):
    """Env set, arg mismatch = explicit rejection."""
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "the-real-token")
    with pytest.raises(ToolError, match="does not match"):
        _call("wrong-token", "user-1", 24)


def test_admin_audit_uses_compare_digest_not_equality(monkeypatch):
    """Naive ``==`` would short-circuit on the first mismatching byte —
    leaking the env token via response-time side channel. The code must
    use ``hmac.compare_digest``. Verify by patching it and confirming
    our patch was the one consulted; raw == would not call it.
    """
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "expected-token")

    import hmac as hmac_mod
    calls: list[tuple[bytes, bytes]] = []

    real_compare_digest = hmac_mod.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(
        "google_docs_mcp.server.hmac.compare_digest", _spy,
    )

    with pytest.raises(ToolError, match="does not match"):
        _call("wrong", "user-1", 24)

    assert calls, (
        "gdocs_admin_audit did not route through hmac.compare_digest — "
        "the token check appears to use naive ==, which leaks the env "
        "value via response-time side channel"
    )


def test_admin_audit_rejects_non_string_token(monkeypatch):
    """admin_token must be a string — int/None/etc. is a signature bug."""
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    for bad in (None, 12345, [], {}):
        with pytest.raises(ToolError, match="admin_token must be a string"):
            _call(bad, "user-1", 24)  # type: ignore[arg-type]


# ---------- user_id validation ---------------------------------------------

def test_admin_audit_rejects_empty_user_id(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    with pytest.raises(ToolError, match="user_id must be a non-empty string"):
        _call("secret", "", 24)


def test_admin_audit_rejects_non_string_user_id(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    for bad in (None, 12345, []):
        with pytest.raises(ToolError, match="user_id must be a non-empty string"):
            _call("secret", bad, 24)  # type: ignore[arg-type]


# ---------- since_hours bounds ---------------------------------------------

def test_admin_audit_rejects_short_hours(monkeypatch):
    """``since_hours < 1`` is nonsense (no window) and must error."""
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    for bad in (0, -1, -24):
        with pytest.raises(ToolError, match="since_hours must be an int in"):
            _call("secret", "user-1", bad)


def test_admin_audit_rejects_long_hours(monkeypatch):
    """``since_hours > 168`` (1 week) would balloon responses."""
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    for bad in (169, 200, 99999):
        with pytest.raises(ToolError, match="since_hours must be an int in"):
            _call("secret", "user-1", bad)


def test_admin_audit_rejects_non_int_hours(monkeypatch):
    """``since_hours`` of bool/str/None/float is a signature bug."""
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    for bad in (True, False, "24", 24.0, None):
        with pytest.raises(ToolError, match="since_hours must be an int in"):
            _call("secret", "user-1", bad)  # type: ignore[arg-type]


# ---------- happy-path window semantics ------------------------------------

def test_admin_audit_returns_user_state_within_window(monkeypatch):
    """Row with updated_at inside the window is returned; outside is not.

    Seeds 3 distinct users at known timestamps:
    - alice — updated_at = now (clearly in any reasonable window)
    - bob   — updated_at = 12h ago (in 24h window, out of 6h window)
    - carol — updated_at = 200h ago (out of any window ≤168h)
    """
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    now = int(time.time())

    _seed_user("alice", updated_at=now)
    _seed_user("bob", updated_at=now - 12 * 3600)
    _seed_user("carol", updated_at=now - 200 * 3600)

    # 24h window: alice and bob each in; carol always out.
    result_alice_24h = _call("secret", "alice", 24)
    assert result_alice_24h["total_entries"] == 1
    assert result_alice_24h["entries"][0]["timestamp"] == now
    assert result_alice_24h["entries"][0]["operation_type"] == "user_state_updated"
    assert result_alice_24h["user_id_prefix"] == "alice"  # 5 chars, <8 so untruncated
    assert result_alice_24h["window_hours"] == 24

    result_bob_24h = _call("secret", "bob", 24)
    assert result_bob_24h["total_entries"] == 1
    assert result_bob_24h["entries"][0]["timestamp"] == now - 12 * 3600

    # 6h window: bob now OUT (12h > 6h).
    result_bob_6h = _call("secret", "bob", 6)
    assert result_bob_6h["total_entries"] == 0
    assert result_bob_6h["entries"] == []

    # carol is 200h old — out of any allowed window.
    result_carol_168h = _call("secret", "carol", 168)
    assert result_carol_168h["total_entries"] == 0


def test_admin_audit_unknown_user_returns_empty_not_error(monkeypatch):
    """Unknown user_id is not an error — returns 0 entries plus the notes.

    The operator may not know whether the user has any server-side
    state yet; an empty result is the meaningful answer ("we have no
    record of this user") and is distinct from a token-or-arg-error.
    """
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    result = _call("secret", "ghost-user-never-seen", 24)
    assert result["total_entries"] == 0
    assert result["entries"] == []
    assert result["user_id_prefix"] == "ghost-us"
    assert "notes" in result and result["notes"]


# ---------- PII handling ---------------------------------------------------

def test_admin_audit_truncates_user_id_in_logs(monkeypatch, caplog):
    """user_id must NEVER appear in full in server logs.

    A Google ``sub`` claim is PII; logs are often shipped to third-
    party aggregators. The audit tool must log only ``user_id[:8]``.
    """
    monkeypatch.setenv("MCP_ADMIN_TOKEN", "secret")
    full_user_id = "1234567890abcdef-this-must-not-leak-in-full"
    _seed_user(full_user_id)

    with caplog.at_level(logging.INFO, logger="google_docs_mcp.server"):
        result = _call("secret", full_user_id, 24)

    # Sanity: the call succeeded.
    assert result["user_id_prefix"] == full_user_id[:8]

    # Walk every log record produced by our logger and assert the full
    # user_id does NOT appear anywhere. The prefix MAY appear (and
    # should, so the operator can correlate against the return value).
    for rec in caplog.records:
        msg = rec.getMessage()
        assert full_user_id not in msg, (
            f"full user_id leaked into log record: {msg!r} — "
            "must truncate to user_id[:8]"
        )
    # Belt + suspenders: at least one log line containing the prefix.
    prefix_appearances = [
        rec.getMessage() for rec in caplog.records
        if full_user_id[:8] in rec.getMessage()
    ]
    assert prefix_appearances, (
        "no log line mentioned user_id[:8] — verify _log.info call "
        "with the prefix is still present (operator needs it to "
        "correlate with the returned user_id_prefix)"
    )
