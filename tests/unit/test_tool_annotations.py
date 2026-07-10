"""ToolAnnotations coverage tests (v2.0.5 / F1).

Regression-prevent the F1 finding (R15 design-external + R16 design-internal
+ R16 design-external + R23 audit): every @mcp.tool decorator must carry a
``ToolAnnotations(...)`` payload so MCP clients (Claude Desktop, claude.ai)
can render correct UX (read-only badge, destructive-action confirmation
prompt, idempotent retry-safe indicator).

If you add a new tool and CI fails here, that's working as intended —
add the annotation per the table in PR #51.
"""
from __future__ import annotations

import asyncio


def _list_tools():
    """Run mcp.list_tools() in a fresh event loop (no pytest-asyncio dep)."""
    from appscriptly.server import mcp
    return asyncio.run(mcp.list_tools())


# ---------------------------------------------------------------------
# F1 — every tool has annotations
# ---------------------------------------------------------------------


def test_all_tools_have_annotations():
    """Every registered @mcp.tool MUST carry annotations (regression-prevent
    F1 reintroduction)."""
    tools = _list_tools()
    missing = [t.name for t in tools if getattr(t, "annotations", None) is None]
    assert not missing, f"Tools without annotations: {missing}"


# ---------------------------------------------------------------------
# Explicit allow-list assertions — prevents accidental hint regressions
# ---------------------------------------------------------------------


READONLY_TOOLS = {
    "gdocs_get_doc_outline",
    "gdocs_read_doc",
    "gdocs_get_tab_url",
    "gdocs_find_doc_by_title",
    # Generalized find — files.list over app-accessible files of any
    # type. Pure read by default (the verify_writable probe is opt-in,
    # default False), same CQRS posture as gdocs_find_doc_by_title.
    "gdocs_find_file",
    "gdocs_server_info",
    "gdocs_test_manifest",
    "gdocs_guide",
    "gdocs_help",
    "gdocs_preview_tab_split",
    "gdocs_admin_audit",
    # v2.3.0: Drive permissions.list — pure read, no writes, no probe
    # side-effects. Sister tool ``gdocs_share_file`` is NOT here
    # (it mutates the ACL via permissions.create).
    "gdocs_list_permissions",
    # v2.3.1: Sheets values.get — pure read of cell values. Sister
    # tools ``gsheets_write_range`` (overwrites cells) and
    # ``gsheets_create_spreadsheet`` (creates a new resource) are
    # NOT readonly.
    "gsheets_read_range",
    # v2.3.2: Slides presentations.get — pure read of the deck's
    # structure + per-slide text. Sister tools
    # ``gslides_replace_all_text`` (mutates text across slides) and
    # ``gslides_create_presentation`` (creates a new deck) are NOT
    # readonly.
    "gslides_get_outline",
    # Forms (new service): pure reads. ``gforms_get_form`` reads the
    # form's structure; ``gforms_list_responses`` / ``gforms_get_response``
    # read submitted responses (forms.responses.readonly scope). Sister
    # tools ``gforms_create_form`` (creates a form), ``gforms_add_question``
    # (adds an item), ``gforms_update_item`` (edits an item) and
    # ``gforms_delete_item`` (removes an item) are NOT readonly.
    "gforms_get_form",
    "gforms_list_responses",
    "gforms_get_response",
    # v2.4.0: Calendar reads — events.list / events.get / calendarList.list
    # / freebusy.query are all pure reads (no writes, no probe side-effects).
    # Sister tools ``gcal_create_event`` / ``gcal_update_event`` (mutate
    # events) and ``gcal_delete_event`` (destructive) are NOT readonly.
    "gcal_list_events",
    "gcal_get_event",
    "gcal_list_calendars",
    "gcal_freebusy",
    # contacts: People API reads. ``gcontacts_list`` (connections.list),
    # ``gcontacts_search`` (searchContacts — the warmup write is an
    # advisory cache prime, not a user-data mutation, so the tool is still
    # a read), and ``gcontacts_get`` (people.get) are pure reads. Sister
    # tools ``gcontacts_create`` / ``gcontacts_update`` (mutate the address
    # book) and ``gcontacts_delete`` (removes a contact) are NOT readonly.
    "gcontacts_list",
    "gcontacts_search",
    "gcontacts_get",
    # CASA-free growth: People API otherContacts.list — a pure read of the
    # auto-saved "other contacts" (contacts.other.readonly is read-only by
    # construction; there is no other-contacts write).
    "gcontacts_list_other_contacts",
    # Gmail (services/gmail/): users.labels.list — a pure read of the
    # mailbox's label objects (no message read, no mutation). Sister tools
    # gmail_send_message (sends mail) / gmail_create_label (creates a
    # label) are NOT readonly, and gmail_delete_label is destructive.
    "gmail_list_labels",
    # CASA-free growth: Apps Script processes.listScriptProcesses — a pure
    # read of a script project's execution history (script.processes is
    # read-only; the tool runs nothing and mutates nothing).
    "as_list_script_processes",
    # Tasks (services/tasks/): tasklists.list / tasks.list — pure reads.
    # Sister tools gtasks_create_* / gtasks_update_task /
    # gtasks_complete_task (mutations) and gtasks_delete_task
    # (destructive) are NOT readonly.
    "gtasks_list_tasklists",
    "gtasks_list_tasks",
    # Comments: Drive comments.list on app-created docs — a pure read of
    # the comment threads (no writes). Sister tools gdocs_create_comment
    # (adds a comment) and gdocs_reply_to_comment (adds a reply) are NOT
    # readonly.
    "gdocs_list_comments",
    # chore/tool-namespace-cleanup — canonical names for the renamed
    # read-only tools (the gdocs_* aliases above stay readonly too; their
    # old names are already in this set).
    "gdrive_find_doc_by_title",   # was gdocs_find_doc_by_title
    "gdrive_find_file",           # was gdocs_find_file
    "gdrive_list_permissions",    # was gdocs_list_permissions
    "server_info",                # was gdocs_server_info
    "server_test_manifest",       # was gdocs_test_manifest
    "server_guide",               # was gdocs_guide
    "server_help",                # was gdocs_help
    "admin_audit",                # was gdocs_admin_audit
    # 2026-07 next wave: three-layer health report - probes only
    # (drive.about.get, script.projects.get, an anonymous /exec GET);
    # mutates nothing.
    "server_health",
}


