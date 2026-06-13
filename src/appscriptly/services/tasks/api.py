"""Google Tasks REST wrapper (Tasks API v1).

The Tasks API is the simple ``list / insert / patch / delete`` resource
shape — no ``batchUpdate`` tagged-union (unlike Docs / Slides / Sheets).
So this module is a thin ergonomic wrapper around two resources:

  * ``service.tasklists()`` — the user's task LISTS
      - ``list``   → ``list_tasklists``
      - ``insert`` → ``create_tasklist``
  * ``service.tasks()``     — the TASKS within a list
      - ``list``   → ``list_tasks``      (show completed / hidden opts)
      - ``insert`` → ``create_task``     (title / notes / due / parent)
      - ``patch``  → ``update_task`` / ``complete_task``
      - ``delete`` → ``delete_task``

All calls go through the shared ``get_service("tasks", "v1", ...)``
chokepoint (so the M2 Port + Adapters seam and the test stub apply
uniformly) and ``execute_with_retry`` for the idempotent reads / patches
(matching the convention proven by the docs / sheets / drive wrappers).

**Scope note.** Calls require the SENSITIVE scope
``https://www.googleapis.com/auth/tasks`` in the OAuth consent. It is
NOT one of Google's RESTRICTED scopes (it's absent from the closed
restricted list — Gmail / Drive / Fit / Chat / Data Portability /
Photos / Health), so it adds no CASA requirement. The scope is declared
ONCE in ``auth.WORKSPACE_SCOPES`` (the #187 single source); existing
user grants pick it up on next token refresh via the
``include_granted_scopes=true`` incremental-consent flow — same
non-breaking pattern that handled the Sheets / Slides / Apps Script
scope additions. No forced re-consent.

**Due dates.** The Tasks API stores ``due`` as an RFC 3339 timestamp
(e.g. ``"2026-06-20T00:00:00.000Z"``) and — importantly — records only
the DATE portion; a time-of-day is accepted but not surfaced in the UI.
The wrapper forwards whatever RFC 3339 string the caller passes
verbatim (the tool layer documents the format), so a caller that passes
a plain ``"2026-06-20"`` will get Google's own 400 rather than a guessy
client-side reshape.

**DEPLOY-TIME PREREQUISITE.** The Tasks API must be enabled in the GCP
project before these calls succeed against the live app. Merging is
un-deployed (``DEPLOY_ENABLED`` off); the enable step rides the eventual
deliberate deploy.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# The Tasks ``tasks.list`` endpoint caps ``maxResults`` at 100 and
# defaults to 20. We default to the cap so a single call returns a
# useful working set without the caller having to know the knob; paging
# beyond 100 is left to a future ``page_token`` enhancement if a real
# need emerges (mirrors the "minimal first" framing of the sheets
# service).
DEFAULT_MAX_RESULTS = 100


def _tasks_service(creds: Credentials):
    """Build a Tasks v1 ``Resource`` through the shared chokepoint."""
    return get_service("tasks", "v1", credentials=creds)


# ---------------------------------------------------------------------
# Task LISTS — tasklists.list / tasklists.insert
# ---------------------------------------------------------------------


def list_tasklists(
    creds: Credentials,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """List the user's task lists via ``tasklists.list``.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        max_results: Page size (1..100). Defaults to 100 (the API cap)
            so the common "show me my lists" case returns everything in
            one call.

    Returns:
        ``{tasklists: [{id, title, updated}, ...]}`` — a flat list of
        the load-bearing fields. The default "@default" list always
        exists and appears here.

    Raises:
        ValueError: ``max_results`` out of the 1..100 range — rejected
            client-side rather than bouncing off Google's 400.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be between 1 and 100.")

    service = _tasks_service(creds)
    # readonly + idempotent — safe to retry on 429/5xx.
    resp = execute_with_retry(
        lambda: service.tasklists().list(maxResults=max_results).execute(),
        idempotent=True,
        op_name="tasks.tasklists.list",
    )
    return {
        "tasklists": [
            {
                "id": item.get("id"),
                "title": item.get("title", ""),
                "updated": item.get("updated"),
            }
            for item in resp.get("items", [])
        ],
    }


