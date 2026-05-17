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
from .crypto import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, sign_upload_url
from .errors import friendly_http_error_message
from .docs_api import (
    TabSpec,
    add_tabs_to_doc,
    append_to_tab as _append_to_tab,
    delete_tab as _delete_tab,
    get_doc_outline as _get_doc_outline,
    make_doc_with_tabs,
    read_all_tabs as _read_all_tabs,
    read_tab_content as _read_tab_content,
    rename_tab as _rename_tab,
    replace_all_text as _replace_all_text,
    set_tab_icons as _set_tab_icons,
)
from .docx_import import convert_docx_to_tabbed_doc as _convert_docx
from .preview import preview_tab_split as _preview_tab_split
from .retrofit import retrofit_existing_docx as _retrofit_existing_docx

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
    drive_file_id: str | None = None,
    split_by: Literal["heading_1", "heading_2", "page_break", "auto"] = "heading_1",
    title: str | None = None,
    tab_icons: list[str] | None = None,
    icons_by_title: dict[str, str] | None = None,
    placeholder_behavior: Literal["delete", "rename", "keep"] = "delete",
    placeholder_title: str = "Overview",
    placeholder_icon: str = "\U0001f4d1",
    replace_doc_id: str | None = None,
    docx_drive_file_id: str | None = None,
) -> dict:
    """Convert a .docx OR Google Doc into a Google Doc with native tabs.

    Three input paths depending on where you're calling from:

    - **Claude Code / Claude Desktop (local MCP)**: pass ``docx_path``
      with the absolute path to the .docx on this machine.
    - **claude.ai cloud chat**: do NOT use this MCP tool directly for
      file upload — the .docx bytes can't traverse the tool boundary
      cleanly. Instead, call ``get_signed_upload_url`` and POST the
      bytes to that URL via your Python sandbox. This is the only
      reliable upload route from cloud chat.
    - **File already on Drive (either .docx or Google Doc)**: pass
      ``drive_file_id``. Works whether the file is still a .docx or
      has been auto-converted to a Google Doc.

    Pipeline: Drive imports the .docx (lossless: tables, cell shading,
    colored borders, images, equations all preserved) → we identify
    split points by walking the converted doc → REST creates empty
    nested tab shells → Apps Script moves content from the primary tab
    into the new shells using ``Element.copy()``.

    Prerequisite: helper Apps Script Web App must be deployed once.
    Run ``google-docs-mcp setup-apps-script`` for setup instructions.

    Args:
        docx_path: Absolute path to a local ``.docx`` (local MCP only).
        drive_file_id: Drive file ID of an existing .docx or Google Doc.
            Accepts both mime types and routes automatically.
        docx_drive_file_id: Deprecated alias for ``drive_file_id``.
            Kept for backward compatibility.
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
        tab_icons: Optional list of single emojis to assign to detected
            tabs in order (first emoji → first detected split, etc.).
            Shorter lists are fine — remaining tabs get no icon. To set
            icons later (or match by title rather than order), use
            ``set_tab_icons``.
        placeholder_behavior: What to do with the original "Tab 1"
            placeholder after content has been split into section tabs.
            - ``"delete"`` (default): remove the now-empty placeholder
              so the sidebar shows only section tabs.
            - ``"rename"``: rename it to ``placeholder_title`` with
              ``placeholder_icon``. Use this if you want a landing/
              intro tab as the first sidebar entry.
            - ``"keep"``: leave it untouched as "Tab 1".
        placeholder_title: Title to use when ``placeholder_behavior``
            is ``"rename"``. Default ``"Overview"``.
        placeholder_icon: Single emoji to use when
            ``placeholder_behavior`` is ``"rename"``. Default 📑.

    Exactly one of ``docx_path`` or ``drive_file_id`` must be set.

    Returns:
        ``{"doc_id", "url", "tabs": [...], "split_strategy_used",
        "warnings": [], "info": [], ...}``. ``tabs`` is the post-
        restructure tab list (id/title/depth). If no split points were
        found, returns the converted single-tab doc unchanged plus a
        ``note`` explaining why.
    """
    if docx_drive_file_id is not None and drive_file_id is None:
        drive_file_id = docx_drive_file_id

    if (docx_path is None) == (drive_file_id is None):
        raise ToolError(
            "Provide exactly one of docx_path or drive_file_id "
            "(got both, or neither)."
        )
    path: Path | None = Path(docx_path).expanduser() if docx_path else None
    try:
        creds = _get_credentials()
        return _convert_docx(
            creds,
            docx_path=path,
            drive_file_id=drive_file_id,
            split_by=split_by,
            title=title,
            tab_icons=tab_icons,
            icons_by_title=icons_by_title,
            placeholder_behavior=placeholder_behavior,
            placeholder_title=placeholder_title,
            placeholder_icon=placeholder_icon,
            replace_doc_id=replace_doc_id,
        )
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def rename_tab(
    doc_id: str,
    tab_id: str,
    title: str | None = None,
    icon_emoji: str | None = None,
) -> dict:
    """Rename a tab and/or set its icon emoji.

    Pass either ``title``, ``icon_emoji``, or both. At least one must
    be non-null. Use ``get_doc_outline`` first to find the ``tab_id``.

    Args:
        doc_id: The document ID.
        tab_id: The tab's ID (from ``get_doc_outline``).
        title: New title for the tab, or ``None`` to leave unchanged.
        icon_emoji: New icon (single emoji, ≤8 UTF-8 bytes), or ``None``
            to leave unchanged.

    Returns:
        ``{"doc_id", "tab_id", "updated_fields": ["title", "iconEmoji"]}``.
    """
    if title is None and icon_emoji is None:
        raise ToolError("Provide at least one of title or icon_emoji")
    if icon_emoji is not None and len(icon_emoji.encode("utf-8")) > 8:
        raise ToolError("icon_emoji must be a single emoji (≤8 UTF-8 bytes)")
    try:
        creds = _get_credentials()
        _rename_tab(creds, doc_id, tab_id, title=title, icon_emoji=icon_emoji)
        updated = []
        if title is not None:
            updated.append("title")
        if icon_emoji is not None:
            updated.append("iconEmoji")
        return {"doc_id": doc_id, "tab_id": tab_id, "updated_fields": updated}
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def delete_tab(doc_id: str, tab_id: str) -> dict:
    """Delete a single tab (and its child tabs) from a Google Doc.

    Use ``get_doc_outline`` first to find the ``tab_id``. If the tab
    has child tabs they are deleted with it (per the Google Docs API
    contract for ``deleteTab``).

    Args:
        doc_id: The document ID.
        tab_id: The tab's ID.

    Returns:
        ``{"doc_id", "deleted_tab_id"}``.
    """
    try:
        creds = _get_credentials()
        _delete_tab(creds, doc_id, tab_id)
        return {"doc_id": doc_id, "deleted_tab_id": tab_id}
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def replace_all_text(
    doc_id: str,
    find: str,
    replace: str,
    match_case: bool = False,
    tab_ids: list[str] | None = None,
) -> dict:
    """Find-and-replace text across tabs.

    By default scope is ALL tabs (matches Google's default behavior
    when ``tabsCriteria`` is omitted). Pass ``tab_ids`` to scope to
    specific tabs — use ``get_doc_outline`` to find IDs first.

    Args:
        doc_id: Document ID.
        find: Substring to search for. Must be non-empty.
        replace: Replacement text (can be empty to delete matches).
        match_case: Whether matching is case-sensitive. Default False.
        tab_ids: Optional list of tab IDs to scope the replacement to.
            Omit (or pass None) to replace across every tab.

    Returns:
        ``{"occurrences_changed": int, "scope": "all_tabs" | [tab_ids]}``.
    """
    try:
        creds = _get_credentials()
        return _replace_all_text(
            creds, doc_id, find, replace,
            match_case=match_case, tab_ids=tab_ids,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def read_all_tabs(doc_id: str) -> dict:
    """Read body content of every tab in a Google Doc.

    Bulk equivalent of calling ``read_tab_content`` for each tab from
    ``get_doc_outline``. Returns paragraphs (style + text) for each
    tab in pre-order traversal.

    Args:
        doc_id: Document ID.

    Returns:
        ``{"doc_id", "tabs": [{tab_id, title, depth, paragraph_count,
        paragraphs: [{style, text}, ...]}, ...]}``.
    """
    try:
        creds = _get_credentials()
        return _read_all_tabs(creds, doc_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def set_tab_icons(doc_id: str, icons_by_title: dict[str, str]) -> dict:
    """Set or update icon emojis on existing tabs by title match.

    Title matching is case-insensitive substring: the first tab whose
    title contains the key (or whose title is contained in the key)
    gets the emoji. Useful right after ``convert_docx_to_tabbed_doc``
    when the caller wants to decorate the auto-named tabs without
    re-running the conversion.

    Args:
        doc_id: The Google Doc ID.
        icons_by_title: Map of tab title (or fragment) to a single
            emoji string. Example::

                {"Profile": "\U0001f464", "Experience": "\U0001f4bc",
                 "Skills": "\U0001f6e0️", "Education": "\U0001f393"}

            Each emoji must be ≤8 UTF-8 bytes (the Docs API limit).

    Returns:
        ``{"updated_count": int,
           "matched": {requested_title: tab_id, ...},
           "unmatched_titles": [...]}``.
    """
    if not icons_by_title:
        raise ToolError("icons_by_title cannot be empty")
    for key, emoji in icons_by_title.items():
        if emoji and len(emoji.encode("utf-8")) > 8:
            raise ToolError(
                f"icons_by_title[{key!r}] must be a single emoji "
                "(≤8 UTF-8 bytes)"
            )
    try:
        creds = _get_credentials()
        return _set_tab_icons(creds, doc_id, icons_by_title)
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def retrofit_existing_docx(
    markers: list[dict],
    docx_path: str | None = None,
    drive_file_id: str | None = None,
    title: str | None = None,
    icons_by_title: dict[str, str] | None = None,
    placeholder_behavior: Literal["delete", "rename", "keep"] = "delete",
    placeholder_title: str = "Overview",
    placeholder_icon: str = "\U0001f4d1",
    replace_doc_id: str | None = None,
) -> dict:
    """Inject Heading 1 markers into a styled .docx, then convert.

    For pre-existing styled documents (curriculum decks, branded
    deliverables) where section boundaries are table banners, not
    Heading paragraphs. The default converter can't split those —
    this tool injects synthetic Heading 1 paragraphs at the boundaries
    you specify, without rebuilding the doc or disturbing its
    formatting, then converts normally.

    Args:
        markers: ordered list of
            ``[{"marker_text": "...", "tab_title": "..."}, ...]``.
            Each ``marker_text`` is a short distinctive phrase that
            appears in the section's banner (case-sensitive substring
            match against visible text of paragraphs and tables).
            The matching block gets a Heading 1 paragraph with
            ``tab_title`` inserted before it.
        docx_path: Absolute path to a local ``.docx`` (local MCP only).
        drive_file_id: Drive file ID of an existing .docx OR Google Doc.
        title / icons_by_title / placeholder_*  / replace_doc_id:
            Pass-through to ``convert_docx_to_tabbed_doc``.

    Returns:
        Same shape as ``convert_docx_to_tabbed_doc``, plus
        ``"retrofit": {"markers_matched": int, "markers_missed": [...]}``.
        If zero markers matched, returns an ``error`` field with
        guidance and does not create a Google Doc.
    """
    path: Path | None = Path(docx_path).expanduser() if docx_path else None
    try:
        creds = _get_credentials()
        return _retrofit_existing_docx(
            creds,
            markers=markers,
            docx_path=path,
            drive_file_id=drive_file_id,
            title=title,
            icons_by_title=icons_by_title,
            placeholder_behavior=placeholder_behavior,
            placeholder_title=placeholder_title,
            placeholder_icon=placeholder_icon,
            replace_doc_id=replace_doc_id,
        )
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def preview_tab_split(
    docx_path: str | None = None,
    drive_file_id: str | None = None,
    split_by: Literal["heading_1", "heading_2", "page_break", "auto"] = "heading_1",
) -> dict:
    """Dry-run: report what tabs would be created without creating a doc.

    Validates a .docx (or already-on-Drive file) before you commit to
    a conversion. Surfaces: detected boundaries, titles, over-length
    titles (will be truncated to 80 chars), and zero-boundary cases.

    Args:
        docx_path: Absolute path to a local ``.docx`` (local MCP only).
        drive_file_id: Drive file ID of an existing .docx OR Google Doc.
            For Google Docs, we export as .docx via Drive then parse.
        split_by: Same as ``convert_docx_to_tabbed_doc``.

    Returns:
        ``{"split_strategy_used", "tab_count", "tabs":
        [{title, raw_title, warnings}, ...], "problems": [...]}``.
        Empty ``problems`` means the convert would proceed cleanly.
    """
    path: Path | None = Path(docx_path).expanduser() if docx_path else None
    try:
        creds = _get_credentials() if drive_file_id else None
        return _preview_tab_split(
            creds=creds, docx_path=path, drive_file_id=drive_file_id,
            split_by=split_by,
        )
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a single-use, time-limited signed URL for direct .docx upload.

    Built for the claude.ai cloud-chat workflow where the model's Python
    sandbox needs to POST a .docx to ``/api/convert`` but can't share
    credentials with the connector. The model calls this tool through
    the OAuth-protected MCP transport, gets back a self-authenticating
    URL, and hands it to its Python sandbox as a literal. The sandbox
    then does an ordinary ``requests.post(url, files=...)`` with NO
    Authorization header — the HMAC signature inside the URL is the
    credential.

    The URL is single-use (the server tracks consumed nonces) and
    expires after ``ttl_seconds`` (default 10 min, max 1 hour).

    Args:
        ttl_seconds: How long the URL stays valid. Default 600s; keep
            short to limit blast radius if the URL leaks into a chat
            transcript.
        max_bytes: Advisory upload size cap baked into the signature.
            Defaults to 50 MB (Drive's converter ceiling).

    Returns:
        ``{"url", "expires_at", "max_bytes", "nonce", "usage_hint"}``.
        ``usage_hint`` is a one-line Python snippet showing how to use
        the URL — the model copies it into the sandbox.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "https://sundeepg98-docs-mcp.fly.dev")
    signing_key = os.environ.get("MCP_BEARER_TOKEN")
    if not signing_key:
        raise ToolError(
            "MCP_BEARER_TOKEN env var not set on the server — "
            "signed URLs require it as the HMAC key."
        )
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ToolError(
            f"ttl_seconds must be 1..{MAX_TTL_SECONDS}, got {ttl_seconds}"
        )

    minted = sign_upload_url(
        base_url=f"{base}/api/convert",
        signing_key=signing_key,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
    )
    minted["usage_hint"] = (
        "requests.post(URL, files={'file': ('doc.docx', open('/path/to/doc.docx','rb'), "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}, "
        "data={'split_by': 'heading_1', 'icons_by_title': '<json-string>'})"
    )
    return minted


def _format_http_error(e: HttpError) -> str:
    return friendly_http_error_message(e)


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
