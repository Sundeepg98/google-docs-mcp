"""Google Docs MCP Server with native Tabs support.

Exposes MCP tools for working with native Google Docs tabs:
``gdocs_make_tabbed_doc``, ``gdocs_add_tabs``, ``gdocs_get_doc_outline``,
``gdocs_append_to_tab``, and ``gdocs_tab_existing_doc``.

The same entry point also implements one-off CLI commands for the
Apps Script setup needed by ``gdocs_tab_existing_doc``; see the
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
from .drive_api import (
    find_doc_by_title as _find_doc_by_title,
    move_to_folder as _move_to_folder,
    trash_drive_file as _trash_drive_file,
    untrash_drive_file as _untrash_drive_file,
)
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

_SERVER_INSTRUCTIONS = """\
This server creates and edits Google Docs with the native Tabs feature
(October 2024+ sidebar tabs). All tools are prefixed ``gdocs_``.

TOOL SELECTION — pick the right tool for what the user is asking:

1. NEW content you are composing in the conversation (you have the
   section titles and the text — no source file):
   -> gdocs_make_tabbed_doc(title, tabs=[{title, content (markdown),
      icon_emoji?, children?}])
   No file. No upload. One MCP call. This is the DEFAULT for any
   request like "make me a tabbed doc with these sections".

2. EXISTING .docx or Google Doc already on Drive — convert its
   heading structure into tabs:
   -> gdocs_tab_existing_doc(drive_file_id=..., split_by="heading_1")
   NEVER use this for content composed in chat — that is
   gdocs_make_tabbed_doc's job.

3. EXISTING styled doc on Drive with NO Heading 1 paragraphs (e.g.
   section banners are inside tables):
   -> gdocs_tab_existing_doc(drive_file_id=..., markers=[
        {marker_text, tab_title}, ...])
   Same tool as #2; passing ``markers`` triggers retrofit (injects
   synthetic Heading 1s before each marker block, then converts).

4. EDIT existing tabs in an existing doc:
   -> gdocs_rename_tab, gdocs_delete_tab, gdocs_set_tab_icons,
      gdocs_replace_all_text, gdocs_add_tabs, gdocs_append_to_tab

5. READ what is in a doc:
   -> gdocs_get_doc_outline (structure + icons, no body text)
   -> gdocs_read_doc(doc_id, tab_id?) (body text — one tab or all)

6. PREVIEW what a conversion would produce before committing:
   -> gdocs_preview_tab_split(docx_path or drive_file_id, split_by)

7. SANDBOX-built .docx that absolutely must ship raw bytes over HTTP
   (the file already exists as bytes in your sandbox and rebuilding
   from text would lose formatting):
   -> gdocs_get_signed_upload_url, then POST. Almost never right for
   new content from chat — prefer gdocs_make_tabbed_doc.

