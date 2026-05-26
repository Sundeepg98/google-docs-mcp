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
    # v2.3.1 — Sheets (2nd new service, minimal start)
    "gsheets_read_range": GSHEETS_READ_RANGE_OUTPUT_SCHEMA,
    "gsheets_write_range": GSHEETS_WRITE_RANGE_OUTPUT_SCHEMA,
    "gsheets_create_spreadsheet": GSHEETS_CREATE_SPREADSHEET_OUTPUT_SCHEMA,
    "gdocs_server_info": GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    "gdocs_test_manifest": GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    "gdocs_guide": GDOCS_GUIDE_OUTPUT_SCHEMA,
    "gdocs_help": GDOCS_HELP_OUTPUT_SCHEMA,
    "gdocs_get_signed_upload_url": GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    "gdocs_setup_apps_script": GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
    "gdocs_reset_authorization": GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    "gdocs_admin_audit": GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
}
