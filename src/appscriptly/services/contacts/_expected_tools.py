"""Declared tool surface for the contacts service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. All 6 People API v1 tools (tools.py wrapping api.py) carry
``service="contacts"``.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gcontacts_list",
    "gcontacts_search",
    "gcontacts_get",
    "gcontacts_create",
    "gcontacts_update",
    "gcontacts_delete",
})
