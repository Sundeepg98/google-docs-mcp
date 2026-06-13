"""Google Docs REST API call sites (v2.2.1 — R14 #8 split).

After the R14 #8 split (audit Gap #1 fix), this module owns ONLY the
functions that actually invoke ``get_service("docs", "v1", ...)`` and
make API calls. The pure helpers are now siblings:

  markdown_render.py — markdown-it state machine, request-payload
                       builders (``_tab_properties``, ``_add_tab_request``,
                       etc.), and the ``TabSpec`` TypedDict
  tab_tree.py        — tree walking (``_flatten_tab_tree``,
                       ``_find_tab_by_id``, ``_get_tab_depth``,
                       ``_find_tab_by_title``)
  api.py             — THIS module: REST calls only

Tools layer (``tools.py``) and other callers continue to import the
public surface from ``services.docs.api`` thanks to the re-exports
below — split is purely internal.

See ``tab_tree.py`` and ``markdown_render.py`` module docstrings for
the split's audit-finding context (Hex 92% / SOLID 78% / Test 78%,
R14 #8, R6 UTF-16 unblock).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from google.oauth2.credentials import Credentials

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

# Re-export the pure helpers + the value types so callers that did
# ``from .api import X`` continue to work post-split. The re-exports
# preserve every pre-v2.2.1 import path.
from .markdown_render import (
    CODE_BG_RGB,
    CODE_FONT,
    TabSpec,
    _add_tab_request,
    _plain_text_requests,
    _rename_tab_request,
    _tab_properties,
    parse_markdown_table,
    render_content_to_requests,
)
from .tab_tree import (
    _find_tab_by_id,
    _find_tab_by_title,
    _flatten_tab_tree,
    _get_tab_depth,
)

MAX_NESTING_DEPTH = 3  # Google Docs UI hard limit: root + 2 child levels


def make_doc_with_tabs(
    creds: Credentials, title: str, tabs: list[TabSpec]
) -> dict:
    """Create a Google Doc with multiple native tabs (up to 3 levels deep).

    Each ``TabSpec`` may carry ``icon_emoji`` (one grapheme, max 8 bytes),
    ``content_format`` (default ``"markdown"``; ``"text"`` for raw), and
    ``children`` (a list of nested ``TabSpec`` for child tabs).

    Tabs are created level-by-level (root tabs first, then depth-1
    children, then depth-2 grandchildren) because Google assigns tabIds
    server-side and child tabs need their parent's ID at creation time.
    """
    flat = _flatten_tab_tree(tabs)
    max_depth = max((d for d, _, _ in flat), default=0)
    if max_depth >= MAX_NESTING_DEPTH:
        raise ValueError(
            f"Max nesting depth is {MAX_NESTING_DEPTH} "
            f"(root + {MAX_NESTING_DEPTH - 1} child levels); "
            f"got depth {max_depth + 1}"
        )

    docs = get_service("docs", "v1", credentials=creds)
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    path_to_tab_id = _materialize_tab_tree(docs, doc_id, flat)

    content_requests: list[dict] = []
    for _depth, path, spec in flat:
        tab_id = path_to_tab_id[path]
        fmt = spec.get("content_format", "markdown")
        content = spec.get("content", "")
        if fmt == "text":
            content_requests.extend(_plain_text_requests(content, tab_id))
        else:
            content_requests.extend(render_content_to_requests(content, tab_id))

    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": content_requests}
        ).execute()

    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "tabs": [
            {
                "title": spec["title"],
                "tab_id": path_to_tab_id[path],
                "depth": depth,
                "parent_tab_id": (
                    path_to_tab_id[path[:-1]] if depth > 0 else None
                ),
            }
            for depth, path, spec in flat
        ],
    }


def _materialize_tab_tree(
    docs: Any,
    doc_id: str,
    flat: list[tuple[int, tuple[int, ...], TabSpec]],
) -> dict[tuple[int, ...], str]:
    """Create the tab structure level-by-level; return path -> tab_id map.

    Each level is one batchUpdate + one re-fetch to learn the new
    server-assigned tab IDs before the next level can reference them
    as ``parentTabId``.
    """
    path_to_tab_id: dict[tuple[int, ...], str] = {}
    max_depth = max((d for d, _, _ in flat), default=0)

    for level in range(max_depth + 1):
        level_specs = [(p, s) for d, p, s in flat if d == level]
        if not level_specs:
            continue

        if level == 0:
            fetched = docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute()
            first_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]
            requests = [_rename_tab_request(first_tab_id, level_specs[0][1])]
            for _path, spec in level_specs[1:]:
                requests.append(_add_tab_request(spec))
        else:
            requests = []
            for path, spec in level_specs:
                parent_id = path_to_tab_id[path[:-1]]
                requests.append(_add_tab_request(spec, parent_tab_id=parent_id))

        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

        fetched = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()

        if level == 0:
            for i, (path, _spec) in enumerate(level_specs):
                path_to_tab_id[path] = fetched["tabs"][i]["tabProperties"]["tabId"]
        else:
            by_parent: dict[tuple[int, ...], list[tuple[int, ...]]] = defaultdict(list)
            for path, _spec in level_specs:
                by_parent[path[:-1]].append(path)
            for parent_path, child_paths in by_parent.items():
                parent_id = path_to_tab_id[parent_path]
                parent_tab = _find_tab_by_id(fetched["tabs"], parent_id)
                if parent_tab is None:
                    raise RuntimeError(
                        f"Parent tab {parent_id} disappeared from re-fetch"
                    )
                child_tabs = parent_tab.get("childTabs") or []
                for i, path in enumerate(child_paths):
                    path_to_tab_id[path] = child_tabs[i]["tabProperties"]["tabId"]

    return path_to_tab_id


def add_tabs_to_doc(
    creds: Credentials,
    doc_id: str,
    tabs: list[TabSpec],
    parent_tab_id: str | None = None,
) -> dict:
    """Append tabs to an existing Google Doc, optionally nested under a parent.

    Same nesting rules apply (max 3 levels). When ``parent_tab_id`` is
    given, the new tabs become its children; otherwise they become
    root-level siblings of existing root tabs.
    """
    flat = _flatten_tab_tree(tabs)
    if not flat:
        return {"tabs": []}

    max_depth = max((d for d, _, _ in flat), default=0)

    docs = get_service("docs", "v1", credentials=creds)

    if parent_tab_id:
        fetched = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        parent_depth = _get_tab_depth(fetched.get("tabs") or [], parent_tab_id)
        if parent_depth < 0:
            raise ValueError(
                f"Parent tab {parent_tab_id} not found in doc {doc_id}"
            )
        if parent_depth + 1 + max_depth >= MAX_NESTING_DEPTH:
            raise ValueError(
                f"Adding under depth-{parent_depth} parent would exceed "
                f"max nesting depth of {MAX_NESTING_DEPTH}"
            )

    path_to_tab_id: dict[tuple[int, ...], str] = {}

    for level in range(max_depth + 1):
        level_specs = [(p, s) for d, p, s in flat if d == level]
        if not level_specs:
            continue

        if level == 0:
            requests = [
                _add_tab_request(s, parent_tab_id=parent_tab_id)
                for _path, s in level_specs
            ]
        else:
            requests = []
            for path, spec in level_specs:
                pid = path_to_tab_id[path[:-1]]
                requests.append(_add_tab_request(spec, parent_tab_id=pid))

        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

        fetched = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()

        if level == 0:
            if parent_tab_id:
                parent = _find_tab_by_id(fetched.get("tabs") or [], parent_tab_id)
                siblings = (parent or {}).get("childTabs") or []
            else:
                siblings = fetched.get("tabs") or []
            new_tabs = siblings[-len(level_specs):]
            for i, (path, _spec) in enumerate(level_specs):
                path_to_tab_id[path] = new_tabs[i]["tabProperties"]["tabId"]
        else:
            by_parent: dict[tuple[int, ...], list[tuple[int, ...]]] = defaultdict(list)
            for path, _spec in level_specs:
                by_parent[path[:-1]].append(path)
            for parent_path, child_paths in by_parent.items():
                pid = path_to_tab_id[parent_path]
                parent_tab = _find_tab_by_id(fetched.get("tabs") or [], pid)
                children = (parent_tab or {}).get("childTabs") or []
                new_children = children[-len(child_paths):]
                for i, path in enumerate(child_paths):
                    path_to_tab_id[path] = new_children[i]["tabProperties"]["tabId"]

    content_requests: list[dict] = []
    for _depth, path, spec in flat:
        tab_id = path_to_tab_id[path]
        fmt = spec.get("content_format", "markdown")
        content = spec.get("content", "")
        if fmt == "text":
            content_requests.extend(_plain_text_requests(content, tab_id))
        else:
            content_requests.extend(render_content_to_requests(content, tab_id))

    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": content_requests}
        ).execute()

    return {
        "tabs": [
            {
                "title": spec["title"],
                "tab_id": path_to_tab_id[path],
                "depth": depth,
                "parent_tab_id": (
                    path_to_tab_id[path[:-1]] if depth > 0 else parent_tab_id
                ),
            }
            for depth, path, spec in flat
        ],
    }


def get_doc_outline(creds: Credentials, doc_id: str) -> dict:
    """Return the doc's tab structure plus its trash state.

    Shape: ``{"doc_id", "trashed", "tabs": [...]}``. Each entry in
    ``tabs`` is ``{tab_id, title, parent_tab_id, depth, index,
    icon_emoji}`` in pre-order traversal.

    ``trashed`` surfaces whether the underlying Drive file is in trash
    (hidden from normal Drive UI but still accessible by ID). When
    True, callers should usually warn the user before continuing to
    edit — the file is invisible to them in Drive.
    """
    docs = get_service("docs", "v1", credentials=creds)
    # includeTabsContent must be True for the tabs[] field to be populated
    # at all; without it the response uses the legacy single-tab schema.
    # PR-Δ3.5: gdocs_get_doc_outline is readonly=True, idempotent=True.
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.outline",
    )

    tabs_out: list[dict] = []

    def walk(tabs: list[dict], parent_id: str | None, depth: int) -> None:
        for i, tab in enumerate(tabs):
            props = tab["tabProperties"]
            tabs_out.append(
                {
                    "tab_id": props["tabId"],
                    "title": props.get("title", ""),
                    "parent_tab_id": parent_id,
                    "depth": depth,
                    "index": i,
                    "icon_emoji": props.get("iconEmoji"),
                }
            )
            walk(tab.get("childTabs") or [], props["tabId"], depth + 1)

    walk(fetched.get("tabs") or [], None, 0)

    # Surface Drive trash state. Best-effort: if the Drive lookup
    # fails we report trashed=False rather than failing the call.
    from appscriptly.services.drive.api import is_file_trashed
    trashed = is_file_trashed(creds, doc_id)

    return {"doc_id": doc_id, "trashed": trashed, "tabs": tabs_out}


def read_tab_content(
    creds: Credentials,
    doc_id: str,
    tab_id: str | None = None,
    tab_title: str | None = None,
) -> dict:
    """Read the body content of a single tab.

    Identify the tab by ``tab_id`` (exact) or ``tab_title`` (first match,
    pre-order). Returns structural metadata plus a paragraphs list so the
    caller can verify what actually landed in the tab — useful right
    after ``convert_docx_to_tabbed_doc`` to confirm content moved
    correctly.

    Tables are reported as a count + a placeholder line; full table
    cell extraction is deferred to a later iteration. Inline images
    show up as ``[image]`` markers within the paragraph text.
    """
    if not tab_id and not tab_title:
        raise ValueError("Provide either tab_id or tab_title")

    docs = get_service("docs", "v1", credentials=creds)
    # PR-Δ3.5: gdocs_read_doc is readonly=True, idempotent=True.
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.read_tab",
    )
    all_tabs = fetched.get("tabs") or []

    if tab_id:
        tab = _find_tab_by_id(all_tabs, tab_id)
    else:
        tab = _find_tab_by_title(all_tabs, tab_title or "")

    if tab is None:
        raise ValueError(
            f"Tab not found in doc {doc_id} "
            f"(tab_id={tab_id!r}, tab_title={tab_title!r})"
        )

    body_content = tab["documentTab"]["body"]["content"]
    paragraphs: list[dict] = []
    table_count = 0
    image_count = 0

    for elem in body_content:
        if "paragraph" in elem:
            para = elem["paragraph"]
            style = para.get("paragraphStyle", {}).get(
                "namedStyleType", "NORMAL_TEXT"
            )
            chunks: list[str] = []
            for pe in para.get("elements", []):
                if "textRun" in pe:
                    chunks.append(pe["textRun"].get("content", ""))
                elif "inlineObjectElement" in pe:
                    chunks.append("[image]")
                    image_count += 1
                elif "person" in pe:
                    chunks.append(
                        f"[person:{pe['person'].get('personProperties', {}).get('email', '?')}]"
                    )
                elif "richLink" in pe:
                    chunks.append("[link]")
            text = "".join(chunks).rstrip("\n")
            if text or style != "NORMAL_TEXT":
                paragraphs.append({"style": style, "text": text})
        elif "table" in elem:
            tbl = elem["table"]
            rows = tbl.get("rows", 0)
            cols = tbl.get("columns", 0)
            table_count += 1
            paragraphs.append(
                {"style": "TABLE", "text": f"[table {rows}x{cols}]"}
            )
        elif "sectionBreak" in elem:
            continue
        elif "tableOfContents" in elem:
            paragraphs.append({"style": "TOC", "text": "[table of contents]"})

    from appscriptly.services.drive.api import is_file_trashed
    return {
        "tab_id": tab["tabProperties"]["tabId"],
        "title": tab["tabProperties"]["title"],
        "trashed": is_file_trashed(creds, doc_id),
        "paragraph_count": sum(
            1 for p in paragraphs if p["style"] not in ("TABLE", "TOC")
        ),
        "table_count": table_count,
        "image_count": image_count,
        "paragraphs": paragraphs,
    }


def replace_all_text(
    creds: Credentials,
    doc_id: str,
    find: str,
    replace: str,
    *,
    match_case: bool = False,
    tab_ids: list[str] | None = None,
) -> dict:
    """Find-and-replace text across some or all tabs.

    By default (``tab_ids=None``), the replacement runs across **every**
    tab in the document. Pass an explicit ``tab_ids`` list to scope the
    operation — important because omitting the field silently hits all
    tabs, which can be a surprise.

    Returns ``{"occurrences_changed": int, "scope": "all_tabs" | tab_ids}``.
    """
    if not find:
        raise ValueError("find string cannot be empty")
    docs = get_service("docs", "v1", credentials=creds)
    req: dict[str, Any] = {
        "replaceAllText": {
            "containsText": {"text": find, "matchCase": match_case},
            "replaceText": replace,
        }
    }
    if tab_ids is not None:
        if not tab_ids:
            raise ValueError("tab_ids list cannot be empty; omit to target all tabs")
        req["replaceAllText"]["tabsCriteria"] = {"tabIds": list(tab_ids)}

    # PR-Δ3.5: gdocs_replace_all_text is idempotent=True (replacing the
    # same text twice is a no-op on the second pass — occurrencesChanged
    # is 0 because the first pass already replaced everything).
    resp = execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.replaceAllText",
    )
    occurrences = (
        resp.get("replies", [{}])[0]
        .get("replaceAllText", {})
        .get("occurrencesChanged", 0)
    )
    return {
        "occurrences_changed": occurrences,
        "scope": "all_tabs" if tab_ids is None else tab_ids,
    }


def insert_table(
    creds: Credentials,
    doc_id: str,
    rows: int,
    columns: int,
    *,
    index: int = 1,
    tab_id: str | None = None,
) -> dict:
    """Insert an empty ``rows`` × ``columns`` table into a document.

    Uses the Docs ``insertTable`` ``batchUpdate`` request at a
    ``Location`` (an ``index`` within the body, optionally scoped to a
    ``tab_id`` for multi-tab docs). The table is created empty;
    populate cells afterward with ``gdocs_replace_all_text`` (seed
    template tokens) or a future cell-level insert tool.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline — no extra grant).
        doc_id: The Google Doc ID.
        rows: Number of rows (>= 1).
        columns: Number of columns (>= 1).
        index: Body location index to insert at. Defaults to 1 (the
            start of the body — index 0 is reserved by Docs, so 1 is
            the first valid insertion point). Must be >= 1.
        tab_id: Optional tab to target. ``None`` targets the document's
            default/first tab (Docs applies the request without a
            ``tabId``); pass an explicit tab id (from
            ``gdocs_get_doc_outline``) for a specific tab in a
            multi-tab doc.

    Returns:
        ``{doc_id, rows, columns, index, tab_id}`` — echoes the request.
        (The Docs ``insertTable`` reply does not carry a stable table
        object id, so there is none to return; the table is locatable
        by its ``index`` / via ``gdocs_read_doc``.)

    Raises:
        ValueError: ``rows`` / ``columns`` < 1, or ``index`` < 1.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if rows < 1 or columns < 1:
        raise ValueError("rows and columns must each be >= 1.")
    if index < 1:
        raise ValueError("index must be >= 1 (index 0 is reserved by Docs).")

    docs = get_service("docs", "v1", credentials=creds)
    location: dict[str, Any] = {"index": index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        location["tabId"] = tab_id
    req = {
        "insertTable": {
            "location": location,
            "rows": rows,
            "columns": columns,
        }
    }
    # NOT idempotent: each call inserts ANOTHER table. Same convention
    # as gslides_create_table / gsheets_create_spreadsheet.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.insertTable",
    )
    return {
        "doc_id": doc_id,
        "rows": rows,
        "columns": columns,
        "index": index,
        "tab_id": tab_id,
    }


