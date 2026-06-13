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
}


# Tools whose effects target external systems (Google APIs). Per R23,
# `gdocs_help` and `gdocs_guide` are pure-local introspection helpers
# that never call out, so they carry openWorldHint=False.
NOT_OPEN_WORLD_TOOLS = {
    "gdocs_help",
    "gdocs_guide",
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
