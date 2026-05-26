"""Tab-tree walking helpers (v2.2.1 — R14 #8 split).

Pure functions that traverse the nested ``tabs`` / ``childTabs`` shape
the Google Docs REST API returns. No Google SDK imports — these
helpers operate on already-fetched dict structures, so they can be
exercised in isolation without ``with_google_api_client(...)``.

Extracted from ``services/docs/api.py`` as part of the R14 #8 split
that closes audit Gap #1 (1,050-LOC ``api.py`` was the single largest
blocker to 100% confidence per Hex 92% / SOLID 78% / Test 78% scores).
The split:

  api.py            — Google Docs REST calls only
  markdown_render.py — markdown-it state machine + request builders
  tab_tree.py       — tree walking (THIS module)

Sibling unblock: this isolation makes the R6 UTF-16 bug independently
testable in ``markdown_render.py`` without dragging Google API mocks
through the test setup.
"""
from __future__ import annotations

from typing import Any


def _flatten_tab_tree(
    tabs: list[Any],
) -> list[tuple[int, tuple[int, ...], Any]]:
    """Pre-order traversal yielding (depth, path, spec) for every tab.

    ``path`` is the tuple of sibling indices from root, e.g. ``(0, 1)``
    means ``tabs[0].children[1]``.

    Operates on the ``TabSpec`` input shape (``children`` key carries
    nested specs). Sibling helper for the server-shape walk: ``api.py``
    holds the ``get_doc_outline``-side walker that uses ``childTabs``
    instead.
    """
    out: list[tuple[int, tuple[int, ...], Any]] = []

    def walk(specs: list[Any], parent_path: tuple[int, ...]) -> None:
        for i, spec in enumerate(specs):
            path = (*parent_path, i)
            out.append((len(path) - 1, path, spec))
            walk(spec.get("children") or [], path)

    walk(tabs, ())
    return out


def _find_tab_by_id(tabs: list[dict], target_id: str) -> dict | None:
    """Recursively locate a tab in a nested ``tabs`` array by its tabId.

    Operates on the SERVER-shape returned by ``documents().get(
    includeTabsContent=True)`` — ``tabProperties.tabId`` keys, nested
    via ``childTabs``. Sibling to ``_flatten_tab_tree`` (which operates
    on the input ``TabSpec`` shape).
    """
    for tab in tabs:
        if tab["tabProperties"]["tabId"] == target_id:
            return tab
        nested = _find_tab_by_id(tab.get("childTabs") or [], target_id)
        if nested is not None:
            return nested
    return None


def _get_tab_depth(tabs: list[dict], target_id: str, current_depth: int = 0) -> int:
    """Return the nesting depth of a tab (root=0), or -1 if not found.

    Same server-shape contract as ``_find_tab_by_id``. Used by
    ``add_tabs_to_doc`` to enforce ``MAX_NESTING_DEPTH`` when adding
    new tabs under an existing parent.
    """
    for tab in tabs:
        if tab["tabProperties"]["tabId"] == target_id:
            return current_depth
        result = _get_tab_depth(
            tab.get("childTabs") or [], target_id, current_depth + 1
        )
        if result >= 0:
            return result
    return -1


def _find_tab_by_title(tabs: list[dict], target_title: str) -> dict | None:
    """Recursively locate a tab in nested ``tabs`` by exact title match.

    Title comparison is case-sensitive and full-string (no substring
    behaviour) — substring matching is a deliberate ``set_tab_icons``
    feature, not the default lookup contract.
    """
    for tab in tabs:
        if tab["tabProperties"].get("title") == target_title:
            return tab
        nested = _find_tab_by_title(tab.get("childTabs") or [], target_title)
        if nested is not None:
            return nested
    return None
