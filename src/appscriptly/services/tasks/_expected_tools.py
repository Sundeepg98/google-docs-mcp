"""Declared tool surface for the tasks service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale: a new tasks tool updates ONLY this file + its own definition
site (no central frozenset, no central server.py import). The leading-
``_`` prefix excludes this module from auto-discovery's import walk (it
registers no tools; it's pure declaration).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gtasks_list_tasklists",
    "gtasks_create_tasklist",
    "gtasks_list_tasks",
    "gtasks_create_task",
    "gtasks_update_task",
    "gtasks_complete_task",
    "gtasks_delete_task",
})