In doubt: if the request reads like "build me a doc with these
sections", use gdocs_make_tabbed_doc and nothing else.
"""

mcp = FastMCP("google-docs", instructions=_SERVER_INSTRUCTIONS)

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
def gdocs_make_tabbed_doc(title: str, tabs: list[TabSpec]) -> dict:
    """DEFAULT tool for building a tabbed Google Doc from text content.

    USE WHEN: You are composing a new document in the conversation —
    you have the section titles and the body text (as markdown) and
    want a Google Doc with one native tab per section. ONE MCP call,
    no file, no upload. This is the right answer for any request like
    "make me a tabbed doc with sections X, Y, Z" or "create a research
    doc covering these topics".

    DO NOT USE when: the source is an existing .docx or Google Doc on
    Drive — that is `gdocs_tab_existing_doc`'s job. Do not build a
    .docx in your sandbox just to call the .docx converter; if you have
    the text, this tool consumes it directly.

    Tabs are separately-navigable sidebar entries (Oct 2024+ Docs
    feature), not just outline headings. Tabs may have child tabs (and
    grandchildren) up to 3 levels deep.

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
def gdocs_add_tabs(
    doc_id: str,
    tabs: list[TabSpec],
    parent_tab_id: str | None = None,
) -> dict:
    """Append tabs to an existing Google Doc, optionally nested.

    Same nesting rules as ``gdocs_make_tabbed_doc``. Pass ``parent_tab_id``
    to make the new tabs children of an existing tab; omit it to add
    them as new root-level tabs.

    Args:
        doc_id: The document ID (from a prior ``gdocs_make_tabbed_doc`` or
            ``find_doc_by_title`` call, or the URL ``/d/{id}/edit``).
        tabs: Same shape as ``gdocs_make_tabbed_doc.tabs``.
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
def gdocs_get_doc_outline(doc_id: str) -> dict:
    """List every tab in a Google Doc with its structure (no body content).

    Useful as a discovery step before ``gdocs_append_to_tab`` or other
    targeted operations — you need a ``tab_id`` to call them.

    Args:
        doc_id: The document ID.

    Returns:
        ``{"doc_id", "trashed": bool, "tabs": [...]}``. Each entry in
        ``tabs`` is ``{"tab_id", "title", "parent_tab_id", "depth",
        "index", "icon_emoji"}`` in pre-order traversal. ``depth`` is
        0 for root tabs; ``parent_tab_id`` is null for root tabs;
        ``index`` is the sibling order under the parent.

        ``trashed`` flags whether the underlying Drive file is in
        trash. The file is hidden from the user's Drive UI when True,
        but API calls (including this one) still work. Surface this
        to the user before doing further edits — they probably didn't
        mean to keep editing a hidden file.
    """
    try:
        creds = _get_credentials()
        return _get_doc_outline(creds, doc_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_read_doc(
    doc_id: str,
    tab_id: str | None = None,
    tab_title: str | None = None,
) -> dict:
    """Read tab body content — one tab or all of them.

    USE WHEN: you need the actual text/paragraphs inside a doc.
    For tab structure WITHOUT body text (faster, smaller), use
    ``gdocs_get_doc_outline`` instead.

    Behavior:
    - ``tab_id`` given (exact) OR ``tab_title`` (first pre-order match):
      returns a single-tab dict.
    - Neither given: returns ALL tabs as a list (whole-doc dump).

    Returns:
        Single-tab mode: ``{"tab_id", "title", "paragraph_count",
        "table_count", "image_count", "paragraphs": [{"style", "text"},
        ...]}``.
        All-tabs mode: ``{"doc_id", "tabs": [{tab_id, title, depth,
        paragraph_count, paragraphs: [...]}, ...]}``.

    ``paragraphs[].style`` is a Docs namedStyleType (``HEADING_1``,
    ``NORMAL_TEXT``, etc.) or ``"TABLE"`` / ``"TOC"`` placeholders.
    ``text`` has trailing newlines stripped; inline images appear as
    ``[image]`` markers.
    """
    try:
        creds = _get_credentials()
        if tab_id is None and tab_title is None:
            return _read_all_tabs(creds, doc_id)
        return _read_tab_content(
            creds, doc_id, tab_id=tab_id, tab_title=tab_title
        )
    except ValueError as e:
        raise ToolError(str(e)) from e
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_append_to_tab(
    doc_id: str,
    tab_id: str,
    content: str,
    content_format: Literal["markdown", "text"] = "markdown",
) -> dict:
    """Append content to the end of an existing tab's body.

    Existing content is left untouched. Markdown is rendered with the
    same renderer as ``gdocs_make_tabbed_doc`` (headings, bold/italic,
    code, lists, links, blockquotes).

    Args:
        doc_id: The document ID.
        tab_id: The target tab's ID (get from ``gdocs_get_doc_outline`` or
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
def gdocs_tab_existing_doc(
    docx_path: str | None = None,
    drive_file_id: str | None = None,
    split_by: Literal["heading_1", "heading_2", "page_break", "auto"] = "heading_1",
    title: str | None = None,
    tab_icons: list[str] | None = None,
    icons_by_title: dict[str, str] | None = None,
    markers: list[dict] | None = None,
    case_sensitive: bool = False,
    placeholder_behavior: Literal["delete", "rename", "keep"] = "delete",
    placeholder_title: str = "Overview",
    placeholder_icon: str = "\U0001f4d1",
    replace_doc_id: str | None = None,
    docx_drive_file_id: str | None = None,
) -> dict:
    """Convert an EXISTING .docx or Google Doc on Drive into tabs.

    USE WHEN: the user already has a document on Drive (.docx or
    native Google Doc) and wants tabs based on its existing structure.

    DO NOT USE when: you are composing the document in the conversation.
    Use ``gdocs_make_tabbed_doc`` instead — give it the section titles
    and markdown content directly. Building a .docx in your sandbox just
    to feed this tool is the wrong shape; you'd lose formatting and add
    4 unnecessary steps.

    Two modes:
    1. ``split_by`` heading style (default). For docs that already have
       Heading 1 (or Heading 2 / page breaks) paragraphs.
    2. ``markers`` retrofit. For styled docs where section boundaries
       are visual (table banners, colored bars) and NO Heading 1
       paragraphs exist. Pass ``markers`` as ordered
       ``[{marker_text, tab_title}, ...]``; the tool injects synthetic
       Heading 1s before each marker block, then runs the normal
       conversion. Original formatting is preserved exactly — only
       headings are added.

    Input paths:
    - ``drive_file_id``: any .docx or Google Doc already on Drive.
      Auto-routes by mime type. PREFERRED.
    - ``docx_path``: absolute path on the SERVER's filesystem. Only
      works for local stdio MCP (Claude Code / Claude Desktop). Does
      NOT work from claude.ai cloud chat — the server cannot see your
      sandbox.
    - Sandbox-built .docx in cloud chat: call ``gdocs_get_signed_upload_url``
      then POST. Only do this if you genuinely have an existing .docx
      file in the sandbox (not a docx you just built from text).

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
            ``gdocs_set_tab_icons``.
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
        markers: Optional list of ``[{marker_text, tab_title}, ...]``
            for the retrofit mode. When provided, the tool injects
            synthetic Heading 1 paragraphs before each marker block
            before converting. Matching is Unicode-normalized
            (NFKC), whitespace-collapsed, case-insensitive by default,
            and tolerant of fragmented OOXML runs.
        case_sensitive: Default False. Only applies when ``markers``
            is given.

    Exactly one of ``docx_path`` or ``drive_file_id`` must be set.

    Returns:
        ``{"doc_id", "url", "tabs": [...], "split_strategy_used",
        "warnings": [], "info": [], ...}``. When ``markers`` was
        provided, also includes ``"retrofit": {"markers_matched",
        "markers_missed": [...]}``. If zero markers matched, returns
        an ``error`` field plus candidate_blocks for debugging.
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
        if markers:
            # Retrofit path: inject Heading 1s before each marker, then
            # convert. Single tool, two modes — discriminated by markers.
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
                case_sensitive=case_sensitive,
            )
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
def gdocs_rename_tab(
    doc_id: str,
    tab_id: str,
    title: str | None = None,
    icon_emoji: str | None = None,
) -> dict:
    """Rename a tab and/or set its icon emoji.

    Pass either ``title``, ``icon_emoji``, or both. At least one must
    be non-null. Use ``gdocs_get_doc_outline`` first to find the ``tab_id``.

    Args:
        doc_id: The document ID.
        tab_id: The tab's ID (from ``gdocs_get_doc_outline``).
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
async def gdocs_server_info() -> dict:
    """Server identity + full tool inventory — for change detection across sessions.

    USE WHEN: you want to confirm what version of the MCP you're
    talking to, detect renames/additions/removals between sessions,
    or verify a redeploy actually rolled out.

    The ``tools`` list is the COMPLETE registered tool inventory
    direct from the server's own registry — not filtered or summarized.
    Counting it and diffing across sessions is the canonical way for a
    caller to detect drift between what their cache thinks the server
    has and what it actually has.

    Returns:
        ``{"version", "build_time", "git_commit", "tool_count",
        "tools": [...]}``.
        ``build_time`` and ``git_commit`` are baked in at Docker build
        time via --build-arg; if the deploy script didn't pass them
        they show as ``"unknown"``.
    """
    # FastMCP's tool registry is async-accessed via list_tools().
    # Making this whole tool async lets us await it directly without
    # nested-event-loop gymnastics.
    try:
        tools = await mcp.list_tools()
        tool_names = sorted(t.name for t in tools)
    except Exception:  # noqa: BLE001
        tool_names = []

    # Read version via importlib.metadata to avoid the circular-import
    # trap (__init__.py imports server.main, so server can't import
    # __version__ from the partially-loaded package at module-load
    # time). Reading from installed package metadata is also more
    # honest — it reflects the wheel that's actually deployed.
    from importlib.metadata import version as _pkg_version
    try:
        ver = _pkg_version("google-docs-mcp")
    except Exception:  # noqa: BLE001
        ver = "unknown"
    return {
        "version": ver,
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
        "tool_count": len(tool_names),
        "tools": tool_names,
    }