# Text-style fields supported by gdocs_format_range, mapped to the
# Docs ``TextStyle`` field name used in the ``fields`` mask. Boolean
# toggles only; the value-bearing styles (font size / family / color)
# are handled separately because they carry a magnitude / string.
_BOOL_STYLE_FIELDS = {
    "bold": "bold",
    "italic": "italic",
    "underline": "underline",
    "strikethrough": "strikethrough",
}


def _hex_to_rgbcolor(hex_color: str) -> dict:
    """Convert ``#RRGGBB`` (or ``RRGGBB``) to a Docs ``RgbColor`` dict.

    Docs expects channel magnitudes in [0, 1]. Raises ValueError on a
    malformed hex string so the caller gets a clear client-side error
    rather than a Docs 400.
    """
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(
            f"color must be a 6-digit hex like '#1a73e8' — got {hex_color!r}."
        )
    try:
        r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        raise ValueError(
            f"color contains non-hex characters — got {hex_color!r}."
        ) from None
    return {"red": r, "green": g, "blue": b}


def format_range(
    creds: Credentials,
    doc_id: str,
    start_index: int,
    end_index: int,
    *,
    tab_id: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    strikethrough: bool | None = None,
    font_size_pt: float | None = None,
    font_family: str | None = None,
    foreground_color: str | None = None,
) -> dict:
    """Apply character formatting to a text range via ``updateTextStyle``.

    Builds a single ``updateTextStyle`` ``batchUpdate`` request over the
    ``[start_index, end_index)`` range (optionally scoped to ``tab_id``)
    with a ``fields`` mask listing exactly the styles that were set —
    so unset styles are left untouched (Docs clears any field named in
    the mask but not provided, hence the precise mask).

    Only the styles you pass are applied; passing none is an error
    (there'd be nothing to do). Boolean styles accept True/False;
    omit (``None``) to leave them as-is.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline — no extra grant).
        doc_id: The Google Doc ID.
        start_index: Range start (inclusive), >= 1.
        end_index: Range end (exclusive), > ``start_index``.
        tab_id: Optional tab to target (from ``gdocs_get_doc_outline``);
            ``None`` targets the default/first tab.
        bold / italic / underline / strikethrough: Optional booleans.
        font_size_pt: Optional font size in points (> 0).
        font_family: Optional font family name (e.g. ``"Roboto"``,
            ``"Arial"``).
        foreground_color: Optional text color as ``#RRGGBB`` hex.

    Returns:
        ``{doc_id, start_index, end_index, tab_id, applied}`` —
        ``applied`` is the list of style field names that were set
        (the ``fields`` mask), for caller confirmation.

    Raises:
        ValueError: bad range, no styles supplied, non-positive font
            size, or malformed color.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if start_index < 1:
        raise ValueError("start_index must be >= 1.")
    if end_index <= start_index:
        raise ValueError("end_index must be greater than start_index.")
    if font_size_pt is not None and font_size_pt <= 0:
        raise ValueError("font_size_pt must be positive.")

    text_style: dict[str, Any] = {}
    fields: list[str] = []
    for arg_val, field_name in (
        (bold, "bold"),
        (italic, "italic"),
        (underline, "underline"),
        (strikethrough, "strikethrough"),
    ):
        if arg_val is not None:
            text_style[field_name] = arg_val
            fields.append(field_name)
    if font_size_pt is not None:
        text_style["fontSize"] = {"magnitude": font_size_pt, "unit": "PT"}
        fields.append("fontSize")
    if font_family is not None:
        if not font_family.strip():
            raise ValueError("font_family cannot be the empty string.")
        text_style["weightedFontFamily"] = {"fontFamily": font_family}
        fields.append("weightedFontFamily")
    if foreground_color is not None:
        text_style["foregroundColor"] = {
            "color": {"rgbColor": _hex_to_rgbcolor(foreground_color)}
        }
        fields.append("foregroundColor")

    if not fields:
        raise ValueError(
            "no styles supplied — pass at least one of bold / italic / "
            "underline / strikethrough / font_size_pt / font_family / "
            "foreground_color."
        )

    docs = get_service("docs", "v1", credentials=creds)
    rng: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        rng["tabId"] = tab_id
    req = {
        "updateTextStyle": {
            "range": rng,
            "textStyle": text_style,
            "fields": ",".join(fields),
        }
    }
    # Idempotent: applying the same style to the same range twice yields
    # the same document state. Matches gdocs_replace_all_text's framing.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.updateTextStyle",
    )
    return {
        "doc_id": doc_id,
        "start_index": start_index,
        "end_index": end_index,
        "tab_id": tab_id,
        "applied": fields,
    }


# Allowed paragraph alignment values → Docs ``Alignment`` enum. Docs
# uses START/CENTER/END/JUSTIFIED; we accept the friendlier aliases.
_ALIGNMENT_MAP = {
    "left": "START",
    "start": "START",
    "center": "CENTER",
    "right": "END",
    "end": "END",
    "justify": "JUSTIFIED",
    "justified": "JUSTIFIED",
}

# Allowed named paragraph styles (Docs ``NamedStyleType``). Restricting
# to this set keeps the surface predictable; the full enum has a few
# more (e.g. SUBTITLE) that can be added if a consumer needs them.
_NAMED_STYLES = {
    "NORMAL_TEXT",
    "TITLE",
    "SUBTITLE",
    "HEADING_1",
    "HEADING_2",
    "HEADING_3",
    "HEADING_4",
    "HEADING_5",
    "HEADING_6",
}


def format_paragraph(
    creds: Credentials,
    doc_id: str,
    start_index: int,
    end_index: int,
    *,
    tab_id: str | None = None,
    alignment: str | None = None,
    named_style: str | None = None,
    line_spacing: float | None = None,
    space_above_pt: float | None = None,
    space_below_pt: float | None = None,
) -> dict:
    """Apply paragraph formatting to a range via ``updateParagraphStyle``.

    Complements ``format_range`` (which does *character* styling). Builds
    a single ``updateParagraphStyle`` ``batchUpdate`` request over
    ``[start_index, end_index)`` (optionally tab-scoped) with a precise
    ``fields`` mask, so only the attributes you pass change.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline — no extra grant).
        doc_id: The Google Doc ID.
        start_index: Range start (inclusive), >= 1.
        end_index: Range end (exclusive), > ``start_index``.
        tab_id: Optional tab to target; ``None`` = default/first tab.
        alignment: One of ``"left"``/``"center"``/``"right"``/
            ``"justify"`` (aliases ``start``/``end``/``justified`` also
            accepted) → Docs ``START``/``CENTER``/``END``/``JUSTIFIED``.
        named_style: A Docs ``NamedStyleType`` (e.g. ``"HEADING_1"``,
            ``"NORMAL_TEXT"``, ``"TITLE"``).
        line_spacing: Line spacing as a PERCENT (Docs convention):
            ``100`` = single, ``150`` = 1.5×, ``200`` = double. > 0.
        space_above_pt: Space before the paragraph, in points (>= 0).
        space_below_pt: Space after the paragraph, in points (>= 0).

    Returns:
        ``{doc_id, start_index, end_index, tab_id, applied}`` —
        ``applied`` is the list of paragraph-style fields that were set.

    Raises:
        ValueError: bad range, no attributes supplied, unknown
            alignment / named_style, or a negative spacing value.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if start_index < 1:
        raise ValueError("start_index must be >= 1.")
    if end_index <= start_index:
        raise ValueError("end_index must be greater than start_index.")

    paragraph_style: dict[str, Any] = {}
    fields: list[str] = []

    if alignment is not None:
        key = alignment.strip().lower()
        if key not in _ALIGNMENT_MAP:
            raise ValueError(
                f"alignment must be one of "
                f"{sorted(set(_ALIGNMENT_MAP))} — got {alignment!r}."
            )
        paragraph_style["alignment"] = _ALIGNMENT_MAP[key]
        fields.append("alignment")
    if named_style is not None:
        ns = named_style.strip().upper()
        if ns not in _NAMED_STYLES:
            raise ValueError(
                f"named_style must be one of {sorted(_NAMED_STYLES)} — "
                f"got {named_style!r}."
            )
        paragraph_style["namedStyleType"] = ns
        fields.append("namedStyleType")
    if line_spacing is not None:
        if line_spacing <= 0:
            raise ValueError("line_spacing must be positive (100 = single).")
        paragraph_style["lineSpacing"] = line_spacing
        fields.append("lineSpacing")
    if space_above_pt is not None:
        if space_above_pt < 0:
            raise ValueError("space_above_pt cannot be negative.")
        paragraph_style["spaceAbove"] = {"magnitude": space_above_pt, "unit": "PT"}
        fields.append("spaceAbove")
    if space_below_pt is not None:
        if space_below_pt < 0:
            raise ValueError("space_below_pt cannot be negative.")
        paragraph_style["spaceBelow"] = {"magnitude": space_below_pt, "unit": "PT"}
        fields.append("spaceBelow")

    if not fields:
        raise ValueError(
            "no paragraph attributes supplied — pass at least one of "
            "alignment / named_style / line_spacing / space_above_pt / "
            "space_below_pt."
        )

    docs = get_service("docs", "v1", credentials=creds)
    rng: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        rng["tabId"] = tab_id
    req = {
        "updateParagraphStyle": {
            "range": rng,
            "paragraphStyle": paragraph_style,
            "fields": ",".join(fields),
        }
    }
    # Idempotent: same paragraph style on the same range twice = same
    # state. Matches format_range.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.updateParagraphStyle",
    )
    return {
        "doc_id": doc_id,
        "start_index": start_index,
        "end_index": end_index,
        "tab_id": tab_id,
        "applied": fields,
    }


