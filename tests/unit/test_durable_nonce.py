"""Unit tests for ``appscriptly.durable_nonce.DurableNonceStore``.

Single-use nonce semantics backed by SQLite on ``/data`` — the property
that makes it worth the disk I/O over the in-process ``NonceStore`` is
that a consumed nonce stays consumed across a restart (a fresh store
instance on the same DB file still rejects the replay).

The autouse ``isolated_db`` fixture (tests/conftest) points
``GOOGLE_DOCS_DATA_DIR`` at a per-test tmp dir, so ``db_path()`` resolves
to a fresh ``oauth_nonces.db`` per test; the local autouse fixture below
resets the module's per-path init guard so a "restart" test starts clean.
"""
from __future__ import annotations

import time

import pytest

from appscriptly import durable_nonce
from appscriptly.durable_nonce import DurableNonceStore


@pytest.fixture(autouse=True)
def _reset_init_guard():
    durable_nonce._initialized_paths.clear()
    yield
    durable_nonce._initialized_paths.clear()


def _row_count(nonce: str) -> int:
    with durable_nonce._connect(durable_nonce.db_path()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM consumed_nonces WHERE nonce = ?", (nonce,)
        ).fetchone()[0]


def test_first_consume_true_replay_false():
    store = DurableNonceStore()
    exp = int(time.time()) + 600
    assert store.consume("nonce-A", exp) is True
    # Replay within the same instance: PRIMARY KEY conflict -> False.
    assert store.consume("nonce-A", exp) is False


def test_distinct_nonces_are_independent():
    store = DurableNonceStore()
    exp = int(time.time()) + 600
    assert store.consume("nonce-1", exp) is True
    assert store.consume("nonce-2", exp) is True
    assert store.consume("nonce-1", exp) is False


def test_consume_survives_simulated_restart():
    """DISCRIMINATING (c): a nonce consumed by one store instance is still
    rejected by a FRESH instance on the same DB file — i.e. across a
    process restart / on another instance sharing the /data volume.

    On main (in-process ``NonceStore``) this FAILS: a fresh instance has
    an empty in-memory set and would accept the replay."""
    exp = int(time.time()) + 600

    store1 = DurableNonceStore()
    assert store1.consume("nonce-restart", exp) is True

    # Simulate a restart: drop the module's init-guard (a fresh process
    # would have none) and build a brand-new store over the SAME DB file.
    durable_nonce._initialized_paths.clear()
    store2 = DurableNonceStore()
    assert store2.consume("nonce-restart", exp) is False, (
        "a fresh store instance must still reject a nonce consumed before "
        "the 'restart' — durability is the whole point"
    )


def test_expired_rows_are_purged_on_write():
    """DISCRIMINATING (d): the opportunistic purge drops rows whose exp has
    passed, so the table stays bounded and an expired nonce is forgotten
    (it could never be validly redeemed anyway — expiry is checked before
    consume)."""
    store = DurableNonceStore()
    now = int(time.time())

    # A short-lived nonce, then let its exp pass.
    assert store.consume("nonce-old", now + 1) is True
    assert _row_count("nonce-old") == 1
    time.sleep(1.2)

    # A later consume runs the purge (DELETE WHERE exp <= now) first.
    assert store.consume("nonce-new", now + 600) is True
    assert _row_count("nonce-old") == 0, "expired row was not purged"
    assert _row_count("nonce-new") == 1


def test_is_a_nonce_store_dropin():
    """DurableNonceStore must substitute anywhere a NonceStore is expected
    (the callback state check + signed-URL replay check both type against
    NonceStore)."""
    from appscriptly.crypto import NonceStore

    assert isinstance(DurableNonceStore(), NonceStore)
