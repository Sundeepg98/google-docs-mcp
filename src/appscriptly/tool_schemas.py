"""JSON Schema constants for every ``@mcp.tool`` output shape (v2.0.6 / F6).

Each tool returns a ``dict``. This module pins the load-bearing keys and
their types so MCP clients can validate responses and so a regression
that drops a key surfaces at the CI guard (see
``tests/unit/test_tool_output_schemas.py``).

**Design choices:**

- ``additionalProperties: True`` everywhere — tools may grow new optional
  fields (e.g. ``warnings``, ``note``, ``v2.x_telemetry``) without a
  breaking-change bump. Locking down every optional field would make
  small additive enhancements look like contract breaks.
- ``required: [...]`` lists ONLY the keys callers can rely on across every
  successful response. If a tool has multiple shapes (matched vs
  unmatched in ``gdocs_help``, status union in ``gdocs_setup_apps_script``),
  the schema uses ``oneOf`` so each variant is independently validated.
- One constant per tool, named ``<TOOL_NAME>_OUTPUT_SCHEMA``. The
  iteration test (``test_every_tool_has_output_schema``) walks
  ``mcp.list_tools()`` and asserts each tool name maps to a registered
  schema, so adding a new tool without a schema fails CI before merge.

**Why not TypedDict + pydantic.TypeAdapter.json_schema?** TypedDict
sub-class hierarchies for the ``oneOf`` cases are awkward in Python
(no ``Annotated[Union[...], Discriminator(...)]`` here today) and the
generated schemas are noisier than hand-written. Hand-written JSON
keeps the file diff-readable and the schema-as-API contract obvious.
"""
from __future__ import annotations

# ---------------------------------------------------------------------
# Common building blocks
# ---------------------------------------------------------------------


