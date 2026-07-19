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


# The convert response contract (T2.1/T2.3 + the S2.5 completion
# manifest). ``doc_id``/``url`` are nullable for the retrofit
# zero-match error return (nothing was created). ``action`` values:
# created | replaced (a prior version was actually trashed) | skipped
# (on_conflict=skip found an existing doc) | failed (error return that
# created nothing). ``completion.pending_sections`` non-empty means
# those sections exist ONLY in the placeholder tab - never delete it.
GDOCS_TAB_EXISTING_DOC_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"], "format": "uri"},
        "action": {
            "type": "string",
            "enum": ["created", "replaced", "skipped", "failed"],
        },
        "on_conflict_action": {
            "type": "string",
            "enum": ["created", "replaced", "skipped"],
        },
        "tabs": {"type": "array"},
        "split_strategy_used": {"type": "string"},
        "heading1_found": {"type": "integer", "minimum": 0},
        "tabs_created": {"type": "integer", "minimum": 0},
        "placeholder": {
            "type": "string",
            "enum": ["deleted", "renamed", "kept", "none"],
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "info": {"type": "array", "items": {"type": "string"}},
        "replaced_doc_id": {"type": "string"},
        "error": {"type": "string"},
        "completion": _object(
            properties={
                "steps_completed": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "import",
                            "shells",
                            "transplant",
                            "verify",
                            "carve",
                            "placeholder",
                            "cosmetics",
                        ],
                    },
                },
                "moved_sections": {
                    "type": "array", "items": {"type": "string"},
                },
                "pending_sections": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            required=["steps_completed", "moved_sections", "pending_sections"],
        ),
    },
    required=[
        "doc_id",
        "url",
        "action",
        "tabs",
        "split_strategy_used",
        "heading1_found",
        "tabs_created",
        "placeholder",
        "warnings",
        "completion",
    ],
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
        # S2.5 defense (2026-07-10, optional/additive): ``forced`` is
        # present (true) only when force=true overrode the non-empty-tab
        # guard; ``warnings`` carries the first-tab-deleted defect note.
        "forced": {"type": "boolean"},
        "warnings": {"type": "array", "items": {"type": "string"}},
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


# Template-fill (Wave 5 S1). ``gdocs_create_named_range`` echoes the
# created marker + its server ``named_range_id`` (nullable if Docs omits
# it in the reply).
GDOCS_CREATE_NAMED_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "named_range_id": {"type": ["string", "null"]},
        "name": {"type": "string"},
        "start_index": {"type": "integer", "minimum": 1},
        "end_index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
    },
    required=["doc_id", "name", "start_index", "end_index"],
)


# ``gdocs_replace_named_range_content`` echoes the request (the Docs
# replaceNamedRangeContent reply carries no match count). ``selector`` is
# "named_range_name" | "named_range_id"; ``scope`` is a tab-scope string
# ("all_tabs"), a tab-id list, or null (for id selection).
GDOCS_REPLACE_NAMED_RANGE_CONTENT_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "selector": {"type": "string"},
        "selector_value": {"type": "string"},
        "text_length": {"type": "integer", "minimum": 0},
        "scope": {"type": ["string", "array", "null"]},
    },
    required=["doc_id", "selector", "selector_value"],
)


# ``gdocs_delete_named_range`` echoes which marker was removed (same
# selector/scope shape as replace, minus text_length).
GDOCS_DELETE_NAMED_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "selector": {"type": "string"},
        "selector_value": {"type": "string"},
        "scope": {"type": ["string", "array", "null"]},
    },
    required=["doc_id", "selector", "selector_value"],
)


# ``gdocs_insert_page_break`` echoes where the break landed.
# ``location_mode`` is "end_of_segment" (index omitted) or "index".
GDOCS_INSERT_PAGE_BREAK_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "location_mode": {"type": "string"},
        "index": {"type": ["integer", "null"]},
        "tab_id": {"type": ["string", "null"]},
    },
    required=["doc_id", "location_mode"],
)


# ``gdocs_insert_image`` echoes the inserted image's stable objectId
# (parsed from the insertInlineImage reply; may be null if Docs omits
# it) plus the request echo.
GDOCS_INSERT_IMAGE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "image_object_id": {"type": ["string", "null"]},
        "index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
        "uri": {"type": "string"},
    },
    required=["doc_id", "image_object_id", "index", "uri"],
)


# ``gdocs_list_comments`` returns the Drive comment resources (with
# nested replies) plus the next-page token.
GDOCS_LIST_COMMENTS_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "comments": {"type": "array"},
        "next_page_token": {"type": ["string", "null"]},
    },
    required=["doc_id", "comments"],
)


# ``gdocs_create_comment`` returns the created Drive comment resource.
GDOCS_CREATE_COMMENT_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "comment": {"type": "object"},
    },
    required=["doc_id", "comment"],
)


# ``gdocs_reply_to_comment`` returns the created Drive reply resource.
GDOCS_REPLY_TO_COMMENT_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "comment_id": {"type": "string"},
        "reply": {"type": "object"},
    },
    required=["doc_id", "comment_id", "reply"],
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


# ``gdocs_edit_range`` echoes the edited range + what happened: a
# deleteContentRange (always) and an optional insertText. ``inserted_units``
# is the UTF-16 code-unit length of any inserted text (0 for a pure delete).
GDOCS_EDIT_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "doc_id": {"type": "string"},
        "start_index": {"type": "integer", "minimum": 1},
        "end_index": {"type": "integer", "minimum": 1},
        "tab_id": {"type": ["string", "null"]},
        "deleted": {"type": "boolean"},
        "inserted": {"type": "boolean"},
        "inserted_units": {"type": "integer", "minimum": 0},
    },
    required=["doc_id", "start_index", "end_index", "deleted", "inserted"],
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


# BUG 2b (2026-07-10) — gdrive_rename_file. Success echoes the file's
# new + previous names; soft-failures (not_found / app_not_authorized)
# return {file_id, reason, message} as data, matching the trash/untrash
# convention. ``file_id`` is the one key present on every shape.
GDRIVE_RENAME_FILE_OUTPUT_SCHEMA = _object(
    properties={
        "file_id": {"type": "string"},
        "name": {"type": "string"},
        "previous_name": {"type": "string"},
        "mimeType": {"type": "string"},
        "reason": {"type": "string"},
        "message": {"type": "string"},
    },
    required=["file_id"],
)


# Template-fill (Wave 5 S1). ``gdrive_copy_file`` returns the NEW file's
# id + name + Drive webViewLink (``name`` nullable defensively; the copy
# always has one, but the field is copied through from the API reply).
GDRIVE_COPY_FILE_OUTPUT_SCHEMA = _object(
    properties={
        "file_id": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "url": {"type": "string"},
    },
    required=["file_id", "url"],
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


# ``gsheets_batch_read`` reads N disjoint ranges in one values.batchGet.
# ``value_ranges`` is a list of ``{range, values}`` dicts, one per
# requested range in order (same 2D row-major ``values`` shape as
# gsheets_read_range).
GSHEETS_BATCH_READ_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "value_ranges": {"type": "array"},
    },
    required=["spreadsheet_id", "value_ranges"],
)


# ``gsheets_batch_write`` writes N disjoint ranges in one
# values.batchUpdate. ``responses`` is a list of ``{updated_range,
# updated_cells}`` dicts (one per written range); the totals aggregate the
# whole batch.
GSHEETS_BATCH_WRITE_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_updated_cells": {"type": "integer", "minimum": 0},
        "total_updated_ranges": {"type": "integer", "minimum": 0},
        "responses": {"type": "array"},
    },
    required=[
        "spreadsheet_id",
        "total_updated_cells",
        "total_updated_ranges",
        "responses",
    ],
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


# ``gsheets_clear_range`` echoes the A1 range Sheets reports it cleared
# (values.clear — formatting left intact).
GSHEETS_CLEAR_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "cleared_range": {"type": "string"},
    },
    required=["spreadsheet_id", "cleared_range"],
)


# ``gsheets_duplicate_sheet`` returns the gid Sheets assigned the copy.
# ``title`` / ``index`` may be absent if Sheets omits them from the reply
# (hence not required), mirroring gsheets_add_sheet.
GSHEETS_DUPLICATE_SHEET_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "sheet_id": {"type": ["integer", "null"]},
        "title": {"type": ["string", "null"]},
        "index": {"type": ["integer", "null"], "minimum": 0},
    },
    required=["spreadsheet_id", "sheet_id"],
)