def edit_range(
    creds: Credentials,
    doc_id: str,
    start_index: int,
    end_index: int,
    *,
    text: str | None = None,
    tab_id: str | None = None,
) -> dict:
    """Edit a specific ``[start_index, end_index)`` span via batchUpdate.

    Location-indexed editing: delete the half-open UTF-16 range
    ``[start_index, end_index)`` with ``deleteContentRange``, then
    (optionally) ``insertText`` ``text`` at ``start_index``. The two
    requests run in ONE ``batchUpdate``; the Docs API applies them
    sequentially, so the insert lands exactly where the deletion left a
    gap (deletion collapses ``[start, end)`` to a zero-width point at
    ``start_index``, and the insert at ``start_index`` fills it). This
    is the "replace a range" primitive that ``gdocs_replace_all_text``
    (whole-doc find/replace) and ``gdocs_append_to_tab`` (append-only)
    don't cover.

    **Index contract — raw UTF-16 code units.** ``start_index`` and
    ``end_index`` are addresses in Google Docs' native coordinate
    system: UTF-16 code units, 1-based (body content starts at index 1;
    index 0 is the section break). This is the SAME address space
    ``gdocs_format_range`` / ``gdocs_format_paragraph`` already accept
    and that ``gdocs_read_doc`` reports, so a caller reads structure,
    computes a range, and edits it without any code-point↔UTF-16
    conversion. Because the API measures in UTF-16, an above-BMP
    character (emoji, math-alphanumeric) occupies 2 units, not 1 — the
    caller's indices must already account for that (Docs' own
    ``startIndex`` / ``endIndex`` values do). The renderer's ``_insert``
    advances by ``len(text.encode("utf-16-le")) // 2`` for the same
    reason (R6 fix, PR #184); this tool consumes indices in that unit
    basis rather than recomputing them.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline — no extra grant).
        doc_id: The Google Doc ID.
        start_index: Range start (inclusive), >= 1, in UTF-16 code units.
        end_index: Range end (exclusive), > ``start_index``, in UTF-16
            code units.
        text: Optional replacement text to insert at ``start_index``
            after the deletion. Omit (or ``None``) for a pure delete.
            Empty string is treated as a pure delete (the Docs
            ``insertText`` request rejects empty text, so no insert is
            emitted).
        tab_id: Optional tab to target (from ``gdocs_get_doc_outline``);
            ``None`` targets the default/first tab.

    Returns:
        ``{doc_id, start_index, end_index, tab_id, deleted, inserted,
        inserted_units}`` — ``deleted`` is always True (the delete
        always runs); ``inserted`` is True iff non-empty ``text`` was
        inserted; ``inserted_units`` is the UTF-16 code-unit length of
        the inserted text (0 for a pure delete).

    Raises:
        ValueError: ``start_index`` < 1 or ``end_index`` <= ``start_index``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if start_index < 1:
        raise ValueError("start_index must be >= 1.")
    if end_index <= start_index:
        raise ValueError("end_index must be greater than start_index.")

    docs = get_service("docs", "v1", credentials=creds)

    # The delete range + (optional) insert location share the same
    # coordinate dict shape that format_range uses — startIndex/endIndex
    # for the range, index for the location, both optionally tab-scoped.
    del_range: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    insert_loc: dict[str, Any] = {"index": start_index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        del_range["tabId"] = tab_id
        insert_loc["tabId"] = tab_id

    requests: list[dict] = [{"deleteContentRange": {"range": del_range}}]

    # Insert AFTER the delete. In a single batchUpdate the requests apply
    # in order: deleteContentRange removes [start, end) (collapsing it to
    # a zero-width gap at start_index and shifting everything at/after
    # end_index left by end_index - start_index), then insertText at
    # start_index fills that gap. Empty/omitted text → pure delete (Docs
    # rejects an empty insertText, so we simply don't emit it).
    inserted = bool(text)
    inserted_units = len(text.encode("utf-16-le")) // 2 if text else 0
    if inserted:
        requests.append(
            {"insertText": {"location": insert_loc, "text": text}}
        )

    # NOT idempotent: a second call deletes a DIFFERENT span (the doc has
    # shifted), so re-running mutates further. Matches insert_table /
    # insert_markdown_table (which also shift document state per call).
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.editRange",
    )
    return {
        "doc_id": doc_id,
        "start_index": start_index,
        "end_index": end_index,
        "tab_id": tab_id,
        "deleted": True,
        "inserted": inserted,
        "inserted_units": inserted_units,
    }


def insert_markdown_table(
    creds: Credentials,
    doc_id: str,
    markdown: str,
    *,
    index: int = 1,
    tab_id: str | None = None,
) -> dict:
    """Parse a markdown table and insert it as a real Google Docs table.

    Two-phase (the only reliable way to populate Docs table cells, whose
    content indices are server-assigned only AFTER the table exists):

      1. Parse the markdown into a rectangular grid
         (``parse_markdown_table``) and ``insertTable`` an empty table
         of that shape at ``index`` (optionally tab-scoped).
      2. Re-fetch the document, read each cell's start index from the
         created table, and ``insertText`` the cell contents in a single
         batchUpdate processed in REVERSE document order (so earlier
         inserts don't shift the indices of later ones).

    Args:
        creds: OAuth credentials carrying the ``documents`` scope.
        doc_id: The Google Doc ID.
        markdown: A GFM markdown table (header row, ``|---|---|``
            separator, body rows).
        index: Body location index to insert at. Default 1. >= 1.
        tab_id: Optional tab to target; ``None`` = default/first tab.

    Returns:
        ``{doc_id, rows, columns, index, tab_id, cells_filled}`` —
        ``cells_filled`` is the count of non-empty cells written.

    Raises:
        ValueError: malformed markdown table, or ``index`` < 1.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if index < 1:
        raise ValueError("index must be >= 1 (index 0 is reserved by Docs).")
    parsed = parse_markdown_table(markdown)  # raises ValueError if bad
    rows, columns, cells = parsed["rows"], parsed["columns"], parsed["cells"]

    docs = get_service("docs", "v1", credentials=creds)
    location: dict[str, Any] = {"index": index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        location["tabId"] = tab_id

    # Phase 1: create the empty table.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{
                "insertTable": {
                    "location": location, "rows": rows, "columns": columns,
                }
            }]},
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.insertTable",
    )

    # Phase 2: re-fetch, find the table, map (row, col) -> cell start
    # index, then insert text in reverse index order.
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True,
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.afterInsertTable",
    )
    cell_starts = _locate_table_cell_starts(fetched, index, tab_id, rows, columns)

    text_requests: list[tuple[int, dict]] = []
    for r in range(rows):
        for c in range(columns):
            text = cells[r][c]
            if not text:
                continue
            start = cell_starts[(r, c)]
            text_requests.append((
                start,
                {"insertText": {
                    "location": (
                        {"index": start, "tabId": tab_id}
                        if tab_id is not None else {"index": start}
                    ),
                    "text": text,
                }},
            ))
    # Reverse document order so each insertion's index stays valid as we
    # mutate the doc from the bottom up.
    text_requests.sort(key=lambda pair: pair[0], reverse=True)
    cells_filled = len(text_requests)
    if text_requests:
        execute_with_retry(
            lambda: docs.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [req for _idx, req in text_requests]},
            ).execute(),
            idempotent=False,
            op_name="docs.documents.batchUpdate.fillTableCells",
        )

    return {
        "doc_id": doc_id,
        "rows": rows,
        "columns": columns,
        "index": index,
        "tab_id": tab_id,
        "cells_filled": cells_filled,
    }


