"""Google Docs MCP Server with native Tabs support.

Exposes one tool to Claude Desktop: ``create_tabbed_doc``.
"""
from pathlib import Path

from fastmcp import FastMCP

from auth import load_credentials
from docs_api import TabSpec, make_doc_with_tabs

CREDS_DIR = Path(__file__).parent / "credentials"
CREDS_DIR.mkdir(exist_ok=True)

mcp = FastMCP("google-docs")


@mcp.tool()
def create_tabbed_doc(title: str, tabs: list[TabSpec]) -> dict:
    """Create a Google Doc with multiple native tabs (Oct 2024 sidebar feature).

    Each tab is a separately-navigable section in the Google Docs left
    sidebar — not just an outline heading. Use one tab per logical
    subtopic.

    Args:
        title: Document title (shown in Google Drive).
        tabs: List of tabs. Each entry is ``{"title": str, "content": str}``.
              Order is preserved; the first entry becomes the default tab.
              Example:
                  [{"title": "Subtopic 1", "content": "..."},
                   {"title": "Subtopic 2", "content": "..."}]

    Returns:
        ``{"doc_id": str, "url": str, "tabs": [{"title", "tab_id"}, ...]}``
    """
    if not tabs:
        raise ValueError("Must provide at least one tab")

    creds = load_credentials(CREDS_DIR)
    return make_doc_with_tabs(creds, title, tabs)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