# ``gsheets_freeze`` returns the flat batch_update envelope — one
# updateSheetProperties request setting gridProperties.frozen*Count.
GSHEETS_FREEZE_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


# ``gsheets_protect_range`` returns the same flat batch_update envelope —
# one addProtectedRange request.
GSHEETS_PROTECT_RANGE_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


# ``gsheets_insert_dimension`` / ``gsheets_delete_dimension`` /
# ``gsheets_merge_cells`` / ``gsheets_set_data_validation`` all return the
# flat batch_update envelope (one request each). Shared shape — declared
# once and reused for the four (matches gsheets_freeze / protect_range).
GSHEETS_INSERT_DIMENSION_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


GSHEETS_DELETE_DIMENSION_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


GSHEETS_MERGE_CELLS_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


GSHEETS_SET_DATA_VALIDATION_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "total_requests", "replies"],
)


# ``gsheets_add_chart`` returns the flat batch_update envelope PLUS the
# Sheets-assigned chart_id (the gid of the new embedded chart, parsed
# from the addChart reply; may be null if Sheets omits it).
GSHEETS_ADD_CHART_OUTPUT_SCHEMA = _object(
    properties={
        "spreadsheet_id": {"type": "string"},
        "chart_id": {"type": ["integer", "null"]},
        "total_requests": {"type": "integer", "minimum": 0},
        "replies": {"type": "array"},
    },
    required=["spreadsheet_id", "chart_id", "total_requests", "replies"],
)


# ---------------------------------------------------------------------
# Calendar (services/calendar/) — v2.4.0 (4th new service)
#
# Scope: https://www.googleapis.com/auth/calendar (SENSITIVE, not
# restricted → no CASA). Event + availability surface over Calendar v3.
# ---------------------------------------------------------------------


# ``gcal_list_events`` returns the raw v3 Event list + the page token.
# ``next_page_token`` is null on the last page (hence not required).
GCAL_LIST_EVENTS_OUTPUT_SCHEMA = _object(
    properties={
        "calendar_id": {"type": "string"},
        "events": {"type": "array"},
        "next_page_token": {"type": ["string", "null"]},
    },
    required=["calendar_id", "events"],
)


# ``gcal_get_event`` returns the single raw v3 Event resource.
GCAL_GET_EVENT_OUTPUT_SCHEMA = _object(
    properties={
        "calendar_id": {"type": "string"},
        "event": {"type": "object"},
    },
    required=["calendar_id", "event"],
)


# ``gcal_create_event`` echoes the new event's id + web link + summary.
# ``html_link`` may be absent in a degenerate API reply (hence not
# required); ``event_id`` is the load-bearing handle for follow-ups.
GCAL_CREATE_EVENT_OUTPUT_SCHEMA = _object(
    properties={
        "calendar_id": {"type": "string"},
        "event_id": {"type": "string"},
        "html_link": {"type": ["string", "null"], "format": "uri"},
        "summary": {"type": "string"},
    },
    required=["calendar_id", "event_id", "summary"],
)


# ``gcal_update_event`` echoes the patched event's id + link + summary.
# ``html_link`` / ``summary`` may be null if the reply omits them.
GCAL_UPDATE_EVENT_OUTPUT_SCHEMA = _object(
    properties={
        "calendar_id": {"type": "string"},
        "event_id": {"type": "string"},
        "html_link": {"type": ["string", "null"], "format": "uri"},
        "summary": {"type": ["string", "null"]},
    },
    required=["calendar_id", "event_id"],
)


# ``gcal_delete_event`` echoes the removed event's id (delete returns an
# empty body on success).
GCAL_DELETE_EVENT_OUTPUT_SCHEMA = _object(
    properties={
        "calendar_id": {"type": "string"},
        "deleted_event_id": {"type": "string"},
    },
    required=["calendar_id", "deleted_event_id"],
)


# ``gcal_list_calendars`` returns the flattened calendar list + page token.
GCAL_LIST_CALENDARS_OUTPUT_SCHEMA = _object(
    properties={
        "calendars": {"type": "array"},
        "next_page_token": {"type": ["string", "null"]},
    },
    required=["calendars"],
)


# ``gcal_freebusy`` echoes the window + Calendar's per-calendar busy map.
GCAL_FREEBUSY_OUTPUT_SCHEMA = _object(
    properties={
        "time_min": {"type": "string"},
        "time_max": {"type": "string"},
        "calendars": {"type": "object"},
    },
    required=["time_min", "time_max", "calendars"],
)


# ---------------------------------------------------------------------
# Slides (services/slides/) — v2.3.2 minimal start (3rd new service)
# ---------------------------------------------------------------------


# One entry in ``gslides_get_outline``'s ``slides`` array. Pins the
# load-bearing per-slide keys: stable object_id, 0-based index, layout
# objectId, flattened text, the page-element inventory, and the
# speaker-notes text. ``elements`` entries are ``{object_id, type}``.
_SLIDE_OUTLINE_ENTRY_SCHEMA = _object(
    properties={
        "object_id": {"type": "string"},
        "index": {"type": "integer", "minimum": 0},
        "layout": {"type": "string"},
        "text": {"type": "string"},
        "elements": {
            "type": "array",
            "items": _object(
                properties={
                    "object_id": {"type": "string"},
                    "type": {"type": "string"},
                },
                required=["object_id", "type"],
            ),
        },
        "notes": {"type": "string"},
    },
    required=["object_id", "index", "layout", "text", "elements", "notes"],
)


GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "title": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "slides": {"type": "array", "items": _SLIDE_OUTLINE_ENTRY_SCHEMA},
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


# ``gslides_create_shape`` inserts a shape (rectangle / ellipse / text
# box / …) onto a slide; echoes the resolved shape_type + the shape
# element's stable objectId. Completes the #155 geometry trio alongside
# create_table + create_line.
GSLIDES_CREATE_SHAPE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "shape_object_id": {"type": "string"},
        "shape_type": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
    },
    required=[
        "presentation_id",
        "slide_object_id",
        "shape_object_id",
        "shape_type",
        "url",
    ],
)


# ``gslides_create_line`` draws a line (start → end) on a slide; echoes
# the resolved line_category + the line element's stable objectId.
GSLIDES_CREATE_LINE_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "line_object_id": {"type": "string"},
        "line_category": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
    },
    required=[
        "presentation_id",
        "slide_object_id",
        "line_object_id",
        "line_category",
        "url",
    ],
)


# ``gslides_set_speaker_notes`` replaces a slide's speaker notes; echoes
# the resolved notes-shape objectId + the notes text that was set.
GSLIDES_SET_SPEAKER_NOTES_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "slide_object_id": {"type": "string"},
        "speaker_notes_object_id": {"type": "string"},
        "notes_text": {"type": "string"},
    },
    required=[
        "presentation_id",
        "slide_object_id",
        "speaker_notes_object_id",
        "notes_text",
    ],
)


# Wave 4 (S1) element-management verbs. ``gslides_delete_object`` removes
# a page element or slide by objectId; echoes the objectId that was
# deleted.
GSLIDES_DELETE_OBJECT_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "deleted_object_id": {"type": "string"},
    },
    required=["presentation_id", "deleted_object_id"],
)


# ``gslides_duplicate_object`` copies a page element or slide; returns the
# duplicate's objectId plus the ``{source: new}`` id map Slides returned.
GSLIDES_DUPLICATE_OBJECT_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "source_object_id": {"type": "string"},
        "new_object_id": {"type": "string"},
        "id_map": {"type": "object"},
    },
    required=[
        "presentation_id",
        "source_object_id",
        "new_object_id",
        "id_map",
    ],
)


# ``gslides_update_element_transform`` sets or composes a page element's
# affine transform; echoes the resolved applyMode + the exact matrix sent.
GSLIDES_UPDATE_ELEMENT_TRANSFORM_OUTPUT_SCHEMA = _object(
    properties={
        "presentation_id": {"type": "string"},
        "object_id": {"type": "string"},
        "apply_mode": {"type": "string"},
        "transform": {"type": "object"},
    },
    required=[
        "presentation_id",
        "object_id",
        "apply_mode",
        "transform",
    ],
)


# ---------------------------------------------------------------------
# Forms (services/forms/) — new service (sensitive scopes, no CASA)
# ---------------------------------------------------------------------


# ``gforms_create_form`` creates a form (title + optional description) and
# returns its id + responder URL.
GFORMS_CREATE_FORM_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "description": {"type": "string"},
    },
    required=["form_id", "url", "title"],
)