def _locate_table_cell_starts(
    document: dict,
    insert_index: int,
    tab_id: str | None,
    rows: int,
    columns: int,
) -> dict[tuple[int, int], int]:
    """Map ``(row, col)`` → first content index of each cell in the table.

    Finds the ``table`` structural element at/after ``insert_index`` in
    the target tab's body, then walks its ``tableRows[].tableCells[]``,
    reading each cell's first paragraph element ``startIndex`` (the
    index at which inserted text lands).

    Raises:
        RuntimeError: the inserted table can't be found / has an
            unexpected shape (defensive — should not happen right after
            a successful insertTable).
    """
    body_content = _table_search_content(document, tab_id)
    table = None
    for element in body_content:
        if "table" in element and element.get("startIndex", -1) >= insert_index:
            table = element["table"]
            break
    if table is None:
        raise RuntimeError(
            "could not locate the inserted table in the re-fetched document."
        )
    table_rows = table.get("tableRows", [])
    if len(table_rows) != rows:
        raise RuntimeError(
            f"inserted table row count {len(table_rows)} != expected {rows}."
        )
    starts: dict[tuple[int, int], int] = {}
    for r, table_row in enumerate(table_rows):
        table_cells = table_row.get("tableCells", [])
        if len(table_cells) != columns:
            raise RuntimeError(
                f"row {r} cell count {len(table_cells)} != expected {columns}."
            )
        for c, cell in enumerate(table_cells):
            cell_content = cell.get("content", [])
            # A fresh cell has one empty paragraph; its startIndex is
            # where text should be inserted.
            if not cell_content:
                raise RuntimeError(f"cell ({r},{c}) has no content element.")
            starts[(r, c)] = cell_content[0]["startIndex"]
    return starts


