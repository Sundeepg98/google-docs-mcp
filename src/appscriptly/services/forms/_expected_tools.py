"""Declared tool surface for the forms service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. The leading-``_`` prefix excludes this module from
auto-discovery's import walk (it registers no tools; it's pure
declaration).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gforms_create_form",
    "gforms_get_form",
    "gforms_add_question",
    "gforms_update_item",
    "gforms_delete_item",
    "gforms_list_responses",
    "gforms_get_response",
})