# Shared sub-schema: one entry in the ``items`` array returned by
# ``gforms_get_form``. ``type`` is a coarse item kind.
_FORMS_ITEM_ENTRY_SCHEMA = _object(
    properties={
        "item_id": {"type": "string"},
        "title": {"type": "string"},
        "type": {"type": "string"},
    },
    required=["item_id", "type"],
)


# ``gforms_get_form`` reads a form's structure (title/description + items).
GFORMS_GET_FORM_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "url": {"type": "string", "format": "uri"},
        "items": {"type": "array", "items": _FORMS_ITEM_ENTRY_SCHEMA},
    },
    required=["form_id", "title", "url", "items"],
)


# ``gforms_add_question`` adds a question; echoes the new item's id +
# question_type + position.
GFORMS_ADD_QUESTION_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "item_id": {"type": "string"},
        "question_type": {"type": "string"},
        "index": {"type": "integer", "minimum": 0},
    },
    required=["form_id", "item_id", "question_type", "index"],
)


# ``gforms_update_item`` updates an item's title/description; echoes the
# position + the fields that changed.
GFORMS_UPDATE_ITEM_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "index": {"type": "integer", "minimum": 0},
        "updated_fields": {"type": "array", "items": {"type": "string"}},
    },
    required=["form_id", "index", "updated_fields"],
)


# ``gforms_delete_item`` deletes an item by position; echoes the position.
GFORMS_DELETE_ITEM_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "deleted_index": {"type": "integer", "minimum": 0},
    },
    required=["form_id", "deleted_index"],
)


# Shared sub-schema: one entry in the ``responses`` array returned by
# ``gforms_list_responses`` (and the body of ``gforms_get_response``).
_FORMS_RESPONSE_ENTRY_SCHEMA = _object(
    properties={
        "response_id": {"type": "string"},
        "create_time": {"type": "string"},
        "last_submitted_time": {"type": "string"},
        "answers": {"type": "object"},
    },
    required=["response_id", "answers"],
)


# ``gforms_list_responses`` lists submitted responses (paginated).
GFORMS_LIST_RESPONSES_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "responses": {
            "type": "array", "items": _FORMS_RESPONSE_ENTRY_SCHEMA,
        },
        "next_page_token": {"type": "string"},
    },
    required=["form_id", "responses", "next_page_token"],
)


# ``gforms_get_response`` reads one submitted response.
GFORMS_GET_RESPONSE_OUTPUT_SCHEMA = _object(
    properties={
        "form_id": {"type": "string"},
        "response_id": {"type": "string"},
        "create_time": {"type": "string"},
        "last_submitted_time": {"type": "string"},
        "answers": {"type": "object"},
    },
    required=["form_id", "response_id", "answers"],
)


# ---------------------------------------------------------------------
# Tasks (services/tasks/) — Google Tasks API v1 (sensitive scope, no CASA)
# ---------------------------------------------------------------------


# Shared sub-schema: one entry in the ``tasklists`` array. Pins the
# load-bearing id + title; ``updated`` (RFC 3339) may be absent.
_TASKLIST_ENTRY_SCHEMA = _object(
    properties={
        "id": {"type": "string"},
        "title": {"type": "string"},
        "updated": {"type": ["string", "null"]},
    },
    required=["id", "title"],
)


# Shared sub-schema: one entry in the ``tasks`` array. ``id`` / ``title``
# / ``status`` are the load-bearing fields; notes / due / completed /
# parent / position are present only on some tasks (sub-tasks, dated /
# completed tasks), hence nullable + not required.
_TASK_ENTRY_SCHEMA = _object(
    properties={
        "id": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
        "due": {"type": ["string", "null"]},
        "completed": {"type": ["string", "null"]},
        "parent": {"type": ["string", "null"]},
        "position": {"type": ["string", "null"]},
        "updated": {"type": ["string", "null"]},
    },
    required=["id", "title"],
)


# ``gtasks_list_tasklists`` returns a flat list of the user's task lists.
GTASKS_LIST_TASKLISTS_OUTPUT_SCHEMA = _object(
    properties={
        "tasklists": {"type": "array", "items": _TASKLIST_ENTRY_SCHEMA},
    },
    required=["tasklists"],
)


# ``gtasks_create_tasklist`` echoes the created list (server-assigned id).
GTASKS_CREATE_TASKLIST_OUTPUT_SCHEMA = _object(
    properties={
        "id": {"type": "string"},
        "title": {"type": "string"},
        "updated": {"type": ["string", "null"]},
    },
    required=["id", "title"],
)


# ``gtasks_list_tasks`` returns the tasks in a list + echoes the queried
# tasklist id so the caller can correlate.
GTASKS_LIST_TASKS_OUTPUT_SCHEMA = _object(
    properties={
        "tasklist": {"type": "string"},
        "tasks": {"type": "array", "items": _TASK_ENTRY_SCHEMA},
    },
    required=["tasklist", "tasks"],
)


# ``gtasks_create_task`` / ``gtasks_update_task`` / ``gtasks_complete_task``
# all return a single task as the flat envelope (same shape as a
# ``gtasks_list_tasks`` entry).
GTASKS_CREATE_TASK_OUTPUT_SCHEMA = _TASK_ENTRY_SCHEMA
GTASKS_UPDATE_TASK_OUTPUT_SCHEMA = _TASK_ENTRY_SCHEMA
GTASKS_COMPLETE_TASK_OUTPUT_SCHEMA = _TASK_ENTRY_SCHEMA


# ``gtasks_delete_task`` echoes what was removed (tasks.delete returns an
# empty 204 body, so there's nothing else to surface).
GTASKS_DELETE_TASK_OUTPUT_SCHEMA = _object(
    properties={
        "tasklist": {"type": "string"},
        "deleted_task_id": {"type": "string"},
    },
    required=["tasklist", "deleted_task_id"],
)


# ---------------------------------------------------------------------
# Contacts (services/contacts/) — People API v1 (new service)
# ---------------------------------------------------------------------
#
# Every read tool returns the flat ``_simplify_person`` projection. The
# load-bearing key across all contact shapes is ``resource_name`` (the
# handle the get/update/delete tools consume); ``etag`` drives the update
# read-modify-write. ``emails`` / ``phones`` are arrays (a contact can
# have several); ``display_name`` / ``organization`` / ``etag`` are
# nullable (a contact may have no name / org, and a freshly-parsed
# response may omit the etag if the mask was narrowed). ``raw`` is the
# untouched People API Person. additionalProperties stays True (the
# _object default) so a future projected field is additive.


# Shared single-contact projection (gcontacts_get + the per-item shape in
# gcontacts_list / gcontacts_search). resource_name is the only field
# guaranteed across every contact (a contact always has a resourceName).
_CONTACT_ENTRY_SCHEMA = _object(
    properties={
        "resource_name": {"type": "string"},
        "etag": {"type": ["string", "null"]},
        "display_name": {"type": ["string", "null"]},
        "emails": {"type": "array", "items": {"type": "string"}},
        "phones": {"type": "array", "items": {"type": "string"}},
        "organization": {"type": ["string", "null"]},
        "raw": {"type": "object"},
    },
    required=["resource_name"],
)


# ``gcontacts_list`` — one page of contacts + the next-page token.
# next_page_token / total_people are nullable (null on the last page /
# when the People API omits the count).
GCONTACTS_LIST_OUTPUT_SCHEMA = _object(
    properties={
        "contacts": {"type": "array", "items": _CONTACT_ENTRY_SCHEMA},
        "next_page_token": {"type": ["string", "null"]},
        "total_people": {"type": ["integer", "null"], "minimum": 0},
    },
    required=["contacts"],
)


# ``gcontacts_search`` — prefix-match hits (searchContacts does not
# paginate, so there is no next-page token — just the count).
GCONTACTS_SEARCH_OUTPUT_SCHEMA = _object(
    properties={
        "contacts": {"type": "array", "items": _CONTACT_ENTRY_SCHEMA},
        "count": {"type": "integer", "minimum": 0},
    },
    required=["contacts", "count"],
)


# ``gcontacts_get`` — a single contact (the flat projection).
GCONTACTS_GET_OUTPUT_SCHEMA = _CONTACT_ENTRY_SCHEMA


# ``gcontacts_create`` — the newly created contact (same projection;
# carries its new resource_name + etag).
GCONTACTS_CREATE_OUTPUT_SCHEMA = _CONTACT_ENTRY_SCHEMA