def _table_search_content(document: dict, tab_id: str | None) -> list[dict]:
    """Return the body content list to search for the table.

    With ``tab_id``, descends into that tab; otherwise uses the default
    tab (``tabs[0]``) when the doc is tab-structured, else the top-level
    ``body`` (older non-tabbed docs).
    """
    tabs = document.get("tabs")
    if tabs:
        if tab_id is not None:
            tab = _find_tab_by_id(tabs, tab_id)
            if tab is None:
                raise RuntimeError(f"tab {tab_id} not found in re-fetched doc.")
        else:
            tab = tabs[0]
        return (
            tab.get("documentTab", {})
            .get("body", {})
            .get("content", [])
        )
    return document.get("body", {}).get("content", [])


def read_all_tabs(creds: Credentials, doc_id: str) -> dict:
    """Read body content of every tab in one call.

    Bulk version of ``read_tab_content``. Useful when you want a
    whole-document dump (e.g. for offline review or text search).

    Returns ``{"doc_id", "tabs": [{tab_id, title, depth, paragraphs:
    [{style, text}, ...]}, ...]}`` — tabs in pre-order traversal.
    """
    docs = get_service("docs", "v1", credentials=creds)
    # PR-Δ3.5: read_all_tabs supports gdocs_read_doc (readonly=True path).
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.read_all",
    )

    out: list[dict] = []

    def walk(tabs: list[dict], depth: int = 0) -> None:
        for tab in tabs:
            props = tab["tabProperties"]
            body = tab.get("documentTab", {}).get("body", {})
            paragraphs = _summarize_body_paragraphs(body.get("content") or [])
            out.append(
                {
                    "tab_id": props["tabId"],
                    "title": props.get("title", ""),
                    "depth": depth,
                    "paragraph_count": len(paragraphs),
                    "paragraphs": paragraphs,
                }
            )
            walk(tab.get("childTabs") or [], depth + 1)

    walk(fetched.get("tabs") or [])
    from appscriptly.services.drive.api import is_file_trashed
    return {
        "doc_id": doc_id,
        "trashed": is_file_trashed(creds, doc_id),
        "tabs": out,
    }


