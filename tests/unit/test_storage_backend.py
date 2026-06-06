"""StorageBackend Protocol + InMemoryBackend coverage (v2.1).

The v2.1 storage abstraction extracts a thin ``StorageBackend`` Protocol
from ``user_store.py``. SqliteBackend keeps the production behavior;
InMemoryBackend exists for test ergonomics. These tests pin down:

  1. Both shipped implementations satisfy the Protocol (runtime check).
  2. InMemoryBackend round-trips through the same public facade as
     SqliteBackend with identical observable behavior — covers the
     promise that test code switching to InMemoryBackend gets the same
     contract.
  3. Cross-user isolation holds for InMemoryBackend (the property the
     production SQLite backend also has, but worth pinning separately
     so a future change to InMemoryBackend doesn't silently regress).
  4. The ``with_backend`` context manager save/restores correctly even
     when the body raises.

The existing ``tests/unit/test_user_store.py`` (which all run against
the default SqliteBackend) is the canonical contract — these new tests
just verify the InMemory drop-in matches it. The full SQLite suite was
already green pre-v2.1, and remains green post-v2.1 (375 → 375).
"""
from __future__ import annotations

import json
import threading

import pytest


# isolated_db fixture is auto-applied from tests/conftest.py (R23 B3
# consolidation, v2.0.5). Canonical version preserves this file's
# pre+post-yield _initialized_paths discipline and adds resets for
# _per_user_locks, _shim_hit_counter, and _creds_cache.


# ---------------------------------------------------------------------
# 1. Protocol shape: both impls satisfy StorageBackend
# ---------------------------------------------------------------------


def test_sqlite_backend_satisfies_protocol():
    """Runtime isinstance check against the @runtime_checkable Protocol.

    If a future refactor renames a method on SqliteBackend (e.g.
    `get_state` → `read_state`), this test fails BEFORE any caller
    breaks. The Protocol is the contract everything else trusts."""
    from appscriptly.user_store import SqliteBackend, StorageBackend
    assert isinstance(SqliteBackend(), StorageBackend), (
        "SqliteBackend no longer satisfies StorageBackend Protocol — "
        "a method was renamed or its signature changed"
    )


def test_inmemory_backend_satisfies_protocol():
    """Same shape check for InMemoryBackend so the two stay swappable."""
    from appscriptly.user_store import InMemoryBackend, StorageBackend
    assert isinstance(InMemoryBackend(), StorageBackend)


def test_protocol_requires_all_four_methods():
    """A class missing any one of the four methods must NOT satisfy
    the Protocol — guards against a future Postgres backend that
    silently forgets to implement, say, ``clear_state``."""
    from appscriptly.user_store import StorageBackend

    class IncompleteBackend:
        def init_schema(self) -> None: ...
        def get_state(self, user_id: str) -> dict: return {}
        def save_state(self, user_id: str, updates: dict) -> None: ...
        # MISSING: clear_state

    assert not isinstance(IncompleteBackend(), StorageBackend), (
        "Protocol runtime check let a backend missing clear_state slip "
        "through — the four-method shape contract isn't enforced"
    )


# ---------------------------------------------------------------------
# 2. InMemoryBackend round-trips through the public facade
# ---------------------------------------------------------------------