@mcp.tool()
def gdocs_get_tab_url(doc_id: str, tab_id: str) -> dict:
    """Build a Google Docs URL that opens directly to a specific tab.

    USE WHEN: you want to give the user a link that lands them on a
    particular tab (e.g. after generating a tabbed doc, link them to
    the section they asked about). Cleaner than telling them "open the
    doc and click tab N".

    No API call — pure URL construction. Google Docs supports the
    ``?tab=t.<TAB_ID>`` query param natively; this just composes it.

    Args:
        doc_id: The document ID.
        tab_id: The tab's ID (from ``gdocs_get_doc_outline``).

    Returns:
        ``{"doc_id", "tab_id", "url"}`` — ``url`` is the deep link.
    """
    url = f"https://docs.google.com/document/d/{doc_id}/edit?tab={tab_id}"
    return {"doc_id": doc_id, "tab_id": tab_id, "url": url}


def _run_batch(
    items: list[str], fn, success_key: str
) -> dict:
    """Apply ``fn(creds, file_id)`` to each id, aggregate per-item.

    Used by the batch forms of trash/untrash. Each item's outcome is
    independent — a 403/404 on one doesn't stop the rest. Returns
    ``{results: [...], summary: {succeeded, skipped, failed}}`` where:
    - succeeded = item ended in the desired terminal state
    - skipped   = soft-failure (not_found, app_not_authorized)
    - failed    = unexpected hard error captured per-item
    """
    creds = _get_credentials()
    results: list[dict] = []
    succeeded = 0
    skipped = 0
    failed = 0
    for fid in items:
        try:
            r = fn(creds, fid)
            results.append(r)
            if r.get("reason"):
                skipped += 1
            elif r.get(success_key) is True or (
                success_key == "active" and r.get("trashed") is False
            ):
                succeeded += 1
            else:
                # Defensive — shouldn't happen
                skipped += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            results.append({
                "file_id": fid,
                "reason": "unexpected_error",
                "message": str(e)[:300],
            })
    return {
        "results": results,
        "summary": {
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
        },
    }