def _summarize_body_paragraphs(content: list[dict]) -> list[dict]:
    """Extract paragraph style + visible text from a body's content list."""
    out: list[dict] = []
    for elem in content:
        if "paragraph" in elem:
            para = elem["paragraph"]
            style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
            text = "".join(
                pe.get("textRun", {}).get("content", "")
                for pe in para.get("elements", [])
            ).rstrip("\n")
            out.append({"style": style, "text": text})
        elif "table" in elem:
            out.append({"style": "TABLE", "text": ""})
        elif "tableOfContents" in elem:
            out.append({"style": "TOC", "text": ""})
    return out


def delete_tab(creds: Credentials, doc_id: str, tab_id: str) -> None:
    """Delete a single tab (and its child tabs) from a Google Doc.

    Uses the REST ``deleteTab`` batchUpdate request (the API name is
    ``deleteTab``, not ``deleteDocumentTab``). Per the API contract,
    deleting a tab cascades to its child tabs.
    """
    docs = get_service("docs", "v1", credentials=creds)
    # PR-Δ3.5: gdocs_delete_tab is destructive=True but idempotent=True
    # (deleting an already-deleted tab returns 400 invalidArgument, which
    # is in the non-retryable 4xx set — propagates to caller as the
    # existing behavior).
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.deleteTab",
    )


