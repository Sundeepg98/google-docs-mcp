"""Google Docs MCP Server with native Tabs support.

Exposes MCP tools for working with native Google Docs tabs:
``create_tabbed_doc``, ``add_tabs``, ``get_doc_outline``,
``append_to_tab``, and ``convert_docx_to_tabbed_doc``.

The same entry point also implements one-off CLI commands for the
Apps Script setup needed by ``convert_docx_to_tabbed_doc``; see the
``cli`` module for those.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
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
    read_tab_content as _read_tab_content,
)
from .docx_import import convert_docx_to_tabbed_doc as _convert_docx

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
def read_tab_content(
    doc_id: str,
    tab_id: str | None = None,
    tab_title: str | None = None,
) -> dict:
    """Read the body content of a single tab.

    Use ``get_doc_outline`` first to see the tab list, then pass either
    a ``tab_id`` (exact) or ``tab_title`` (first pre-order match).

    Returns:
        ``{"tab_id", "title", "paragraph_count", "table_count",
        "image_count", "paragraphs": [{"style", "text"}, ...]}``.
        ``paragraphs[].style`` is a Docs namedStyleType
        (``HEADING_1``, ``NORMAL_TEXT``, etc.) or ``"TABLE"`` /
        ``"TOC"`` placeholders. ``text`` has trailing newlines stripped;
        inline images appear as ``[image]`` markers inside the text.

    This is the canonical "what's actually inside this tab?" tool —
    use it after ``convert_docx_to_tabbed_doc`` to confirm content
    moved correctly without opening the doc in a browser.
    """
    try:
        creds = _get_credentials()
        return _read_tab_content(creds, doc_id, tab_id=tab_id, tab_title=tab_title)
    except ValueError as e:
        raise ToolError(str(e)) from e
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


@mcp.tool()
def convert_docx_to_tabbed_doc(
    docx_path: str | None = None,
    docx_drive_file_id: str | None = None,
    split_by: Literal["heading_1", "heading_2", "page_break", "auto"] = "heading_1",
    title: str | None = None,
) -> dict:
    """Convert a .docx into a Google Doc with native nested tabs.

    Pipeline: Drive imports the .docx (lossless: tables, cell shading,
    colored borders, images, equations all preserved) → we identify
    split points by walking the converted doc → REST creates empty
    nested tab shells → Apps Script moves content from the primary tab
    into the new shells using ``Element.copy()`` (the only path that
    preserves drawings, equations, and table cell shading because no
    REST request type can re-emit those).

    Prerequisite: you must have deployed the helper Apps Script Web
    App once. Run ``google-docs-mcp setup-apps-script`` to get the
    deployment recipe, then ``google-docs-mcp configure-webapp <URL>``
    with the URL after deploying.

    Args:
        docx_path: Absolute path to a local ``.docx`` file. Use this
            when the file lives on the machine the MCP server runs on
            (Claude Code, Claude Desktop). Must end in ``.docx``.
        docx_drive_file_id: Google Drive file ID of a .docx already
            uploaded to Drive. Use this when calling from Claude.ai
            cloud chat — cloud chat can upload the .docx via its own
            Drive connector and pass us the resulting file ID.
            Requires the ``drive.readonly`` OAuth scope (added in
            v0.8.0; you may need to re-authorize once).
        split_by: How to identify tab boundaries in the converted doc.
            - ``"heading_1"`` (default): each Heading 1 paragraph
              starts a new tab; the tab title is the heading text.
            - ``"heading_2"``: same but for Heading 2.
            - ``"page_break"``: every page break starts a new tab.
              Title is auto-generated (``Page 2``, ``Page 3``, ...).
            - ``"auto"``: try heading_1 → heading_2 → page_break and
              use the first strategy that finds any split points.
        title: Optional override for the resulting doc's title. Defaults
            to the .docx filename without extension.

    Exactly one of ``docx_path`` or ``docx_drive_file_id`` must be set.

    Returns:
        ``{"doc_id", "url", "tabs": [...], "split_strategy_used", ...}``.
        ``tabs`` is the post-restructure tab list from the Apps Script
        side (each entry has ``id``, ``title``, ``depth``). If no split
        points were found, returns the converted single-tab doc unchanged
        plus a ``note`` field explaining why.
    """
    if (docx_path is None) == (docx_drive_file_id is None):
        raise ToolError(
            "Provide exactly one of docx_path or docx_drive_file_id "
            "(got both, or neither)."
        )
    path: Path | None = Path(docx_path).expanduser() if docx_path else None
    try:
        creds = _get_credentials()
        return _convert_docx(
            creds,
            docx_path=path,
            docx_drive_file_id=docx_drive_file_id,
            split_by=split_by,
            title=title,
        )
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


def _format_http_error(e: HttpError) -> str:
    details = e.error_details if hasattr(e, "error_details") else str(e)
    return f"Google Docs API error: {e.status_code} {e.reason}. Details: {details}"


_CLI_SUBCOMMANDS = {"setup-apps-script", "configure-webapp", "status", "help", "-h", "--help"}


def main() -> None:
    """Entry point.

    Dispatches in order:
      1. ``google-docs-mcp <cli-subcommand>`` -> route to ``cli.py``
      2. ``MCP_TRANSPORT=http`` env var (or ``--http`` flag) -> run as
         remote HTTP server (Fly.io / cloud chat use case). Listens on
         ``$PORT`` (default 8080). Includes both the FastMCP ``/mcp``
         endpoint AND a simple ``/api/convert`` REST endpoint for
         clients that don't speak MCP protocol (e.g. cloud chat's
         Python sandbox).
      3. Otherwise -> stdio (Claude Code / Claude Desktop).
    """
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_SUBCOMMANDS:
        from .cli import cli_main
        sys.exit(cli_main(sys.argv[1:]))

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if "--http" in sys.argv:
        transport = "http"

    if transport == "http":
        from .http_server import run_http
        port = int(os.environ.get("PORT", "8080"))
        run_http(mcp, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