# ``gcontacts_update`` — the updated contact (same projection; etag is
# the NEW post-update value).
GCONTACTS_UPDATE_OUTPUT_SCHEMA = _CONTACT_ENTRY_SCHEMA


# ``gcontacts_delete`` — echoes the removed contact's resourceName.
GCONTACTS_DELETE_OUTPUT_SCHEMA = _object(
    properties={
        "resource_name": {"type": "string"},
        "deleted": {"type": "boolean"},
    },
    required=["resource_name", "deleted"],
)


# ``gcontacts_list_other_contacts`` — one page of the auto-saved "other
# contacts" (People API otherContacts.list). Same flat _CONTACT_ENTRY
# projection as the regular contacts reads; next_page_token is null on the
# last page. otherContacts has no total count, so there's no total_people.
GCONTACTS_LIST_OTHER_CONTACTS_OUTPUT_SCHEMA = _object(
    properties={
        "contacts": {"type": "array", "items": _CONTACT_ENTRY_SCHEMA},
        "next_page_token": {"type": ["string", "null"]},
    },
    required=["contacts"],
)


# ---------------------------------------------------------------------
# Gmail (services/gmail/) — Gmail API v1 (send + labels; CASA-free)
# ---------------------------------------------------------------------
#
# Two scopes, four tools. ``gmail_send_message`` uses gmail.send
# (SENSITIVE, no CASA); the three label tools use gmail.labels
# (NON-sensitive). additionalProperties stays True (the _object default)
# so a future field is additive.


# ``gmail_send_message`` — the sent message's identifiers. ``id`` is the
# load-bearing field (the message id Gmail assigned); ``thread_id`` is the
# conversation; ``label_ids`` are the labels Gmail attached (usually
# ["SENT"]).
GMAIL_SEND_MESSAGE_OUTPUT_SCHEMA = _object(
    properties={
        "id": {"type": "string"},
        "thread_id": {"type": ["string", "null"]},
        "label_ids": {"type": "array", "items": {"type": "string"}},
    },
    required=["id"],
)


# Shared single-label projection (gmail_create_label + the per-item shape
# in gmail_list_labels). ``id`` is the only field guaranteed across every
# label (a label always has an id); ``type`` is "system" or "user".
_GMAIL_LABEL_ENTRY_SCHEMA = _object(
    properties={
        "id": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "type": {"type": ["string", "null"]},
        "message_list_visibility": {"type": ["string", "null"]},
        "label_list_visibility": {"type": ["string", "null"]},
    },
    required=["id"],
)


# ``gmail_create_label`` — the created label (the flat projection).
GMAIL_CREATE_LABEL_OUTPUT_SCHEMA = _GMAIL_LABEL_ENTRY_SCHEMA


# ``gmail_list_labels`` — all labels (system + user) + the count.
# users.labels.list does not paginate, so there's no next-page token.
GMAIL_LIST_LABELS_OUTPUT_SCHEMA = _object(
    properties={
        "labels": {"type": "array", "items": _GMAIL_LABEL_ENTRY_SCHEMA},
        "count": {"type": "integer", "minimum": 0},
    },
    required=["labels", "count"],
)


# ``gmail_delete_label`` — echoes the removed label id.
GMAIL_DELETE_LABEL_OUTPUT_SCHEMA = _object(
    properties={
        "label_id": {"type": "string"},
        "deleted": {"type": "boolean"},
    },
    required=["label_id", "deleted"],
)


# ---------------------------------------------------------------------
# Apps Script — web-app deploy (ROADMAP 59)
# ---------------------------------------------------------------------


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
        # Present only for an ANYONE_ANONYMOUS deploy: the per-deploy HMAC
        # signing key auto-injected into the public endpoint's doPost guard,
        # plus the header scheme the caller must use. (v2.0c.)
        "hmac_key": {"type": "string"},
        "hmac_instructions": {"type": "string"},
        # Activation UX (Stream 3 / gap #7; prior fixed for N-S3V-1). Present
        # ONLY for an ANYONE_ANONYMOUS deploy, which is probed post-deploy.
        # ``status`` is "ready" (positively reachable) | "needs_activation"
        # (the PRIOR - the consent door, or a probe still inconclusive after
        # the settle retries); the four unified activation_* fields ride along
        # when status == "needs_activation". additionalProperties stays True
        # so this is additive.
        "status": {
            "type": "string",
            "enum": ["ready", "needs_activation"],
        },
        "activation_required": {"type": "boolean"},
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
        "activation_instructions": {"type": "string"},
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


# One execution-process entry in ``as_list_script_processes``. The Apps
# Script API ``Process`` resource carries the function/type/status plus
# timing; we surface the load-bearing fields flat. ``project_name`` is the
# script's title; the timing fields are RFC3339/duration strings when the
# API supplies them (nullable otherwise). additionalProperties stays True.
_AS_PROCESS_ENTRY_SCHEMA = _object(
    properties={
        "project_name": {"type": ["string", "null"]},
        "function_name": {"type": ["string", "null"]},
        "process_type": {"type": ["string", "null"]},
        "process_status": {"type": ["string", "null"]},
        "start_time": {"type": ["string", "null"]},
        "duration": {"type": ["string", "null"]},
        "user_access_level": {"type": ["string", "null"]},
    },
    required=[],
)


# ``as_list_script_processes`` returns one page of a script project's
# execution history (Apps Script API processes.list /
# processes.listScriptProcesses). ``processes`` is the flat list;
# ``next_page_token`` is null on the last page; ``script_id`` echoes the
# queried project. additionalProperties stays True (the _object default).
AS_LIST_SCRIPT_PROCESSES_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": ["string", "null"]},
        "processes": {"type": "array", "items": _AS_PROCESS_ENTRY_SCHEMA},
        "next_page_token": {"type": ["string", "null"]},
        "count": {"type": "integer", "minimum": 0},
    },
    required=["processes", "count"],
)


# ``as_check_activation`` (Stream 3) answers "is this deployed automation
# live yet?" via one of two methods, auto-selected by whether ``exec_url``
# is supplied. ``webapp_probe`` GETs a web app's /exec and reads the health
# verdict; ``process_history`` reads the project's execution history for the
# activation function. ``activated`` is tri-state: True (evidence it is
# live) / False (evidence it is not yet) / null (indeterminate - a run is in
# progress, or the probe was inconclusive). additionalProperties stays True
# (the _object default) so a future field is additive.
AS_CHECK_ACTIVATION_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": ["string", "null"]},
        "activated": {"type": ["boolean", "null"]},
        "method": {
            "type": "string",
            "enum": ["webapp_probe", "process_history"],
        },
        "activation_function": {"type": ["string", "null"]},
        "activation_url": {"type": ["string", "null"], "format": "uri"},
        # webapp_probe only: "serving" | "needs_activation" | "gone" |
        # "unknown" (mirrors the WebAppHealth verdict).
        "exec_state": {"type": ["string", "null"]},
        # process_history only: the matched activation run's status + when.
        "last_status": {"type": ["string", "null"]},
        "last_run_time": {"type": ["string", "null"]},
        "matched_processes": {"type": "array"},
        "message": {"type": "string"},
    },
    required=["activated", "method", "message"],
)


# One entry in the ``automations`` array returned by
# ``as_list_installed_automations`` (the forward-only lifecycle inventory).
# ``script_id`` + ``tool`` are the load-bearing keys (the id the other
# lifecycle/observability tools take, and which installer minted it);
# ``container_id`` is null for a standalone web app. additionalProperties
# stays True so a future field is additive.
_AS_AUTOMATION_ENTRY_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "tool": {"type": "string"},
        "container_id": {"type": ["string", "null"]},
        "container_kind": {"type": ["string", "null"]},
        "deployment_id": {"type": ["string", "null"]},
        "project_url": {"type": ["string", "null"]},
        "exec_url": {"type": ["string", "null"]},
        "content_hash": {"type": ["string", "null"]},
        "created_at": {"type": ["integer", "null"]},
        "activation_model": {"type": "string"},
        "handler_functions": {"type": "array", "items": {"type": "string"}},
    },
    required=["script_id", "tool"],
)


# ``as_list_installed_automations`` — the ledger-backed inventory. Minted
# Apps Script projects are invisible to drive.file (S0-1), so this is the
# only discovery surface; it is forward-only (nothing backfills it).
AS_LIST_INSTALLED_AUTOMATIONS_OUTPUT_SCHEMA = _object(
    properties={
        "automations": {
            "type": "array", "items": _AS_AUTOMATION_ENTRY_SCHEMA,
        },
        "count": {"type": "integer", "minimum": 0},
    },
    required=["automations", "count"],
)