def rename_tab(
    creds: Credentials,
    doc_id: str,
    tab_id: str,
    *,
    title: str | None = None,
    icon_emoji: str | None = None,
) -> None:
    """Rename a tab and/or set its icon emoji.

    Pass either ``title``, ``icon_emoji``, or both. Wraps
    ``updateDocumentTabProperties`` with the appropriate field mask.
    """
    fields = []
    props: dict[str, Any] = {"tabId": tab_id}
    if title is not None:
        props["title"] = title
        fields.append("title")
    if icon_emoji is not None:
        props["iconEmoji"] = icon_emoji
        fields.append("iconEmoji")
    if not fields:
        return
    docs = get_service("docs", "v1", credentials=creds)
    # PR-Δ3.5: gdocs_rename_tab is idempotent=True (renaming to the same
    # name twice yields the same end state).
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "updateDocumentTabProperties": {
                            "tabProperties": props,
                            "fields": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.updateTabProperties",
    )


def set_tab_icons(
    creds: Credentials,
    doc_id: str,
    icons_by_title: dict[str, str],
) -> dict:
    """Set/update icon emojis on existing tabs by matching tab titles.

    Title matching is case-insensitive substring: the first tab whose
    title contains the key (or whose title is contained in the key)
    gets the emoji. Useful right after ``convert_docx_to_tabbed_doc``
    when the caller wants to decorate the auto-named tabs.

    Returns the count of tabs updated, the title -> tab_id map of
    matches, and a list of keys that didn't match any tab.
    """
    if not icons_by_title:
        raise ValueError("icons_by_title cannot be empty")

    docs = get_service("docs", "v1", credentials=creds)
    # PR-Δ3.5: gdocs_set_tab_icons is idempotent=True (setting the same
    # emoji on the same tab twice is a no-op).
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.set_icons",
    )

    # Collect all (tab_id, title) pairs across the nesting.
    all_tabs: list[tuple[str, str]] = []

    def walk(tabs: list[dict]) -> None:
        for tab in tabs:
            props = tab["tabProperties"]
            all_tabs.append((props["tabId"], props.get("title", "")))
            walk(tab.get("childTabs") or [])

    walk(fetched.get("tabs") or [])

    matched: dict[str, str] = {}  # key -> tab_id
    requests: list[dict] = []

    for key, emoji in icons_by_title.items():
        if not emoji:
            continue
        key_low = key.lower()
        for tab_id, title in all_tabs:
            if tab_id in matched.values():
                continue  # don't reuse a tab for multiple keys
            t_low = title.lower()
            if key_low in t_low or t_low in key_low:
                matched[key] = tab_id
                requests.append(
                    {
                        "updateDocumentTabProperties": {
                            "tabProperties": {
                                "tabId": tab_id,
                                "iconEmoji": emoji,
                            },
                            "fields": "iconEmoji",
                        }
                    }
                )
                break

    if requests:
        # PR-Δ3.5: same idempotence rationale as the fetch above.
        execute_with_retry(
            lambda: docs.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute(),
            idempotent=True,
            op_name="docs.documents.batchUpdate.set_icons",
        )

    unmatched = [k for k in icons_by_title if k not in matched]
    return {
        "updated_count": len(requests),
        "matched": matched,
        "unmatched_titles": unmatched,
    }


