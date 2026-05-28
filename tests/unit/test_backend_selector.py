"""PR-Δ6 — Backend selector env-var contract.

The selector + ``init_default_backend_from_env`` are the runtime
seam that picks SqliteBackend vs VercelKvBackend based on the
``STORAGE_BACKEND`` env var. These tests pin the matrix:

  - unset / sqlite → SqliteBackend
  - vercel_kv + KV env vars set → VercelKvBackend
  - vercel_kv + KV env vars missing → SqliteBackend + WARNING (fail-soft)
  - unknown value → SqliteBackend + WARNING

The fail-soft behavior is the security-critical one: a misconfigured
operator deploy must not 500 on every request. The selector logs
the warning + falls back; downstream the bad backend choice surfaces
as "user needs to re-auth" rather than as an opaque crash.
"""
from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------
# Default + explicit-sqlite paths
# ---------------------------------------------------------------------


def test_select_backend_returns_sqlite_when_env_unset(monkeypatch):
    """The default (env var unset) must be SqliteBackend — pre-PR-Δ6
    behavior, preserves every existing test + every existing Fly deploy."""
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    backend = select_backend()
    assert isinstance(backend, SqliteBackend)


def test_select_backend_returns_sqlite_when_env_explicitly_sqlite(monkeypatch):
    """Explicit ``STORAGE_BACKEND=sqlite`` returns SqliteBackend. Operator
    intent: declarative documentation of the backend choice (rather than
    implicit unset)."""
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    assert isinstance(select_backend(), SqliteBackend)


def test_select_backend_case_insensitive(monkeypatch):
    """Operator-facing env-var values shouldn't be case-sensitive — a
    typo like ``STORAGE_BACKEND=Sqlite`` shouldn't silently fall through
    to the unknown-value warning path."""
    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    for value in ("SQLITE", "Sqlite", "sQlItE"):
        monkeypatch.setenv("STORAGE_BACKEND", value)
        assert isinstance(select_backend(), SqliteBackend), (
            f"case-insensitive match broke for value={value!r}"
        )


def test_select_backend_strips_surrounding_whitespace(monkeypatch):
    """Operators occasionally paste env-var values with stray
    whitespace; the selector strips so the matcher sees the clean
    value."""
    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    monkeypatch.setenv("STORAGE_BACKEND", "  sqlite  ")
    assert isinstance(select_backend(), SqliteBackend)


# ---------------------------------------------------------------------
# vercel_kv path — happy + fail-soft
# ---------------------------------------------------------------------


def test_select_backend_returns_vercel_kv_when_env_set_and_kv_present(
    monkeypatch,
):
    """``STORAGE_BACKEND=vercel_kv`` + KV env vars present → VercelKvBackend.
    The happy-path Vercel deploy case."""
    monkeypatch.setenv("STORAGE_BACKEND", "vercel_kv")
    monkeypatch.setenv("KV_REST_API_URL", "https://fake.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "fake-tok")

    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.storage.vercel_kv_backend import VercelKvBackend

    backend = select_backend()
    assert isinstance(backend, VercelKvBackend)


def test_select_backend_falls_back_to_sqlite_when_vercel_kv_env_missing(
    monkeypatch, caplog,
):
    """``STORAGE_BACKEND=vercel_kv`` + KV env vars MISSING → fail-soft.
    Returns SqliteBackend with a WARNING log explaining the fallback.
    The deploy still boots; the operator fixes the missing KV binding
    when they see the warning in the function logs."""
    monkeypatch.setenv("STORAGE_BACKEND", "vercel_kv")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)

    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    with caplog.at_level(logging.WARNING, logger="google_docs_mcp.storage.selector"):
        backend = select_backend()

    assert isinstance(backend, SqliteBackend), (
        "vercel_kv with missing KV env vars should fail-soft to "
        "SqliteBackend, not crash"
    )
    # WARNING line names the missing var so the operator can fix it.
    assert any(
        "VercelKvBackend construction failed" in r.message
        for r in caplog.records
    ), f"expected fallback warning; got: {[r.message for r in caplog.records]!r}"


def test_select_backend_falls_back_to_sqlite_for_unknown_value(
    monkeypatch, caplog,
):
    """Unknown ``STORAGE_BACKEND`` value (typo like ``vercelkv`` without
    the underscore) → fail-soft to SqliteBackend + WARNING. Bias is
    toward safety: a deploy with a typo'd backend choice shouldn't
    500 the entire surface."""
    monkeypatch.setenv("STORAGE_BACKEND", "vercelkv")  # missing underscore
    from google_docs_mcp.storage.backend_selector import select_backend
    from google_docs_mcp.user_store import SqliteBackend

    with caplog.at_level(logging.WARNING, logger="google_docs_mcp.storage.selector"):
        backend = select_backend()

    assert isinstance(backend, SqliteBackend)
    # Log explicitly mentions the typo'd value so the operator can grep for it.
    assert any(
        "unknown STORAGE_BACKEND value" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------
# init_default_backend_from_env — the operator-entrypoint helper
# ---------------------------------------------------------------------


def test_init_default_backend_from_env_swaps_module_default(monkeypatch):
    """``init_default_backend_from_env`` is what the Vercel ``api/index.py``
    calls at startup. It must actually swap the module-level
    ``_backend`` so subsequent ``get_state`` / ``save_state`` calls
    route to the resolved backend."""
    from google_docs_mcp import user_store
    from google_docs_mcp.user_store import (
        SqliteBackend,
        get_backend,
        init_default_backend_from_env,
        set_backend,
    )

    # Capture the current backend so we can restore at end-of-test.
    original = get_backend()
    try:
        # Force a known starting state.
        set_backend(SqliteBackend())
        assert isinstance(get_backend(), SqliteBackend)

        # Set env to vercel_kv with full KV env vars — should swap.
        monkeypatch.setenv("STORAGE_BACKEND", "vercel_kv")
        monkeypatch.setenv("KV_REST_API_URL", "https://fake.upstash.io")
        monkeypatch.setenv("KV_REST_API_TOKEN", "fake-tok")

        returned = init_default_backend_from_env()
        from google_docs_mcp.storage.vercel_kv_backend import VercelKvBackend
        assert isinstance(returned, VercelKvBackend)
        # And the module default actually swapped.
        assert isinstance(get_backend(), VercelKvBackend)
    finally:
        # Restore so cross-test state stays clean.
        set_backend(original)


def test_init_default_backend_from_env_is_noop_for_unset_env(monkeypatch):
    """When STORAGE_BACKEND is unset (the test + Fly default), calling
    ``init_default_backend_from_env`` returns a SqliteBackend without
    changing observable behavior."""
    from google_docs_mcp.user_store import (
        SqliteBackend,
        get_backend,
        init_default_backend_from_env,
        set_backend,
    )

    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    original = get_backend()
    try:
        set_backend(SqliteBackend())
        before = get_backend()
        init_default_backend_from_env()
        after = get_backend()
        # Both are SqliteBackend instances (different instance is fine —
        # the contract is the type, not object identity).
        assert isinstance(before, SqliteBackend)
        assert isinstance(after, SqliteBackend)
    finally:
        set_backend(original)