# ``as_uninstall_automation`` — undeploy + disarm + ledger-forget. HONESTLY
# PARTIAL (S0-4): the project FILE always lingers (no projects.delete;
# drive.file cannot trash a script project), so ``project_file_removed`` is
# always False and ``message`` states what remains. ``status`` is
# ``uninstalled`` normally or ``already_gone`` if the project was deleted
# already. additionalProperties stays True (a ``note`` field appears when an
# unrecorded id is uninstalled).
AS_UNINSTALL_AUTOMATION_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "status": {"type": "string"},
        "undeployed_count": {"type": "integer", "minimum": 0},
        "undeploy_errors": {"type": "array", "items": {"type": "string"}},
        "content_disarmed": {"type": "boolean"},
        "ledger_forgotten": {"type": "boolean"},
        "project_file_removed": {"type": "boolean"},
        "project_url": {"type": "string", "format": "uri"},
        "message": {"type": "string"},
    },
    required=["script_id", "status"],
)


# ``as_update_automation`` — re-push current codegen to the EXISTING project
# (consent-preserving). ``status`` is ``updated`` or ``unchanged``.
# ``needs_reactivation`` is True only when the new manifest ADDS an OAuth
# scope the deployed version lacked; the activation fields
# (``activation_required`` / ``activation_function`` / ``activation_url`` /
# ``activation_instructions``, from the shared activation contract) are then
# present too. additionalProperties stays True (the _object default).
AS_UPDATE_AUTOMATION_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "status": {"type": "string"},
        "content_hash_before": {"type": ["string", "null"]},
        "content_hash_after": {"type": "string"},
        "deployment_id": {"type": ["string", "null"]},
        "needs_reactivation": {"type": "boolean"},
        "added_scopes": {"type": "array", "items": {"type": "string"}},
        # True when the server regenerated the .gs + manifest from the recorded
        # recipe (script_body omitted on a recipe automation); False when the
        # caller-supplied script_body was pushed (a raw as_generate_bound_script
        # automation, or an explicit body on a recipe one). Stream S5.
        "regenerated_from_recipe": {"type": "boolean"},
        "message": {"type": "string"},
        # Present only when needs_reactivation is True (shared activation shape).
        "activation_required": {"type": "boolean"},
        "activation_function": {"type": ["string", "null"]},
        "activation_url": {"type": ["string", "null"], "format": "uri"},
        "activation_instructions": {"type": ["string", "null"]},
    },
    required=[
        "script_id", "status", "content_hash_after", "needs_reactivation",
        "regenerated_from_recipe",
    ],
)


# ``as_list_recipes`` - the read-only install catalog projected from the
# internal recipe registry (services/apps_script/_recipes.py). Pure-local (no
# Google API). Each entry names the typed installer tool to CALL
# (``installer_tool`` == ``name`` today) so a caller can list then install;
# ``params`` is a per-arg summary (the installer tool carries the full typed
# input schema). ``activation_models`` is a legend: each model present in the
# catalog mapped to one honest line about activation. additionalProperties
# stays True (the _object default) so a future per-entry field is additive.
_AS_RECIPE_PARAM_SCHEMA = _object(
    properties={
        "name": {"type": "string"},
        "type": {"type": "string"},
        "required": {"type": "boolean"},
    },
    required=["name", "required"],
)

_AS_RECIPE_ENTRY_SCHEMA = _object(
    properties={
        "name": {"type": "string"},
        "installer_tool": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "version": {"type": "string"},
        "container_kind": {"type": "string"},
        "activation_model": {"type": "string"},
        "params": {"type": "array", "items": _AS_RECIPE_PARAM_SCHEMA},
    },
    required=[
        "name",
        "installer_tool",
        "title",
        "summary",
        "version",
        "container_kind",
        "activation_model",
        "params",
    ],
)

AS_LIST_RECIPES_OUTPUT_SCHEMA = _object(
    properties={
        "recipes": {"type": "array", "items": _AS_RECIPE_ENTRY_SCHEMA},
        "count": {"type": "integer", "minimum": 0},
        "activation_models": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
    required=["recipes", "count", "activation_models"],
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
        # Unified activation contract (Stream 3): the editor deep link +
        # the exact function to run. trigger_active stays as the legacy
        # alias. See appscriptly/activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
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
        "activation_url",
        "activation_function",
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


# ``as_install_edit_trigger`` (ROADMAP_SPECS #8) composes the bound-script
# generator into a reactive onEdit automation for a Sheet. Returns the
# bound project's IDs + the Sheet it bound to + the trigger TYPE ("onEdit")
# + the parsed handler name + a deep-link, PLUS the honest
# trigger-activation state (same shape as as_install_sheet_dashboard:
# an installable trigger only exists once installTrigger runs, and deploy
# doesn't run it). additionalProperties stays True (the _object default).
AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "trigger_type": {"type": "string", "enum": ["onEdit"]},
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
        # Unified activation contract (Stream 3). See activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "sheet_id",
        "trigger_type",
        "trigger_handler",
        "project_url",
        "trigger_active",
        "activation_required",
        "activation_instructions",
        "activation_url",
        "activation_function",
    ],
)


# ``as_install_form_handler`` (ROADMAP_SPECS #8) composes the bound-script
# generator into a reactive onFormSubmit automation for a Form — the ONE
# reactive surface a Form has (the generic primitive otherwise rejects
# Forms; this purpose-built path lifts that). Returns the bound project's
# IDs + the Form it bound to + the trigger TYPE ("onFormSubmit") + the
# parsed handler name + a deep-link, PLUS the honest trigger-activation
# state. additionalProperties stays True (the _object default).
AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "form_id": {"type": "string"},
        "trigger_type": {"type": "string", "enum": ["onFormSubmit"]},
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
        # Unified activation contract (Stream 3). See activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "form_id",
        "trigger_type",
        "trigger_handler",
        "project_url",
        "trigger_active",
        "activation_required",
        "activation_instructions",
        "activation_url",
        "activation_function",
    ],
)


# ``as_install_sheet_menu`` (service-parity) is the Sheets analogue of
# ``as_install_doc_menu`` — it composes the bound-script generator into a
# "install a custom menu into a Sheet" feature via an onOpen builder that
# calls ``SpreadsheetApp.getUi()``. Returns the bound project's IDs + the
# Sheet it bound to + the installed menu's title and item count (echoed so
# the caller can confirm what was wired), plus the script-editor deep link.
# additionalProperties stays True (the _object default) so a future field
# is additive. Same shape as AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA with
# ``sheet_id`` in place of ``doc_id``.
AS_INSTALL_SHEET_MENU_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "menu_title": {"type": "string"},
        "item_count": {"type": "integer", "minimum": 1},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "sheet_id",
        "menu_title",
        "item_count",
        "project_url",
    ],
)


# ``as_install_slides_menu`` (service-parity) is the Slides analogue of
# ``as_install_doc_menu`` — it composes the bound-script generator into a
# "install a custom menu into a presentation" feature via an onOpen builder
# that calls ``SlidesApp.getUi()``. Returns the bound project's IDs + the
# presentation it bound to + the installed menu's title and item count
# (echoed so the caller can confirm what was wired), plus the script-editor
# deep link. additionalProperties stays True (the _object default) so a
# future field is additive. Same shape as AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA
# with ``presentation_id`` in place of ``doc_id``.
AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "presentation_id": {"type": "string"},
        "menu_title": {"type": "string"},
        "item_count": {"type": "integer", "minimum": 1},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "presentation_id",
        "menu_title",
        "item_count",
        "project_url",
    ],
)


# ``as_refresh_linked_slides`` (service-parity) composes the bound-script
# generator into a "refresh linked slides" feature for Slides: a bound
# script whose ``refreshLinkedSlides()`` walks ``getSlides()`` and calls
# ``slide.refreshSlide()`` on each slide that is LINKED to a source deck
# (master-deck → client-deck sync the REST API cannot do). A custom menu
# item makes it one-click. Like the video-deck render half this is an
# ON-DEMAND action, not a persistent trigger: the deploy WIRES the function
# but the refresh only happens when the function runs, so ``run_required``
# is True with ``run_instructions`` spelling out the one step.
# ``refreshed_count`` is nullable — the linked-slide count is only known
# once the function runs (the tool doesn't read the deck), so a successful
# deploy returns null here. additionalProperties stays True (the _object
# default) so a future field is additive.
AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "presentation_id": {"type": "string"},
        "refresh_function": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "refreshed_count": {"type": ["integer", "null"], "minimum": 0},
        "run_required": {"type": "boolean"},
        "run_instructions": {"type": "string"},
        # Unified activation contract (Stream 3): run_required /
        # run_instructions stay as the legacy aliases; these four carry the
        # canonical shape (an on-demand action, so activation = one run).
        # See appscriptly/activation.py.
        "activation_required": {"type": "boolean"},
        "activation_function": {"type": "string"},
        "activation_url": {"type": "string", "format": "uri"},
        "activation_instructions": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "presentation_id",
        "refresh_function",
        "project_url",
        "run_required",
        "run_instructions",
        "activation_required",
        "activation_function",
        "activation_url",
        "activation_instructions",
    ],
)


