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
# v1.1+ multi-tenant cloud auth — imported lazily-via-function so stdio
# users without the OAuth env vars don't trip import-time errors.
from .credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from .gas_deploy import GAS_DEPLOY_SCOPES
from .oauth_google import resolve_runtime_oauth_config
from .setup_apps_script import (
    setup_apps_script_auto,
    setup_apps_script_for_user,
)

_SERVER_INSTRUCTIONS = """\
google-docs-fly — create, edit, read, and manage Google Docs with
native sidebar Tabs (October 2024+ feature). All tools prefixed gdocs_.

START HERE: call ``gdocs_guide()`` for the orientation as a structured
payload, or ``gdocs_server_info()`` for build version + verified CI
test status.

THE 5 CORE WORKFLOWS
====================

1. NEW DOC from content composed in chat
   Goal: build a tabbed doc from text you have in the conversation.
   Tools: gdocs_make_tabbed_doc(title, tabs=[{title, content, ...}])
   Notes: ONE call. No file. No upload. DEFAULT for any request like
   "make me a doc with sections X, Y, Z".

2. CONVERT EXISTING DOC with Heading 1 paragraphs
   Goal: take a Google Doc / .docx on Drive that already has H1s and
   turn each H1 section into its own native tab.
   Tools: gdocs_preview_tab_split(drive_file_id=..., split_by="heading_1")
          -> gdocs_tab_existing_doc(drive_file_id=..., split_by="heading_1")
          -> gdocs_get_doc_outline(doc_id=...)   # verify the result
   Notes: Preview first — destructive conversion is one-way.

3. RETROFIT STYLED DOC with NO Heading 1s
   Goal: a styled doc where section breaks aren't H1s (banners in
   styled tables, shaded paragraphs, etc.).
   Tools: gdocs_tab_existing_doc(drive_file_id=...,
              markers=[{marker_text, tab_title}, ...])
   Notes: Same tool as #2; passing ``markers`` triggers RETROFIT mode
   (injects synthetic H1s before each marker block, then converts).
   NEVER rebuild a styled .docx from text — formatting would be lost.
   Use retrofit instead.

4. CONVERT SANDBOX .docx (bytes only, no Drive file)
   Goal: convert a .docx the model has built / has as raw bytes in
   its sandbox (cloud chat scenario).
   Tools: gdocs_get_signed_upload_url(...) -> POST {url} with the
          .docx bytes as multipart upload
   Notes: ``docx_path`` arguments DO NOT WORK from cloud chat — the
   server cannot see the caller's filesystem. Signed-URL upload is
   the only sandbox-bytes path. The POST is equivalent to
   gdocs_tab_existing_doc; use this when the .docx lives in your
   sandbox rather than on Drive.

5. CLEANUP — trash / restore Drive files
   Tools: gdocs_trash_file(file_id), gdocs_untrash_file(file_id)
   Notes: ONLY acts on files this app created. Files created
   elsewhere return app_not_authorized (no recovery — the file
   belongs to its owner). file_id accepts a string or list (batch).

NON-OBVIOUS OPERATING RULES
===========================
- Never rebuild a styled .docx from text. Retrofit (workflow #3)
  preserves formatting; rebuilding loses it.
- ``docx_path`` arguments do NOT work from cloud chat — the server
  cannot see the caller's filesystem. Use signed-URL upload
  (workflow #4) or drive_file_id.
- ``placeholder_behavior="rename"`` preserves a title / index page;
  the default "remove" deletes it. Use "rename" when the source has
  a meaningful cover page worth keeping.
- This app can only trash files IT created. Drive returns
  appNotAuthorizedToFile (403) on others; the file belongs to its
  owner and only they can trash it.
- First use requires interactive Google OAuth consent. The client
  must open the consent URL in a browser — it cannot be automated.
  Subsequent calls reuse the cached token until it expires.

EDIT TOOLS (after creating / converting)
========================================
gdocs_rename_tab, gdocs_delete_tab, gdocs_set_tab_icons,
gdocs_replace_all_text, gdocs_add_tabs, gdocs_append_to_tab

READ TOOLS
==========
gdocs_get_doc_outline — structure + icons, no body text (cheap)
gdocs_read_doc(doc_id, tab_id?) — body text, one tab or all
gdocs_get_tab_url(doc_id, tab_id) — direct deep-link to a tab

DRIVE MANAGEMENT
================
gdocs_find_doc_by_title, gdocs_move_to_folder,
gdocs_trash_file, gdocs_untrash_file

INTROSPECTION
=============
gdocs_guide() — this orientation as a structured payload
gdocs_server_info() — version + verified CI test status (digest,
  ci_run_url, mutation_check with stale_patches / imprecise_patches)
gdocs_test_manifest() — full test inventory + per-test outcomes
"""

