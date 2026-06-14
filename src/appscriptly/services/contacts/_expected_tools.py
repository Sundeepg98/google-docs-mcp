"""Declared tool surface for the contacts service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. All 7 People API v1 tools (tools.py wrapping api.py) carry
``service="contacts"``. The 7th, ``gcontacts_list_other_contacts``, was
added by the CASA-free scope-growth PR and uses the dedicated read-only
``contacts.other.readonly`` scope (the other six use the full
``contacts`` scope).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gcontacts_list",
    "gcontacts_list_other_contacts",
    "gcontacts_search",
    "gcontacts_get",
    "gcontacts_create",
    "gcontacts_update",
    "gcontacts_delete",
})