# ``as_grade_form_responses`` (service-parity) composes the bound-script
# generator into a "push computed grades onto submitted quiz responses"
# feature for Forms: a bound script whose ``gradeResponses()`` builds
# per-question grades (via ``FormResponse.withItemGrade`` /
# ``ItemResponse.setScore``) and calls ``FormApp.getActiveForm()
# .submitGrades(responses)``. This is the WRITE counterpart to the
# read-only response tools and is an ON-DEMAND action, not a trigger — the
# deploy WIRES the grader but grading only happens when ``gradeResponses``
# runs (its own one-time authorization for the full ``forms`` scope, which
# lives in the GENERATED manifest, NOT appscriptly's own consent). So
# ``run_required`` is True with ``run_instructions``. ``graded_count`` is
# nullable — the count is only known once the grader runs. The full
# ``forms`` scope is reported under ``manifest_scope`` for transparency (it
# is the bound script's scope, declared in the generated appsscript.json).
# additionalProperties stays True (the _object default) so a future field
# is additive.
AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "form_id": {"type": "string"},
        "grade_function": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "graded_count": {"type": ["integer", "null"], "minimum": 0},
        "run_required": {"type": "boolean"},
        "run_instructions": {"type": "string"},
        # Unified activation contract (Stream 3): run_required /
        # run_instructions stay as the legacy aliases; these four carry the
        # canonical shape (an on-demand action, so activation = one run).
        # See appscriptly/activation.py.
        "activation_required": {"type": "boolean"},
        "activation_function": {"type": "string"},
        "activation_url": {"type": "string", "format": "uri"},
        "activation_instructions": {"type": "string"},
        "manifest_scope": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "form_id",
        "grade_function",
        "project_url",
        "run_required",
        "run_instructions",
        "activation_required",
        "activation_function",
        "activation_url",
        "activation_instructions",
        "manifest_scope",
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
        # Unified activation contract (Stream 3): activation_note stays as
        # the legacy alias (it carries the extra batch/encode/token detail);
        # these four carry the canonical shape (activation = one renderFrames
        # run). See appscriptly/activation.py.
        "activation_required": {"type": "boolean"},
        "activation_function": {"type": "string"},
        "activation_url": {"type": "string", "format": "uri"},
        "activation_instructions": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
    },
    required=[
        "script_id",
        "deployment_id",
        "presentation_id",
        "frames_batch_id",
        "render_function",
        "activation_note",
        "activation_required",
        "activation_function",
        "activation_url",
        "activation_instructions",
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


# ``as_install_calendar_sync`` (GAS service-parity — Calendar) composes the
# bound-script generator into a TIME-DRIVEN "create/sync Calendar events
# from Sheet rows" automation. A bound script on a Sheet runs the caller's
# sync function on a schedule via an installable time trigger; the function
# uses ``CalendarApp`` to create/update events from the Sheet's rows — the
# kind of cross-surface (Sheet -> Calendar) automation the Calendar REST
# tools can't express. Same time-driven shape as
# AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA, plus ``manifest_scope`` (the
# ``calendar`` scope the GENERATED bound script declares — reported for
# transparency; it lives in the generated manifest, NOT appscriptly's own
# consent). The deploy WIRES the trigger but does NOT run installTrigger, so
# ``trigger_active`` is False / ``activation_required`` True with the
# one-step instruction. additionalProperties stays True (the _object
# default) so a future field is additive.
AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "schedule": {"type": "string", "enum": ["daily", "hourly", "weekly"]},
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
        # Unified activation contract (Stream 3). See activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
        "manifest_scope": {"type": "string"},
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
        "activation_url",
        "activation_function",
        "manifest_scope",
    ],
)


# ``as_install_task_rollover`` (GAS service-parity — Tasks) composes the
# bound-script generator into a TIME-DRIVEN Tasks automation. A bound script
# on a Sheet runs the caller's task function on a schedule via an
# installable time trigger; the function uses the Tasks ADVANCED service
# (``Tasks.Tasks``) to roll over incomplete tasks, create tasks from Sheet
# rows, etc. — recurring Tasks orchestration the Tasks REST tools (one-shot
# CRUD) don't express. Same time-driven shape as the calendar-sync schema,
# plus ``manifest_scope`` (the ``tasks`` scope the GENERATED bound script
# declares — in the generated manifest, NOT appscriptly's own consent). The
# deploy WIRES the trigger but does NOT run installTrigger, so
# ``trigger_active`` is False / ``activation_required`` True.
# additionalProperties stays True (the _object default) so a future field is
# additive.
AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "sheet_id": {"type": "string"},
        "schedule": {"type": "string", "enum": ["daily", "hourly", "weekly"]},
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
        # Unified activation contract (Stream 3). See activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
        "manifest_scope": {"type": "string"},
        # The Tasks advanced service must be enabled in the generated
        # script's manifest (dependencies.enabledAdvancedServices) for
        # ``Tasks.Tasks...`` to resolve — echoed so the caller knows the
        # generated manifest wired it.
        "advanced_service": {"type": "string"},
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
        "activation_url",
        "activation_function",
        "manifest_scope",
        "advanced_service",
    ],
)


