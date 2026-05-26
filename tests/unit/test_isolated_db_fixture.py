"""R23 B3: prove the canonical ``isolated_db`` fixture really does reset
every shared module-level dict in ``google_docs_mcp``.

Before consolidation, five test files each declared a local ``isolated_db``
with subtly different cleanup discipline. That divergence was the symptom of
test-isolation debt — under ``pytest -n auto`` it would surface as flakes
the moment two workers raced on the shared dicts.

These tests pin the canonical fixture's contract: the module dicts are
empty/None at the START of every test (pre-yield reset), and they're
empty/None again at the START of THIS test (post-yield reset from the
previous test). The pre-yield assertions cover both directions in one
go — if the post-yield reset regressed, a prior test's pollution would
fail the pre-yield assertion here.
"""
from __future__ import annotations


def test_isolated_db_fixture_clears_all_module_state(isolated_db):
    """Every shared module-level dict starts each test in a known-empty state."""
    from google_docs_mcp import credentials, keys, user_store
    # M3 Phase C (v2.1.5): _creds_cache moved from server.py to
    # _tool_helpers.py with _get_credentials.
    from google_docs_mcp import _tool_helpers as server_mod

    assert credentials._per_user_locks == {}, (
        "credentials._per_user_locks not cleared — per-user lock "
        "contention from a prior test will bleed into this one"
    )
    assert user_store._initialized_paths == set(), (
        "user_store._initialized_paths not cleared — this test's fresh "
        "tmp DB will skip the schema-init / ALTER path"
    )
    # _shim_hit_counter retains its three known keys but values must be 0.
    assert keys.get_shim_hit_counters() == {
        "api_bearer": 0,
        "oauth_state": 0,
        "signed_url": 0,
    }, (
        "keys._shim_hit_counter not zeroed — assertions of "
        "'shim_hits == 0' will spuriously fail after a prior test "
        "triggered the back-compat shim"
    )
    assert server_mod._creds_cache is None, (
        "server._creds_cache not cleared — stdio-mode creds from a "
        "prior test will short-circuit this test's per-user resolver"
    )


def test_isolated_db_pollutes_shared_state_then_next_test_sees_clean(
    isolated_db,
):
    """First half of a two-test pair: deliberately pollute every dict.

    The next test (``test_followup_after_pollution_starts_clean``) runs
    in the same module right after, alphabetically. If the post-yield
    reset in conftest's ``isolated_db`` regresses, that next test's
    pre-yield assertions will catch the leak.
    """
    import threading

    from google_docs_mcp import credentials, keys, user_store
    # M3 Phase C (v2.1.5): _creds_cache moved from server.py to
    # _tool_helpers.py with _get_credentials.
    from google_docs_mcp import _tool_helpers as server_mod

    credentials._per_user_locks["poison-user"] = threading.Lock()
    user_store._initialized_paths.add(isolated_db)  # any Path works
    # Mutate the shim counter via the lock-guarded mechanism.
    with keys._shim_hit_counter_lock:
        keys._shim_hit_counter["api_bearer"] = 999
    server_mod._creds_cache = object()  # sentinel

    # Sanity: the mutations actually landed.
    assert credentials._per_user_locks
    assert user_store._initialized_paths
    assert keys.get_shim_hit_counters()["api_bearer"] == 999
    assert server_mod._creds_cache is not None


def test_qq_followup_after_pollution_starts_clean(isolated_db):
    """Second half. Name starts with ``test_qq`` so it sorts AFTER
    ``test_isolated_db_pollutes_shared_state_then_next_test_sees_clean``
    in the same module — pytest default order is lexicographic."""
    from google_docs_mcp import credentials, keys, user_store
    # M3 Phase C (v2.1.5): _creds_cache moved from server.py to
    # _tool_helpers.py with _get_credentials.
    from google_docs_mcp import _tool_helpers as server_mod

    assert credentials._per_user_locks == {}, "post-yield reset regressed"
    assert user_store._initialized_paths == set(), "post-yield reset regressed"
    assert keys.get_shim_hit_counters() == {
        "api_bearer": 0,
        "oauth_state": 0,
        "signed_url": 0,
    }, "post-yield reset regressed"
    assert server_mod._creds_cache is None, "post-yield reset regressed"
