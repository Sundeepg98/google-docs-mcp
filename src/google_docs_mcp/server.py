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
    """Create a Google Doc with multiple native tabs (Oct 2024 sidebar feature).

    Each tab is a separately-navigable section in the Google Docs left
    sidebar — not just an outline heading. Use one tab per logical
    subtopic.

    Args:
        title: Document title (shown in Google Drive).
        tabs: List of tabs. Each entry is
            ``{"title": str, "content": str, "icon_emoji"?: str, "content_format"?: "markdown"|"text"}``.
            Order is preserved; the first entry becomes the default tab.
            ``content`` is rendered as markdown by default — set
            ``content_format: "text"`` for raw text. ``icon_emoji`` is
            an optional single emoji shown beside the tab title.

    Returns:
        ``{"doc_id": str, "url": str, "tabs": [{"title", "tab_id"}, ...]}``
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
