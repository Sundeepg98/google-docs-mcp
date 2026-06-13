"""Declared tool surface for the calendar service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale: the multi-service ``test_tool_registration.py`` aggregates
every ``services/*/_expected_tools.py::EXPECTED`` into ``declared`` and
asserts ``declared == registered`` (the live ``mcp.list_tools()`` set).

A new calendar tool updates ONLY this file + its own definition site —
no central frozenset, no central server.py import. The leading-``_``
prefix excludes this module from auto-discovery's import walk (it
registers no tools; it's pure declaration).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gcal_list_events",
    "gcal_get_event",
    "gcal_create_event",
    "gcal_update_event",
    "gcal_delete_event",
    "gcal_list_calendars",
    "gcal_freebusy",
})
