"""Declared tool surface for the sheets service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gsheets_read_range",
    "gsheets_write_range",
    "gsheets_batch_read",
    "gsheets_batch_write",
    "gsheets_create_spreadsheet",
    "gsheets_format_range",
    "gsheets_apply_conditional_format",
    "gsheets_append_rows",
    "gsheets_add_sheet",
    "gsheets_delete_sheet",
    "gsheets_rename_sheet",
    "gsheets_clear_range",
    "gsheets_duplicate_sheet",
    "gsheets_freeze",
    "gsheets_protect_range",
    "gsheets_insert_dimension",
    "gsheets_delete_dimension",
    "gsheets_merge_cells",
    "gsheets_set_data_validation",
    "gsheets_add_chart",
})