@mcp.tool()
def gdocs_find_doc_by_title(
    query: str,
    exact: bool = False,
    include_trashed: bool = False,
) -> dict:
    """Look up a Google Doc / .docx by title — find a file_id from a name.

    USE WHEN: you have a doc name (the user just told you, or it's
    from a past session) and need its file_id to call any other tool.
    Without this, the only way to get a file_id was to have it pasted
    into the conversation.

    Matches are returned newest-first by modified_time, so the most
    recent doc with that title is at index 0. Each match flags
    ``trashed`` and ``owned_by_app``:
    - ``trashed: true`` means the file is in Drive Trash (hidden from
      the user's Drive UI; recoverable for 30 days)
    - ``owned_by_app: true`` means this OAuth app's drive.file scope
      can write to it (trash, rename, move). False = read-only —
      app didn't create it.

    Args:
        query: Title text to search for.
        exact: True = exact title match. False (default) = substring
            ("contains") match.
        include_trashed: False (default) excludes trashed files from
            results. Pass True to surface them too.

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``. Empty matches
        means nothing matched — try a substring (exact=False) or
        broaden the query.
    """
    if not query.strip():
        raise ToolError("query cannot be empty")
    try:
        creds = _get_credentials()
        return _find_doc_by_title(
            creds, query,
            exact=exact, include_trashed=include_trashed,
        )
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_move_to_folder(file_id: str, folder_id: str) -> dict:
    """Move a Drive file into a folder (out of root or wherever it lives).

    USE WHEN: the MCP just created a doc (which lands in Drive root by
    default) and you want to file it into a project / curriculum
    folder. Also works for moving any existing file.

    Uses ``files.update(addParents, removeParents)`` — moves in place,
    not a copy. The file's content and ID are unchanged.

    Soft-failure (returned as data, not raised) matches the trash
    tools' contract so batch workflows can skip-and-continue:
    - ``reason: "not_found"`` — file_id doesn't resolve
    - ``reason: "folder_not_found"`` — folder_id doesn't resolve OR
      points at something that isn't a folder
    - ``reason: "app_not_authorized"`` — OAuth app's drive.file scope
      can't write to this file (file wasn't created by this app)

    Args:
        file_id: The file to move.
        folder_id: The destination folder's Drive ID.

    Returns:
        Success: ``{file_id, name, mimeType, parents: [folder_id, ...]}``.
        No-op (already there): same shape plus ``note`` explaining.
        Soft-failure: ``{file_id, reason, message, ...}``.
    """
    try:
        creds = _get_credentials()
        return _move_to_folder(creds, file_id, folder_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_untrash_file(file_id) -> dict:
    """Restore a trashed Drive file back to its original location.

    Inverse of ``gdocs_trash_file``. Ships together so a wrong trash
    call by the agent is recoverable. Works only within Drive's 30-day
    trash window — beyond that the file is permanently gone and this
    returns ``reason: "not_found"``.

    Uses ``files.update(trashed=False)``. Same soft-failure handling
    as ``gdocs_trash_file`` (404 and 403 returned as data, not raised),
    so batch restores can skip-and-continue.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch untrash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input ID — independent outcomes.

    Returns (single-ID mode):
        Success: ``{"file_id", "name", "mimeType", "trashed": False,
        "was_already_active": bool}``. ``was_already_active=True``
        means the file wasn't trashed to begin with (idempotent no-op).
        Soft-failure: ``{"file_id", "trashed": <current>, "reason",
        "message"}`` with ``reason`` in {``"not_found"``,
        ``"app_not_authorized"``}.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _untrash_drive_file, "active")
    try:
        creds = _get_credentials()
        return _untrash_drive_file(creds, file_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_trash_file(file_id) -> dict:
    """Move a Drive file (Google Doc, .docx, anything) to trash.

    USE WHEN: you need to clean up an obsolete Drive file — a
    superseded conversion, a test doc, a broken output. ``gdocs_delete_tab``
    only removes a tab within a doc; this removes the whole document
    (or any other Drive file by ID).

    Uses ``files.update(trashed=True)``, NOT ``files.delete``. The file
    moves to Drive Trash and is recoverable for 30 days. Permanent
    deletion is intentionally not exposed.

    Idempotent: trashing an already-trashed file succeeds and the
    response flags ``was_already_trashed: true``.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch trash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input — each item processed
            independently (one soft-failure does not abort the rest).

    Returns (single-ID mode):
        ``{"file_id", "name", "mimeType", "trashed": True,
        "was_already_trashed": bool}``. ``name`` lets the caller confirm
        the right file was touched.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _trash_drive_file, "trashed")
    try:
        creds = _get_credentials()
        return _trash_drive_file(creds, file_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_delete_tab(doc_id: str, tab_id: str) -> dict:
    """Delete a single tab (and its child tabs) from a Google Doc.

    Use ``gdocs_get_doc_outline`` first to find the ``tab_id``. If the tab
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
def gdocs_replace_all_text(
    doc_id: str,
    find: str,
    replace: str,
    match_case: bool = False,
    tab_ids: list[str] | None = None,
) -> dict:
    """Find-and-replace text across tabs.

    By default scope is ALL tabs (matches Google's default behavior
    when ``tabsCriteria`` is omitted). Pass ``tab_ids`` to scope to
    specific tabs — use ``gdocs_get_doc_outline`` to find IDs first.

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
def gdocs_set_tab_icons(doc_id: str, icons_by_title: dict[str, str]) -> dict:
    """Set or update icon emojis on existing tabs by title match.

    Title matching is case-insensitive substring: the first tab whose
    title contains the key (or whose title is contained in the key)
    gets the emoji. Useful right after ``gdocs_tab_existing_doc``
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
def gdocs_preview_tab_split(
    docx_path: str | None = None,
    drive_file_id: str | None = None,
    split_by: Literal["heading_1", "heading_2", "page_break", "auto"] = "heading_1",
) -> dict:
    """Dry-run: report what tabs would be created without creating a doc.

    Validates a .docx (or already-on-Drive file) before you commit to
    a conversion. Surfaces: detected boundaries, titles, over-length
    titles (will be truncated to 50 chars — the Google Docs API limit),
    and zero-boundary cases.

    Args:
        docx_path: Absolute path to a local ``.docx`` (local MCP only).
        drive_file_id: Drive file ID of an existing .docx OR Google Doc.
            For Google Docs, we export as .docx via Drive then parse.
        split_by: Same as ``gdocs_tab_existing_doc``.

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
def gdocs_get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a signed URL ONLY for uploading an existing .docx file's bytes.

    USE WHEN: you genuinely have an existing .docx file in your Python
    sandbox (e.g. one a user uploaded, one a pipeline produced) and
    need to POST its raw bytes to /api/convert from cloud chat. The
    signed URL is the credential — no Authorization header needed.

    DO NOT USE when:
    - You are composing new content from text. Use ``gdocs_make_tabbed_doc``
      — it takes markdown directly and skips this upload dance entirely.
      Building a .docx in the sandbox just to upload it here is pointless
      extra work.
    - The .docx already lives on Drive. Use
      ``gdocs_tab_existing_doc(drive_file_id=...)`` instead.

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
