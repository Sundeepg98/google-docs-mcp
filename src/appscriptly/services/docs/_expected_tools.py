"""Declared tool surface for the docs service.

Decentralized witness (auto-discovery registration refactor): the
multi-service ``test_tool_registration.py`` aggregates every
``services/*/_expected_tools.py::EXPECTED`` into ``declared`` and
asserts ``declared == registered`` (the live ``mcp.list_tools()`` set).

A new docs tool updates ONLY this file + its own definition site — no
central frozenset, no central server.py import. The leading-``_``
prefix excludes this module from auto-discovery's import walk (it
registers no tools; it's pure declaration).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gdocs_make_tabbed_doc",
    "gdocs_add_tabs",
    "gdocs_get_doc_outline",
    "gdocs_read_doc",
    "gdocs_append_to_tab",
    "gdocs_tab_existing_doc",
    "gdocs_rename_tab",
    "gdocs_get_tab_url",
    "gdocs_delete_tab",
    "gdocs_replace_all_text",
    "gdocs_insert_table",
    "gdocs_format_range",
    "gdocs_set_tab_icons",
    "gdocs_preview_tab_split",
})
