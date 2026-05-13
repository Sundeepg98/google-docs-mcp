"""Google Docs MCP Server with native Tabs support.

Exposes one tool to MCP clients: ``create_tabbed_doc``.
"""
from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from .auth import default_data_dir, load_credentials
from .docs_api import TabSpec, make_doc_with_tabs

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
        raise ToolError(
            f"Google Docs API error: {e.status_code} {e.reason}. "
            f"Details: {e.error_details if hasattr(e, 'error_details') else str(e)}"
        ) from e


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