def _object(properties: dict, required: list[str]) -> dict:
    """Minimal helper — ``type: object`` with ``additionalProperties: True``.

    Permissive on extra fields by design (see module docstring).
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


# Shared sub-schema: one entry in the ``tabs`` array returned by
# make_doc_with_tabs / add_tabs_to_doc. Each entry pins the four
# load-bearing keys; depth + parent_tab_id can be None for top-level tabs.
_TAB_ENTRY_SCHEMA = _object(
    properties={
        "title": {"type": "string"},
        "tab_id": {"type": "string"},
        "depth": {"type": "integer", "minimum": 0},
        "parent_tab_id": {"type": ["string", "null"]},
    },
    required=["title", "tab_id"],
)


# ---------------------------------------------------------------------
# Creation / mutation tools
# ---------------------------------------------------------------------


GDOCS_MAKE_TABBED_DOC_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "tabs": {"type": "array", "items": _TAB_ENTRY_SCHEMA},
    },
    required=["doc_id", "url", "tabs"],
)


GDOCS_ADD_TABS_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "tabs": {"type": "array", "items": _TAB_ENTRY_SCHEMA},
    },
    required=["doc_id", "url", "tabs"],
)


GDOCS_TAB_EXISTING_DOC_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "action": {"type": "string", "enum": ["created", "replaced"]},
        "tabs": {"type": "array"},
        "split_strategy_used": {"type": "string"},
    },
    required=["doc_id", "url", "action", "tabs", "split_strategy_used"],
)


GDOCS_APPEND_TO_TAB_OUTPUT_SCHEMA = _object(
    properties={
        "tab_id": {"type": "string"},
        "appended_chars": {"type": "integer", "minimum": 0},
    },
    required=["tab_id", "appended_chars"],
)


GDOCS_RENAME_TAB_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "tab_id": {"type": "string"},
        "updated_fields": {"type": "array", "items": {"type": "string"}},
    },
    required=["doc_id", "tab_id", "updated_fields"],
)


GDOCS_DELETE_TAB_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "deleted_tab_id": {"type": "string"},
    },
    required=["doc_id", "deleted_tab_id"],
)


GDOCS_REPLACE_ALL_TEXT_OUTPUT_SCHEMA = _object(
    properties={
        "occurrences_changed": {"type": "integer", "minimum": 0},
    },
    required=["occurrences_changed"],
)


# ``gdocs_insert_table`` echoes the inserted table's shape + location.
# ``tab_id`` is nullable (None = default/first tab).
GDOCS_INSERT_TABLE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "rows": {"type": "integer", "minimum": 1},
        "columns": {"type": "integer", "minimum": 1},
        "index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
    },
    required=["doc_id", "rows", "columns", "index"],
)


# ``gdocs_format_range`` echoes the formatted range + the list of style
# fields actually applied (the updateTextStyle ``fields`` mask).
GDOCS_FORMAT_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "start_index": {"type": "integer", "minimum": 1},
        "end_index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
        "applied": {"type": "array", "items": {"type": "string"}},
    },
    required=["doc_id", "start_index", "end_index", "applied"],
)


# ``gdocs_format_paragraph`` echoes the range + the paragraph-style
# fields actually applied (the updateParagraphStyle ``fields`` mask).
GDOCS_FORMAT_PARAGRAPH_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "start_index": {"type": "integer", "minimum": 1},
        "end_index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
        "applied": {"type": "array", "items": {"type": "string"}},
    },
    required=["doc_id", "start_index", "end_index", "applied"],
)


# ``gdocs_insert_markdown_table`` echoes the parsed shape + how many
# non-empty cells were populated.
GDOCS_INSERT_MARKDOWN_TABLE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "rows": {"type": "integer", "minimum": 1},
        "columns": {"type": "integer", "minimum": 1},
        "index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
        "cells_filled": {"type": "integer", "minimum": 0},
    },
    required=["doc_id", "rows", "columns", "index", "cells_filled"],
)


GDOCS_SET_TAB_ICONS_OUTPUT_SCHEMA = _object(
    properties={
        "updated_count": {"type": "integer", "minimum": 0},
    },
    required=["updated_count"],
)


# ---------------------------------------------------------------------
# Read / inspection tools
# ---------------------------------------------------------------------


GDOCS_GET_DOC_OUTLINE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "trashed": {"type": "boolean"},
        "tabs": {"type": "array"},
    },
    required=["doc_id", "tabs"],
)


GDOCS_READ_DOC_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        # The shape branches by what was requested (tab vs full doc);
        # we pin the discriminator-level keys only.
    },
    required=["doc_id"],
)


GDOCS_GET_TAB_URL_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "tab_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
    },
    required=["doc_id", "tab_id", "url"],
)


GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA = _object(
    properties={
        "matches": {"type": "array"},
        "count": {"type": "integer", "minimum": 0},
    },
    required=["matches", "count"],
)


# Drive generalized find (services/drive/api.py::find_file). Same
# {matches, count} shape as find_doc_by_title — the two are drop-in
# interchangeable for consumers; find_file just searches all app-
# accessible mimeTypes with optional mime/fullText/folder filters.
GDOCS_FIND_FILE_OUTPUT_SCHEMA = _object(
    properties={
        "matches": {"type": "array"},
        "count": {"type": "integer", "minimum": 0},
    },
    required=["matches", "count"],
)


GDOCS_PREVIEW_TAB_SPLIT_OUTPUT_SCHEMA = _object(
    properties={
        "split_strategy_used": {"type": "string"},
        "tab_count": {"type": "integer", "minimum": 0},
        "tabs": {"type": "array"},
        "problems": {"type": "array"},
    },
    required=["split_strategy_used", "tab_count", "tabs", "problems"],
)


# ---------------------------------------------------------------------
# Drive file operations
# ---------------------------------------------------------------------


GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA = _object(
    properties={
        "file_id": {"type": "string"},
    },
    required=["file_id"],
)


GDOCS_TRASH_FILE_OUTPUT_SCHEMA = _object(
    properties={
        # Returns either single-file dict OR batch dict with results/summary.
        # Pin the common discriminator: file_id (single) or results (batch).
    },
    required=[],
)


GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA = _object(
    properties={
        # Same shape variance as gdocs_trash_file.
    },
    required=[],
)


# v2.3.0 — Drive sharing (services/drive/sharing.py)
GDOCS_SHARE_FILE_OUTPUT_SCHEMA = _object(
    properties={
        "permission_id": {"type": "string"},
        "role": {"type": "string"},
        "granted_to": {"type": "string"},
        "file_id": {"type": "string"},
    },
    required=["permission_id", "role", "file_id"],
)


GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA = _object(
    properties={
        "file_id": {"type": "string"},
        "permissions": {"type": "array"},
    },
    required=["file_id", "permissions"],
)


# Drive folder create (services/drive/api.py::create_folder).
GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA = _object(
    properties={
        "folder_id": {"type": "string"},
        "name": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        # None when the folder was created in Drive root.
        "parent_folder_id": {"type": ["string", "null"]},
    },
    required=["folder_id", "name", "url"],
)


# Drive permission revoke (services/drive/sharing.py::revoke_permission).
# Success and soft-failure share file_id / permission_id / revoked; the
# remaining keys vary by branch (was_already_absent on success, reason +
# message on soft-failure), covered by additionalProperties.
GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA = _object(
    properties={
        "file_id": {"type": "string"},
        "permission_id": {"type": "string"},
        "revoked": {"type": "boolean"},
    },
    required=["file_id", "permission_id", "revoked"],
)


# Drive export (services/drive/api.py::export_doc). Success and
# soft-failure both carry source_file_id; the success branch adds the
# export + new-file keys, the soft-failure branch adds reason + message
# (covered by additionalProperties). Only source_file_id is guaranteed
# across both shapes — same single-required-key pattern as the move /
# trash soft-failure schemas. download_url is nullable (Drive may omit
# webContentLink); size_bytes nullable when Drive doesn't report size.
GDOCS_EXPORT_DOC_OUTPUT_SCHEMA = _object(
    properties={
        "source_file_id": {"type": "string"},
        "source_mime_type": {"type": "string"},
        "export_format": {"type": "string"},
        "export_mime_type": {"type": "string"},
        "exported_file_id": {"type": "string"},
        "name": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "download_url": {"type": ["string", "null"]},
        "size_bytes": {"type": ["integer", "null"]},
    },
    required=["source_file_id"],
)


# ---------------------------------------------------------------------
# Sheets (services/sheets/) — v2.3.1 minimal start (2nd new service)
# ---------------------------------------------------------------------


GSHEETS_READ_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "range": {"type": "string"},
        "values": {"type": "array"},
    },
    required=["range", "values"],
)


GSHEETS_WRITE_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "updated_range": {"type": "string"},
        "updated_cells": {"type": "integer", "minimum": 0},
    },
    required=["updated_range", "updated_cells"],
)


GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
    },
    required=["spreadsheet_id", "url", "title"],
)


# ``gsheets_format_range`` returns the flat ``batch_update`` envelope:
# the spreadsheet id, the number of batchUpdate requests sent (1 — a
# single repeatCell), and Sheets' raw per-request reply list.
GSHEETS_FORMAT_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


# ``gsheets_apply_conditional_format`` returns the same flat batch_update
# envelope as gsheets_format_range — one addConditionalFormatRule request.
GSHEETS_APPLY_CONDITIONAL_FORMAT_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


# ``gsheets_append_rows`` appends rows after a table's last row
# (values.append — race-free) and returns where they landed + counts.
GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA = _object(
    properties={
        "updated_range": {"type": "string"},
        "updated_cells": {"type": "integer", "minimum": 0},
        "updated_rows": {"type": "integer", "minimum": 0},
    },
    required=["updated_range", "updated_cells", "updated_rows"],
)


# ``gsheets_add_sheet`` adds a tab and returns the gid Sheets assigned
# it. ``sheet_id`` / ``index`` are integers Sheets echoes back;
# ``index`` may be absent if Sheets omits it from the reply (hence not
# required).
GSHEETS_ADD_SHEET_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "sheet_id": {"type": "integer"},
        "title": {"type": "string"},
        "index": {"type": ["integer", "null"], "minimum": 0},
    },
    required=["spreadsheet_id", "sheet_id", "title"],
)


# ``gsheets_delete_sheet`` echoes the removed tab's gid.
GSHEETS_DELETE_SHEET_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "deleted_sheet_id": {"type": "integer"},
    },
    required=["spreadsheet_id", "deleted_sheet_id"],
)


# ``gsheets_rename_sheet`` echoes the tab's gid + its new name.
GSHEETS_RENAME_SHEET_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "sheet_id": {"type": "integer"},
        "title": {"type": "string"},
    },
    required=["spreadsheet_id", "sheet_id", "title"],
)


# ---------------------------------------------------------------------
# Slides (services/slides/) — v2.3.2 minimal start (3rd new service)
# ---------------------------------------------------------------------


GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "title": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "slides": {"type": "array"},
    },
    required=["presentation_id", "title", "url", "slides"],
)


GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "occurrences_changed": {"type": "integer", "minimum": 0},
    },
    required=["presentation_id", "occurrences_changed"],
)


GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
    },
    required=["presentation_id", "url", "title"],
)


# ``gslides_add_slide`` appends a slide (optionally with title + body)
# and returns the new slide's stable objectId plus a deep-link URL.
GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
    },
    required=["presentation_id", "slide_object_id", "url"],
)


# ``gslides_create_image`` inserts an image (by URL) onto a slide and
# returns the new image element's stable objectId + deep-link URL.
GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "image_object_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
    },
    required=[
        "presentation_id",
        "slide_object_id",
        "image_object_id",
        "url",
    ],
)


# ``gslides_create_table`` inserts an empty rows×columns table onto a
# slide; echoes the dimensions + the table element's stable objectId.
GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "table_object_id": {"type": "string"},
        "rows": {"type": "integer", "minimum": 1},
        "columns": {"type": "integer", "minimum": 1},
        "url": {"type": "string", "format": "uri"},
    },
    required=[
        "presentation_id",
        "slide_object_id",
        "table_object_id",
        "rows",
        "columns",
        "url",
    ],
)


# ``as_deploy_web_app`` (ROADMAP 59) deploys a standalone Apps Script
# project carrying a doGet/doPost handler as a Web App, returning the
# live /exec endpoint + the IDs/version. ``exec_url`` is the load-bearing
# field (the webhook/HTTP endpoint the caller wires into Slack/Stripe/cron).
AS_DEPLOY_WEB_APP_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "exec_url": {"type": "string", "format": "uri"},
        "execute_as": {"type": "string"},
        "access": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=["script_id", "deployment_id", "version", "exec_url"],
)


# ---------------------------------------------------------------------
# Apps Script — bound-script generator (PR-Δ7)
# ---------------------------------------------------------------------


# ``as_generate_bound_script`` returns the IDs + a deep-link to the
# generated bound project. ``container_kind`` is the resolved
# docs/sheets/slides discriminator (echoed so the caller sees what was
# detected). additionalProperties stays True (the _object default) so a
# future field (e.g. ``version_number``, ``warnings``) is additive.
AS_GENERATE_BOUND_SCRIPT_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "container_id": {"type": "string"},
        "container_kind": {
            "type": "string",
            "enum": ["docs", "sheets", "slides"],
        },
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "container_id",
        "container_kind",
        "project_url",
    ],
)


# ``as_install_custom_function`` (PR-Δ10) returns the deployed IDs plus
# the Sheets-friendly ``usage_hint`` (the literal ``=FUNCTION(...)`` the
# user types) and the echoed ``function_name`` / ``sheet_id``.
# additionalProperties stays True (the _object default) so a future
# field (e.g. ``needs_reload``, ``warnings``) is additive.
AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "function_name": {"type": "string"},
        "usage_hint": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "sheet_id",
        "function_name",
        "usage_hint",
        "project_url",
    ],
)


# ``as_install_sheet_dashboard`` (PR-Δ9) returns the deployed bound
# script's IDs + the schedule it wired + the trigger HANDLER name + a
# deep-link, PLUS the honest trigger-activation state. ``trigger_active``
# is False on a fresh deploy: an installable time trigger only exists once
# ``installTrigger`` runs, and deploy doesn't run it, so the schedule
# isn't live until the user does the one-time run. ``activation_required``
# + ``activation_instructions`` spell that out. additionalProperties stays
# True (the _object default) so a future field is additive.
AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "schedule": {
            "type": "string",
            "enum": ["daily", "hourly", "weekly"],
        },
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "sheet_id",
        "schedule",
        "trigger_handler",
        "project_url",
        "trigger_active",
        "activation_required",
        "activation_instructions",
    ],
)


# ``as_install_doc_menu`` (PR-Δ8) composes the bound-script generator
# into a "install a custom menu into a Doc" feature. Returns the bound
# project's IDs + the Doc it bound to + the installed menu's title and
# item count (echoed so the caller can confirm what was wired), plus the
# script-editor deep link. additionalProperties stays True (the _object
# default) so a future field is additive.
AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "doc_id": {"type": "string"},
        "menu_title": {"type": "string"},
        "item_count": {"type": "integer", "minimum": 1},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "doc_id",
        "menu_title",
        "item_count",
        "project_url",
    ],
)


# ``as_generate_video_deck`` (PR-Δ11) composes the bound-script generator
# into the RENDER half of a slides-to-video pipeline. Returns the bound
# project's IDs + the deck it bound to + the output folder + the render
# function name + an HONEST activation note (the frames don't exist until
# renderFrames runs). ``frames_expected`` is nullable — the slide count is
# only known once renderFrames runs (the tool doesn't read the deck), so a
# successful deploy returns null here. additionalProperties stays True (the
# _object default) so a future field (e.g. the eventual encode pointer) is
# additive.
AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "presentation_id": {"type": "string"},
        # Base-tier redesign: the renderer POSTs frames to the server's
        # signed staging area instead of a Drive folder, so the contract
        # returns the batch handle (passed to as_encode_video) rather than
        # an output_folder_name.
        "frames_batch_id": {"type": "string"},
        "frames_expected": {"type": ["integer", "null"], "minimum": 0},
        "render_function": {"type": "string"},
        "activation_note": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "presentation_id",
        "frames_batch_id",
        "render_function",
        "activation_note",
        "project_url",
    ],
)


# PR-Δ12 — encode rendered slide frames into an MP4 (the ENCODE half of
# the slides-to-video pipeline; server-side ffmpeg compute, composes the
# Drive folder as_generate_video_deck produced).
AS_ENCODE_VIDEO_OUTPUT_SCHEMA = _object(
    properties={
        "video_file_id": {"type": "string"},
        "video_url": {"type": "string", "format": "uri"},
        "frame_count": {"type": "integer", "minimum": 1},
        "duration_sec": {"type": "number", "minimum": 0},
        "fps": {"type": "integer", "minimum": 1},
        "output_name": {"type": "string"},
    },
    required=[
        "video_file_id",
        "video_url",
        "frame_count",
        "duration_sec",
        "fps",
        "output_name",
    ],
)


# ---------------------------------------------------------------------
# Server identity / diagnostics / local-only
# ---------------------------------------------------------------------


GDOCS_SERVER_INFO_OUTPUT_SCHEMA = _object(
    properties={
        "version": {"type": "string"},
        "build_time": {"type": "string"},
        "git_commit": {"type": "string"},
        "tool_count": {"type": "integer", "minimum": 0},
        "tools": {"type": "array", "items": {"type": "string"}},
        "test_suite": {"type": "object"},
    },
    required=["version", "tool_count", "tools"],
)


GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA = _object(
    properties={
        "status": {
            "type": "string",
            "enum": ["ok", "unknown", "tampered"],
        },
        "total": {"type": "integer", "minimum": 0},
        "tests": {"type": "array"},
        "named_regression_guards": {
            "type": "object",
            "properties": {
                "present": {"type": "array"},
                "missing": {"type": "array"},
            },
            "required": ["present", "missing"],
            "additionalProperties": True,
        },
    },
    required=["status", "total", "tests", "named_regression_guards"],
)


GDOCS_GUIDE_OUTPUT_SCHEMA = _object(
    properties={
        "server": {"type": "object"},
    },
    required=["server"],
)


# ``gdocs_help`` has two distinct shapes (matched / unmatched). MCP spec
# requires the top-level output_schema to be a ``type: object`` (FastMCP
# enforces this), so we keep the variant-specific keys nested under
# ``oneOf`` inside ``properties``. The only field guaranteed across both
# shapes is ``matched`` (boolean discriminator).
GDOCS_HELP_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        # Variant-specific keys — present on one branch, absent on the
        # other. Documented here so MCP clients can introspect.
        "matched_pattern": {"type": "string"},
        "key": {"type": "string"},
        "pattern": {"type": "string"},
        "severity": {
            "type": "string",
            "enum": ["info", "warning", "error"],
        },
        "retriable": {"type": "boolean"},
        "wait_seconds": {"type": ["integer", "null"]},
        "do": {"type": "string"},
        "user_message": {"type": "string"},
        "related_tool": {"type": ["string", "null"]},
        "planned": {"type": "boolean"},
        "available_patterns": {
            "type": "array",
            "items": {"type": "string"},
        },
        "suggestion": {"type": "string"},
    },
    "required": ["matched"],
    "additionalProperties": True,
    # Stricter per-variant constraints — the iteration test exercises
    # both branches and validates against the full ``oneOf`` schema.
    "oneOf": [
        {
            "properties": {"matched": {"const": True}},
            "required": [
                "matched", "matched_pattern", "key", "pattern", "severity",
                "retriable", "do", "user_message",
            ],
        },
        {
            "properties": {"matched": {"const": False}},
            "required": ["matched", "available_patterns", "suggestion"],
        },
    ],
}


# ---------------------------------------------------------------------
# Auth / setup / signed URLs
# ---------------------------------------------------------------------


GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA = _object(
    properties={
        "url": {"type": "string", "format": "uri"},
        "expires_at": {"type": "integer"},
        "max_bytes": {"type": "integer", "minimum": 1},
        "nonce": {"type": "string"},
        "user_id": {"type": "string"},
        "usage_hint": {"type": "string"},
    },
    required=["url", "expires_at", "max_bytes", "nonce", "user_id"],
)


# ``gdocs_setup_apps_script`` returns one of three status variants:
# "ready" (deploy succeeded / already in place), "needs_authorization"
# (NeedsReauthError surfaced with auth_url), or "failed". MCP spec
# requires top-level ``type: object``; per-variant constraints live in
# nested ``oneOf``.
GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "needs_authorization", "failed"],
        },
        # Variant-specific fields:
        "url": {"type": "string", "format": "uri"},
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "auth_url": {"type": "string", "format": "uri"},
        "error": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["status"],
    "additionalProperties": True,
    "oneOf": [
        {
            "properties": {"status": {"const": "ready"}},
            "required": ["status", "url", "script_id", "deployment_id"],
        },
        {
            "properties": {"status": {"const": "needs_authorization"}},
            "required": ["status", "auth_url", "message"],
        },
        {
            "properties": {"status": {"const": "failed"}},
            "required": ["status"],
        },
    ],
}


GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA = _object(
    properties={
        "status": {"type": "string", "const": "reset"},
        "message": {"type": "string"},
        "cleared": {"type": ["array", "object", "boolean"]},
    },
    required=["status", "message"],
)


# ---------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------


GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA = _object(
    properties={
        "user_id_prefix": {"type": "string"},
        "window_hours": {"type": "integer", "minimum": 0},
        "total_entries": {"type": "integer", "minimum": 0},
        "entries": {"type": "array"},
        "notes": {"type": "string"},
    },
    required=["user_id_prefix", "window_hours", "total_entries", "entries"],
)


# ---------------------------------------------------------------------
# Registry — name -> schema. Used by the iteration guard test.
# ---------------------------------------------------------------------


TOOL_OUTPUT_SCHEMAS: dict[str, dict] = {
    "gdocs_make_tabbed_doc": GDOCS_MAKE_TABBED_DOC_OUTPUT_SCHEMA,
    "gdocs_add_tabs": GDOCS_ADD_TABS_OUTPUT_SCHEMA,
    "gdocs_tab_existing_doc": GDOCS_TAB_EXISTING_DOC_OUTPUT_SCHEMA,
    "gdocs_append_to_tab": GDOCS_APPEND_TO_TAB_OUTPUT_SCHEMA,
    "gdocs_rename_tab": GDOCS_RENAME_TAB_OUTPUT_SCHEMA,
    "gdocs_delete_tab": GDOCS_DELETE_TAB_OUTPUT_SCHEMA,
    "gdocs_replace_all_text": GDOCS_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
    "gdocs_insert_table": GDOCS_INSERT_TABLE_OUTPUT_SCHEMA,
    "gdocs_format_range": GDOCS_FORMAT_RANGE_OUTPUT_SCHEMA,
    "gdocs_format_paragraph": GDOCS_FORMAT_PARAGRAPH_OUTPUT_SCHEMA,
    "gdocs_insert_markdown_table": GDOCS_INSERT_MARKDOWN_TABLE_OUTPUT_SCHEMA,
    "gdocs_set_tab_icons": GDOCS_SET_TAB_ICONS_OUTPUT_SCHEMA,
    "gdocs_get_doc_outline": GDOCS_GET_DOC_OUTLINE_OUTPUT_SCHEMA,
    "gdocs_read_doc": GDOCS_READ_DOC_OUTPUT_SCHEMA,
    "gdocs_get_tab_url": GDOCS_GET_TAB_URL_OUTPUT_SCHEMA,
    "gdocs_find_doc_by_title": GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    "gdocs_preview_tab_split": GDOCS_PREVIEW_TAB_SPLIT_OUTPUT_SCHEMA,
    "gdocs_move_to_folder": GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    "gdocs_trash_file": GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    "gdocs_untrash_file": GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
    "gdocs_share_file": GDOCS_SHARE_FILE_OUTPUT_SCHEMA,
    "gdocs_list_permissions": GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
    # Drive folder create + permission revoke (drive.file scope, additive).
    "gdocs_create_folder": GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA,
    "gdocs_revoke_permission": GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA,
    # Drive export (files.export → portable format, stored in Drive).
    "gdocs_export_doc": GDOCS_EXPORT_DOC_OUTPUT_SCHEMA,
    # Drive generalized find (files.list, any mimeType, app-accessible
    # corpus — drive.file scope, additive).
    "gdocs_find_file": GDOCS_FIND_FILE_OUTPUT_SCHEMA,
    # v2.3.1 — Sheets (2nd new service, minimal start)
    "gsheets_read_range": GSHEETS_READ_RANGE_OUTPUT_SCHEMA,
    "gsheets_write_range": GSHEETS_WRITE_RANGE_OUTPUT_SCHEMA,
    "gsheets_create_spreadsheet": GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA,
    "gsheets_format_range": GSHEETS_FORMAT_RANGE_OUTPUT_SCHEMA,
    # Sheets conditional formatting (this PR — wires the existing builder)
    "gsheets_apply_conditional_format": GSHEETS_APPLY_CONDITIONAL_FORMAT_OUTPUT_SCHEMA,
    # Sheets append + tab lifecycle (this PR)
    "gsheets_append_rows": GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA,
    "gsheets_add_sheet": GSHEETS_ADD_SHEET_OUTPUT_SCHEMA,
    "gsheets_delete_sheet": GSHEETS_DELETE_SHEET_OUTPUT_SCHEMA,
    "gsheets_rename_sheet": GSHEETS_RENAME_SHEET_OUTPUT_SCHEMA,
    # v2.3.2 — Slides (3rd new service, minimal start)
    "gslides_get_outline": GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA,
    "gslides_replace_all_text": GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
    "gslides_create_presentation": GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
    "gslides_add_slide": GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA,
    "gslides_create_image": GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA,
    "gslides_create_table": GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA,
    # ROADMAP 59 — deploy a standalone doGet/doPost project as a Web App
    "as_deploy_web_app": AS_DEPLOY_WEB_APP_OUTPUT_SCHEMA,
    # PR-Δ7 — Apps Script bound-script generator (the feature foundation)
    "as_generate_bound_script": AS_GENERATE_BOUND_SCRIPT_OUTPUT_SCHEMA,
    # PR-Δ8 — install a custom menu into a Doc (composes the Δ7 primitive)
    "as_install_doc_menu": AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA,
    # PR-Δ10 — custom spreadsheet function installer (composes PR-Δ7)
    "as_install_custom_function": AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA,
    # PR-Δ9 — scheduled dashboard refresh for Sheets (composes PR-Δ7)
    "as_install_sheet_dashboard": AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA,
    # PR-Δ11 — render a Slides deck to video frames (composes PR-Δ7;
    # the render half of the slides-to-video pipeline)
    "as_generate_video_deck": AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
    # PR-Δ12 — encode those rendered frames into an MP4 (server-side
    # ffmpeg; the encode half that completes slides-to-video)
    "as_encode_video": AS_ENCODE_VIDEO_OUTPUT_SCHEMA,
    "gdocs_server_info": GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    "gdocs_test_manifest": GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    "gdocs_guide": GDOCS_GUIDE_OUTPUT_SCHEMA,
    "gdocs_help": GDOCS_HELP_OUTPUT_SCHEMA,
    "gdocs_get_signed_upload_url": GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    # PR-α / v2.3.4 — Workspace automation runtime install.
    # gdocs_install_automation is canonical; gdocs_setup_apps_script
    # is a deprecation alias kept registered for backward compatibility.
    # BOTH share the same output schema (same underlying installer).
    "gdocs_install_automation": GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
    "gdocs_setup_apps_script": GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
    "gdocs_reset_authorization": GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    "gdocs_admin_audit": GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
}
