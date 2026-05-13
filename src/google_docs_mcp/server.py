"""Google Docs MCP Server with native Tabs support.

Exposes one tool to MCP clients: ``create_tabbed_doc``.
"""
from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from .auth import default_data_dir, load_credentials
from .docs_api import (
    TabSpec,
    add_tabs_to_doc,
    append_to_tab as _append_to_tab,
    get_doc_outline as _get_doc_outline,
    make_doc_with_tabs,
)

mcp = FastMCP("google-docs")

# Lazy module-level cache so the OAuth flow / discovery client setup
# happens on the first tool call rather than at import time. Subsequent
# tool calls reuse the cached credentials.
_creds_cache = None


def _get_credentials():
    global _creds_cache
    if _creds_cache is None or not _creds_cache.valid:
        _creds_cache = load_credentials(default_data_dir())
    return _creds_cache


@mcp.tool()
def create_tabbed_doc(title: str, tabs: list[TabSpec]) -> dict:
    """Create a Google Doc with native tabs, optionally nested up to 3 levels.

    Each tab is a separately-navigable section in the Google Docs left
    sidebar — not just an outline heading. Tabs can have child tabs
    (and grandchildren) for hierarchical structure.

    Args:
        title: Document title (shown in Google Drive).
        tabs: List of tabs. Each entry is a dict with these fields:
            - ``title`` (str, required): the tab name
            - ``content`` (str, required): the tab body, rendered as
              markdown by default
            - ``icon_emoji`` (str, optional): a single emoji shown
              beside the tab title (max 8 UTF-8 bytes)
            - ``content_format`` (``"markdown"`` | ``"text"``, optional,
              default ``"markdown"``): set to ``"text"`` to skip
              markdown parsing for pre-formatted content
            - ``children`` (list[TabSpec], optional): child tabs nested
              under this one. Max nesting depth is 3 levels (root +
              2 child levels).

            Order is preserved at every level; the first root entry
            becomes the default tab.

            Example with nesting:

                [{"title": "Section A", "content": "...", "children": [
                    {"title": "A.1", "content": "..."},
                    {"title": "A.2", "content": "...", "children": [
                        {"title": "A.2.i", "content": "..."}
                    ]}
                 ]},
                 {"title": "Section B", "content": "..."}]

    Returns:
        ``{"doc_id": str, "url": str, "tabs": [{"title", "tab_id",
        "depth", "parent_tab_id"}, ...]}``. The ``tabs`` list is in
        pre-order traversal so callers can reconstruct the tree.
    """
    if not tabs:
        raise ToolError("Must provide at least one tab")

    for i, tab in enumerate(tabs):
        if not tab.get("title"):
            raise ToolError(f"tabs[{i}] is missing a non-empty title")
        icon = tab.get("icon_emoji")
        if icon and len(icon.encode("utf-8")) > 8:
            raise ToolError(
                f"tabs[{i}].icon_emoji must be a single emoji (≤8 UTF-8 bytes)"
            )

    try:
        creds = _get_credentials()
        return make_doc_with_tabs(creds, title, tabs)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def add_tabs(
    doc_id: str,
    tabs: list[TabSpec],
    parent_tab_id: str | None = None,
) -> dict:
    """Append tabs to an existing Google Doc, optionally nested.

    Same nesting rules as ``create_tabbed_doc``. Pass ``parent_tab_id``
    to make the new tabs children of an existing tab; omit it to add
    them as new root-level tabs.

    Args:
        doc_id: The document ID (from a prior ``create_tabbed_doc`` or
            ``find_doc_by_title`` call, or the URL ``/d/{id}/edit``).
        tabs: Same shape as ``create_tabbed_doc.tabs``.
        parent_tab_id: Optional. If given, new tabs are children of this
            tab. Total nesting (parent depth + new tabs depth) must not
            exceed 3 levels.

    Returns:
        ``{"tabs": [{"title", "tab_id", "depth", "parent_tab_id"}, ...]}``
        for the newly created tabs only.
    """
    if not tabs:
        raise ToolError("Must provide at least one tab")
    for i, tab in enumerate(tabs):
        if not tab.get("title"):
            raise ToolError(f"tabs[{i}] is missing a non-empty title")
        icon = tab.get("icon_emoji")
        if icon and len(icon.encode("utf-8")) > 8:
            raise ToolError(
                f"tabs[{i}].icon_emoji must be a single emoji (≤8 UTF-8 bytes)"
            )

    try:
        creds = _get_credentials()
        return add_tabs_to_doc(creds, doc_id, tabs, parent_tab_id=parent_tab_id)
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def get_doc_outline(doc_id: str) -> list[dict]:
    """List every tab in a Google Doc with its structure (no body content).

    Useful as a discovery step before ``append_to_tab`` or other
    targeted operations — you need a ``tab_id`` to call them.

    Args:
        doc_id: The document ID.

    Returns:
        A flat pre-order list. Each entry is
        ``{"tab_id", "title", "parent_tab_id", "depth", "index", "icon_emoji"}``.
        ``depth`` is 0 for root tabs. ``parent_tab_id`` is ``null`` for
        root tabs. ``index`` is the tab's sibling order under its parent.
    """
    try:
        creds = _get_credentials()
        return _get_doc_outline(creds, doc_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def append_to_tab(
    doc_id: str,
    tab_id: str,
    content: str,
    content_format: Literal["markdown", "text"] = "markdown",
) -> dict:
    """Append content to the end of an existing tab's body.

    Existing content is left untouched. Markdown is rendered with the
    same renderer as ``create_tabbed_doc`` (headings, bold/italic,
    code, lists, links, blockquotes).

    Args:
        doc_id: The document ID.
        tab_id: The target tab's ID (get from ``get_doc_outline`` or
            from the ``tabs[].tab_id`` of a prior create/add call).
        content: The content to append.
        content_format: ``"markdown"`` (default) or ``"text"``.

    Returns:
        ``{"tab_id": str, "appended_chars": int}``.
    """
    try:
        creds = _get_credentials()
        return _append_to_tab(
            creds, doc_id, tab_id, content, content_format=content_format
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


def _format_http_error(e: HttpError) -> str:
    details = e.error_details if hasattr(e, "error_details") else str(e)
    return f"Google Docs API error: {e.status_code} {e.reason}. Details: {details}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