def create_tasklist(creds: Credentials, title: str) -> dict:
    """Create a new task list via ``tasklists.insert``.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        title: The new list's title. Blank rejected client-side.

    Returns:
        ``{id, title, updated}`` — the created list (``id`` is the
        server-assigned tasklist id used to target ``list_tasks`` /
        ``create_task``).

    Raises:
        ValueError: empty / whitespace ``title``.
        HttpError: from the underlying SDK — propagated.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")

    service = _tasks_service(creds)
    # NOT idempotent: re-running inserts ANOTHER list (Tasks does not
    # de-dupe by title). Same convention as gsheets_create_spreadsheet.
    resp = service.tasklists().insert(body={"title": title.strip()}).execute()
    return {
        "id": resp.get("id"),
        "title": resp.get("title", title.strip()),
        "updated": resp.get("updated"),
    }


# ---------------------------------------------------------------------
# TASKS — tasks.list / tasks.insert / tasks.patch / tasks.delete
# ---------------------------------------------------------------------


def _task_envelope(item: dict) -> dict:
    """Flatten a Tasks ``Task`` resource to the load-bearing fields.

    ``parent`` / ``due`` / ``notes`` / ``completed`` are present only on
    some tasks (sub-tasks, dated tasks, etc.), so they're surfaced when
    Google includes them and omitted otherwise — callers read with
    ``.get``.
    """
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "status": item.get("status"),
        "notes": item.get("notes"),
        "due": item.get("due"),
        "completed": item.get("completed"),
        "parent": item.get("parent"),
        "position": item.get("position"),
        "updated": item.get("updated"),
    }


def list_tasks(
    creds: Credentials,
    tasklist: str,
    *,
    show_completed: bool = False,
    show_hidden: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """List tasks in a task list via ``tasks.list``.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        tasklist: The task list id (from ``list_tasklists``, or the
            literal ``"@default"`` for the user's default list).
        show_completed: Include completed tasks. Defaults to False
            (the API default — only needs-action tasks come back).
        show_hidden: Include hidden tasks (completed tasks that the
            user has "cleared" from the list become hidden rather than
            deleted). Defaults to False.
        max_results: Page size (1..100). Defaults to 100 (the API cap).

    Returns:
        ``{tasklist, tasks: [{id, title, status, notes, due, completed,
        parent, position, updated}, ...]}`` — ``tasklist`` echoes the
        id queried so the caller can correlate.

    Raises:
        ValueError: blank ``tasklist`` or ``max_results`` out of range.
        HttpError: from the underlying SDK — propagated.

    Note:
        The Tasks API quirk: ``showCompleted`` only takes effect when it
        is True; ``showHidden`` is independent. Asking for completed
        tasks that have been cleared also needs ``show_hidden=True`` —
        the docstring on ``gtasks_list_tasks`` spells this out for
        callers.
    """
    if not tasklist or not tasklist.strip():
        raise ValueError("tasklist cannot be empty.")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be between 1 and 100.")

    service = _tasks_service(creds)
    # readonly + idempotent — safe to retry.
    resp = execute_with_retry(
        lambda: service.tasks().list(
            tasklist=tasklist,
            showCompleted=show_completed,
            showHidden=show_hidden,
            maxResults=max_results,
        ).execute(),
        idempotent=True,
        op_name="tasks.tasks.list",
    )
    return {
        "tasklist": tasklist,
        "tasks": [_task_envelope(item) for item in resp.get("items", [])],
    }


def create_task(
    creds: Credentials,
    tasklist: str,
    title: str,
    *,
    notes: str | None = None,
    due: str | None = None,
    parent: str | None = None,
) -> dict:
    """Create a task in a list via ``tasks.insert``.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        tasklist: The task list id (or ``"@default"``).
        title: The task title. Blank rejected client-side.
        notes: Optional free-text notes / description.
        due: Optional due date as an RFC 3339 timestamp, e.g.
            ``"2026-06-20T00:00:00.000Z"``. Tasks records only the DATE
            portion (no time-of-day shown in the UI). Forwarded verbatim
            — an invalid string surfaces Google's own 400.
        parent: Optional parent task id — makes this a SUB-task of that
            parent (the parent must already exist in the same list). Omit
            for a top-level task.

    Returns:
        The created task as the flat envelope (see ``list_tasks``);
        ``id`` is the server-assigned task id.

    Raises:
        ValueError: blank ``title`` / blank ``tasklist``.
        HttpError: from the underlying SDK — propagated (e.g. an unknown
            ``parent`` id, or a malformed ``due`` timestamp).

    Note:
        ``parent`` is passed as a QUERY parameter on ``tasks.insert``
        (not a body field) — that's the Tasks API contract for creating
        a sub-task. The body carries title / notes / due.
    """
    if not tasklist or not tasklist.strip():
        raise ValueError("tasklist cannot be empty.")
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")

    body: dict[str, Any] = {"title": title.strip()}
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due

    insert_kwargs: dict[str, Any] = {"tasklist": tasklist, "body": body}
    if parent is not None:
        if not parent.strip():
            raise ValueError("parent cannot be the empty string; omit it instead.")
        insert_kwargs["parent"] = parent

    service = _tasks_service(creds)
    # NOT idempotent: re-running inserts ANOTHER task. Same convention as
    # gsheets_append_rows / create_tasklist.
    resp = service.tasks().insert(**insert_kwargs).execute()
    return _task_envelope(resp)


def update_task(
    creds: Credentials,
    tasklist: str,
    task: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    status: str | None = None,
) -> dict:
    """Patch a task's fields via ``tasks.patch`` (partial update).

    Only the fields you pass are changed (``tasks.patch`` is a partial
    update — unset fields are left untouched). Pass at least one field;
    an all-``None`` call is rejected client-side as a no-op.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        tasklist: The task list id (or ``"@default"``).
        task: The task id (from ``list_tasks`` / ``create_task``).
        title: New title.
        notes: New notes (pass ``""`` to clear).
        due: New due date (RFC 3339 timestamp; see ``create_task``).
        status: ``"needsAction"`` or ``"completed"``. Setting
            ``"completed"`` is what ``complete_task`` does; setting
            ``"needsAction"`` re-opens a completed task. Other values
            are rejected client-side (the Tasks status enum has exactly
            these two).

    Returns:
        The updated task as the flat envelope (see ``list_tasks``).

    Raises:
        ValueError: blank ids, no fields supplied, or an invalid
            ``status`` value.
        HttpError: from the underlying SDK — propagated.

    Note:
        Dispatched ``idempotent=True`` — patching the same fields to the
        same values twice yields the same task state, so it's safe to
        retry on a transient 429/5xx. Mirrors ``gdocs_rename_tab`` /
        ``gsheets_rename_sheet``.

        When ``status`` is set to ``"needsAction"`` we ALSO clear
        ``completed`` (send it as ``None``): the Tasks API keeps the old
        ``completed`` timestamp otherwise, leaving a re-opened task in an
        inconsistent state (status=needsAction but a completed date set).
    """
    if not tasklist or not tasklist.strip():
        raise ValueError("tasklist cannot be empty.")
    if not task or not task.strip():
        raise ValueError("task cannot be empty.")
    if status is not None and status not in ("needsAction", "completed"):
        raise ValueError(
            'status must be "needsAction" or "completed" — got '
            f"{status!r}."
        )

    body: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            raise ValueError("title cannot be the empty string.")
        body["title"] = title.strip()
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    if status is not None:
        body["status"] = status
        if status == "needsAction":
            # Re-opening: clear the stale completion timestamp so the
            # task isn't left status=needsAction with a completed date.
            body["completed"] = None

    if not body:
        raise ValueError(
            "no fields supplied — pass at least one of title / notes / "
            "due / status."
        )

    service = _tasks_service(creds)
    resp = execute_with_retry(
        lambda: service.tasks().patch(
            tasklist=tasklist, task=task, body=body,
        ).execute(),
        idempotent=True,
        op_name="tasks.tasks.patch",
    )
    return _task_envelope(resp)


def complete_task(creds: Credentials, tasklist: str, task: str) -> dict:
    """Mark a task completed via ``tasks.patch`` (status convenience).

    Thin convenience over ``update_task`` with ``status="completed"`` —
    the single most common task mutation, surfaced as its own verb so a
    caller doesn't have to know the status enum.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        tasklist: The task list id (or ``"@default"``).
        task: The task id.

    Returns:
        The updated task as the flat envelope; ``status`` is
        ``"completed"`` and ``completed`` carries the server timestamp.

    Raises:
        ValueError: blank ids.
        HttpError: from the underlying SDK — propagated.

    Note:
        Idempotent: completing an already-completed task yields the same
        state (Tasks just re-stamps ``completed``). Delegates to
        ``update_task``, which dispatches ``idempotent=True``.
    """
    return update_task(creds, tasklist, task, status="completed")


def delete_task(creds: Credentials, tasklist: str, task: str) -> dict:
    """Delete a task from a list via ``tasks.delete``.

    Args:
        creds: OAuth credentials carrying the ``tasks`` scope.
        tasklist: The task list id (or ``"@default"``).
        task: The task id to delete.

    Returns:
        ``{tasklist, deleted_task_id}`` — echoes what was removed
        (``tasks.delete`` returns an empty 204 body, so there's nothing
        else to surface).

    Raises:
        ValueError: blank ids.
        HttpError: from the underlying SDK — propagated. Deleting an
            already-deleted task id returns a 4xx (not retried — see
            below).

    Note:
        DESTRUCTIVE (the task is gone — deleting a parent also removes
        its sub-tasks per the Tasks API contract). Dispatched WITHOUT
        retry: ``tasks.delete`` returns no body to confirm idempotence
        and a transient-error replay on a successful delete would hit a
        404, so we honor the destructive-op safety floor with a plain
        ``.execute()`` (matching ``gsheets_delete_sheet`` /
        ``gdocs_delete_tab`` at the api layer).
    """
    if not tasklist or not tasklist.strip():
        raise ValueError("tasklist cannot be empty.")
    if not task or not task.strip():
        raise ValueError("task cannot be empty.")

    service = _tasks_service(creds)
    # No execute_with_retry: delete is destructive + returns no body to
    # confirm idempotence. Let HttpError propagate to the tool envelope.
    service.tasks().delete(tasklist=tasklist, task=task).execute()
    return {"tasklist": tasklist, "deleted_task_id": task}
