"""Google Tasks MCP tool registrations (4th new service).

Mirrors the layout established by ``services/sheets/tools.py`` and
``services/slides/tools.py``: ``@workspace_tool``-decorated functions
that register with the live ``mcp`` instance when this module is
imported. ``server.py``'s auto-discovery walk
(``pkgutil.walk_packages`` over ``services/``) imports this leaf module
as a side effect — no central import edit needed.

**Tools registered here** (7 tasks-service tools):

1. ``gtasks_list_tasklists``   — list the user's task lists
2. ``gtasks_create_tasklist``  — create a new task list
3. ``gtasks_list_tasks``       — list tasks in a list (completed/hidden)
4. ``gtasks_create_task``      — create a task (title/notes/due/parent)
5. ``gtasks_update_task``      — patch a task's fields
6. ``gtasks_complete_task``    — mark a task completed (status convenience)
7. ``gtasks_delete_task``      — delete a task from a list

(Authoritative declaration: ``services/tasks/_expected_tools.py``.)

The first trio enables a complete workflow:
``create_tasklist`` → ``create_task`` → ``list_tasks``; the default
``"@default"`` list also works as the ``tasklist`` for every task tool
without creating a list first.

**Scope.** Every tool declares ``scopes=[TASKS_SCOPE]`` — the SENSITIVE
(not restricted → no CASA) ``.../auth/tasks`` scope. It is part of the
baseline consent set (declared in the #187 single source
``auth.WORKSPACE_SCOPES``), so the per-tool declaration is redundant for
resolution (``_check_scopes_or_raise`` passes immediately) but kept for
explicit documentation + the machine-readable ``tool.annotations.scopes``
field — the same convention ``gas_deploy`` follows after its scopes were
promoted to baseline.

**Import discipline.** Same as ``services/sheets/tools.py``:
- the api module is imported via the standard ``from ... import`` aliases;
- ``@workspace_tool(service="tasks", ...)`` carries the service= literal
  that drives the partition test + telemetry.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError

from appscriptly.decorators import workspace_tool
from appscriptly.services.tasks.api import (
    complete_task as _complete_task,
    create_task as _create_task,
    create_tasklist as _create_tasklist,
    delete_task as _delete_task,
    list_tasklists as _list_tasklists,
    list_tasks as _list_tasks,
    update_task as _update_task,
)
from appscriptly.services.tasks.scopes import TASKS_SCOPE
from appscriptly.tool_schemas import (
    GTASKS_COMPLETE_TASK_OUTPUT_SCHEMA,
    GTASKS_CREATE_TASK_OUTPUT_SCHEMA,
    GTASKS_CREATE_TASKLIST_OUTPUT_SCHEMA,
    GTASKS_DELETE_TASK_OUTPUT_SCHEMA,
    GTASKS_LIST_TASKLISTS_OUTPUT_SCHEMA,
    GTASKS_LIST_TASKS_OUTPUT_SCHEMA,
    GTASKS_UPDATE_TASK_OUTPUT_SCHEMA,
)


# ---------------------------------------------------------------------
# 1. gtasks_list_tasklists — tasklists.list (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="List the user's Google Tasks lists",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_LIST_TASKLISTS_OUTPUT_SCHEMA,
)
def gtasks_list_tasklists(creds, max_results: int = 100) -> dict:
    """List the user's Google Tasks lists.

    USE WHEN: the agent needs to discover which task lists exist before
    reading or adding tasks — you need a ``tasklist`` id to call
    ``gtasks_list_tasks`` / ``gtasks_create_task`` (or use the literal
    ``"@default"`` for the user's primary list without listing first).

    Uses the Tasks API ``tasklists.list`` endpoint. The default
    ``"@default"`` list always exists and appears here.

    Args:
        max_results: Page size (1..100). Defaults to 100 (the API cap)
            so the common "show me my lists" case returns everything in
            one call.

    Returns:
        ``{tasklists: [{id, title, updated}, ...]}`` — ``id`` is the
        list id you pass to the task tools; ``updated`` is an RFC 3339
        timestamp.

    Choreography: the natural discovery step before any task operation.
    Pair with ``gtasks_list_tasks`` (read a list's tasks) or
    ``gtasks_create_tasklist`` (make a new list).
    """
    try:
        return _list_tasklists(creds, max_results=max_results)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 2. gtasks_create_tasklist — tasklists.insert
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="Create a new Google Tasks list",
    # Creating a fresh list isn't a mutation of existing state.
    readonly=False,
    destructive=False,
    # Re-running creates ANOTHER list (Tasks doesn't de-dupe by title).
    idempotent=False,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_CREATE_TASKLIST_OUTPUT_SCHEMA,
)
def gtasks_create_tasklist(creds, title: str) -> dict:
    """Create a new Google Tasks list.

    USE WHEN: the user wants a SEPARATE list for a project / context
    (e.g. "Groceries", "Sprint 14") rather than dumping everything into
    the default list.

    Uses the Tasks API ``tasklists.insert`` endpoint.

    Args:
        title: Title for the new list. Must be non-empty.

    Returns:
        ``{id, title, updated}`` — ``id`` is the server-assigned list id;
        pipe it straight into ``gtasks_create_task`` /
        ``gtasks_list_tasks``.

    Choreography: typically the FIRST call when starting a new
    task-tracked workflow; follow with ``gtasks_create_task`` to add
    tasks. To add to an EXISTING list, skip this and use that list's id
    (or ``"@default"``).
    """
    try:
        return _create_tasklist(creds, title)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 3. gtasks_list_tasks — tasks.list (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="List tasks in a Google Tasks list",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_LIST_TASKS_OUTPUT_SCHEMA,
)
def gtasks_list_tasks(
    creds,
    tasklist: str = "@default",
    show_completed: bool = False,
    show_hidden: bool = False,
    max_results: int = 100,
) -> dict:
    """List tasks in a Google Tasks list.

    USE WHEN: the agent needs to see the tasks in a list — to summarize,
    check what's outstanding, or find a task id for a follow-up update /
    complete / delete.

    Uses the Tasks API ``tasks.list`` endpoint.

    Args:
        tasklist: The task list id (from ``gtasks_list_tasklists``).
            Defaults to ``"@default"`` — the user's primary list — so
            the common "what's on my list" case needs no prior lookup.
        show_completed: Include completed tasks. Default False (only
            needs-action tasks come back).
        show_hidden: Include hidden tasks. When a user "clears" completed
            tasks they become HIDDEN rather than deleted; to see those
            you need BOTH ``show_completed=True`` AND ``show_hidden=True``.
            Default False.
        max_results: Page size (1..100). Default 100 (the API cap).

    Returns:
        ``{tasklist, tasks: [{id, title, status, notes, due, completed,
        parent, position, updated}, ...]}``. ``status`` is
        ``"needsAction"`` or ``"completed"``; ``parent`` is set on
        sub-tasks; ``due`` / ``completed`` are RFC 3339 timestamps when
        present.

    Choreography: get the ``tasklist`` from ``gtasks_list_tasklists``
    (or use ``"@default"``). The returned task ``id``s feed
    ``gtasks_update_task`` / ``gtasks_complete_task`` /
    ``gtasks_delete_task``.
    """
    try:
        return _list_tasks(
            creds,
            tasklist,
            show_completed=show_completed,
            show_hidden=show_hidden,
            max_results=max_results,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 4. gtasks_create_task — tasks.insert
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="Create a task in a Google Tasks list",
    readonly=False,
    destructive=False,
    # Re-running creates ANOTHER task. Same convention as create_tasklist.
    idempotent=False,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_CREATE_TASK_OUTPUT_SCHEMA,
)
def gtasks_create_task(
    creds,
    tasklist: str = "@default",
    title: str = "",
    notes: str | None = None,
    due: str | None = None,
    parent: str | None = None,
) -> dict:
    """Create a task in a Google Tasks list.

    USE WHEN: the agent should add a to-do — a reminder, an action item,
    a follow-up. The single most common Tasks write.

    Uses the Tasks API ``tasks.insert`` endpoint.

    Args:
        tasklist: The task list id (from ``gtasks_list_tasklists``).
            Defaults to ``"@default"`` (the user's primary list).
        title: The task title. Must be non-empty.
        notes: Optional free-text notes / description.
        due: Optional due date as an RFC 3339 timestamp, e.g.
            ``"2026-06-20T00:00:00.000Z"``. Google Tasks records only the
            DATE part (no time-of-day is shown in the UI). Pass a full
            RFC 3339 string — a bare ``"2026-06-20"`` is rejected by
            Google.
        parent: Optional parent task id — makes this a SUB-task of that
            parent (which must already exist in the same list). Omit for
            a top-level task.

    Returns:
        The created task: ``{id, title, status, notes, due, completed,
        parent, position, updated}``. ``id`` is the server-assigned task
        id (feed it to update / complete / delete).

    Choreography: get the ``tasklist`` from ``gtasks_list_tasklists`` (or
    use ``"@default"``); for a sub-task, get the ``parent`` task id from
    ``gtasks_list_tasks`` first. Verify with ``gtasks_list_tasks``.
    """
    try:
        return _create_task(
            creds, tasklist, title, notes=notes, due=due, parent=parent,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 5. gtasks_update_task — tasks.patch (partial update)
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="Update fields of a Google Tasks task",
    # Patching fields in place is not destructive (the task persists; a
    # field can be re-patched). Matches gdocs_rename_tab.
    readonly=False,
    destructive=False,
    # Patching the same fields to the same values twice = same state.
    idempotent=True,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_UPDATE_TASK_OUTPUT_SCHEMA,
)
def gtasks_update_task(
    creds,
    tasklist: str,
    task: str,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    status: str | None = None,
) -> dict:
    """Update fields of a task (partial update — only what you pass).

    USE WHEN: editing an existing task — fix a typo'd title, add/clear
    notes, change a due date, or re-open a completed task
    (``status="needsAction"``). To mark a task DONE, prefer the
    dedicated ``gtasks_complete_task``.

    Uses the Tasks API ``tasks.patch`` endpoint (a partial update — unset
    fields are left untouched). Pass at least one field.

    Args:
        tasklist: The task list id (from ``gtasks_list_tasklists`` /
            ``gtasks_list_tasks``, or ``"@default"``).
        task: The task id (from ``gtasks_list_tasks`` /
            ``gtasks_create_task``).
        title: New title (cannot be set to empty).
        notes: New notes. Pass ``""`` to clear existing notes.
        due: New due date (RFC 3339 timestamp; see
            ``gtasks_create_task`` for the format note).
        status: ``"needsAction"`` or ``"completed"``. Setting
            ``"needsAction"`` re-opens a completed task (and clears its
            completion timestamp); ``"completed"`` marks it done
            (``gtasks_complete_task`` is the convenience for that).

    Returns:
        The updated task as the flat envelope (same shape as a
        ``gtasks_list_tasks`` entry).

    Choreography: get the ``task`` id (and ``tasklist``) from
    ``gtasks_list_tasks`` first. For the common "mark done" case use
    ``gtasks_complete_task`` instead of ``status="completed"`` here.
    """
    try:
        return _update_task(
            creds, tasklist, task,
            title=title, notes=notes, due=due, status=status,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 6. gtasks_complete_task — tasks.patch (status convenience)
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="Mark a Google Tasks task completed",
    readonly=False,
    destructive=False,
    # Completing an already-completed task yields the same state.
    idempotent=True,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_COMPLETE_TASK_OUTPUT_SCHEMA,
)
def gtasks_complete_task(creds, tasklist: str, task: str) -> dict:
    """Mark a task as completed (done).

    USE WHEN: a task is finished. This is the convenience verb for the
    single most common task mutation — equivalent to
    ``gtasks_update_task(..., status="completed")`` but you don't have to
    know the status enum.

    Uses the Tasks API ``tasks.patch`` endpoint with
    ``status="completed"``.

    Args:
        tasklist: The task list id (from ``gtasks_list_tasklists`` /
            ``gtasks_list_tasks``, or ``"@default"``).
        task: The task id (from ``gtasks_list_tasks``).

    Returns:
        The updated task as the flat envelope; ``status`` is
        ``"completed"`` and ``completed`` carries the server timestamp.

    Choreography: get the ``task`` id from ``gtasks_list_tasks`` first.
    To RE-OPEN a completed task, use ``gtasks_update_task`` with
    ``status="needsAction"``.
    """
    try:
        return _complete_task(creds, tasklist, task)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 7. gtasks_delete_task — tasks.delete
# ---------------------------------------------------------------------


@workspace_tool(
    service="tasks",
    title="Delete a task from a Google Tasks list",
    readonly=False,
    # Removing a task deletes it (and its sub-tasks) — genuinely
    # destructive. Matches gsheets_delete_sheet / gdocs_delete_tab.
    destructive=True,
    # Deleting an already-deleted task id 4xxs rather than double-deleting,
    # so the OUTCOME is idempotent in intent; annotated True to match
    # gdocs_delete_tab. (The api layer dispatches non-retried to honor the
    # destructive-op safety floor.)
    idempotent=True,
    external=True,
    creds=True,
    scopes=[TASKS_SCOPE],
    output_schema=GTASKS_DELETE_TASK_OUTPUT_SCHEMA,
)
def gtasks_delete_task(creds, tasklist: str, task: str) -> dict:
    """Delete a task from a list — removes it (and any sub-tasks).

    USE WHEN: a task should be removed entirely (not just completed). To
    mark a task DONE while keeping it, use ``gtasks_complete_task``
    instead. DESTRUCTIVE: deleting a parent task also removes its
    sub-tasks (per the Tasks API contract).

    Uses the Tasks API ``tasks.delete`` endpoint.

    Args:
        tasklist: The task list id (from ``gtasks_list_tasklists`` /
            ``gtasks_list_tasks``, or ``"@default"``).
        task: The task id to delete (from ``gtasks_list_tasks``).

    Returns:
        ``{tasklist, deleted_task_id}`` — echoes what was removed
        (``tasks.delete`` returns an empty body).

    Choreography: get the ``task`` id from ``gtasks_list_tasks`` first.
    To merely COMPLETE a task instead of deleting it, use
    ``gtasks_complete_task``.
    """
    try:
        return _delete_task(creds, tasklist, task)
    except ValueError as e:
        raise ToolError(str(e)) from e