mcp = FastMCP("google-docs", instructions=_SERVER_INSTRUCTIONS)
# auth=None at construction so stdio (Claude Desktop / Code) runs
# without auth middleware. HTTP transport sets mcp.auth = GoogleProvider
# at startup via configure_auth_for_http() — see main() and Phase 7.

# Lazy module-level cache for the stdio/no-auth-context path. HTTP
# mode bypasses this entirely — see _get_credentials() below.
_creds_cache = None


def _get_credentials():
    """Return valid Google API Credentials for the caller.

    Two modes, transparently:

    - **HTTP / multi-tenant** (FastMCP has an auth provider, calling
      user identified by ``get_access_token().claims["sub"]``):
      resolve via ``credentials.get_credentials_for_user``. Refreshes
      per-user, persists back to user_store. On NeedsReauthError,
      raises ToolError with a Markdown link to the consent URL — the
      Claude client renders the URL as clickable.

    - **Stdio / single-tenant** (no auth context, local trust model):
      operator's cached OAuth token at ``~/.google-docs-mcp/token.json``,
      lazy-loaded and cached in-process. Preserves the v1.0 stdio
      experience bit-for-bit.

    The mode branch is observable via ``current_user_id_or_none()``
    returning a value vs None. Until Phase 7 wires GoogleProvider, the
    HTTP path is dormant and all callers fall into the stdio branch.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        global _creds_cache
        if _creds_cache is None or not _creds_cache.valid:
            _creds_cache = load_credentials(default_data_dir())
        return _creds_cache

    try:
        return get_credentials_for_user(
            user_id, **resolve_runtime_oauth_config(),
        )
    except NeedsReauthError as e:
        raise ToolError(
            f"Google API access required.\n\n"
            f"**[Click here to authorize]({e.auth_url})**\n\n"
            f"After granting access, re-run this tool."
        ) from e


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

    Choreography: typically the FIRST tool in the `new_doc` workflow.
    Follow with ``gdocs_get_tab_url`` (deep-link any tab) or
    ``gdocs_get_doc_outline`` (verify). For existing files use
    ``gdocs_tab_existing_doc`` instead — not this tool.
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

    Choreography: typically preceded by ``gdocs_get_doc_outline`` to
    know the existing structure (and pick a ``parent_tab_id`` if
    nesting). For a brand-new doc use ``gdocs_make_tabbed_doc`` —
    don't create an empty doc then add tabs.
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

    Choreography: the universal discovery step. Typical patterns:
    (a) after ``gdocs_tab_existing_doc`` to verify the resulting
        structure;
    (b) before edit tools (``gdocs_rename_tab``, ``gdocs_delete_tab``,
        ``gdocs_set_tab_icons``, ``gdocs_append_to_tab``) to obtain
        the ``tab_id`` they need;
    (c) before ``gdocs_get_tab_url`` to compose a deep-link.
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

    Choreography: typically preceded by ``gdocs_get_doc_outline`` to
    pick the tab_id (single-tab mode). For tab structure WITHOUT body
    text use ``gdocs_get_doc_outline`` (faster, smaller).
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

    Choreography: get the tab_id from ``gdocs_get_doc_outline`` first
    (or from a prior create/add call). To create new tabs (not append
    to existing) use ``gdocs_add_tabs`` instead.
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

    Choreography:
    - Typically preceded by ``gdocs_preview_tab_split`` to validate
      the split before this (destructive, one-way) conversion call.
    - Follow with ``gdocs_get_doc_outline`` to verify the resulting
      tab structure.

    NOTE: ``docx_path`` does NOT work from cloud chat — the server
    cannot see the caller's filesystem. For sandbox .docx bytes, use
    ``gdocs_get_signed_upload_url`` and POST instead.
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

    Choreography: get the tab_id from ``gdocs_get_doc_outline`` first.
    For multi-tab icon edits use ``gdocs_set_tab_icons`` (title-keyed
    batch).
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

    Choreography: typical introspection trio — pair with
    ``gdocs_guide()`` (workflows + rules + tool groupings) and
    ``gdocs_test_manifest()`` (full per-test inventory). Cheap; no
    Google API call required.
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

    # Append GIT_COMMIT as semver build metadata so every deploy from
    # a distinct commit reports a unique version string — without
    # requiring a manual pyproject bump on every hot-fix. Format
    # follows semver §10: `version+buildmetadata`. PEP 440 also
    # tolerates `+local` segments for the same purpose.
    git_commit = os.environ.get("GIT_COMMIT", "unknown")
    if git_commit and git_commit != "unknown":
        ver = f"{ver}+{git_commit}"

    return {
        "version": ver,
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "git_commit": git_commit,
        "tool_count": len(tool_names),
        "tools": tool_names,
        "test_suite": _read_test_suite_status(git_commit),
    }


def _find_test_results_path() -> Path | None:
    """Locate the test-results.json artifact.

    Container path first (/app/test-results.json, populated by
    Dockerfile COPY), then CWD as local-dev fallback. Evaluated at
    call time — NOT at import — so monkeypatched cwds in tests work.
    """
    candidates = [
        Path("/app/test-results.json"),
        Path.cwd() / "test-results.json",
    ]
    return next((p for p in candidates if p.exists()), None)


def _canonical_digest(data: dict) -> str:
    """SHA-256 of the JSON with ``_meta`` removed, sorted-key serialized.

    The digest is computed over everything EXCEPT the ``_meta`` block
    (because the digest itself lives inside _meta — chicken/egg).
    Canonicalization (sort_keys + tight separators) gives a stable
    hash regardless of Python's dict-iteration order.
    """
    import hashlib
    import json as _json
    payload = {k: v for k, v in data.items() if k != "_meta"}
    canon = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _read_test_suite_status(deployed_commit: str) -> dict:
    """Surface the CI test-suite status baked into the build.

    deploy.sh writes ``test-results.json`` via pytest-json-report,
    embeds ``_git_commit`` + ``_ci_run_url`` + ``_meta.digest``, and
    the Dockerfile COPIes it into the image. If the file's absent or
    unparseable (vanilla `docker build` skips it; SKIP_TESTS writes a
    stub), return ``{"status": "unknown"}`` per the documented
    contract.

    **Tamper detection.** At read time we re-canonicalize the JSON
    (minus ``_meta``) and compare the recomputed digest against the
    stored one. If they diverge, somebody edited the file
    post-build — return ``status: "tampered"`` so a caller can
    distinguish "the suite passed but someone fiddled with the
    numbers" from a legitimate pass.

    ``test_suite.commit`` should equal the running build's
    ``git_commit``; divergence means the image shipped without a
    matching test run — itself a red flag worth surfacing.
    """
    import json
    from datetime import datetime, timezone

    # mutation_check is independent state (separate artifact), so it
    # gets attached to whatever we return — even the unknown branches.
    # Callers can rely on the field always being present.
    mutation_check = _read_mutation_check()

    path = _find_test_results_path()
    if path is None:
        return {"status": "unknown", "mutation_check": mutation_check}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "mutation_check": mutation_check}

    summary = data.get("summary") or {}
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))

    # pytest-json-report's "created" is a unix timestamp; convert to
    # ISO 8601 UTC. SKIP_TESTS stub doesn't include "created" — fall
    # back to "unknown".
    created_ts = data.get("created")
    if isinstance(created_ts, (int, float)):
        last_run = datetime.fromtimestamp(
            created_ts, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    else:
        last_run = "unknown"

    # Test-suite commit + CI run URL written by deploy.sh.
    test_commit = data.get("_git_commit", "unknown")
    ci_run_url = data.get("_ci_run_url", "")

    # Report digest verification — tamper detection.
    stored_meta = data.get("_meta") or {}
    stored_digest = stored_meta.get("digest", "")
    recomputed_digest = _canonical_digest(data)
    digest_matches = bool(stored_digest) and stored_digest == recomputed_digest

    # Status logic: must have a populated summary AND zero failures
    # AND the digest must verify. SKIP_TESTS stub has empty summary
    # → status="unknown" naturally. Mismatched digest → "tampered"
    # even if the numbers look green.
    if not summary:
        status = "unknown"
    elif stored_digest and not digest_matches:
        status = "tampered"
    elif failed == 0 and passed > 0:
        status = "passed"
    else:
        status = "failed"

    return {
        "last_run": last_run,
        "commit": test_commit,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "status": status,
        "ci_run_url": ci_run_url,
        "report_digest": stored_digest,
        "mutation_check": mutation_check,
    }


def _read_mutation_check() -> dict:
    """Surface mutation-test results baked into the build.

    Reads /app/mutation-check.json (CWD fallback for local dev),
    produced by scripts/mutation_check.py in CI. Summarizes to
    {ran, caught, status, asleep_guards, stale_patches,
    imprecise_patches}. Missing file → unknown.

    Failure modes the gate distinguishes (v1.2.2+):
      asleep_guards     — patch applied but the named guard didn't
                          notice the bug (test rot).
      stale_patches     — patch's `find` text is gone, or applied
                          without tripping anything (mutation rot).
      imprecise_patches — patch broke the target AND unrelated tests
                          (over-broad mutation).

    Status "passed" only when caught == ran AND all three buckets are
    empty. Pre-1.2.2 artifacts (no stale/imprecise fields) default
    the new fields to [] for back-compat.
    """
    import json

    candidates = [
        Path("/app/mutation-check.json"),
        Path.cwd() / "mutation-check.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {"status": "unknown", "ran": 0, "caught": 0}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "ran": 0, "caught": 0}

    return {
        "ran": int(data.get("ran", 0)),
        "caught": int(data.get("caught", 0)),
        "status": data.get("status", "unknown"),
        "asleep_guards": list(data.get("asleep_guards", [])),
        "stale_patches": list(data.get("stale_patches", [])),
        "imprecise_patches": list(data.get("imprecise_patches", [])),
    }


@mcp.tool()
def gdocs_test_manifest() -> dict:
    """List every test in the CI artifact + its pass/fail outcome.

    Read / verify / audit / inspect / list the test inventory of the
    running build. Use to: confirm specific named regression guards
    (e.g. test_owned_by_app_consistency) actually exist and passed,
    spot-check what "203 passed" means, find which test failed if
    test_suite.status is not "passed".

    Returned shape:
        {
          status: "ok" | "unknown" | "tampered",
          total: int,
          tests: [{nodeid: str, outcome: "passed"|"failed"|"skipped"}, ...],
          named_regression_guards: {
            present: [list of named-guard test ids found in the suite],
            missing: [list of named guards NOT found — should be empty],
          },
        }

    Status "unknown" when the artifact's missing/unparseable;
    "tampered" when the report_digest doesn't match the canonicalized
    payload (same logic as gdocs_server_info.test_suite); "ok"
    otherwise.

    Choreography: pairs with ``gdocs_server_info.test_suite``. The
    summary is in server_info; this tool gives the full per-test
    breakdown. No Google API call.
    """
    import json

    path = _find_test_results_path()
    if path is None:
        return {
            "status": "unknown",
            "reason": "test-results.json not found in container",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "reason": "test-results.json unparseable",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    stored_digest = (data.get("_meta") or {}).get("digest", "")
    digest_matches = stored_digest and stored_digest == _canonical_digest(data)
    if stored_digest and not digest_matches:
        return {
            "status": "tampered",
            "reason": "report_digest mismatch — file was edited after CI",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    tests_raw = data.get("tests") or []
    tests = [
        {"nodeid": t.get("nodeid", ""), "outcome": t.get("outcome", "")}
        for t in tests_raw
    ]

    # The 8 named regression guards from v1.1.x — see CHANGELOG and
    # tests/unit/test_*.py docstrings. If any are missing the suite's
    # coverage of cycle bugs has regressed.
    REQUIRED_GUARDS = [
        "test_owned_by_app_agrees_with_trash_outcome",
        "test_trash_file_id_accepts_str_or_list",
        "test_inject_matches_fragmented_runs",
        "test_deploy_webapp_body_does_not_include_entryPoints",
        "test_preview_flags_what_convert_truncates",
        "test_auth_pkce_consistency_every_url",
        "test_tool_descriptions_truthful",
        "test_tool_discoverability_via_server_info",
    ]
    test_names = {t["nodeid"].split("::")[-1].split("[")[0] for t in tests}
    present = [g for g in REQUIRED_GUARDS if g in test_names]
    missing = [g for g in REQUIRED_GUARDS if g not in test_names]

    return {
        "status": "ok",
        "total": len(tests),
        "tests": tests,
        "named_regression_guards": {"present": present, "missing": missing},
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

    Choreography: get the tab_id from ``gdocs_get_doc_outline`` first.
    Often called as the FINAL step of new-doc / convert workflows to
    hand the user a direct link to the right tab. No API call.
    """
    url = f"https://docs.google.com/document/d/{doc_id}/edit?tab={tab_id}"
    return {"doc_id": doc_id, "tab_id": tab_id, "url": url}