DESTRUCTIVE_TOOLS = {
    "gdocs_delete_tab",
    "gdocs_trash_file",
    "gdocs_reset_authorization",
    # Deletes a content span (deleteContentRange) before any optional
    # re-insert — removes existing document text, so MCP clients should
    # be able to prompt for confirmation. (gdocs_format_range is NOT here:
    # it only restyles, never removes content.)
    "gdocs_edit_range",
    "gsheets_delete_sheet",  # removes a tab + all its cell data
    # Revokes a share (permissions.delete) — removes someone's access,
    # the inverse of gdocs_share_file. Destructive so MCP clients can
    # prompt for confirmation. (gdocs_create_folder is NOT here — it
    # only adds state.)
    "gdocs_revoke_permission",
    # Forms (new service): removes a form item (deleteItem). Destructive
    # so MCP clients can prompt for confirmation. (gforms_update_item is
    # NOT here — it only edits an item's title/description, never removes
    # content.)
    "gforms_delete_item",
    # v2.4.0: Calendar events.delete — removes an event (and its data).
    # Sister tool ``gcal_update_event`` only patches (NOT destructive);
    # ``gcal_create_event`` only adds. Destructive so MCP clients can
    # prompt for confirmation.
    "gcal_delete_event",
    # contacts: People API deleteContact — removes a contact from the
    # address book (no API-restorable trash). Destructive so MCP clients
    # can prompt for confirmation. Sister tools ``gcontacts_create`` /
    # ``gcontacts_update`` only add / modify state, so they are NOT here.
    "gcontacts_delete",
    # Tasks (services/tasks/): tasks.delete — removes a task (and its
    # sub-tasks). Destructive so MCP clients can prompt for confirmation.
    # (gtasks_complete_task is NOT here — it only flips status, the task
    # persists.)
    "gtasks_delete_task",
    # Gmail (services/gmail/): users.labels.delete — removes a label
    # object. Destructive so MCP clients can prompt for confirmation.
    # (gmail_create_label only adds; gmail_send_message only sends; so
    # neither is here. Removing a label does NOT delete the messages that
    # carried it, but the label object itself is gone.)
    "gmail_delete_label",
    # Sheets batchUpdate (deleteDimension): removes rows/columns and their
    # cell data (and shifts later cells). Destructive so MCP clients can
    # prompt for confirmation. Sister tool gsheets_insert_dimension only
    # adds blank rows/cols, and gsheets_merge_cells only combines them, so
    # those are NOT here. (gsheets_clear_range only blanks values; it is a
    # values wipe, not a structural delete, and is not in this list.)
    "gsheets_delete_dimension",
    # chore/tool-namespace-cleanup — canonical names for the renamed
    # destructive tools (the gdocs_* aliases above stay destructive too;
    # their old names are already in this set).
    "gdrive_trash_file",          # was gdocs_trash_file
    "gdrive_revoke_permission",   # was gdocs_revoke_permission
    "account_reset_authorization",  # was gdocs_reset_authorization
}


# Tools whose effects target external systems (Google APIs). Per R23,
# `gdocs_help` and `gdocs_guide` are pure-local introspection helpers
# that never call out, so they carry openWorldHint=False.
NOT_OPEN_WORLD_TOOLS = {
    "gdocs_help",
    "gdocs_guide",
    # chore/tool-namespace-cleanup — canonical names for the two
    # pure-local introspection helpers (openWorldHint=False). The old
    # gdocs_* aliases above remain pure-local too.
    "server_help",   # was gdocs_help
    "server_guide",  # was gdocs_guide
}


