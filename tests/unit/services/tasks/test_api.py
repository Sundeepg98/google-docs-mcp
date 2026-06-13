"""Co-located tests for services/tasks/api.py.

Mirrors ``tests/unit/services/sheets/test_api.py``: exercise the module
via ``with_google_api_client(InMemoryGoogleAPIClient)`` so the real
``get_service`` chokepoint runs but the Tasks HTTP boundary is stubbed.
No real OAuth, no real Tasks round-trip.

Tests cover four surfaces:

1. **Module-level constants** — pin ``DEFAULT_MAX_RESULTS``.
2. **Pre-API validation** — the ``ValueError`` branches (blank ids,
   out-of-range max_results, empty patch, bad status enum).
3. **Tasks call shape** — the right method chain
   (``service.tasklists().list/insert`` / ``service.tasks().list/
   insert/patch/delete``) gets the right kwargs (``tasklist`` / ``task``
   ids, ``parent`` as a query param on insert, the body shapes, the
   ``showCompleted`` / ``showHidden`` flags).
4. **Response envelope shape** — the flat envelopes the tool layer
   surfaces.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.tasks.api import (
    DEFAULT_MAX_RESULTS,
    complete_task,
    create_task,
    create_tasklist,
    delete_task,
    list_tasklists,
    list_tasks,
    update_task,
)


# ---------------------------------------------------------------------
# Module-level constants — public surface canary
# ---------------------------------------------------------------------


def test_default_max_results_is_the_api_cap():
    """100 = the Tasks API maxResults cap. Pinned so a stray edit that
    shrinks the default doesn't silently truncate callers' lists."""
    assert DEFAULT_MAX_RESULTS == 100


# ---------------------------------------------------------------------
# list_tasklists — tasklists.list
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_tasklists():
    tasks = MagicMock(name="tasks-v1-stub-tasklists")
    tasks.tasklists().list().execute.return_value = {
        "items": [
            {"id": "LIST1", "title": "My List", "updated": "2026-06-13T00:00:00.000Z"},
            {"id": "@default", "title": "My Tasks", "updated": "2026-06-12T00:00:00.000Z"},
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def _last_kwargs(mock_method) -> dict:
    """The kwargs of the most recent call that actually carried kwargs."""
    for call in reversed(mock_method.call_args_list):
        if call.kwargs:
            return call.kwargs
    raise AssertionError("no call captured kwargs")


def test_list_tasklists_passes_max_results(stub_tasks_for_tasklists):
    list_tasklists(MagicMock(), max_results=50)
    kw = _last_kwargs(stub_tasks_for_tasklists.tasklists().list)
    assert kw["maxResults"] == 50


def test_list_tasklists_defaults_to_cap(stub_tasks_for_tasklists):
    list_tasklists(MagicMock())
    kw = _last_kwargs(stub_tasks_for_tasklists.tasklists().list)
    assert kw["maxResults"] == DEFAULT_MAX_RESULTS


def test_list_tasklists_returns_flat_envelope(stub_tasks_for_tasklists):
    result = list_tasklists(MagicMock())
    assert result == {
        "tasklists": [
            {"id": "LIST1", "title": "My List", "updated": "2026-06-13T00:00:00.000Z"},
            {"id": "@default", "title": "My Tasks", "updated": "2026-06-12T00:00:00.000Z"},
        ],
    }


def test_list_tasklists_empty_when_no_items(stub_tasks_for_tasklists):
    """Tasks omits ``items`` for an empty result; envelope defaults to []."""
    stub_tasks_for_tasklists.tasklists().list().execute.return_value = {}
    result = list_tasklists(MagicMock())
    assert result == {"tasklists": []}


def test_list_tasklists_rejects_out_of_range_max_results():
    with pytest.raises(ValueError, match="max_results must be between 1 and 100"):
        list_tasklists(MagicMock(), max_results=0)
    with pytest.raises(ValueError, match="max_results must be between 1 and 100"):
        list_tasklists(MagicMock(), max_results=101)


# ---------------------------------------------------------------------
# create_tasklist — tasklists.insert
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_create_list():
    tasks = MagicMock(name="tasks-v1-stub-create-list")
    tasks.tasklists().insert().execute.return_value = {
        "id": "NEWLIST",
        "title": "Groceries",
        "updated": "2026-06-13T00:00:00.000Z",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def test_create_tasklist_rejects_blank_title():
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_tasklist(MagicMock(), "")
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_tasklist(MagicMock(), "   ")


def test_create_tasklist_builds_title_body(stub_tasks_for_create_list):
    create_tasklist(MagicMock(), "Groceries")
    kw = _last_kwargs(stub_tasks_for_create_list.tasklists().insert)
    assert kw["body"] == {"title": "Groceries"}


def test_create_tasklist_strips_whitespace(stub_tasks_for_create_list):
    create_tasklist(MagicMock(), "  Groceries  ")
    kw = _last_kwargs(stub_tasks_for_create_list.tasklists().insert)
    assert kw["body"]["title"] == "Groceries"


def test_create_tasklist_returns_envelope(stub_tasks_for_create_list):
    result = create_tasklist(MagicMock(), "Groceries")
    assert result == {
        "id": "NEWLIST",
        "title": "Groceries",
        "updated": "2026-06-13T00:00:00.000Z",
    }


# ---------------------------------------------------------------------
# list_tasks — tasks.list
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_list():
    tasks = MagicMock(name="tasks-v1-stub-list")
    tasks.tasks().list().execute.return_value = {
        "items": [
            {
                "id": "T1", "title": "Buy milk", "status": "needsAction",
                "due": "2026-06-20T00:00:00.000Z", "position": "00000000000000000000",
                "updated": "2026-06-13T00:00:00.000Z",
            },
            {
                "id": "T2", "title": "Done thing", "status": "completed",
                "completed": "2026-06-12T00:00:00.000Z", "parent": "T1",
            },
        ],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def test_list_tasks_passes_tasklist_and_flags(stub_tasks_for_list):
    list_tasks(
        MagicMock(), "LIST-ABC",
        show_completed=True, show_hidden=True, max_results=25,
    )
    kw = _last_kwargs(stub_tasks_for_list.tasks().list)
    assert kw["tasklist"] == "LIST-ABC"
    assert kw["showCompleted"] is True
    assert kw["showHidden"] is True
    assert kw["maxResults"] == 25


def test_list_tasks_defaults_flags_false_and_cap(stub_tasks_for_list):
    list_tasks(MagicMock(), "LIST1")
    kw = _last_kwargs(stub_tasks_for_list.tasks().list)
    assert kw["showCompleted"] is False
    assert kw["showHidden"] is False
    assert kw["maxResults"] == DEFAULT_MAX_RESULTS


def test_list_tasks_returns_flattened_envelope(stub_tasks_for_list):
    result = list_tasks(MagicMock(), "LIST1")
    assert result["tasklist"] == "LIST1"
    assert result["tasks"][0] == {
        "id": "T1", "title": "Buy milk", "status": "needsAction",
        "notes": None, "due": "2026-06-20T00:00:00.000Z", "completed": None,
        "parent": None, "position": "00000000000000000000",
        "updated": "2026-06-13T00:00:00.000Z",
    }
    # Sub-task surfaces parent + completed.
    assert result["tasks"][1]["parent"] == "T1"
    assert result["tasks"][1]["completed"] == "2026-06-12T00:00:00.000Z"


def test_list_tasks_empty_when_no_items(stub_tasks_for_list):
    stub_tasks_for_list.tasks().list().execute.return_value = {}
    result = list_tasks(MagicMock(), "LIST1")
    assert result == {"tasklist": "LIST1", "tasks": []}


def test_list_tasks_rejects_blank_tasklist():
    with pytest.raises(ValueError, match="tasklist cannot be empty"):
        list_tasks(MagicMock(), "   ")


def test_list_tasks_rejects_out_of_range_max_results():
    with pytest.raises(ValueError, match="max_results must be between 1 and 100"):
        list_tasks(MagicMock(), "LIST1", max_results=999)


# ---------------------------------------------------------------------
# create_task — tasks.insert
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_create():
    tasks = MagicMock(name="tasks-v1-stub-create")
    tasks.tasks().insert().execute.return_value = {
        "id": "NEWTASK", "title": "Buy milk", "status": "needsAction",
        "notes": "2%", "due": "2026-06-20T00:00:00.000Z",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def test_create_task_rejects_blank_title(stub_tasks_for_create):
    with pytest.raises(ValueError, match="title cannot be empty"):
        create_task(MagicMock(), "LIST1", "")


def test_create_task_rejects_blank_tasklist(stub_tasks_for_create):
    with pytest.raises(ValueError, match="tasklist cannot be empty"):
        create_task(MagicMock(), "  ", "Buy milk")


def test_create_task_builds_body_with_notes_and_due(stub_tasks_for_create):
    create_task(
        MagicMock(), "LIST-ABC", "Buy milk",
        notes="2%", due="2026-06-20T00:00:00.000Z",
    )
    kw = _last_kwargs(stub_tasks_for_create.tasks().insert)
    assert kw["tasklist"] == "LIST-ABC"
    assert kw["body"] == {
        "title": "Buy milk",
        "notes": "2%",
        "due": "2026-06-20T00:00:00.000Z",
    }
    # No parent supplied → not passed as a query param.
    assert "parent" not in kw


def test_create_task_minimal_body_when_only_title(stub_tasks_for_create):
    """notes / due omitted → not present in the body (don't send nulls)."""
    create_task(MagicMock(), "LIST1", "Buy milk")
    kw = _last_kwargs(stub_tasks_for_create.tasks().insert)
    assert kw["body"] == {"title": "Buy milk"}


def test_create_task_passes_parent_as_query_param(stub_tasks_for_create):
    """A sub-task: ``parent`` is a QUERY param on tasks.insert, NOT a body
    field — that's the Tasks API contract for creating a sub-task."""
    create_task(MagicMock(), "LIST1", "Sub item", parent="PARENT-TASK")
    kw = _last_kwargs(stub_tasks_for_create.tasks().insert)
    assert kw["parent"] == "PARENT-TASK"
    assert "parent" not in kw["body"]


def test_create_task_rejects_empty_string_parent(stub_tasks_for_create):
    with pytest.raises(ValueError, match="parent cannot be the empty string"):
        create_task(MagicMock(), "LIST1", "x", parent="   ")


def test_create_task_strips_title(stub_tasks_for_create):
    create_task(MagicMock(), "LIST1", "  Buy milk  ")
    kw = _last_kwargs(stub_tasks_for_create.tasks().insert)
    assert kw["body"]["title"] == "Buy milk"


def test_create_task_returns_envelope(stub_tasks_for_create):
    result = create_task(MagicMock(), "LIST1", "Buy milk")
    assert result["id"] == "NEWTASK"
    assert result["title"] == "Buy milk"
    assert result["status"] == "needsAction"
    assert result["due"] == "2026-06-20T00:00:00.000Z"


# ---------------------------------------------------------------------
# update_task / complete_task — tasks.patch
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_patch():
    tasks = MagicMock(name="tasks-v1-stub-patch")
    tasks.tasks().patch().execute.return_value = {
        "id": "T1", "title": "Updated", "status": "completed",
        "completed": "2026-06-13T00:00:00.000Z",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def test_update_task_rejects_blank_ids(stub_tasks_for_patch):
    with pytest.raises(ValueError, match="tasklist cannot be empty"):
        update_task(MagicMock(), "  ", "T1", title="x")
    with pytest.raises(ValueError, match="task cannot be empty"):
        update_task(MagicMock(), "LIST1", "  ", title="x")


def test_update_task_rejects_no_fields(stub_tasks_for_patch):
    with pytest.raises(ValueError, match="no fields supplied"):
        update_task(MagicMock(), "LIST1", "T1")


def test_update_task_rejects_empty_title(stub_tasks_for_patch):
    with pytest.raises(ValueError, match="title cannot be the empty string"):
        update_task(MagicMock(), "LIST1", "T1", title="   ")


def test_update_task_rejects_bad_status(stub_tasks_for_patch):
    with pytest.raises(ValueError, match='status must be "needsAction" or "completed"'):
        update_task(MagicMock(), "LIST1", "T1", status="done")


def test_update_task_builds_patch_body(stub_tasks_for_patch):
    update_task(
        MagicMock(), "LIST-ABC", "T1",
        title="Renamed", notes="new", due="2026-07-01T00:00:00.000Z",
    )
    kw = _last_kwargs(stub_tasks_for_patch.tasks().patch)
    assert kw["tasklist"] == "LIST-ABC"
    assert kw["task"] == "T1"
    assert kw["body"] == {
        "title": "Renamed",
        "notes": "new",
        "due": "2026-07-01T00:00:00.000Z",
    }


def test_update_task_notes_can_be_cleared(stub_tasks_for_patch):
    """Passing notes='' clears notes — the empty string IS sent (unlike
    a None, which means 'leave unchanged')."""
    update_task(MagicMock(), "LIST1", "T1", notes="")
    kw = _last_kwargs(stub_tasks_for_patch.tasks().patch)
    assert kw["body"] == {"notes": ""}


def test_update_task_reopen_clears_completed_timestamp(stub_tasks_for_patch):
    """status=needsAction must ALSO clear ``completed`` (send None) so a
    re-opened task isn't left with a stale completion date."""
    update_task(MagicMock(), "LIST1", "T1", status="needsAction")
    kw = _last_kwargs(stub_tasks_for_patch.tasks().patch)
    assert kw["body"] == {"status": "needsAction", "completed": None}


def test_complete_task_sets_status_completed(stub_tasks_for_patch):
    complete_task(MagicMock(), "LIST1", "T1")
    kw = _last_kwargs(stub_tasks_for_patch.tasks().patch)
    assert kw["body"] == {"status": "completed"}


def test_complete_task_returns_envelope(stub_tasks_for_patch):
    result = complete_task(MagicMock(), "LIST1", "T1")
    assert result["id"] == "T1"
    assert result["status"] == "completed"
    assert result["completed"] == "2026-06-13T00:00:00.000Z"


# ---------------------------------------------------------------------
# delete_task — tasks.delete
# ---------------------------------------------------------------------


@pytest.fixture
def stub_tasks_for_delete():
    tasks = MagicMock(name="tasks-v1-stub-delete")
    # tasks.delete returns an empty 204 body.
    tasks.tasks().delete().execute.return_value = None
    with with_google_api_client(InMemoryGoogleAPIClient({("tasks", "v1"): tasks})):
        yield tasks


def test_delete_task_rejects_blank_ids(stub_tasks_for_delete):
    with pytest.raises(ValueError, match="tasklist cannot be empty"):
        delete_task(MagicMock(), "  ", "T1")
    with pytest.raises(ValueError, match="task cannot be empty"):
        delete_task(MagicMock(), "LIST1", "  ")


def test_delete_task_calls_delete_endpoint(stub_tasks_for_delete):
    delete_task(MagicMock(), "LIST-ABC", "T1")
    kw = _last_kwargs(stub_tasks_for_delete.tasks().delete)
    assert kw["tasklist"] == "LIST-ABC"
    assert kw["task"] == "T1"


def test_delete_task_returns_echo_envelope(stub_tasks_for_delete):
    result = delete_task(MagicMock(), "LIST1", "T1")
    assert result == {"tasklist": "LIST1", "deleted_task_id": "T1"}