# ``as_install_contact_sync`` (GAS service-parity — Contacts) composes the
# bound-script generator into a REACTIVE onFormSubmit automation that
# creates/updates a Google contact from each submission via ``ContactsApp``.
# Like as_install_form_handler it binds DIRECTLY to a Form (lifting the
# generic primitive's Forms rejection) and wires an installable
# ``onFormSubmit`` trigger; like as_grade_form_responses it lands a
# SENSITIVE scope (``contacts``) in the GENERATED manifest only (NOT
# appscriptly's own consent), reported under ``manifest_scope``. Same honest
# trigger-activation state as the form-handler schema (trigger_active False
# / activation_required True). additionalProperties stays True (the _object
# default) so a future field is additive.
AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA = _object(
    properties={
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "form_id": {"type": "string"},
        "trigger_type": {"type": "string", "enum": ["onFormSubmit"]},
        "trigger_handler": {"type": "string"},
        "project_url": {"type": "string", "format": "uri"},
        "trigger_active": {"type": "boolean"},
        "activation_required": {"type": "boolean"},
        "activation_instructions": {"type": "string"},
        # Unified activation contract (Stream 3). See activation.py.
        "activation_url": {"type": "string", "format": "uri"},
        "activation_function": {"type": "string"},
        "manifest_scope": {"type": "string"},
    },
    required=[
        "script_id",
        "deployment_id",
        "form_id",
        "trigger_type",
        "trigger_handler",
        "project_url",
        "trigger_active",
        "activation_required",
        "activation_instructions",
        "activation_url",
        "activation_function",
        "manifest_scope",
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
            # PR-D: "needs_activation" = the runtime deployed fine and
            # is waiting for the user's ONE-TIME per-script consent
            # (Run + Allow in the script editor). Returned as data, not
            # an exception: pre-PR-D this state either reported "ready"
            # (a lie - the endpoint serves Google's 403 door) or raised
            # a misdiagnosed "re-authorize / API disabled" error.
            "enum": [
                "ready",
                "needs_activation",
                "needs_authorization",
                "failed",
            ],
        },
        # Variant-specific fields:
        "url": {"type": "string", "format": "uri"},
        "script_id": {"type": "string"},
        "deployment_id": {"type": "string"},
        "auth_url": {"type": "string", "format": "uri"},
        "activation_url": {"type": "string", "format": "uri"},
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
            "properties": {"status": {"const": "needs_activation"}},
            "required": ["status", "activation_url", "message"],
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


# T1.2 (2026-07-10) — server_health. Three-layer health report:
# the MCP server itself, the Google API credential path, and the
# per-user Apps Script automation runtime (/exec web app).
# ``exec`` includes "unknown" beyond the contract's four states for
# the case where the liveness probe is transport-inconclusive
# (timeout / DNS) - reporting "serving" or "needs_activation" on no
# evidence would prompt wrong user action.
SERVER_HEALTH_OUTPUT_SCHEMA = _object(
    properties={
        "server": {"type": "string", "const": "ok"},
        "google_api": {
            "type": "string",
            "enum": ["ok", "unauthorized", "error"],
        },
        "google_api_detail": {"type": ["string", "null"]},
        "automation_runtime": _object(
            properties={
                "installed": {"type": "boolean"},
                "exec": {
                    "type": "string",
                    "enum": [
                        "serving",
                        "needs_activation",
                        "api_disabled",
                        "not_installed",
                        "unknown",
                    ],
                },
                "remediation_url": {"type": ["string", "null"]},
                "detail": {"type": ["string", "null"]},
            },
            required=["installed", "exec", "remediation_url"],
        ),
    },
    required=["server", "google_api", "automation_runtime"],
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
    # Wave 5 (S1) template fill - named-range trio + page break (all ride
    # the deployed documents scope; no new scope).
    "gdocs_create_named_range": GDOCS_CREATE_NAMED_RANGE_OUTPUT_SCHEMA,
    "gdocs_replace_named_range_content": (
        GDOCS_REPLACE_NAMED_RANGE_CONTENT_OUTPUT_SCHEMA
    ),
    "gdocs_delete_named_range": GDOCS_DELETE_NAMED_RANGE_OUTPUT_SCHEMA,
    "gdocs_insert_page_break": GDOCS_INSERT_PAGE_BREAK_OUTPUT_SCHEMA,
    # Inline image insert (rides the deployed documents scope; Docs
    # fetches the URI server-side, so no Drive scope needed)
    "gdocs_insert_image": GDOCS_INSERT_IMAGE_OUTPUT_SCHEMA,
    # Comments on app-created docs (Drive comments/replies under the
    # deployed drive.file scope)
    "gdocs_list_comments": GDOCS_LIST_COMMENTS_OUTPUT_SCHEMA,
    "gdocs_create_comment": GDOCS_CREATE_COMMENT_OUTPUT_SCHEMA,
    "gdocs_reply_to_comment": GDOCS_REPLY_TO_COMMENT_OUTPUT_SCHEMA,
    "gdocs_format_range": GDOCS_FORMAT_RANGE_OUTPUT_SCHEMA,
    "gdocs_format_paragraph": GDOCS_FORMAT_PARAGRAPH_OUTPUT_SCHEMA,
    "gdocs_edit_range": GDOCS_EDIT_RANGE_OUTPUT_SCHEMA,
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
    # Sheets VALUES batch ops (values.batchGet / values.batchUpdate)
    "gsheets_batch_read": GSHEETS_BATCH_READ_OUTPUT_SCHEMA,
    "gsheets_batch_write": GSHEETS_BATCH_WRITE_OUTPUT_SCHEMA,
    "gsheets_create_spreadsheet": GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA,
    "gsheets_format_range": GSHEETS_FORMAT_RANGE_OUTPUT_SCHEMA,
    # Sheets conditional formatting (this PR — wires the existing builder)
    "gsheets_apply_conditional_format": GSHEETS_APPLY_CONDITIONAL_FORMAT_OUTPUT_SCHEMA,
    # Sheets append + tab lifecycle (this PR)
    "gsheets_append_rows": GSHEETS_APPEND_ROWS_OUTPUT_SCHEMA,
    "gsheets_add_sheet": GSHEETS_ADD_SHEET_OUTPUT_SCHEMA,
    "gsheets_delete_sheet": GSHEETS_DELETE_SHEET_OUTPUT_SCHEMA,
    "gsheets_rename_sheet": GSHEETS_RENAME_SHEET_OUTPUT_SCHEMA,
    # Sheets values.clear + duplicate/freeze/protect lifecycle (this PR)
    "gsheets_clear_range": GSHEETS_CLEAR_RANGE_OUTPUT_SCHEMA,
    "gsheets_duplicate_sheet": GSHEETS_DUPLICATE_SHEET_OUTPUT_SCHEMA,
    "gsheets_freeze": GSHEETS_FREEZE_OUTPUT_SCHEMA,
    "gsheets_protect_range": GSHEETS_PROTECT_RANGE_OUTPUT_SCHEMA,
    # Sheets dimension ops + merge + data-validation + chart (ride the
    # existing batch.py builder seam; deployed spreadsheets scope)
    "gsheets_insert_dimension": GSHEETS_INSERT_DIMENSION_OUTPUT_SCHEMA,
    "gsheets_delete_dimension": GSHEETS_DELETE_DIMENSION_OUTPUT_SCHEMA,
    "gsheets_merge_cells": GSHEETS_MERGE_CELLS_OUTPUT_SCHEMA,
    "gsheets_set_data_validation": GSHEETS_SET_DATA_VALIDATION_OUTPUT_SCHEMA,
    "gsheets_add_chart": GSHEETS_ADD_CHART_OUTPUT_SCHEMA,
    # v2.4.0 — Calendar (4th new service): event + availability surface.
    # Scope https://www.googleapis.com/auth/calendar (SENSITIVE, no CASA).
    "gcal_list_events": GCAL_LIST_EVENTS_OUTPUT_SCHEMA,
    "gcal_get_event": GCAL_GET_EVENT_OUTPUT_SCHEMA,
    "gcal_create_event": GCAL_CREATE_EVENT_OUTPUT_SCHEMA,
    "gcal_update_event": GCAL_UPDATE_EVENT_OUTPUT_SCHEMA,
    "gcal_delete_event": GCAL_DELETE_EVENT_OUTPUT_SCHEMA,
    "gcal_list_calendars": GCAL_LIST_CALENDARS_OUTPUT_SCHEMA,
    "gcal_freebusy": GCAL_FREEBUSY_OUTPUT_SCHEMA,
    # v2.3.2 — Slides (3rd new service, minimal start)
    "gslides_get_outline": GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA,
    "gslides_replace_all_text": GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
    "gslides_create_presentation": GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
    "gslides_add_slide": GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA,
    "gslides_create_image": GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA,
    "gslides_create_table": GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA,
    # #155 geometry trio — createShape + createLine complete the set
    "gslides_create_shape": GSLIDES_CREATE_SHAPE_OUTPUT_SCHEMA,
    "gslides_create_line": GSLIDES_CREATE_LINE_OUTPUT_SCHEMA,
    # Speaker-notes write path (rides the deployed presentations scope)
    "gslides_set_speaker_notes": GSLIDES_SET_SPEAKER_NOTES_OUTPUT_SCHEMA,
    # Wave 4 (S1) element-management verbs (delete / duplicate / transform)
    "gslides_delete_object": GSLIDES_DELETE_OBJECT_OUTPUT_SCHEMA,
    "gslides_duplicate_object": GSLIDES_DUPLICATE_OBJECT_OUTPUT_SCHEMA,
    "gslides_update_element_transform": (
        GSLIDES_UPDATE_ELEMENT_TRANSFORM_OUTPUT_SCHEMA
    ),
    # Forms (new service, sensitive scopes forms.body +
    # forms.responses.readonly — NOT restricted, no CASA)
    "gforms_create_form": GFORMS_CREATE_FORM_OUTPUT_SCHEMA,
    "gforms_get_form": GFORMS_GET_FORM_OUTPUT_SCHEMA,
    "gforms_add_question": GFORMS_ADD_QUESTION_OUTPUT_SCHEMA,
    "gforms_update_item": GFORMS_UPDATE_ITEM_OUTPUT_SCHEMA,
    "gforms_delete_item": GFORMS_DELETE_ITEM_OUTPUT_SCHEMA,
    "gforms_list_responses": GFORMS_LIST_RESPONSES_OUTPUT_SCHEMA,
    "gforms_get_response": GFORMS_GET_RESPONSE_OUTPUT_SCHEMA,
    # Contacts (services/contacts/) — People API v1 (new service)
    "gcontacts_list": GCONTACTS_LIST_OUTPUT_SCHEMA,
    "gcontacts_search": GCONTACTS_SEARCH_OUTPUT_SCHEMA,
    "gcontacts_get": GCONTACTS_GET_OUTPUT_SCHEMA,
    "gcontacts_create": GCONTACTS_CREATE_OUTPUT_SCHEMA,
    "gcontacts_update": GCONTACTS_UPDATE_OUTPUT_SCHEMA,
    "gcontacts_delete": GCONTACTS_DELETE_OUTPUT_SCHEMA,
    # CASA-free growth — "other contacts" read (contacts.other.readonly,
    # SENSITIVE, no CASA): People API otherContacts.list (auto-saved).
    "gcontacts_list_other_contacts": GCONTACTS_LIST_OTHER_CONTACTS_OUTPUT_SCHEMA,
    # Gmail (services/gmail/) — Gmail API v1 (send + labels). gmail.send is
    # SENSITIVE (no CASA); gmail.labels is NON-sensitive.
    "gmail_send_message": GMAIL_SEND_MESSAGE_OUTPUT_SCHEMA,
    "gmail_create_label": GMAIL_CREATE_LABEL_OUTPUT_SCHEMA,
    "gmail_list_labels": GMAIL_LIST_LABELS_OUTPUT_SCHEMA,
    "gmail_delete_label": GMAIL_DELETE_LABEL_OUTPUT_SCHEMA,
    # Tasks (services/tasks/) — Google Tasks API v1 (sensitive scope, no CASA)
    "gtasks_list_tasklists": GTASKS_LIST_TASKLISTS_OUTPUT_SCHEMA,
    "gtasks_create_tasklist": GTASKS_CREATE_TASKLIST_OUTPUT_SCHEMA,
    "gtasks_list_tasks": GTASKS_LIST_TASKS_OUTPUT_SCHEMA,
    "gtasks_create_task": GTASKS_CREATE_TASK_OUTPUT_SCHEMA,
    "gtasks_update_task": GTASKS_UPDATE_TASK_OUTPUT_SCHEMA,
    "gtasks_complete_task": GTASKS_COMPLETE_TASK_OUTPUT_SCHEMA,
    "gtasks_delete_task": GTASKS_DELETE_TASK_OUTPUT_SCHEMA,
    # ROADMAP 59 — deploy a standalone doGet/doPost project as a Web App
    "as_deploy_web_app": AS_DEPLOY_WEB_APP_OUTPUT_SCHEMA,
    # PR-Δ7 — Apps Script bound-script generator (the feature foundation)
    "as_generate_bound_script": AS_GENERATE_BOUND_SCRIPT_OUTPUT_SCHEMA,
    # CASA-free growth — Apps Script execution-history read
    # (script.processes, SENSITIVE, no CASA): processes.list /
    # processes.listScriptProcesses. Observability companion to the
    # create+deploy levers above.
    "as_list_script_processes": AS_LIST_SCRIPT_PROCESSES_OUTPUT_SCHEMA,
    # Stream 3 — verify a deployed automation is activated yet (web-app
    # probe or execution-history read; companion to the activation UX).
    "as_check_activation": AS_CHECK_ACTIVATION_OUTPUT_SCHEMA,
    # Wave 2 (S4) - read-only install catalog projected from the recipe
    # registry (_recipes.py); the discovery surface for the as_install_* family.
    "as_list_recipes": AS_LIST_RECIPES_OUTPUT_SCHEMA,
    # Automation lifecycle — forward-only inventory + honest partial
    # uninstall (ledger-backed; closes the install-only gap, S0-1..S0-4).
    "as_list_installed_automations": AS_LIST_INSTALLED_AUTOMATIONS_OUTPUT_SCHEMA,
    "as_uninstall_automation": AS_UNINSTALL_AUTOMATION_OUTPUT_SCHEMA,
    "as_update_automation": AS_UPDATE_AUTOMATION_OUTPUT_SCHEMA,
    # PR-Δ8 — install a custom menu into a Doc (composes the Δ7 primitive)
    "as_install_doc_menu": AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA,
    # PR-Δ10 — custom spreadsheet function installer (composes PR-Δ7)
    "as_install_custom_function": AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA,
    # PR-Δ9 — scheduled dashboard refresh for Sheets (composes PR-Δ7)
    "as_install_sheet_dashboard": AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA,
    # ROADMAP_SPECS #8 — reactive onEdit trigger for Sheets (composes PR-Δ7)
    "as_install_edit_trigger": AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA,
    # ROADMAP_SPECS #8 — reactive onFormSubmit handler for Forms (composes
    # PR-Δ7; lifts the Forms hard-rejection for this one reactive surface)
    "as_install_form_handler": AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA,
    # GAS service-parity — Sheets custom menu (Sheets analogue of
    # as_install_doc_menu; SpreadsheetApp.getUi(); composes PR-Δ7)
    "as_install_sheet_menu": AS_INSTALL_SHEET_MENU_OUTPUT_SCHEMA,
    # GAS service-parity — Slides custom menu (Slides analogue of
    # as_install_doc_menu; SlidesApp.getUi(); composes PR-Δ7)
    "as_install_slides_menu": AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA,
    # GAS service-parity — refresh linked slides (getSlides()→refreshSlide();
    # master-deck→client-deck sync REST cannot do; composes PR-Δ7)
    "as_refresh_linked_slides": AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA,
    # GAS service-parity — push computed grades onto quiz responses
    # (FormApp.submitGrades(); full forms scope in GENERATED manifest only;
    # composes PR-Δ7)
    "as_grade_form_responses": AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA,
    # PR-Δ11 — render a Slides deck to video frames (composes PR-Δ7;
    # the render half of the slides-to-video pipeline)
    "as_generate_video_deck": AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
    # PR-Δ12 — encode those rendered frames into an MP4 (server-side
    # ffmpeg; the encode half that completes slides-to-video)
    "as_encode_video": AS_ENCODE_VIDEO_OUTPUT_SCHEMA,
    # GAS service-parity (Calendar) — time-driven create/sync Calendar
    # events from Sheet rows (CalendarApp; calendar scope in GENERATED
    # manifest only; composes PR-Δ7)
    "as_install_calendar_sync": AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA,
    # GAS service-parity (Tasks) — time-driven Tasks orchestration via the
    # Tasks advanced service (tasks scope in GENERATED manifest only;
    # composes PR-Δ7)
    "as_install_task_rollover": AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA,
    # GAS service-parity (Contacts) — reactive onFormSubmit contact
    # create/sync (ContactsApp; contacts scope in GENERATED manifest only;
    # binds directly to a Form; composes PR-Δ7)
    "as_install_contact_sync": AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA,
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
    # -----------------------------------------------------------------
    # chore/tool-namespace-cleanup — canonical names for the 18 renamed
    # tools. Each maps to the SAME schema object as its gdocs_* alias
    # above (one implementation, two registrations). The old gdocs_*
    # entries are retained for the deprecated aliases (removal v3.0).
    # -----------------------------------------------------------------
    # Drive (gdrive_*).
    "gdrive_find_doc_by_title": GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    "gdrive_move_to_folder": GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    "gdrive_trash_file": GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    "gdrive_untrash_file": GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
    "gdrive_share_file": GDOCS_SHARE_FILE_OUTPUT_SCHEMA,
    "gdrive_list_permissions": GDOCS_LIST_PERMISSIONS_OUTPUT_SCHEMA,
    "gdrive_create_folder": GDOCS_CREATE_FOLDER_OUTPUT_SCHEMA,
    "gdrive_revoke_permission": GDOCS_REVOKE_PERMISSION_OUTPUT_SCHEMA,
    "gdrive_export_file": GDOCS_EXPORT_DOC_OUTPUT_SCHEMA,
    "gdrive_find_file": GDOCS_FIND_FILE_OUTPUT_SCHEMA,
    "gdrive_get_signed_upload_url": GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    # BUG 2b (2026-07-10) — canonical-only (no gdocs_ alias; the tool
    # never existed under the legacy prefix).
    "gdrive_rename_file": GDRIVE_RENAME_FILE_OUTPUT_SCHEMA,
    # Wave 5 (S1) - canonical-only copy tool (template-fill enabler; no
    # gdocs_ alias; drive.file scope, no new scope).
    "gdrive_copy_file": GDRIVE_COPY_FILE_OUTPUT_SCHEMA,
    # admin / introspection / auth.
    "server_info": GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    "server_test_manifest": GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    "server_guide": GDOCS_GUIDE_OUTPUT_SCHEMA,
    "server_help": GDOCS_HELP_OUTPUT_SCHEMA,
    # T1.2 (2026-07-10) — canonical-only new tool.
    "server_health": SERVER_HEALTH_OUTPUT_SCHEMA,
    "admin_audit": GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
    "account_reset_authorization": GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    # Apps Script installer (3rd registration; shares the installer schema).
    "as_install_automation": GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
}
