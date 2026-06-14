"""Declared tool surface for the gmail service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale: a new gmail tool updates ONLY this file + its own definition
site (no central frozenset, no central server.py import). The leading-
``_`` prefix excludes this module from auto-discovery's import walk (it
registers no tools; it's pure declaration).

All 4 Gmail tools (tools.py wrapping api.py) carry ``service="gmail"``:
  * ``gmail_send_message``  — gmail.send  (SENSITIVE, no CASA)
  * ``gmail_create_label``  — gmail.labels (NON-sensitive)
  * ``gmail_list_labels``   — gmail.labels (NON-sensitive)
  * ``gmail_delete_label``  — gmail.labels (NON-sensitive)
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gmail_send_message",
    "gmail_create_label",
    "gmail_list_labels",
    "gmail_delete_label",
})
