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
