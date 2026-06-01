"""Declared tool surface for the sheets service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gsheets_read_range",
    "gsheets_write_range",
    "gsheets_create_spreadsheet",
    "gsheets_format_range",
})