def test_readonly_tools_marked_readonly():
    """Explicit allow-list — prevents accidental regressions on read-only hints."""
    tools = {t.name: t for t in _list_tools()}
    for name in READONLY_TOOLS:
        assert name in tools, f"unknown tool in READONLY_TOOLS allow-list: {name}"
        a = tools[name].annotations
        assert a is not None, f"{name}: annotations missing"
        assert a.readOnlyHint is True, f"{name}: readOnlyHint should be True, got {a.readOnlyHint}"


def test_mutating_tools_not_marked_readonly():
    """Tools NOT in the read-only allow-list must NOT claim to be read-only."""
    tools = _list_tools()
    for t in tools:
        if t.name in READONLY_TOOLS:
            continue
        assert t.annotations is not None, f"{t.name}: annotations missing"
        assert t.annotations.readOnlyHint is False, (
            f"{t.name}: readOnlyHint True but tool is not in READONLY_TOOLS allow-list"
        )


def test_destructive_tools_marked_destructive():
    """Tools that delete / revoke state must declare destructiveHint=True
    so MCP clients can prompt for confirmation."""
    tools = {t.name: t for t in _list_tools()}
    for name in DESTRUCTIVE_TOOLS:
        assert name in tools, f"unknown tool in DESTRUCTIVE_TOOLS allow-list: {name}"
        a = tools[name].annotations
        assert a is not None, f"{name}: annotations missing"
        assert a.destructiveHint is True, (
            f"{name}: destructiveHint should be True, got {a.destructiveHint}"
        )


def test_non_destructive_tools_not_marked_destructive():
    """Non-destructive tools must NOT claim destructiveHint=True (false-positive
    confirmation prompts erode user trust)."""
    tools = _list_tools()
    for t in tools:
        if t.name in DESTRUCTIVE_TOOLS:
            continue
        assert t.annotations is not None, f"{t.name}: annotations missing"
        assert t.annotations.destructiveHint is False, (
            f"{t.name}: destructiveHint True but tool is not in DESTRUCTIVE_TOOLS allow-list"
        )


def test_local_tools_marked_not_open_world():
    """Pure-local introspection helpers (gdocs_help, gdocs_guide) must declare
    openWorldHint=False so clients know they don't reach external systems."""
    tools = {t.name: t for t in _list_tools()}
    for name in NOT_OPEN_WORLD_TOOLS:
        assert name in tools, f"unknown tool in NOT_OPEN_WORLD_TOOLS allow-list: {name}"
        a = tools[name].annotations
        assert a is not None, f"{name}: annotations missing"
        assert a.openWorldHint is False, (
            f"{name}: openWorldHint should be False (no external API call), got {a.openWorldHint}"
        )


def test_google_api_tools_marked_open_world():
    """Tools that touch Google APIs must declare openWorldHint=True so clients
    know the tool has external side-effects / dependencies."""
    tools = _list_tools()
    for t in tools:
        if t.name in NOT_OPEN_WORLD_TOOLS:
            continue
        assert t.annotations is not None, f"{t.name}: annotations missing"
        assert t.annotations.openWorldHint is True, (
            f"{t.name}: openWorldHint should be True (Google API), "
            f"got {t.annotations.openWorldHint}"
        )


def test_all_tools_have_human_readable_title():
    """Every annotation should carry a non-empty title for client UI display."""
    tools = _list_tools()
    missing_title = []
    for t in tools:
        title = getattr(t.annotations, "title", None) if t.annotations else None
        if not title:
            missing_title.append(t.name)
    assert not missing_title, f"Tools without annotation title: {missing_title}"


# ---------------------------------------------------------------------
# R28 design-internal nit — gdocs_admin_audit title must describe what
# the function actually does. Pre-fix it claimed "list registered users";
# the body (server.py near line 2447) actually calls
# ``user_store.get_state(user_id)`` and returns timestamp bounds for a
# single user_id — not a user list. Misleading titles erode trust in
# the annotation surface and confuse operators reading the MCP client
# UI before they read the docstring.
# ---------------------------------------------------------------------


def test_admin_audit_title_describes_actual_behavior():
    """Regression: gdocs_admin_audit returns timestamp bounds for one
    user_id, not a user list. Title must not say "list users" or
    "list registered users"."""
    tools = {t.name: t for t in _list_tools()}
    assert "gdocs_admin_audit" in tools, "gdocs_admin_audit not registered"
    title = tools["gdocs_admin_audit"].annotations.title
    lowered = title.lower()
    assert "list" not in lowered, (
        f"Title misrepresents function: {title!r}. "
        f"gdocs_admin_audit returns timestamp bounds for a single user_id "
        f"(see server.py user_store.get_state call), not a user list."
    )
    assert any(kw in lowered for kw in ("timeline", "forensic", "audit", "state")), (
        f"Title doesn't describe actual behavior: {title!r}. "
        f"Should reference timeline / forensic / audit / state."
    )
