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
from googleapiclient.errors import HttpError

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
    _is_table_separator,
    _plain_text_requests,
    _rename_tab_request,
    _split_table_row,
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


def _doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


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
    table_tabs: list[tuple[str, str]] = []
    for _depth, path, spec in flat:
        tab_id = path_to_tab_id[path]
        fmt = spec.get("content_format", "markdown")
        content = spec.get("content", "")
        if fmt == "text":
            content_requests.extend(_plain_text_requests(content, tab_id))
        elif _content_has_table(content):
            # Tables need the two-phase (insertTable -> re-fetch -> fill),
            # which can't share the one-shot batch; apply per tab below.
            table_tabs.append((tab_id, content))
        else:
            content_requests.extend(render_content_to_requests(content, tab_id))

    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": content_requests}
        ).execute()
    for tab_id, content in table_tabs:
        _apply_markdown_content(docs, doc_id, tab_id, content)

    return {
        "doc_id": doc_id,
        "url": _doc_url(doc_id),
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
            fetched = execute_with_retry(
                lambda: docs.documents().get(
                    documentId=doc_id, includeTabsContent=True
                ).execute(),
                idempotent=True,
                op_name="docs.documents.get.materializeTabTree.firstTab",
            )
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

        fetched = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.materializeTabTree.newTabIds",
        )

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

    Returns ``{"doc_id", "url", "tabs"}`` - the same envelope as
    ``make_doc_with_tabs``, which is also what
    ``GDOCS_ADD_TABS_OUTPUT_SCHEMA`` requires. Returning only ``tabs``
    made FastMCP output validation fail EVERY ``gdocs_add_tabs`` call
    AFTER both mutating batchUpdates had landed, so a client retrying
    the "failed" call duplicated the tabs (found live, 2026-07-02
    demo).
    """
    flat = _flatten_tab_tree(tabs)
    if not flat:
        return {"doc_id": doc_id, "url": _doc_url(doc_id), "tabs": []}

    max_depth = max((d for d, _, _ in flat), default=0)

    docs = get_service("docs", "v1", credentials=creds)

    if parent_tab_id:
        fetched = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.addTabs.parentDepth",
        )
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

        fetched = execute_with_retry(
            lambda: docs.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(),
            idempotent=True,
            op_name="docs.documents.get.addTabs.newTabIds",
        )

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
    table_tabs: list[tuple[str, str]] = []
    for _depth, path, spec in flat:
        tab_id = path_to_tab_id[path]
        fmt = spec.get("content_format", "markdown")
        content = spec.get("content", "")
        if fmt == "text":
            content_requests.extend(_plain_text_requests(content, tab_id))
        elif _content_has_table(content):
            # Tables need the two-phase (insertTable -> re-fetch -> fill),
            # which can't share the one-shot batch; apply per tab below.
            table_tabs.append((tab_id, content))
        else:
            content_requests.extend(render_content_to_requests(content, tab_id))

    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": content_requests}
        ).execute()
    for tab_id, content in table_tabs:
        _apply_markdown_content(docs, doc_id, tab_id, content)

    return {
        "doc_id": doc_id,
        "url": _doc_url(doc_id),
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


# Valid ``suggestionsViewMode`` values for ``documents.get``. Controls
# how tracked-change SUGGESTIONS render in the returned content:
#   * DEFAULT_FOR_CURRENT_ACCESS - Docs picks per the caller's access
#     (inline suggestions if the user can edit, accepted preview if not).
#   * SUGGESTIONS_INLINE         - suggestions returned inline (with
#     suggestedInsertionIds / suggestedDeletionIds on the runs).
#   * PREVIEW_SUGGESTIONS_ACCEPTED - content as if all suggestions were
#     accepted.
#   * PREVIEW_WITHOUT_SUGGESTIONS - content as if all suggestions were
#     rejected (the clean current text).
_SUGGESTIONS_VIEW_MODES = frozenset({
    "DEFAULT_FOR_CURRENT_ACCESS",
    "SUGGESTIONS_INLINE",
    "PREVIEW_SUGGESTIONS_ACCEPTED",
    "PREVIEW_WITHOUT_SUGGESTIONS",
})


def _check_suggestions_view_mode(mode: str | None) -> None:
    """Reject an unknown ``suggestions_view_mode`` client-side.

    ``None`` is allowed (the param is omitted from the request, so Docs
    applies its own default). A non-None typo surfaces a clear message
    naming the valid modes rather than a generic Google 400.
    """
    if mode is not None and mode not in _SUGGESTIONS_VIEW_MODES:
        raise ValueError(
            f"suggestions_view_mode must be one of "
            f"{sorted(_SUGGESTIONS_VIEW_MODES)} (or omitted); got {mode!r}."
        )


def read_tab_content(
    creds: Credentials,
    doc_id: str,
    tab_id: str | None = None,
    tab_title: str | None = None,
    *,
    suggestions_view_mode: str | None = None,
    include_indices: bool = False,
) -> dict:
    """Read the body content of a single tab.

    Identify the tab by ``tab_id`` (exact) or ``tab_title`` (first match,
    pre-order). Returns structural metadata plus a paragraphs list so the
    caller can verify what actually landed in the tab, useful right
    after ``convert_docx_to_tabbed_doc`` to confirm content moved
    correctly.

    Tables are reported as a ``[table RxC]`` marker followed by the
    extracted cell text (rows joined by " || ", cells by " | ");
    ``table_count`` still counts them. Inline images show up as
    ``[image]`` markers within the paragraph text (and ``image_count``).
    Both read paths share one element-summarizer, so ``read_all_tabs``
    emits the identical per-element markers.

    ``suggestions_view_mode`` (optional) controls how tracked-change
    suggestions render: ``PREVIEW_WITHOUT_SUGGESTIONS`` reads the clean
    current text, ``PREVIEW_SUGGESTIONS_ACCEPTED`` reads as if all
    suggestions were accepted, ``SUGGESTIONS_INLINE`` keeps them inline.
    Omit to use Docs' default for the caller's access.

    ``include_indices`` (optional): when True, every ``paragraphs``
    entry also carries ``start_index`` / ``end_index``. For a paragraph
    ``[start_index, end_index)`` covers exactly the paragraph's ``text``
    (its terminating newline is NOT included), which is the span
    ``create_named_range`` consumes, so a caller can pass it straight
    through and a later ``replace_named_range_content`` fills the field
    without merging the next paragraph. ``insert_page_break`` takes a
    single ``index`` (use a paragraph's ``start_index``). A caller never
    has to compute an index client-side.
    """
    if not tab_id and not tab_title:
        raise ValueError("Provide either tab_id or tab_title")
    _check_suggestions_view_mode(suggestions_view_mode)

    docs = get_service("docs", "v1", credentials=creds)
    get_kwargs: dict[str, Any] = {
        "documentId": doc_id,
        "includeTabsContent": True,
    }
    if suggestions_view_mode is not None:
        get_kwargs["suggestionsViewMode"] = suggestions_view_mode
    # PR-Δ3.5: gdocs_read_doc is readonly=True, idempotent=True.
    fetched = execute_with_retry(
        lambda: docs.documents().get(**get_kwargs).execute(),
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
    paragraphs, table_count, image_count = _summarize_body_content(
        body_content, include_indices=include_indices
    )

    from appscriptly.services.drive.api import is_file_trashed
    return {
        # BUG 3 / S2.1 (2026-07-10): the tool-level output schema
        # (GDOCS_READ_DOC_OUTPUT_SCHEMA) requires ``doc_id`` on every
        # gdocs_read_doc response, but this single-tab payload never
        # carried it — so BOTH single-tab modes (tab_id and tab_title)
        # failed MCP output validation ("'doc_id' is a required
        # property") while the whole-doc mode (read_all_tabs, which
        # does include doc_id) passed. Echoing doc_id here makes all
        # three modes satisfy the declared schema.
        "doc_id": doc_id,
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


# ---------------------------------------------------------------------
# Template fill: named ranges + page break (single-request builders).
# Each follows the insert_table shape (one documents.batchUpdate request,
# conditional tabId threading). Named ranges are the robust template
# primitive: mark a span once, then rewrite it server-side with ZERO
# client index arithmetic. Indices only ever come FROM A READ
# (gdocs_read_doc include_indices), never computed here.
# ---------------------------------------------------------------------


def create_named_range(
    creds: Credentials,
    doc_id: str,
    name: str,
    start_index: int,
    end_index: int,
    *,
    tab_id: str | None = None,
) -> dict:
    """Create a named range over an EXISTING span ``[start_index, end_index)``.

    A named range is a stable, server-managed marker over a run of
    content. It is the robust anchor for template fill: mark a span
    once, then rewrite it any number of times with
    ``replace_named_range_content`` WITHOUT re-reading or recomputing
    indices. The range is tab-scoped via ``tab_id`` for multi-tab docs.

    The indices MUST come from a prior read. Call
    ``read_tab_content`` / ``read_all_tabs`` with ``include_indices``
    and pass a paragraph's ``start_index`` / ``end_index`` straight
    through. That read returns the CONTENT span (the paragraph's
    terminating newline excluded), so a plain-string
    ``replace_named_range_content`` fill later swaps the text and
    preserves the paragraph break. This function NEVER computes indices
    from text; searching for a token and deriving its span is a separate,
    higher risk capability that is out of scope here.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline, no extra grant).
        doc_id: The Google Doc ID.
        name: The named-range name. One name may cover several ranges;
            ``replace_named_range_content`` / ``delete_named_range``
            address every range sharing the name (optionally tab-scoped).
        start_index: Span start (>= 1; index 0 is reserved by Docs),
            read from a prior ``include_indices`` read.
        end_index: Span end, exclusive (> ``start_index``), from the
            same read.
        tab_id: Optional tab to scope the range to (from
            ``get_doc_outline`` / a read). Omit for the default tab.

    Returns:
        ``{doc_id, named_range_id, name, start_index, end_index,
        tab_id}``. ``named_range_id`` is the server id parsed from the
        ``createNamedRange`` reply, to address this one range precisely.

    Raises:
        ValueError: empty ``name``, ``start_index`` < 1, or
            ``end_index`` <= ``start_index``.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.
    """
    if not name or not name.strip():
        raise ValueError("name cannot be empty.")
    if start_index < 1:
        raise ValueError("start_index must be >= 1 (index 0 is reserved by Docs).")
    if end_index <= start_index:
        raise ValueError("end_index must be greater than start_index.")

    docs = get_service("docs", "v1", credentials=creds)
    range_obj: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        range_obj["tabId"] = tab_id
    req = {"createNamedRange": {"name": name, "range": range_obj}}
    # NOT idempotent: each call creates ANOTHER range (a name may map to
    # multiple ranges). Single attempt, same posture as insert_table.
    resp = execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.createNamedRange",
    )
    named_range_id = (
        resp.get("replies", [{}])[0]
        .get("createNamedRange", {})
        .get("namedRangeId")
    )
    return {
        "doc_id": doc_id,
        "named_range_id": named_range_id,
        "name": name,
        "start_index": start_index,
        "end_index": end_index,
        "tab_id": tab_id,
    }


def _named_range_selector(
    named_range_name: str | None,
    named_range_id: str | None,
    tab_ids: list[str] | None,
    *,
    name_field: str,
) -> tuple[dict, str, str, Any]:
    """Build the shared ``{<name_field>|namedRangeId (+tabsCriteria)}``.

    Used by ``replace_named_range_content`` + ``delete_named_range`` so
    the request CONSTRUCTION stays in lockstep, but that shared shape
    does NOT imply shared server behavior. Two real Docs API
    asymmetries: (1) the by-NAME field key differs -
    ``replaceNamedRangeContent`` selects by ``namedRangeName`` while
    ``deleteNamedRange`` selects by ``name`` (each caller passes the
    right one as ``name_field``); (2) with ``tabsCriteria`` omitted,
    replace spans all tabs but delete does NOT, so the ``"all_tabs"``
    scope returned for a name-only delete reflects request intent, not
    delete's actual reach. Returns
    ``(fields, selector, selector_value, scope)`` where ``fields`` is
    merged into the request, ``selector`` is ``"named_range_name"`` or
    ``"named_range_id"`` (the tool's own echo label, NOT the API field),
    and ``scope`` is the tab scope (``"all_tabs"`` / a tab-id list) for
    name selection or ``None`` for id selection.

    Enforces: EXACTLY ONE of name/id; ``tab_ids`` only with a name (an
    id is globally unique, so tab scoping is meaningless for it).
    """
    if named_range_id is not None:
        if named_range_name is not None:
            raise ValueError(
                "Provide exactly one of named_range_name or named_range_id."
            )
        if tab_ids is not None:
            raise ValueError(
                "tab_ids applies only to named_range_name "
                "(a named_range_id is globally unique)."
            )
        return {"namedRangeId": named_range_id}, "named_range_id", named_range_id, None
    if named_range_name is None:
        raise ValueError(
            "Provide exactly one of named_range_name or named_range_id."
        )
    fields: dict[str, Any] = {name_field: named_range_name}
    if tab_ids is not None:
        if not tab_ids:
            raise ValueError(
                "tab_ids list cannot be empty; omit it to target all tabs."
            )
        fields["tabsCriteria"] = {"tabIds": list(tab_ids)}
    scope: Any = "all_tabs" if tab_ids is None else list(tab_ids)
    return fields, "named_range_name", named_range_name, scope


def replace_named_range_content(
    creds: Credentials,
    doc_id: str,
    text: str,
    *,
    named_range_name: str | None = None,
    named_range_id: str | None = None,
    tab_ids: list[str] | None = None,
) -> dict:
    """Replace the content of a named range with ``text`` (server-resolved).

    THE template-fill leverage primitive. The server locates the range
    (by name or id) and swaps its content for ``text`` with ZERO client
    index arithmetic, so it never goes stale as the document changes.
    More robust than a whole-doc ``replace_all_text``, which matches raw
    text anywhere in the body.

    Provide EXACTLY ONE of ``named_range_name`` or ``named_range_id``:
    by NAME addresses every range sharing that name (scope with
    ``tab_ids`` to a subset of tabs; omit = all tabs); by ID addresses
    one specific range (``tab_ids`` not applicable).

    Args:
        creds: OAuth credentials carrying the ``documents`` scope.
        doc_id: The Google Doc ID.
        text: Replacement text (typically non-empty).
        named_range_name: Address every range with this name.
        named_range_id: Address one range by its server id (from
            ``create_named_range``).
        tab_ids: With ``named_range_name`` only, limit the replace to
            these tabs. Omit to hit every tab.

    Returns:
        ``{doc_id, selector, selector_value, text_length, scope}``. The
        Docs ``replaceNamedRangeContent`` reply carries NO match count,
        so this echoes the request and does NOT report how many ranges
        matched. A name matching nothing is a no-op success (relied on
        by the delete then replace cleanup path).

    Raises:
        ValueError: neither or both selectors; ``tab_ids`` with
            ``named_range_id``; empty ``tab_ids`` list.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.
    """
    fields, selector, selector_value, scope = _named_range_selector(
        named_range_name, named_range_id, tab_ids, name_field="namedRangeName"
    )
    docs = get_service("docs", "v1", credentials=creds)
    req = {"replaceNamedRangeContent": {"text": text, **fields}}
    # Idempotent: replacing a range's content with the same text twice
    # lands the same final state (same posture as replace_all_text).
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=True,
        op_name="docs.documents.batchUpdate.replaceNamedRangeContent",
    )
    return {
        "doc_id": doc_id,
        "selector": selector,
        "selector_value": selector_value,
        "text_length": len(text),
        "scope": scope,
    }


def delete_named_range(
    creds: Credentials,
    doc_id: str,
    *,
    named_range_name: str | None = None,
    named_range_id: str | None = None,
    tab_ids: list[str] | None = None,
) -> dict:
    """Delete a named range MARKER (not the content it covered).

    Removes the named-range anchor so the span is no longer addressable
    by ``replace_named_range_content``; the text and paragraphs the
    range covered stay in the document untouched. The cleanup step of
    the mark then fill then clear template loop.

    Provide EXACTLY ONE of ``named_range_name`` or ``named_range_id``.
    Selector shape matches ``replace_named_range_content``, but the tab
    reach is ASYMMETRIC: omitting ``tab_ids`` does NOT fan out to all
    tabs the way replace does, so a marker in a non-default tab requires
    its ``tab_ids`` (Docs otherwise answers "No named range with name").

    Args:
        creds: OAuth credentials carrying the ``documents`` scope.
        doc_id: The Google Doc ID.
        named_range_name: Remove every range with this name.
        named_range_id: Remove one range by its server id.
        tab_ids: With ``named_range_name`` only, limit removal to these
            tabs. Unlike replace, omitting does NOT hit every tab: a
            marker in a non-default tab is reached only by passing its
            ``tab_ids``.

    Returns:
        ``{doc_id, selector, selector_value, scope}`` echoing which
        marker was removed.

    Raises:
        ValueError: selector rules violated (see
            ``replace_named_range_content``).
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.
    """
    fields, selector, selector_value, scope = _named_range_selector(
        named_range_name, named_range_id, tab_ids, name_field="name"
    )
    docs = get_service("docs", "v1", credentials=creds)
    req = {"deleteNamedRange": fields}
    # Destructive marker removal: single attempt (NOT retried) to honor
    # the destructive-op safety floor. A lost-response retry could
    # surface a spurious "not found" on the second delete.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.deleteNamedRange",
    )
    return {
        "doc_id": doc_id,
        "selector": selector,
        "selector_value": selector_value,
        "scope": scope,
    }


def insert_page_break(
    creds: Credentials,
    doc_id: str,
    *,
    index: int | None = None,
    tab_id: str | None = None,
) -> dict:
    """Insert a page break at the tab body end (default) or at ``index``.

    Default (``index`` omitted) appends the break at the END of the
    tab's body via ``endOfSegmentLocation`` - the arithmetic-free path,
    no read required. Pass ``index`` (read from an ``include_indices``
    read) to break at a precise position; the index MUST come from a
    read, this function never computes it.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope.
        doc_id: The Google Doc ID.
        index: Optional body index to break at (>= 1). Omit to append at
            the tab body end.
        tab_id: Optional tab to target. Omit for the default tab.

    Returns:
        ``{doc_id, location_mode, index, tab_id}`` where
        ``location_mode`` is ``"end_of_segment"`` (index omitted) or
        ``"index"``.

    Raises:
        ValueError: ``index`` < 1, or ``tab_id`` is the empty string.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.
    """
    if tab_id is not None and not tab_id.strip():
        raise ValueError("tab_id cannot be the empty string; omit it instead.")
    docs = get_service("docs", "v1", credentials=creds)
    if index is None:
        end_loc: dict[str, Any] = {}
        if tab_id is not None:
            end_loc["tabId"] = tab_id
        page_break: dict[str, Any] = {"endOfSegmentLocation": end_loc}
        location_mode = "end_of_segment"
    else:
        if index < 1:
            raise ValueError("index must be >= 1 (index 0 is reserved by Docs).")
        loc: dict[str, Any] = {"index": index}
        if tab_id is not None:
            loc["tabId"] = tab_id
        page_break = {"location": loc}
        location_mode = "index"
    req = {"insertPageBreak": page_break}
    # NOT idempotent: each call inserts ANOTHER page break. Single
    # attempt, same posture as insert_table.
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [req]}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.insertPageBreak",
    )
    return {
        "doc_id": doc_id,
        "location_mode": location_mode,
        "index": index,
        "tab_id": tab_id,
    }


# Points-to-EMU is not needed for Docs images: the Docs API sizes inline
# images in points (PT) via a Dimension, unlike Slides which uses EMU.
# 1 PT = 1/72 inch. These defaults are intentionally None (let Docs use
# the image's natural size) unless the caller supplies a size.


def insert_image(
    creds: Credentials,
    doc_id: str,
    image_uri: str,
    *,
    index: int = 1,
    tab_id: str | None = None,
    width_pt: float | None = None,
    height_pt: float | None = None,
) -> dict:
    """Insert an inline image from a URI via ``insertInlineImage``.

    Google Docs fetches the image from ``image_uri`` SERVER-SIDE (the URI
    must be publicly reachable by Google and the image under Docs' size /
    format limits: PNG / JPEG / GIF, <= 50 MB / 25 megapixels). Because
    Docs does the fetch, this needs only the baseline ``documents`` scope
    (no Drive scope) for both the doc edit and the image retrieval.

    Args:
        creds: OAuth credentials carrying the ``documents`` scope
            (baseline, no extra grant).
        doc_id: The Google Doc ID.
        image_uri: A publicly accessible ``http(s)`` URI to the image.
            Docs fetches it at insert time and stores its own copy; the
            URI does not need to stay live afterward. Rejected
            client-side if blank or not http(s).
        index: Body location index to insert at. Defaults to 1 (the
            start of the body; index 0 is reserved by Docs). Must be
            >= 1.
        tab_id: Optional tab to target (from ``gdocs_get_doc_outline``).
            ``None`` targets the document's default/first tab.
        width_pt: Optional image width in points (PT, 1/72 inch). When
            BOTH width_pt and height_pt are given, the image is sized to
            that box; when neither is given, Docs uses the image's
            natural size. Supplying only one is rejected (Docs requires
            both dimensions together for an objectSize).
        height_pt: Optional image height in points. See ``width_pt``.

    Returns:
        ``{doc_id, image_object_id, index, tab_id, uri}``.
        ``image_object_id`` is the inserted image's stable objectId
        (parsed from the ``insertInlineImage`` reply; a valid target for
        a later positioned-object update / delete).

    Raises:
        ValueError: blank / non-http(s) ``image_uri``, ``index`` < 1,
            empty ``tab_id``, a non-positive dimension, or exactly one
            of width_pt / height_pt supplied.
        HttpError: from the underlying SDK on 4xx / 5xx (e.g. Docs
            could not fetch / decode the image), propagated.
    """
    if not image_uri or not image_uri.strip():
        raise ValueError("image_uri cannot be empty.")
    if not image_uri.strip().lower().startswith(("http://", "https://")):
        raise ValueError(
            "image_uri must be a public http(s) URL (Google Docs fetches "
            "the image server-side); got a non-http(s) value."
        )
    if index < 1:
        raise ValueError("index must be >= 1 (index 0 is reserved by Docs).")
    if (width_pt is None) != (height_pt is None):
        raise ValueError(
            "width_pt and height_pt must be supplied together (Docs sizes "
            "an inline image with both dimensions) or both omitted (to use "
            "the image's natural size)."
        )
    for name, value in (("width_pt", width_pt), ("height_pt", height_pt)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be > 0 when supplied; got {value}.")

    docs = get_service("docs", "v1", credentials=creds)
    location: dict[str, Any] = {"index": index}
    if tab_id is not None:
        if not tab_id.strip():
            raise ValueError("tab_id cannot be the empty string; omit it instead.")
        location["tabId"] = tab_id

    insert_req: dict[str, Any] = {"location": location, "uri": image_uri.strip()}
    if width_pt is not None and height_pt is not None:
        insert_req["objectSize"] = {
            "width": {"magnitude": width_pt, "unit": "PT"},
            "height": {"magnitude": height_pt, "unit": "PT"},
        }

    # NOT idempotent: each call inserts ANOTHER image. Same convention as
    # insert_table / gslides_create_image.
    resp = execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": [{"insertInlineImage": insert_req}]}
        ).execute(),
        idempotent=False,
        op_name="docs.documents.batchUpdate.insertInlineImage",
    )
    image_object_id = None
    for reply in resp.get("replies", []) or []:
        ins = reply.get("insertInlineImage")
        if ins and ins.get("objectId"):
            image_object_id = ins["objectId"]
            break
    return {
        "doc_id": doc_id,
        "image_object_id": image_object_id,
        "index": index,
        "tab_id": tab_id,
        "uri": image_uri.strip(),
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
    if tab_id is not None and not tab_id.strip():
        raise ValueError("tab_id cannot be the empty string; omit it instead.")
    parsed = parse_markdown_table(markdown)  # raises ValueError if bad

    docs = get_service("docs", "v1", credentials=creds)
    cells_filled = _fill_table_cells(
        docs, doc_id, markdown, index=index, tab_id=tab_id
    )
    return {
        "doc_id": doc_id,
        "rows": parsed["rows"],
        "columns": parsed["columns"],
        "index": index,
        "tab_id": tab_id,
        "cells_filled": cells_filled,
    }


def _batch_update(
    docs: Any, doc_id: str, requests: list[dict], *, op_name: str
) -> None:
    """Run one non-idempotent ``documents.batchUpdate`` with retry."""
    execute_with_retry(
        lambda: docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute(),
        idempotent=False,
        op_name=op_name,
    )


def _fill_table_cells(
    docs: Any, doc_id: str, markdown: str, *, index: int, tab_id: str | None
) -> int:
    """Insert a real Docs table at ``index`` and fill its cells.

    The live-proven two-phase (see ``insert_markdown_table``): ``insertTable``,
    then re-fetch and ``insertText`` each cell's content at its
    SERVER-assigned start index in REVERSE document order. No client-side
    index arithmetic. Returns the count of non-empty cells filled.
    """
    parsed = parse_markdown_table(markdown)
    rows, columns, cells = parsed["rows"], parsed["columns"], parsed["cells"]

    location: dict[str, Any] = {"index": index}
    if tab_id is not None:
        location["tabId"] = tab_id

    # Phase 1: create the empty table.
    _batch_update(
        docs,
        doc_id,
        [{"insertTable": {"location": location, "rows": rows, "columns": columns}}],
        op_name="docs.documents.batchUpdate.insertTable",
    )
    # Phase 2: re-fetch, map (row, col) -> server cell start, fill reverse.
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
            loc = (
                {"index": start, "tabId": tab_id}
                if tab_id is not None else {"index": start}
            )
            text_requests.append(
                (start, {"insertText": {"location": loc, "text": text}})
            )
    # Reverse document order so each insertion's index stays valid as we
    # mutate the doc from the bottom up.
    text_requests.sort(key=lambda pair: pair[0], reverse=True)
    if text_requests:
        _batch_update(
            docs,
            doc_id,
            [req for _idx, req in text_requests],
            op_name="docs.documents.batchUpdate.fillTableCells",
        )
    return len(text_requests)


def _is_fence(line: str) -> bool:
    """A fenced-code-block delimiter line (``` or ~~~)."""
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")


def _content_has_table(content: str) -> bool:
    """True if markdown ``content`` contains a GFM table OUTSIDE a fenced
    code block (a header line immediately followed by a ``|---|---|``
    separator whose column count matches the header). Pipes inside a
    fence are code, not a table.

    The column-count clause mirrors ``_split_content_segments`` so the
    two agree on what a table is: a header/separator pair with mismatched
    column counts is not a GFM table, so it is NOT routed to the table
    path (it renders as prose)."""
    lines = content.split("\n")
    in_fence = False
    for i in range(len(lines) - 1):
        if _is_fence(lines[i]):
            in_fence = not in_fence
            continue
        if (
            not in_fence
            and "|" in lines[i]
            and "|" in lines[i + 1]
            and _is_table_separator(lines[i + 1])
            and len(_split_table_row(lines[i + 1])) == len(_split_table_row(lines[i]))
        ):
            return True
    return False


def _split_content_segments(content: str) -> list[tuple[str, str]]:
    """Split markdown ``content`` into ordered ``("text"|"table", chunk)``
    segments, isolating each GFM table block.

    The doc builder renders each ``table`` segment through the two-phase
    table path (real Docs table) and each ``text`` segment through the
    one-shot markdown renderer, applying them in order at re-fetched
    indices. A table block is a header line, its ``|---|---|`` separator,
    and the consecutive non-blank pipe rows that follow.
    """
    lines = content.split("\n")
    segments: list[tuple[str, str]] = []
    text_buf: list[str] = []
    n = len(lines)

    def flush_text() -> None:
        if text_buf:
            chunk = "\n".join(text_buf)
            if chunk.strip():
                segments.append(("text", chunk))
            text_buf.clear()

    i = 0
    in_fence = False
    while i < n:
        if _is_fence(lines[i]):
            # Pipes inside a fenced code block are code, not a table;
            # keep the whole fence in the text segment.
            in_fence = not in_fence
            text_buf.append(lines[i])
            i += 1
            continue
        is_table_header = (
            not in_fence
            and "|" in lines[i]
            and i + 1 < n
            and "|" in lines[i + 1]
            and _is_table_separator(lines[i + 1])
            and len(_split_table_row(lines[i + 1])) == len(_split_table_row(lines[i]))
        )
        if is_table_header:
            flush_text()
            block = [lines[i], lines[i + 1]]
            j = i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                block.append(lines[j])
                j += 1
            segments.append(("table", "\n".join(block)))
            i = j
        else:
            text_buf.append(lines[i])
            i += 1
    flush_text()
    return segments


def _tab_body_end_index(docs: Any, doc_id: str, tab_id: str | None) -> int:
    """Re-fetch and return the index to insert at the END of the tab body
    (just before the body's implicit trailing newline)."""
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True,
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.tabBodyEnd",
    )
    body_content = _table_search_content(fetched, tab_id)
    return body_content[-1]["endIndex"] - 1 if body_content else 1


def _apply_markdown_content(
    docs: Any, doc_id: str, tab_id: str, content: str
) -> None:
    """Apply markdown ``content`` to ``tab_id`` with GFM tables as real
    Docs tables via the live-proven two-phase.

    Content is split at table boundaries and each segment is applied at a
    FRESHLY RE-FETCHED insertion index. There is NO client-side table-index
    arithmetic: every index - cell starts AND the position of content that
    follows a table - is read back from the live document. This is the fix
    for the mid-content ``insertTable`` leading-newline shift that made the
    one-shot renderer emit out-of-bounds insert indices.
    """
    for kind, segment in _split_content_segments(content):
        insert_at = _tab_body_end_index(docs, doc_id, tab_id)
        if kind == "text":
            requests = render_content_to_requests(
                segment, tab_id, starting_index=insert_at
            )
            if requests:
                _batch_update(
                    docs,
                    doc_id,
                    requests,
                    op_name="docs.documents.batchUpdate.tabTextSegment",
                )
        else:
            _fill_table_cells(docs, doc_id, segment, index=insert_at, tab_id=tab_id)


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


def read_all_tabs(
    creds: Credentials,
    doc_id: str,
    *,
    suggestions_view_mode: str | None = None,
    include_indices: bool = False,
) -> dict:
    """Read body content of every tab in one call.

    Bulk version of ``read_tab_content``. Useful when you want a
    whole-document dump (e.g. for offline review or text search).

    ``suggestions_view_mode`` (optional) controls how tracked-change
    suggestions render (same values + meaning as ``read_tab_content``);
    omit to use Docs' default for the caller's access.

    ``include_indices`` (optional): when True, every ``paragraphs``
    entry (in every tab) also carries ``start_index`` / ``end_index``,
    identical to ``read_tab_content`` (the shared summarizer produces
    both). For a paragraph the span covers exactly its ``text`` (the
    terminating newline excluded), ready to pass straight to
    ``create_named_range``.

    Returns ``{"doc_id", "tabs": [{tab_id, title, depth,
    paragraph_count, table_count, image_count, paragraphs:
    [{style, text}, ...]}, ...]}`` (tabs in pre-order traversal). Each
    tab's ``paragraphs`` come from the same element-summarizer
    ``read_tab_content`` uses, so the per-element markers match.
    """
    _check_suggestions_view_mode(suggestions_view_mode)
    docs = get_service("docs", "v1", credentials=creds)
    get_kwargs: dict[str, Any] = {
        "documentId": doc_id,
        "includeTabsContent": True,
    }
    if suggestions_view_mode is not None:
        get_kwargs["suggestionsViewMode"] = suggestions_view_mode
    # PR-Δ3.5: read_all_tabs supports gdocs_read_doc (readonly=True path).
    fetched = execute_with_retry(
        lambda: docs.documents().get(**get_kwargs).execute(),
        idempotent=True,
        op_name="docs.documents.get.read_all",
    )

    out: list[dict] = []

    def walk(tabs: list[dict], depth: int = 0) -> None:
        for tab in tabs:
            props = tab["tabProperties"]
            body = tab.get("documentTab", {}).get("body", {})
            paragraphs, table_count, image_count = _summarize_body_content(
                body.get("content") or [], include_indices=include_indices
            )
            out.append(
                {
                    "tab_id": props["tabId"],
                    "title": props.get("title", ""),
                    "depth": depth,
                    "paragraph_count": sum(
                        1 for p in paragraphs if p["style"] not in ("TABLE", "TOC")
                    ),
                    "table_count": table_count,
                    "image_count": image_count,
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


# Recursion cap for nested tables (Docs permits a table inside a cell).
# Bounds the cell-text walk; real docs nest at most a couple of levels.
_MAX_CELL_TABLE_DEPTH = 6


# Sentinel: ``_tag`` distinguishes "no end_index override, use the raw
# element endIndex" (tables, TOC) from a paragraph passing its trimmed
# content end (possibly ``None`` for an index-0 element).
_RAW_END = object()


def _summarize_body_content(
    content: list[dict], *, _depth: int = 0, include_indices: bool = False
) -> tuple[list[dict], int, int]:
    """Summarize a body content list into ``{style, text}`` entries + counts.

    The SINGLE element-summarizer shared by both read paths -
    ``read_tab_content`` (single tab) and ``read_all_tabs`` (bulk) - so
    their per-element emission is byte-identical. Returns
    ``(paragraphs, table_count, image_count)``.

    When ``include_indices`` is True, each emitted top-level entry also
    carries ``start_index`` / ``end_index``. ``start_index`` is the Docs
    element ``startIndex`` (``None`` for the index-0 element, which Docs
    omits). For a PARAGRAPH ``end_index`` is the CONTENT end: the element
    ``endIndex`` with its paragraph-terminating newline excluded, so the
    returned ``[start_index, end_index)`` span covers exactly ``text``.
    That is the span ``create_named_range`` consumes; passing the raw
    element ``endIndex`` (which points past the ``\n``) would let a
    later ``replace_named_range_content`` delete the terminator and merge
    the next paragraph (finding F4). Non-paragraph elements (table, TOC)
    keep the raw element ``endIndex``. Table-cell recursion always
    summarizes WITHOUT indices (cells are flattened to text, not
    addressable here).

    Element -> emission:
      textRun             -> its content
      inlineObjectElement -> "[image]"              (+ image_count)
      person              -> "[person:<email>]"     (email, or "?")
      richLink            -> "[link]"
      table               -> "[table RxC]" + extracted cell text (+ table_count)
      tableOfContents     -> "[table of contents]"
      sectionBreak        -> skipped

    The counts are TOP-LEVEL only: an image or table nested inside a
    table cell shows its marker in that cell's extracted text but is
    not added to ``image_count`` / ``table_count``.

    Empty ``NORMAL_TEXT`` paragraphs are dropped (blank lines); a
    styled-but-empty paragraph (e.g. an empty heading) is kept so the
    structure stays visible.
    """
    paragraphs: list[dict] = []
    table_count = 0
    image_count = 0

    def _tag(entry: dict, elem: dict, *, end_index: Any = _RAW_END) -> dict:
        # Attach server startIndex/endIndex when requested. Docs omits
        # startIndex on the index-0 element, so start_index may be None.
        # A PARAGRAPH passes end_index = its CONTENT end (the element
        # endIndex with the paragraph-terminating newline excluded) so
        # the returned [start_index, end_index) covers exactly the
        # returned text; a named range built straight from that span
        # fills WITHOUT swallowing the paragraph break (finding F4).
        # Non-paragraph elements (table, TOC) pass no override and keep
        # the raw element endIndex.
        if include_indices:
            entry["start_index"] = elem.get("startIndex")
            entry["end_index"] = (
                elem.get("endIndex") if end_index is _RAW_END else end_index
            )
        return entry

    for elem in content:
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
                    email = (
                        pe["person"]
                        .get("personProperties", {})
                        .get("email", "?")
                    )
                    chunks.append(f"[person:{email}]")
                elif "richLink" in pe:
                    chunks.append("[link]")
            raw = "".join(chunks)
            text = raw.rstrip("\n")
            if text or style != "NORMAL_TEXT":
                # Trim end_index to the content end: a Docs paragraph
                # element's endIndex points PAST its terminating newline,
                # so the raw span includes it and a named-range fill would
                # delete it, merging the next paragraph (F4). len(raw) -
                # len(text) is the trailing-newline count (1 for a normal
                # paragraph); subtract it so the span matches ``text``.
                end = elem.get("endIndex")
                if end is not None:
                    end -= len(raw) - len(text)
                paragraphs.append(
                    _tag({"style": style, "text": text}, elem, end_index=end)
                )
        elif "table" in elem:
            tbl = elem["table"]
            rows = tbl.get("rows", 0)
            cols = tbl.get("columns", 0)
            table_count += 1
            marker = f"[table {rows}x{cols}]"
            cell_text = _extract_table_cell_text(tbl, _depth=_depth)
            paragraphs.append(
                _tag(
                    {
                        "style": "TABLE",
                        "text": f"{marker} {cell_text}" if cell_text else marker,
                    },
                    elem,
                )
            )
        elif "sectionBreak" in elem:
            continue
        elif "tableOfContents" in elem:
            paragraphs.append(
                _tag({"style": "TOC", "text": "[table of contents]"}, elem)
            )
    return paragraphs, table_count, image_count


def _extract_table_cell_text(table: dict, *, _depth: int = 0) -> str:
    """Extract visible cell text from a Docs ``table`` structural element.

    Walks ``tableRows[].tableCells[].content`` - the same traversal the
    write path uses in ``_locate_table_cell_starts`` - and summarizes
    each cell with ``_summarize_body_content`` so nested markers (images,
    links, nested tables) render consistently. Cells are joined by " | "
    within a row; non-empty rows by " || ". Returns "" when the table has
    no cell text, so an empty table stays just its ``[table RxC]`` marker.
    """
    if _depth >= _MAX_CELL_TABLE_DEPTH:
        return ""
    rows_out: list[str] = []
    for table_row in table.get("tableRows", []):
        cells_out: list[str] = []
        for cell in table_row.get("tableCells", []):
            cell_paras, _t, _i = _summarize_body_content(
                cell.get("content", []), _depth=_depth + 1
            )
            cells_out.append(" ".join(p["text"] for p in cell_paras if p["text"]))
        if any(cells_out):
            rows_out.append(" | ".join(cells_out))
    return " || ".join(rows_out)


def _summarize_body_paragraphs(content: list[dict]) -> list[dict]:
    """Back-compat wrapper: the paragraph list only.

    Kept so pre-existing importers keep working; the shared logic (and
    the table_count / image_count) lives in ``_summarize_body_content``.
    """
    return _summarize_body_content(content)[0]


# ---------------------------------------------------------------------
# S2.3 (2026-07-10): known Google-side defect around tab-property updates
# ---------------------------------------------------------------------
#
# Live-reproduced against the production Docs API (session-2 bug report
# + controlled experiment on scratch docs): once a document's ORIGINAL
# FIRST TAB (the implicitly created tab, id "t.0") has been deleted,
# EVERY subsequent ``updateDocumentTabProperties`` batchUpdate on that
# document fails with a deterministic HTTP 500 ("Internal error
# encountered.") - single request or batch, icon or title, root or
# child target, including tabs created AFTER the delete. The state is
# durable (persists across minutes; adding tabs does not clear it).
# Deleting a NON-first tab does not trigger it, and the same requests
# succeed on the same doc BEFORE the first-tab delete, so the request
# shape is not at fault. Reads, addDocumentTab, deleteTab, and content
# edits keep working on the poisoned doc.
#
# We cannot fix Google's backend; what we CAN do is stop reporting the
# failure as a mystery transient. When an updateDocumentTabProperties
# batch 500s AND the fetched tab list shows no root tab with id "t.0",
# the classifier below produces a specific, honest, non-retryable
# error instead of the generic "transient 500, retry" guidance (which
# provably wastes callers' time here - operator hit it 3/3).

_FIRST_TAB_DELETED_500_MESSAGE = (
    "Google returned 500 for a tab-properties update on a document "
    "whose original first tab (id \"t.0\") has been deleted. This "
    "matches a known Google Docs API defect: after the original first "
    "tab is deleted, every updateDocumentTabProperties request (tab "
    "renames and icon changes) on that document fails with 500, "
    "durably.\n"
    "Retryable: false - retrying the same call will keep failing. "
    "Adding tabs does NOT clear the state either (verified live): "
    "gdocs_add_tabs succeeds, but property updates stay broken, "
    "including on the tabs it just added - do not chase that as a "
    "remediation.\n"
    "Workarounds: set icons and titles BEFORE deleting the original "
    "first tab; give new tabs their icon_emoji at creation time "
    "(addDocumentTab accepts it, so gdocs_make_tabbed_doc / "
    "gdocs_add_tabs still decorate correctly); or rebuild the "
    "document. (first_tab_deleted_500)"
)


def _classify_tab_props_500(root_tabs: list[dict], e: HttpError) -> str | None:
    """Return the defect diagnosis when a tabProperties 500 matches it.

    ``root_tabs`` is the document's CURRENT root-level ``tabs`` list
    (fetched fresh). Returns the enriched message when (a) the error is
    an HTTP 500 and (b) no root tab carries the implicit first-tab id
    ``"t.0"`` - the observed signature of the defect. Returns None for
    anything else so the caller falls back to the generic envelope
    (which correctly marks a plain 500 as retryable).

    Only ever runs on the ERROR path; a healthy call never pays for
    this check.
    """
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "resp", None), "status", None)
    if status != 500:
        return None
    has_original_first_tab = any(
        (t.get("tabProperties") or {}).get("tabId") == "t.0"
        for t in root_tabs
    )
    if has_original_first_tab:
        return None
    return _FIRST_TAB_DELETED_500_MESSAGE


def _count_meaningful_body_elements(body_content: list[dict]) -> dict:
    """Count content a user would regret losing in one tab body.

    Returns ``{"paragraphs": n, "tables": n, "images": n, "other": n}``
    where ``paragraphs`` counts paragraphs carrying non-whitespace text
    or an inline object / person chip / rich link, ``tables`` counts
    tables, ``images`` counts inline objects, and ``other`` counts
    tables-of-contents. A freshly created tab (one empty NORMAL_TEXT
    paragraph) counts zero everywhere.
    """
    paragraphs = tables = images = other = 0
    for elem in body_content:
        if "paragraph" in elem:
            para = elem["paragraph"]
            text = "".join(
                pe.get("textRun", {}).get("content", "")
                for pe in para.get("elements", [])
            )
            inline = sum(
                1
                for pe in para.get("elements", [])
                if "inlineObjectElement" in pe
                or "person" in pe
                or "richLink" in pe
            )
            images += sum(
                1
                for pe in para.get("elements", [])
                if "inlineObjectElement" in pe
            )
            if text.strip() or inline:
                paragraphs += 1
        elif "table" in elem:
            tables += 1
        elif "tableOfContents" in elem:
            other += 1
    return {
        "paragraphs": paragraphs,
        "tables": tables,
        "images": images,
        "other": other,
    }


def inspect_tab_content(creds: Credentials, doc_id: str, tab_id: str) -> dict:
    """Summarize one tab (plus its descendants) for the delete guard.

    S2.5 defense (2026-07-10): ``deleteTab`` is permanent - no trash,
    no undo - and the session-2 data-loss incident was a placeholder
    delete that silently destroyed the only copy of four sections
    still sitting inside "Tab 1". Before deleting, the tool layer
    calls this to learn whether the target (INCLUDING child tabs,
    which ``deleteTab`` cascades to) still holds meaningful content.

    Returns::

        {"found": bool,
         "title": str,                # target tab's title ("" if not found)
         "is_first_root_tab": bool,   # target is the doc's first root tab
         "original_first_tab_present": bool,  # the doc still has its
                                      # ORIGINAL first tab (id "t.0" -
                                      # the same convention the
                                      # first_tab_deleted_500 classifier
                                      # keys on); False = the document
                                      # is ALREADY tab-property poisoned
         "child_tab_count": int,      # descendants (cascade-deleted too)
         "non_empty_elements": int,   # total meaningful elements, tab+descendants
         "counts": {"paragraphs", "tables", "images", "other"}}
    """
    docs = get_service("docs", "v1", credentials=creds)
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.inspect_tab",
    )
    root_tabs = fetched.get("tabs") or []
    # Google assigns the deterministic id "t.0" to every document's
    # original first tab; its absence from the ROOT list is the exact
    # signal _classify_tab_props_500 uses for the first_tab_deleted_500
    # defect. Surfaced here so the delete tool can phrase its advisory
    # honestly (will-trigger vs already-affected - R2, retest 2).
    original_first_tab_present = any(
        (rt.get("tabProperties") or {}).get("tabId") == "t.0"
        for rt in root_tabs
    )
    tab = _find_tab_by_id(root_tabs, tab_id)
    if tab is None:
        return {
            "found": False,
            "title": "",
            "is_first_root_tab": False,
            "original_first_tab_present": original_first_tab_present,
            "child_tab_count": 0,
            "non_empty_elements": 0,
            "counts": {"paragraphs": 0, "tables": 0, "images": 0, "other": 0},
        }

    first_root_id = (
        (root_tabs[0].get("tabProperties") or {}).get("tabId")
        if root_tabs
        else None
    )

    totals = {"paragraphs": 0, "tables": 0, "images": 0, "other": 0}
    child_count = 0

    def accumulate(node: dict, *, is_target: bool) -> None:
        nonlocal child_count
        if not is_target:
            child_count += 1
        body = (node.get("documentTab") or {}).get("body") or {}
        counts = _count_meaningful_body_elements(body.get("content") or [])
        for key in totals:
            totals[key] += counts[key]
        for child in node.get("childTabs") or []:
            accumulate(child, is_target=False)

    accumulate(tab, is_target=True)

    return {
        "found": True,
        "title": (tab.get("tabProperties") or {}).get("title", ""),
        "is_first_root_tab": first_root_id == tab_id,
        "original_first_tab_present": original_first_tab_present,
        "child_tab_count": child_count,
        "non_empty_elements": sum(totals.values()),
        "counts": totals,
    }


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
    try:
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
    except HttpError as e:
        # S2.3: rename shares set_tab_icons' failure mode (live-proven:
        # a title-only update 500s identically on a poisoned doc). This
        # path has no pre-fetched tab list, so fetch one now - error
        # path only, and only for a 500 - to run the same classifier.
        status = getattr(e, "status_code", None)
        if status == 500:
            try:
                # includeTabsContent=True is what reliably populates
                # ``tabs`` (without it Docs serves the legacy first-tab
                # body shape); the fields filter keeps this error-path
                # fetch to root tab ids only.
                fetched = execute_with_retry(
                    lambda: docs.documents().get(
                        documentId=doc_id,
                        includeTabsContent=True,
                        fields="tabs(tabProperties(tabId))",
                    ).execute(),
                    idempotent=True,
                    op_name="docs.documents.get.classify_500",
                )
            except HttpError:
                raise e from None
            diagnosis = _classify_tab_props_500(fetched.get("tabs") or [], e)
            if diagnosis is not None:
                raise ValueError(diagnosis) from e
        raise


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
        try:
            # PR-Δ3.5: same idempotence rationale as the fetch above.
            execute_with_retry(
                lambda: docs.documents().batchUpdate(
                    documentId=doc_id, body={"requests": requests}
                ).execute(),
                idempotent=True,
                op_name="docs.documents.batchUpdate.set_icons",
            )
        except HttpError as e:
            # S2.3: reclassify the deterministic post-first-tab-delete
            # 500 (see _classify_tab_props_500) so callers get a real
            # diagnosis + "don't retry" instead of the generic
            # transient-500 guidance. ValueError is the tool layer's
            # "diagnosed condition" channel (mapped to ToolError there).
            diagnosis = _classify_tab_props_500(
                fetched.get("tabs") or [], e
            )
            if diagnosis is not None:
                raise ValueError(diagnosis) from e
            raise

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
    fetched = execute_with_retry(
        lambda: docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(),
        idempotent=True,
        op_name="docs.documents.get.appendToTab",
    )
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
        _batch_update(
            docs,
            doc_id,
            [{"insertText": {
                "location": {"tabId": tab_id, "index": end_index},
                "text": content,
            }}],
            op_name="docs.documents.batchUpdate.appendText",
        )
    elif _content_has_table(content):
        # Tables render via the two-phase re-fetch path (real Docs tables,
        # no client-side index arithmetic).
        _apply_markdown_content(docs, doc_id, tab_id, content)
    else:
        requests = render_content_to_requests(
            content, tab_id, starting_index=end_index
        )
        if requests:
            _batch_update(
                docs, doc_id, requests,
                op_name="docs.documents.batchUpdate.appendMarkdown",
            )

    return {"tab_id": tab_id, "appended_chars": len(content)}


# ---------------------------------------------------------------------
# Comments (Drive v3 comments() / replies()) on app-created docs
# ---------------------------------------------------------------------
#
# Comments live on the DRIVE API, not the Docs API, so these go through
# get_service("drive", "v3", ...). Under the app's drive.file scope, the
# comments + replies endpoints only reach files THIS app created or that
# the user opened with it (owned_by_app); a file the app can't access
# returns 404/403, which propagates to the tool envelope. No new scope:
# drive.file (already deployed) covers comment read + write on those
# files. The Drive comments API REQUIRES a ``fields`` mask on every
# call, so each request passes an explicit field list.

# The comment/reply fields surfaced back to callers. Drive's comments
# API mandates a non-empty ``fields`` mask; this pins the useful subset.
_COMMENT_FIELDS = (
    "id,content,resolved,createdTime,modifiedTime,"
    "author(displayName,me),"
    "replies(id,content,createdTime,modifiedTime,action,author(displayName,me))"
)
_COMMENT_LIST_FIELDS = f"comments({_COMMENT_FIELDS}),nextPageToken"
_REPLY_FIELDS = "id,content,createdTime,modifiedTime,action,author(displayName,me)"


def list_comments(
    creds: Credentials,
    doc_id: str,
    *,
    include_deleted: bool = False,
    page_size: int = 100,
) -> dict:
    """List comments (and their replies) on an app-created doc.

    Uses the Drive v3 ``comments.list`` endpoint. Works only on files
    this app can access under ``drive.file`` (the ones it created or the
    user opened with it); other files return 404/403 from Drive.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope
            (baseline, no extra grant).
        doc_id: The Google Doc (Drive file) ID.
        include_deleted: When True, include deleted comments (their
            content is blanked by Drive but the thread structure
            remains). Default False.
        page_size: Max comments to return (Drive caps at 100). This
            returns the first page only; a follow-up page token is
            surfaced for callers that need more.

    Returns:
        ``{doc_id, comments: [...], next_page_token}``. Each comment is
        the Drive comment resource (id, content, resolved, author,
        timestamps, nested replies). ``next_page_token`` is None when
        there are no further pages.

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx (e.g. 404 if the
            app can't access the file), propagated to the tool envelope.
    """
    drive = get_service("drive", "v3", credentials=creds)
    resp = execute_with_retry(
        lambda: drive.comments().list(
            fileId=doc_id,
            includeDeleted=include_deleted,
            pageSize=page_size,
            fields=_COMMENT_LIST_FIELDS,
        ).execute(),
        idempotent=True,
        op_name="drive.comments.list",
    )
    return {
        "doc_id": doc_id,
        "comments": resp.get("comments", []),
        "next_page_token": resp.get("nextPageToken"),
    }


def create_comment(creds: Credentials, doc_id: str, content: str) -> dict:
    """Create a top-level (unanchored) comment on an app-created doc.

    Uses the Drive v3 ``comments.create`` endpoint. The comment is
    document-level (not anchored to a text range); range-anchored
    comments need a Drive ``anchor`` payload, deferred to a follow-up.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        doc_id: The Google Doc (Drive file) ID.
        content: The comment text. Rejected client-side if blank.

    Returns:
        ``{doc_id, comment: {...}}``: the created Drive comment resource
        (id, content, author, timestamps).

    Raises:
        ValueError: blank ``content``.
        HttpError: from the underlying SDK on 4xx / 5xx (e.g. 404 if the
            app can't access the file), propagated.
    """
    if not content or not content.strip():
        raise ValueError("content cannot be empty (a comment needs text).")
    drive = get_service("drive", "v3", credentials=creds)
    # NOT idempotent: each call creates ANOTHER comment.
    comment = execute_with_retry(
        lambda: drive.comments().create(
            fileId=doc_id,
            body={"content": content},
            fields=_COMMENT_FIELDS,
        ).execute(),
        idempotent=False,
        op_name="drive.comments.create",
    )
    return {"doc_id": doc_id, "comment": comment}


def reply_to_comment(
    creds: Credentials,
    doc_id: str,
    comment_id: str,
    content: str,
) -> dict:
    """Reply to an existing comment on an app-created doc.

    Uses the Drive v3 ``replies.create`` endpoint. Adds a reply under an
    existing comment thread.

    Args:
        creds: OAuth credentials carrying the ``drive.file`` scope.
        doc_id: The Google Doc (Drive file) ID.
        comment_id: The id of the comment to reply to (from
            ``list_comments`` / ``create_comment``).
        content: The reply text. Rejected client-side if blank.

    Returns:
        ``{doc_id, comment_id, reply: {...}}``: the created Drive reply
        resource (id, content, author, timestamps).

    Raises:
        ValueError: blank ``content`` or ``comment_id``.
        HttpError: from the underlying SDK on 4xx / 5xx (e.g. 404 if the
            file/comment isn't app-accessible), propagated.
    """
    if not comment_id or not comment_id.strip():
        raise ValueError("comment_id cannot be empty.")
    if not content or not content.strip():
        raise ValueError("content cannot be empty (a reply needs text).")
    drive = get_service("drive", "v3", credentials=creds)
    # NOT idempotent: each call creates ANOTHER reply.
    reply = execute_with_retry(
        lambda: drive.replies().create(
            fileId=doc_id,
            commentId=comment_id,
            body={"content": content},
            fields=_REPLY_FIELDS,
        ).execute(),
        idempotent=False,
        op_name="drive.replies.create",
    )
    return {"doc_id": doc_id, "comment_id": comment_id, "reply": reply}




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
    "create_comment",
    "delete_tab",
    "edit_range",
    "get_doc_outline",
    "insert_image",
    "inspect_tab_content",
    "list_comments",
    "make_doc_with_tabs",
    "read_all_tabs",
    "read_tab_content",
    "reply_to_comment",
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