def append_to_tab(
    creds: Credentials,
    doc_id: str,
    tab_id: str,
    content: str,
    content_format: str = "markdown",
) -> dict:
    """Append content to the end of an existing tab's body.

    Markdown is rendered like in ``create_tabbed_doc``; ``text`` mode
    inserts raw. Existing content is untouched.
    """
    if not content:
        return {"tab_id": tab_id, "appended_chars": 0}

    docs = get_service("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    tab = _find_tab_by_id(fetched.get("tabs") or [], tab_id)
    if tab is None:
        raise ValueError(f"Tab {tab_id} not found in doc {doc_id}")

    body_content = tab["documentTab"]["body"]["content"]
    # The body always ends with an implicit trailing newline at endIndex - 1.
    # Insert just before it so we extend the body rather than overflowing it.
    end_index = (
        body_content[-1]["endIndex"] - 1 if body_content else 1
    )

    if content_format == "text":
        requests = [
            {
                "insertText": {
                    "location": {"tabId": tab_id, "index": end_index},
                    "text": content,
                }
            }
        ]
    else:
        requests = render_content_to_requests(
            content, tab_id, starting_index=end_index
        )

    if requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

    return {"tab_id": tab_id, "appended_chars": len(content)}


__all__ = [
    # Constants
    "CODE_BG_RGB",
    "CODE_FONT",
    "MAX_NESTING_DEPTH",
    # Types
    "TabSpec",
    # REST call sites
    "add_tabs_to_doc",
    "append_to_tab",
    "delete_tab",
    "edit_range",
    "get_doc_outline",
    "make_doc_with_tabs",
    "read_all_tabs",
    "read_tab_content",
    "rename_tab",
    "replace_all_text",
    "set_tab_icons",
    # Re-exported pure helpers (still imported via .api by callers)
    "_add_tab_request",
    "_find_tab_by_id",
    "_find_tab_by_title",
    "_flatten_tab_tree",
    "_get_tab_depth",
    "_plain_text_requests",
    "_rename_tab_request",
    "_summarize_body_paragraphs",
    "_tab_properties",
    "render_content_to_requests",
]
