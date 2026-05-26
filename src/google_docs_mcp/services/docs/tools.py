"""Google Docs MCP tool registrations (M3 POC — v2.1.3).

This module defines the ``@gdocs_tool``-decorated tool functions for
the Google Docs service. Importing this module triggers registration
with the live ``mcp`` instance — ``server.py`` performs the import
at the bottom of its module, AFTER constructing ``mcp`` and AFTER
registering its remaining (non-docs) tools.

**Tools registered here** (12 docs-service tools, in canonical order):

1.  ``gdocs_make_tabbed_doc``       — create a new tabbed Google Doc from text
2.  ``gdocs_add_tabs``              — append tabs to an existing doc
3.  ``gdocs_get_doc_outline``       — read tab structure (no body content)
4.  ``gdocs_read_doc``              — read full text content of a doc
5.  ``gdocs_append_to_tab``         — append content to an existing tab
6.  ``gdocs_tab_existing_doc``      — convert .docx / existing doc into tabs
7.  ``gdocs_rename_tab``            — rename a tab (and/or change its icon)
8.  ``gdocs_get_tab_url``           — build a deep-link URL to a specific tab
9.  ``gdocs_delete_tab``            — delete a tab from a doc
10. ``gdocs_replace_all_text``      — find-and-replace across tabs
11. ``gdocs_set_tab_icons``         — set emoji icons on tabs
12. ``gdocs_preview_tab_split``     — dry-run a doc's tab split (no API call)

The remaining 12 tools (drive, gas_deploy, admin, introspection,
auth) stay in ``server.py`` for the M3 POC. Migrating them follows
in the next phase pending user review of this POC.

**Import discipline.** This module imports from ``server.py`` (for
``_validate_title`` and the ``_*`` API helper aliases). ``server.py``
imports this module ONCE at module bottom — AFTER its own tool
decorators run and AFTER ``decorators.register(mcp, ...)`` wires the
``@gdocs_tool`` decorator. The asymmetric order avoids a circular
import: tools.py → server.py runs first; server.py → tools.py runs
second (via the bottom-of-file import).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastmcp.exceptions import ToolError

from google_docs_mcp.decorators import workspace_tool
from google_docs_mcp.docx_import import convert_docx_to_tabbed_doc as _convert_docx
from google_docs_mcp.preview import preview_tab_split as _preview_tab_split
from google_docs_mcp.retrofit import retrofit_existing_docx as _retrofit_existing_docx
from google_docs_mcp.services.docs.api import (
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
from google_docs_mcp.tool_schemas import (
    GDOCS_ADD_TABS_OUTPUT_SCHEMA,
    GDOCS_APPEND_TO_TAB_OUTPUT_SCHEMA,
    GDOCS_DELETE_TAB_OUTPUT_SCHEMA,
    GDOCS_GET_DOC_OUTLINE_OUTPUT_SCHEMA,
    GDOCS_GET_TAB_URL_OUTPUT_SCHEMA,
    GDOCS_MAKE_TABBED_DOC_OUTPUT_SCHEMA,
    GDOCS_PREVIEW_TAB_SPLIT_OUTPUT_SCHEMA,
    GDOCS_READ_DOC_OUTPUT_SCHEMA,
    GDOCS_RENAME_TAB_OUTPUT_SCHEMA,
    GDOCS_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
    GDOCS_SET_TAB_ICONS_OUTPUT_SCHEMA,
    GDOCS_TAB_EXISTING_DOC_OUTPUT_SCHEMA,
)

# Tool-layer helpers.
#
# M3 Phase C (v2.1.5) split the pre-existing _get_server_helpers()
# 3-tuple shim because the 3-consumer extraction trigger fired
# (docs + drive + gas_deploy all want the same 2 of the 3 helpers).
# Direct top-level imports from _tool_helpers replace the shim for
# _get_credentials + _format_http_error — they have ZERO server.py
# dependency, so no circular-import risk.
#
# _validate_title is docs-only (TabSpec titles, Drive file names);
# it STAYS in server.py. Lazy-imported here via a single-purpose
# shim because server.py imports THIS module at its bottom — so a
# top-level "from google_docs_mcp.server import _validate_title"
# would circular at import time.
from google_docs_mcp._tool_helpers import (
    _format_http_error,
    _get_credentials,
)


def _get_validate_title():
    """Module-load-time lookup of server._validate_title.

    server.py imports tools.py at its bottom — by the time tools.py
    is parsed, server.py module-level code has finished executing and
    _validate_title is available. Single attribute lookup at module
    load; per-call cost is zero.
    """
    from google_docs_mcp import server as _server
    return _server._validate_title


_validate_title = _get_validate_title()


# ---------------------------------------------------------------------
# 1. gdocs_make_tabbed_doc
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Create a new tabbed Google Doc",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_MAKE_TABBED_DOC_OUTPUT_SCHEMA,
)
def gdocs_make_tabbed_doc(creds, title: str, tabs: list[TabSpec]) -> dict:
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
    _validate_title(title)
    if not tabs:
        raise ToolError("Must provide at least one tab")

    for i, tab in enumerate(tabs):
        if not tab.get("title"):
            raise ToolError(f"tabs[{i}] is missing a non-empty title")
        _validate_title(tab["title"], field=f"tabs[{i}].title")
        icon = tab.get("icon_emoji")
        if icon and len(icon.encode("utf-8")) > 8:
            raise ToolError(
                f"tabs[{i}].icon_emoji must be a single emoji (≤8 UTF-8 bytes)"
            )

    return make_doc_with_tabs(creds, title, tabs)


# ---------------------------------------------------------------------
# 2. gdocs_add_tabs
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Append tabs to an existing Google Doc",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_ADD_TABS_OUTPUT_SCHEMA,
)
def gdocs_add_tabs(
    creds,
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
        _validate_title(tab["title"], field=f"tabs[{i}].title")
        icon = tab.get("icon_emoji")
        if icon and len(icon.encode("utf-8")) > 8:
            raise ToolError(
                f"tabs[{i}].icon_emoji must be a single emoji (≤8 UTF-8 bytes)"
            )

    try:
        return add_tabs_to_doc(creds, doc_id, tabs, parent_tab_id=parent_tab_id)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 3. gdocs_get_doc_outline
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Read doc outline (tab structure only)",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_GET_DOC_OUTLINE_OUTPUT_SCHEMA,
)
def gdocs_get_doc_outline(creds, doc_id: str) -> dict:
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
    return _get_doc_outline(creds, doc_id)


# ---------------------------------------------------------------------
# 4. gdocs_read_doc
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Read full text content of a Google Doc",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_READ_DOC_OUTPUT_SCHEMA,
)
def gdocs_read_doc(
    creds,
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
        if tab_id is None and tab_title is None:
            return _read_all_tabs(creds, doc_id)
        return _read_tab_content(
            creds, doc_id, tab_id=tab_id, tab_title=tab_title
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 5. gdocs_append_to_tab
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Append content to an existing tab",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_APPEND_TO_TAB_OUTPUT_SCHEMA,
)
def gdocs_append_to_tab(
    creds,
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
        return _append_to_tab(
            creds, doc_id, tab_id, content, content_format=content_format
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 6. gdocs_tab_existing_doc
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Convert .docx or existing Doc into tabbed Google Doc",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
    output_schema=GDOCS_TAB_EXISTING_DOC_OUTPUT_SCHEMA,
)
def gdocs_tab_existing_doc(
    creds,
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
    if title is not None:
        _validate_title(title)
    path: Path | None = Path(docx_path).expanduser() if docx_path else None
    try:
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


# ---------------------------------------------------------------------
# 7. gdocs_rename_tab
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Rename a tab and/or change its icon",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_RENAME_TAB_OUTPUT_SCHEMA,
)
def gdocs_rename_tab(
    creds,
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
    if title is not None:
        _validate_title(title)
    if icon_emoji is not None and len(icon_emoji.encode("utf-8")) > 8:
        raise ToolError("icon_emoji must be a single emoji (≤8 UTF-8 bytes)")
    _rename_tab(creds, doc_id, tab_id, title=title, icon_emoji=icon_emoji)
    updated = []
    if title is not None:
        updated.append("title")
    if icon_emoji is not None:
        updated.append("iconEmoji")
    return {"doc_id": doc_id, "tab_id": tab_id, "updated_fields": updated}


# ---------------------------------------------------------------------
# 8. gdocs_get_tab_url
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Build deep-link URL to a specific tab",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_GET_TAB_URL_OUTPUT_SCHEMA,
)
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


# ---------------------------------------------------------------------
# 9. gdocs_delete_tab
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Delete a tab from a Google Doc",
    readonly=False, destructive=True, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_DELETE_TAB_OUTPUT_SCHEMA,
)
def gdocs_delete_tab(creds, doc_id: str, tab_id: str) -> dict:
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
    _delete_tab(creds, doc_id, tab_id)
    return {"doc_id": doc_id, "deleted_tab_id": tab_id}


# ---------------------------------------------------------------------
# 10. gdocs_replace_all_text
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Replace all matching text in a doc",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
)
def gdocs_replace_all_text(
    creds,
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
        return _replace_all_text(
            creds, doc_id, find, replace,
            match_case=match_case, tab_ids=tab_ids,
        )
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 11. gdocs_set_tab_icons
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Set emoji icons on tabs",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_SET_TAB_ICONS_OUTPUT_SCHEMA,
)
def gdocs_set_tab_icons(creds, doc_id: str, icons_by_title: dict[str, str]) -> dict:
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
    the auto-named tabs. For a SINGLE tab where you have the
    ``tab_id`` (and might also want to change the title), use
    ``gdocs_rename_tab`` instead — it edits both fields in one call.
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
        return _set_tab_icons(creds, doc_id, icons_by_title)
    except ValueError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 12. gdocs_preview_tab_split
# ---------------------------------------------------------------------


@workspace_tool(
    service="docs",
    title="Preview how a doc would split into tabs (dry-run)",
    readonly=True, destructive=False, idempotent=True, external=True,
    # creds=False: this tool fetches creds CONDITIONALLY (only when
    # drive_file_id is provided; the docx_path branch needs no auth).
    # Decorator's unconditional creds-fetch would change that contract.
    output_schema=GDOCS_PREVIEW_TAB_SPLIT_OUTPUT_SCHEMA,
)
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
    # HttpError is imported lazily — only the drive_file_id branch can
    # raise it, and that path lazy-imports the creds. Keeping the import
    # inside the function avoids pulling googleapiclient into the
    # module's import cost.
    from googleapiclient.errors import HttpError

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