def test_inmemory_backend_round_trip_via_facade():
    """Plug InMemoryBackend in via with_backend; the public save_state /
    get_state functions must behave identically — same merge semantics,
    same return shape, same validator pass."""
    from appscriptly.user_store import (
        InMemoryBackend, get_state, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        save_state(
            "user-A",
            {
                "apps_script_url": "https://script.google.com/macros/s/AAA/exec",
                "apps_script_script_id": "SID-A",
            },
        )
        state = get_state("user-A")
        assert state["user_id"] == "user-A"
        assert state["apps_script_url"] == "https://script.google.com/macros/s/AAA/exec"
        assert state["apps_script_script_id"] == "SID-A"
        assert isinstance(state["created_at"], int)
        assert isinstance(state["updated_at"], int)


def test_inmemory_backend_merges_updates_not_overwrites():
    """The same killer guard as test_user_store.py's merge test — a
    partial update must NOT erase other fields. SqliteBackend gets
    this from the SET-clause-of-only-given-cols UPDATE; InMemoryBackend
    must give the same answer via ``row.update(updates)``."""
    from appscriptly.user_store import (
        InMemoryBackend, get_state, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        save_state(
            "u",
            {
                "apps_script_url": "https://script.google.com/macros/s/X/exec",
                "apps_script_script_id": "SID-1",
            },
        )
        save_state("u", {"apps_script_deployment_id": "D-1"})  # partial!

        state = get_state("u")
        assert state["apps_script_url"] == "https://script.google.com/macros/s/X/exec"
        assert state["apps_script_script_id"] == "SID-1"
        assert state["apps_script_deployment_id"] == "D-1"


def test_inmemory_backend_facade_runs_field_validators():
    """The facade's _FIELD_VALIDATORS pass must fire regardless of
    backend — otherwise InMemoryBackend tests could accidentally
    write data SqliteBackend would reject."""
    from appscriptly.user_store import (
        InMemoryBackend, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        with pytest.raises(ValueError, match="apps_script_url"):
            save_state("u", {"apps_script_url": "http://bad-not-https"})


# ---------------------------------------------------------------------
# 3. Cross-user isolation (InMemoryBackend)
# ---------------------------------------------------------------------


def test_inmemory_backend_isolates_users():
    """User A's state must be invisible to a read for user B and vice
    versa. Pinned separately from SQLite's isolation because future
    changes to InMemoryBackend (e.g. an indexing layer for faster
    iteration) could break this property without breaking SQLite."""
    from appscriptly.user_store import (
        InMemoryBackend, get_state, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        save_state("alice", {"google_creds_json": '{"token": "alice-tok"}'})
        save_state("bob", {"google_creds_json": '{"token": "bob-tok"}'})

        alice = get_state("alice")
        bob = get_state("bob")

        assert json.loads(alice["google_creds_json"])["token"] == "alice-tok"
        assert json.loads(bob["google_creds_json"])["token"] == "bob-tok"
        # Make sure the absent-user case stays absent.
        assert get_state("carol") == {}


def test_inmemory_backend_clear_state_is_idempotent():
    """clear_state on an absent user_id must NOT raise — matches the
    SqliteBackend's idempotent DELETE-WHERE semantics."""
    from appscriptly.user_store import (
        InMemoryBackend, clear_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        clear_state("user-never-existed")  # must not raise


def test_inmemory_backend_get_state_omits_none_columns():
    """The SqliteBackend filters NULL columns out of the returned dict
    so callers can use ``if "field" in state``. InMemoryBackend must
    match — otherwise tests using it would silently see None-valued
    keys that SQLite-backed code paths never see."""
    from appscriptly.user_store import (
        InMemoryBackend, get_state, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        save_state("u", {"google_creds_json": '{"token": "x"}'})
        state = get_state("u")
        # google_creds_json was set; the apps_script_* keys must be
        # absent (NOT present-with-None).
        assert "google_creds_json" in state
        assert "apps_script_url" not in state
        assert "apps_script_script_id" not in state


# ---------------------------------------------------------------------
# 4. with_backend context manager save/restore
# ---------------------------------------------------------------------


def test_with_backend_restores_previous_backend_on_normal_exit():
    """After ``with with_backend(X)`` exits normally, get_backend()
    returns the previous backend — not X. This is what makes the
    helper safe to use in test fixtures without explicit teardown."""
    from appscriptly.user_store import (
        InMemoryBackend, get_backend, with_backend,
    )

    before = get_backend()
    with with_backend(InMemoryBackend()) as bk:
        assert get_backend() is bk
    assert get_backend() is before


def test_with_backend_restores_previous_backend_on_exception():
    """Same restore guarantee when the with-body raises — a test that
    crashes inside the block must NOT poison subsequent tests with
    a leftover InMemoryBackend."""
    from appscriptly.user_store import (
        InMemoryBackend, get_backend, with_backend,
    )

    before = get_backend()
    with pytest.raises(RuntimeError, match="boom"):
        with with_backend(InMemoryBackend()):
            raise RuntimeError("boom")
    assert get_backend() is before


def test_set_backend_returns_previous_for_manual_restore():
    """The non-context-manager form (``set_backend``) returns the
    previous backend so callers without ``with`` can still restore."""
    from appscriptly.user_store import (
        InMemoryBackend, get_backend, set_backend,
    )

    original = get_backend()
    new = InMemoryBackend()
    previous = set_backend(new)
    try:
        assert previous is original
        assert get_backend() is new
    finally:
        set_backend(previous)
    assert get_backend() is original


# ---------------------------------------------------------------------
# 5. Concurrent safety of InMemoryBackend
# ---------------------------------------------------------------------


def test_inmemory_backend_handles_concurrent_writes_to_different_users():
    """Same property the SqliteBackend has via WAL — concurrent threads
    writing to DIFFERENT user_ids must all land cleanly. InMemoryBackend
    serializes via a single threading.Lock, but the contract is the
    same: no torn writes, last-write-wins per user."""
    from appscriptly.user_store import (
        InMemoryBackend, get_state, save_state, with_backend,
    )

    with with_backend(InMemoryBackend()):
        def worker(uid: str) -> None:
            for i in range(20):
                tag = uid.replace("-", "") + str(i)
                save_state(uid, {
                    "apps_script_url": f"https://script.google.com/macros/s/{tag}/exec",
                })

        threads = [
            threading.Thread(target=worker, args=(f"thr-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(5):
            uid = f"thr-{i}"
            state = get_state(uid)
            tag = uid.replace("-", "") + "19"
            assert state["apps_script_url"] == f"https://script.google.com/macros/s/{tag}/exec"


# ---------------------------------------------------------------------
# 6. SqliteBackend is still the module default
# ---------------------------------------------------------------------


def test_default_backend_is_sqlite():
    """A regression guard: a careless module-level edit setting
    ``_backend = InMemoryBackend()`` would silently make production
    runs in-memory. The default must always be SqliteBackend."""
    from appscriptly.user_store import SqliteBackend, get_backend
    assert isinstance(get_backend(), SqliteBackend), (
        f"default backend is no longer SqliteBackend (got "
        f"{type(get_backend()).__name__}) — production data would not "
        f"persist"
    )