@mcp.tool()
def gdocs_guide() -> dict:
    """Orientation payload — the "start here" / --help for this server.

    Returns the same content as the connect-time server ``instructions``
    string, as a structured dict so it is machine-readable and always
    callable. Use when:

    - the client truncated or ignored connect-time instructions
    - you want machine-readable workflow choreography / tool groupings
    - you need to confirm which tools belong to which workflow before
      sequencing a multi-tool plan

    No arguments. No side effects. Cheap (no API calls). Typically the
    first call an agent makes after connecting — pairs naturally with
    ``gdocs_server_info()`` (version + verified CI test status).

    Returned shape:
        {
          server: {name, version, what_it_does, all_tools_prefixed,
                   more_info},
          workflows: [{name, goal, tool_sequence, notes}, ...],
          operating_rules: [str, ...],
          tool_groups: {build_new: [...], convert_existing: [...],
                        edit_tabs: [...], read: [...],
                        drive_management: [...], setup_and_auth: [...],
                        introspection: [...]},
        }
    """
    from . import __version__

    return {
        "server": {
            "name": "google-docs-fly",
            "version": __version__,
            "what_it_does": (
                "Create, edit, read, and manage Google Docs with native "
                "sidebar Tabs (October 2024+ feature)."
            ),
            "all_tools_prefixed": "gdocs_",
            "more_info": (
                "Call gdocs_server_info for build version + verified CI "
                "test status (digest, ci_run_url, mutation_check)."
            ),
        },
        "workflows": [
            {
                "name": "new_doc",
                "goal": "Build a tabbed doc from content composed in chat",
                "tool_sequence": ["gdocs_make_tabbed_doc"],
                "notes": (
                    "ONE call. No file. No upload. DEFAULT for any 'make "
                    "me a doc with sections X, Y, Z' request."
                ),
            },
            {
                "name": "convert_doc_with_headings",
                "goal": (
                    "Convert an existing Drive doc that already has "
                    "Heading 1 paragraphs into tabs"
                ),
                "tool_sequence": [
                    "gdocs_preview_tab_split",
                    "gdocs_tab_existing_doc",
                    "gdocs_get_doc_outline",
                ],
                "notes": (
                    "Preview first to validate the split; convert; then "
                    "outline to verify the result. Conversion is one-way."
                ),
            },
            {
                "name": "retrofit_styled_doc",
                "goal": (
                    "Retrofit a styled doc that has NO Heading 1 "
                    "paragraphs (e.g. banners inside styled tables)"
                ),
                "tool_sequence": [
                    "gdocs_tab_existing_doc(markers=[...])",
                ],
                "notes": (
                    "Same tool as convert_doc_with_headings; passing "
                    "`markers` triggers retrofit mode (injects synthetic "
                    "H1s before each marker block, then converts). NEVER "
                    "rebuild a styled .docx from text — formatting would "
                    "be lost."
                ),
            },
            {
                "name": "convert_sandbox_docx",
                "goal": (
                    "Convert a .docx that exists only as bytes in the "
                    "caller's sandbox (cloud chat scenario)"
                ),
                "tool_sequence": [
                    "gdocs_get_signed_upload_url",
                    "POST {url}",
                ],
                "notes": (
                    "`docx_path` does NOT work from cloud chat — the "
                    "server cannot see the caller's filesystem. The POST "
                    "is equivalent to gdocs_tab_existing_doc; use this "
                    "route when the .docx is in your sandbox."
                ),
            },
            {
                "name": "cleanup",
                "goal": "Trash / restore Drive files this app created",
                "tool_sequence": [
                    "gdocs_trash_file",
                    "gdocs_untrash_file",
                ],
                "notes": (
                    "ONLY acts on files this app created; others return "
                    "app_not_authorized. file_id accepts a string or "
                    "list (batch)."
                ),
            },
        ],
        "operating_rules": [
            (
                "Never rebuild a styled .docx from text. Use retrofit "
                "(workflow `retrofit_styled_doc`) to preserve formatting."
            ),
            (
                "`docx_path` arguments do NOT work from cloud chat — the "
                "server cannot see the caller's filesystem. Use "
                "signed-URL upload (workflow `convert_sandbox_docx`) or "
                "drive_file_id."
            ),
            (
                "`placeholder_behavior='rename'` preserves a title / "
                "index page; the default 'remove' deletes it. Use "
                "'rename' when the source has a meaningful cover page."
            ),
            (
                "Trash tools only act on files THIS app created. Drive "
                "returns appNotAuthorizedToFile (403) on others; the "
                "file belongs to its owner and only they can trash it."
            ),
            (
                "First use requires interactive Google OAuth consent. "
                "The client must open the consent URL in a browser — "
                "this cannot be automated. Subsequent calls reuse the "
                "cached token until it expires."
            ),
        ],
        "tool_groups": {
            "build_new": ["gdocs_make_tabbed_doc"],
            "convert_existing": [
                "gdocs_preview_tab_split",
                "gdocs_tab_existing_doc",
                "gdocs_get_signed_upload_url",
            ],
            "edit_tabs": [
                "gdocs_rename_tab",
                "gdocs_delete_tab",
                "gdocs_set_tab_icons",
                "gdocs_replace_all_text",
                "gdocs_add_tabs",
                "gdocs_append_to_tab",
            ],
            "read": [
                "gdocs_get_doc_outline",
                "gdocs_read_doc",
                "gdocs_get_tab_url",
            ],
            "drive_management": [
                "gdocs_find_doc_by_title",
                "gdocs_move_to_folder",
                "gdocs_trash_file",
                "gdocs_untrash_file",
            ],
            "setup_and_auth": [
                "gdocs_setup_apps_script",
                "gdocs_reset_authorization",
            ],
            "introspection": [
                "gdocs_server_info",
                "gdocs_test_manifest",
                "gdocs_guide",
            ],
        },
    }


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
    verify_writable: bool = True,
) -> dict:
    """Look up a Google Doc / .docx by title — find a file_id from a name.

    USE WHEN: you have a doc name (the user just told you, or it's
    from a past session) and need its file_id to call any other tool.

    Matches return newest-first by modified_time. Each match flags
    ``trashed`` and ``owned_by_app``:
    - ``trashed: true`` means the file is in Drive Trash (hidden from
      the user's Drive UI; recoverable for 30 days)
    - ``owned_by_app: true`` means this OAuth app's drive.file scope
      can ACTUALLY write to it — i.e. ``gdocs_trash_file`` /
      ``gdocs_untrash_file`` / ``gdocs_move_to_folder`` will succeed.
      This is verified via a batched no-op write probe (NOT inferred
      from user-level capabilities which can disagree).

    Args:
        query: Title text to search for.
        exact: True = exact title match. False (default) = substring
            ("contains") match.
        include_trashed: False (default) excludes trashed files from
            results.
        verify_writable: True (default) probes each match with a
            batched no-op update to determine actual writability under
            this app's drive.file scope. Pass False to skip the probe
            (faster, but ``owned_by_app`` will be ``None`` and the
            caller must verify before mutating).

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``.
        ``owned_by_app`` is ``True``/``False`` if probed, ``None`` if
        ``verify_writable=False``.

    Choreography: returns a ``file_id`` that feeds straight into
    ``gdocs_tab_existing_doc`` (drive_file_id), ``gdocs_move_to_folder``,
    ``gdocs_trash_file``, ``gdocs_read_doc`` (as doc_id for Google
    Docs), and ``gdocs_get_doc_outline``. Check ``owned_by_app``
    before any write — others fail with app_not_authorized.
    """
    if not query.strip():
        raise ToolError("query cannot be empty")
    try:
        creds = _get_credentials()
        return _find_doc_by_title(
            creds, query,
            exact=exact,
            include_trashed=include_trashed,
            verify_writable=verify_writable,
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

    Choreography: file_id typically from ``gdocs_find_doc_by_title`` or
    from a prior create call. ``folder_id`` from the user (URL) or
    ``gdocs_find_doc_by_title`` with mimeType filter — Drive folder
    IDs look identical to file IDs.

    NOTE: same app-ownership constraint as the trash tools — moving a
    file this app didn't create returns ``reason: "app_not_authorized"``.
    """
    try:
        creds = _get_credentials()
        return _move_to_folder(creds, file_id, folder_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_untrash_file(file_id: str | list[str]) -> dict:
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

    Choreography: pairs with ``gdocs_trash_file`` for recovery.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` — the
    file belongs to its owner and only they can restore it.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _untrash_drive_file, "active")
    try:
        creds = _get_credentials()
        return _untrash_drive_file(creds, file_id)
    except HttpError as e:
        raise ToolError(_format_http_error(e)) from e


@mcp.tool()
def gdocs_trash_file(file_id: str | list[str]) -> dict:
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

    Choreography: pair with ``gdocs_untrash_file`` for recovery within
    Drive's 30-day trash window. file_id often comes from
    ``gdocs_find_doc_by_title`` or from a prior create call.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` (HTTP
    403 appNotAuthorizedToFile) — the file belongs to its owner and
    only they can trash it. The agent has no recovery; surface to
    the user.
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

    Choreography: get the tab_id from ``gdocs_get_doc_outline`` first.
    To delete an entire DOCUMENT (not just one tab) use
    ``gdocs_trash_file`` instead.
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

    Choreography: globally scoped by default — no tab_id needed for
    whole-doc find/replace. For per-tab scope, get tab_ids from
    ``gdocs_get_doc_outline`` first.
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

    Choreography: get the current tab titles from
    ``gdocs_get_doc_outline`` first so your keys actually match.
    Often paired right after ``gdocs_tab_existing_doc`` to decorate
    the auto-named tabs.
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

    Choreography: typically called BEFORE ``gdocs_tab_existing_doc``
    to validate the split — conversion is one-way and destructive,
    so the preview lets you confirm titles / catch zero-boundary
    cases before committing.

    NOTE: ``docx_path`` does NOT work from cloud chat — the server
    cannot see the caller's filesystem. Use ``drive_file_id`` (or
    upload via ``gdocs_get_signed_upload_url`` first then convert).
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

    Choreography: this is the FIRST step of the `convert_sandbox_docx`
    workflow. Mint the URL here, then POST the .docx bytes to that URL
    from the sandbox. The POST is equivalent to
    ``gdocs_tab_existing_doc`` — use this route when the .docx lives
    only as bytes in the sandbox rather than on Drive.

    NOTE: ``docx_path`` arguments on other tools do NOT work from
    cloud chat (server can't see the caller's filesystem); this
    signed-URL upload flow is the sandbox-bytes path.
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


# current_user_id_or_none lives in credentials.py so docx_import et al
# can share it without circular imports.


@mcp.tool()
def gdocs_setup_apps_script() -> dict:
    """One-shot setup of the Apps Script Web App needed for lossless retrofit.

    Run this once per user (cloud) or once per machine (local stdio)
    to enable ``gdocs_tab_existing_doc`` — the path that uses Apps
    Script for lossless content moves (preserving drawings, equations,
    tables, cell shading that no REST request type can re-emit).

    Without this setup, ``gdocs_tab_existing_doc`` fails with "Apps
    Script Web App URL not configured." Other tools
    (``gdocs_make_tabbed_doc``, edit tools, read tools) do not need
    this Apps-Script-specific setup — but, like all tools in this
    server, they DO require the one-time Google OAuth authorization
    grant (Drive + Docs scopes). The OAuth grant happens automatically
    on first tool call: any tool that needs creds returns
    ``status: "needs_authorization"`` with a click-to-authorize URL;
    after consent, all subsequent tools in the session work without
    further prompts. Only ``gdocs_tab_existing_doc``'s lossless
    retrofit path additionally needs THIS tool
    (``gdocs_setup_apps_script``) to have been run once.

    Idempotent: safe to retry if interrupted; resumes from the last
    successful step. The user_store row (cloud) or
    ``~/.google-docs-mcp/setup-state.json`` (local) keeps the ledger.

    Returns ``{status, url, script_id, deployment_id, message}`` on
    success. On cloud-mode auth failure, returns
    ``{status: "needs_authorization", auth_url, message}`` — emit
    the message verbatim so Claude renders the URL as a clickable link.

    Choreography: required ONCE before
    ``gdocs_tab_existing_doc(markers=[...])`` (retrofit path) and the
    Apps-Script-backed retrofit pipeline in general. After successful
    setup, run any retrofit conversion freely.

    NOTE: First call typically returns ``needs_authorization`` with a
    URL the user MUST open in a browser — Google OAuth consent
    cannot be automated. After consent, re-run this tool to complete
    the Web App deploy.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        # Stdio / no-auth-context mode: local CLI behavior.
        # Uses the operator's cached OAuth token at ~/.google-docs-mcp/.
        try:
            deployment = setup_apps_script_auto()
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Apps Script setup failed: {e}") from e
        return {
            "status": "ready",
            "url": deployment.url,
            "script_id": deployment.script_id,
            "deployment_id": deployment.deployment_id,
            "message": (
                "Apps Script Web App is deployed. You can now use "
                "gdocs_tab_existing_doc."
            ),
        }

    # HTTP / multi-tenant mode: per-user creds, per-user user_store ledger.
    try:
        oauth_cfg = resolve_runtime_oauth_config()
    except RuntimeError as e:
        raise ToolError(f"Server OAuth config error: {e}") from e

    try:
        creds = get_credentials_for_user(
            user_id,
            required_scopes=GAS_DEPLOY_SCOPES,
            **oauth_cfg,
        )
    except NeedsReauthError as e:
        return {
            "status": "needs_authorization",
            "auth_url": e.auth_url,
            "message": (
                f"Google API access required to set up your Apps Script "
                f"Web App.\n\n**[Click here to authorize]({e.auth_url})**"
                f"\n\nAfter granting access, re-run this tool."
            ),
        }

    try:
        deployment = setup_apps_script_for_user(creds, user_id)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Apps Script setup failed: {e}") from e

    return {
        "status": "ready",
        "url": deployment.url,
        "script_id": deployment.script_id,
        "deployment_id": deployment.deployment_id,
        "message": (
            "Apps Script Web App is deployed under your Google account. "
            "You can now use gdocs_tab_existing_doc and other tools "
            "that need lossless content moves."
        ),
    }


@mcp.tool()
def gdocs_reset_authorization(full: bool = False) -> dict:
    """Reset / revoke / clear stored Google OAuth credentials. Force re-consent.

    Use this tool to: sign out, re-authorize, re-consent after a scope
    change, switch Google accounts, recover from a stale or revoked
    grant, force a fresh OAuth flow for testing (PKCE / consent
    screen), or roll back to the needs_authorization state. Equivalent
    in spirit to "log out and log back in" for the Google Drive / Docs
    / Apps Script API access this server uses on your behalf.

    USE WHEN: you want to force a fresh OAuth consent flow on the next
    call — for testing PKCE / re-consenting after a scope change /
    recovering from a stale or revoked grant / switching the Google
    account this server acts as.

    HTTP mode (cloud chat, claude.ai connector):
      - Default (``full=False``): clears only the stored Google
        credentials (``google_creds_json``). The user's Apps Script
        Web App setup (URL, script_id, deployment_id) is preserved.
        Next tool call that needs creds returns
        ``status: "needs_authorization"`` with a fresh auth_url.
      - ``full=True``: clears the entire user_store row, including
        the Apps Script setup. Next call to ``gdocs_setup_apps_script``
        will create a NEW project in Drive.

    Stdio mode (Claude Desktop / Code on a developer laptop):
      - Default: deletes the cached OAuth token at
        ``~/.google-docs-mcp/token.json``. Next tool call triggers
        the local browser-consent flow.
      - ``full=True``: also deletes the local Apps Script
        ``setup-state.json`` ledger and the URL in ``config.json`` —
        next ``setup-apps-script`` CLI run will create a new project.

    DOES NOT trash any Apps Script projects in your Drive — those
    remain (you can manually delete them in Drive if you want to
    free up space). Just clears the local/server-side record of the
    authorization.

    Args:
        full: If True, also clear Apps Script setup state, not just
            credentials. Default False (least destructive).

    Returns:
        ``{status: "reset", message: str, cleared: [list of what
        was cleared]}``.

    Choreography: after reset, the very next tool call that needs
    creds will return ``needs_authorization`` with a fresh consent
    URL. Re-running ``gdocs_setup_apps_script`` afterwards is
    typical if you also passed ``full=True``.
    """
    user_id = current_user_id_or_none()
    cleared: list[str] = []

    if user_id is not None:
        # HTTP / multi-tenant mode
        from . import user_store
        if full:
            user_store.clear_state(user_id)
            cleared.append("user_store row (creds + apps_script_*)")
        else:
            # Only nuke google_creds_json; preserve apps_script_*
            user_store.save_state(user_id, {"google_creds_json": None})
            cleared.append("google_creds_json")
        return {
            "status": "reset",
            "message": (
                "Authorization cleared for your account. The next tool "
                "call that needs Google API access will return "
                "'needs_authorization' with a fresh auth URL — click it "
                "to re-consent."
            ),
            "cleared": cleared,
        }

    # Stdio / no-auth-context mode
    data_dir = default_data_dir()
    token_file = data_dir / "token.json"
    if token_file.exists():
        token_file.unlink()
        cleared.append(str(token_file))
    if full:
        setup_state_file = data_dir / "setup-state.json"
        if setup_state_file.exists():
            setup_state_file.unlink()
            cleared.append(str(setup_state_file))
        cfg_file = data_dir / "config.json"
        if cfg_file.exists():
            cfg_file.unlink()
            cleared.append(str(cfg_file))

    # Bust the module-level creds cache so the next tool call doesn't
    # return the in-memory token that we just deleted from disk.
    global _creds_cache
    _creds_cache = None

    return {
        "status": "reset",
        "message": (
            "Local OAuth token cleared. The next tool call will trigger "
            "the local browser-consent flow."
            + (" Apps Script setup state also cleared." if full else "")
        ),
        "cleared": cleared,
    }


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
        from .oauth_google import configure_auth_for_http

        # v1.1+: wire GoogleProvider so HTTP requests are per-user
        # authenticated. Stdio path below intentionally skips this —
        # local trust model, single user, no auth middleware.
        configure_auth_for_http(mcp)

        port = int(os.environ.get("PORT", "8080"))
        run_http(mcp, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
