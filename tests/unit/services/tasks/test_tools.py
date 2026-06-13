"""Per-tool behavior tests for services/tasks/tools.py.

Covers the tool-layer envelope: the ``@workspace_tool(creds=True,
scopes=[TASKS_SCOPE])`` wrapper's credential injection, parameter
forwarding into the api module, and the ``ValueError -> ToolError``
translation each tool body performs.

**Creds-injection note.** The tasks tools declare
``scopes=[TASKS_SCOPE]``, so the decorator resolves credentials via
``_resolve_credentials_for_scopes`` (NOT the bound ``_get_credentials_fn``
the no-scope tools use). In stdio mode that branch calls
``auth.load_credentials(default_data_dir(), extra_scopes=...)``. We
intercept it by stubbing ``current_user_id_or_none`` → None (force the
stdio branch) and ``load_credentials`` → a stub, so no real OAuth runs.

Per-tool API-shape coverage (method chains, body shapes, query params,
envelopes) lives in ``test_api.py``; this file is the decorator-boundary
+ error-mapping witness.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.tasks import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Force the decorator's scoped-creds resolution down the stdio branch
    and hand it a stub, so @workspace_tool(creds=True, scopes=[...]) never
    attempts real OAuth.

    The decorator's ``_resolve_credentials_for_scopes`` imports
    ``current_user_id_or_none`` + ``load_credentials`` from their home
    modules at CALL time, so we patch them at the source modules.
    """
    import appscriptly.auth as auth_mod
    import appscriptly.credentials as creds_mod

    monkeypatch.setattr(creds_mod, "current_user_id_or_none", lambda: None)
    monkeypatch.setattr(
        auth_mod, "load_credentials",
        lambda *a, **k: stub_creds,
    )


@pytest.fixture
def tasks_stub():
    """A Tasks v1 Resource stub with all method chains pre-wired to
    plausible default responses. Individual tests override per-call."""
    tasks = MagicMock(name="tasks-v1-stub")
    tasks.tasklists().list().execute.return_value = {"items": []}
    tasks.tasklists().insert().execute.return_value = {
        "id": "L1", "title": "L", "updated": "2026-06-13T00:00:00.000Z",
    }
    tasks.tasks().list().execute.return_value = {"items": []}
    tasks.tasks().insert().execute.return_value = {
        "id": "T1", "title": "t", "status": "needsAction",
    }
    tasks.tasks().patch().execute.return_value = {
        "id": "T1", "title": "t", "status": "completed",
        "completed": "2026-06-13T00:00:00.000Z",
    }
    tasks.tasks().delete().execute.return_value = None
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


# ---------------------------------------------------------------------
# Happy path — creds injected, params forwarded, envelope returned
# ---------------------------------------------------------------------


def test_list_tasklists_happy_path(tasks_stub):
    tasks_stub.tasklists().list().execute.return_value = {
        "items": [{"id": "L1", "title": "Mine", "updated": "u"}],
    }
    result = tools.gtasks_list_tasklists()
    assert result == {"tasklists": [{"id": "L1", "title": "Mine", "updated": "u"}]}


def test_create_tasklist_happy_path(tasks_stub):
    result = tools.gtasks_create_tasklist("My List")
    assert result["id"] == "L1"
    kw = next(
        c.kwargs for c in reversed(tasks_stub.tasklists().insert.call_args_list)
        if c.kwargs
    )
    assert kw["body"] == {"title": "My List"}


def test_list_tasks_defaults_to_at_default_list(tasks_stub):
    """The tool defaults ``tasklist`` to "@default" so the common case
    needs no prior tasklist lookup."""
    tools.gtasks_list_tasks()
    kw = next(
        c.kwargs for c in reversed(tasks_stub.tasks().list.call_args_list)
        if c.kwargs
    )
    assert kw["tasklist"] == "@default"


def test_create_task_forwards_all_params(tasks_stub):
    tools.gtasks_create_task(
        "LIST1", "Buy milk",
        notes="2%", due="2026-06-20T00:00:00.000Z", parent="P1",
    )
    kw = next(
        c.kwargs for c in reversed(tasks_stub.tasks().insert.call_args_list)
        if c.kwargs
    )
    assert kw["tasklist"] == "LIST1"
    assert kw["parent"] == "P1"
    assert kw["body"] == {
        "title": "Buy milk", "notes": "2%", "due": "2026-06-20T00:00:00.000Z",
    }


def test_update_task_happy_path(tasks_stub):
    result = tools.gtasks_update_task("LIST1", "T1", title="Renamed")
    assert result["id"] == "T1"


def test_complete_task_happy_path(tasks_stub):
    result = tools.gtasks_complete_task("LIST1", "T1")
    assert result["status"] == "completed"
    kw = next(
        c.kwargs for c in reversed(tasks_stub.tasks().patch.call_args_list)
        if c.kwargs
    )
    assert kw["body"] == {"status": "completed"}


def test_delete_task_happy_path(tasks_stub):
    result = tools.gtasks_delete_task("LIST1", "T1")
    assert result == {"tasklist": "LIST1", "deleted_task_id": "T1"}


# ---------------------------------------------------------------------
# Error mapping — ValueError (pre-API validation) -> ToolError
# ---------------------------------------------------------------------


def test_create_tasklist_blank_title_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="title cannot be empty"):
        tools.gtasks_create_tasklist("   ")


def test_create_task_blank_title_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="title cannot be empty"):
        tools.gtasks_create_task("LIST1", "")


def test_update_task_no_fields_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="no fields supplied"):
        tools.gtasks_update_task("LIST1", "T1")


def test_update_task_bad_status_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="status must be"):
        tools.gtasks_update_task("LIST1", "T1", status="done")


def test_delete_task_blank_id_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="task cannot be empty"):
        tools.gtasks_delete_task("LIST1", "   ")


def test_list_tasks_out_of_range_raises_toolerror(tasks_stub):
    with pytest.raises(ToolError, match="max_results must be between 1 and 100"):
        tools.gtasks_list_tasks("LIST1", max_results=500)
