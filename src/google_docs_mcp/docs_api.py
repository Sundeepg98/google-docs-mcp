"""Google Docs API wrapper with native Tabs + markdown rendering.

Walks the markdown-it-py token stream to emit ``insertText`` requests
forward (with index tracking) and collects style ranges; formatting
requests run after inserts so indices stay stable. Adapted from the
pattern used in ``a-bonus/google-docs-mcp`` (MIT, TypeScript) — re-
implemented in Python, not vendored.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

# typing_extensions for backward compat: NotRequired was added to typing
# in 3.11, and pydantic requires typing_extensions.TypedDict (not
# typing.TypedDict) for proper schema generation on Python < 3.12.
# Importing both from typing_extensions makes the package usable on
# the full 3.10+ range we claim to support.
from typing_extensions import NotRequired, TypedDict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from markdown_it import MarkdownIt
from markdown_it.token import Token

CODE_FONT = "Roboto Mono"
CODE_BG_RGB = {"red": 0.945, "green": 0.957, "blue": 0.965}  # #F1F3F4
MAX_NESTING_DEPTH = 3  # Google Docs UI hard limit: root + 2 child levels


class TabSpec(TypedDict):
    title: str
    content: str
    icon_emoji: NotRequired[str | None]
    content_format: NotRequired[Literal["markdown", "text"]]
    children: NotRequired[list[TabSpec]]


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

    docs = build("docs", "v1", credentials=creds)
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


def _flatten_tab_tree(
    tabs: list[TabSpec],
) -> list[tuple[int, tuple[int, ...], TabSpec]]:
    """Pre-order traversal yielding (depth, path, spec) for every tab.

    ``path`` is the tuple of sibling indices from root, e.g. ``(0, 1)``
    means ``tabs[0].children[1]``.
    """
    out: list[tuple[int, tuple[int, ...], TabSpec]] = []

    def walk(specs: list[TabSpec], parent_path: tuple[int, ...]) -> None:
        for i, spec in enumerate(specs):
            path = (*parent_path, i)
            out.append((len(path) - 1, path, spec))
            walk(spec.get("children") or [], path)

    walk(tabs, ())
    return out


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


def _find_tab_by_id(tabs: list[dict], target_id: str) -> dict | None:
    """Recursively locate a tab in a nested ``tabs`` array by its tabId."""
    for tab in tabs:
        if tab["tabProperties"]["tabId"] == target_id:
            return tab
        nested = _find_tab_by_id(tab.get("childTabs") or [], target_id)
        if nested is not None:
            return nested
    return None


def _get_tab_depth(tabs: list[dict], target_id: str, current_depth: int = 0) -> int:
    """Return the nesting depth of a tab (root=0), or -1 if not found."""
    for tab in tabs:
        if tab["tabProperties"]["tabId"] == target_id:
            return current_depth
        result = _get_tab_depth(
            tab.get("childTabs") or [], target_id, current_depth + 1
        )
        if result >= 0:
            return result
    return -1


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

    docs = build("docs", "v1", credentials=creds)

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
    docs = build("docs", "v1", credentials=creds)
    # includeTabsContent must be True for the tabs[] field to be populated
    # at all; without it the response uses the legacy single-tab schema.
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

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
    from .drive_api import is_file_trashed
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

    docs = build("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
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

    from .drive_api import is_file_trashed
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
    docs = build("docs", "v1", credentials=creds)
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

    resp = docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": [req]}
    ).execute()
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
    docs = build("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

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
    from .drive_api import is_file_trashed
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
    docs = build("docs", "v1", credentials=creds)
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
    ).execute()


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
    docs = build("docs", "v1", credentials=creds)
    docs.documents().batchUpdate(
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
    ).execute()


def _find_tab_by_title(tabs: list[dict], target_title: str) -> dict | None:
    """Recursively locate a tab in nested ``tabs`` by exact title match."""
    for tab in tabs:
        if tab["tabProperties"].get("title") == target_title:
            return tab
        nested = _find_tab_by_title(tab.get("childTabs") or [], target_title)
        if nested is not None:
            return nested
    return None


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

    docs = build("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

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
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

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
    content_format: Literal["markdown", "text"] = "markdown",
) -> dict:
    """Append content to the end of an existing tab's body.

    Markdown is rendered like in ``create_tabbed_doc``; ``text`` mode
    inserts raw. Existing content is untouched.
    """
    if not content:
        return {"tab_id": tab_id, "appended_chars": 0}

    docs = build("docs", "v1", credentials=creds)
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


def _tab_properties(tab: TabSpec, *, include_title: bool = True) -> dict:
    props: dict[str, Any] = {}
    if include_title:
        props["title"] = tab["title"]
    icon = tab.get("icon_emoji")
    if icon:
        props["iconEmoji"] = icon
    return props


def _rename_tab_request(tab_id: str, tab: TabSpec) -> dict:
    props = _tab_properties(tab)
    props["tabId"] = tab_id
    fields = ",".join(k for k in props if k != "tabId")
    return {
        "updateDocumentTabProperties": {
            "tabProperties": props,
            "fields": fields,
        }
    }


def _add_tab_request(tab: TabSpec, parent_tab_id: str | None = None) -> dict:
    props = _tab_properties(tab)
    if parent_tab_id:
        props["parentTabId"] = parent_tab_id
    return {"addDocumentTab": {"tabProperties": props}}


def _plain_text_requests(content: str, tab_id: str) -> list[dict]:
    if not content:
        return []
    return [
        {
            "insertText": {
                "location": {"tabId": tab_id, "index": 1},
                "text": content,
            }
        }
    ]


# ───────────────────────── markdown renderer ─────────────────────────


@dataclass
class _Ctx:
    tab_id: str
    current_index: int = 1
    inserts: list[dict] = field(default_factory=list)
    formats: list[dict] = field(default_factory=list)
    style_stack: list[dict] = field(default_factory=list)
    text_ranges: list[tuple[int, int, dict]] = field(default_factory=list)
    para_ranges: list[tuple[int, int, str]] = field(default_factory=list)
    list_items: list[dict] = field(default_factory=list)
    open_items: list[int] = field(default_factory=list)
    list_stack: list[str] = field(default_factory=list)
    para_start: int | None = None
    heading_level: int | None = None


def _loc(ctx: _Ctx, idx: int) -> dict:
    return {"index": idx, "tabId": ctx.tab_id}


def _range(ctx: _Ctx, s: int, e: int) -> dict:
    return {"startIndex": s, "endIndex": e, "tabId": ctx.tab_id}


def _insert(ctx: _Ctx, text: str) -> None:
    if not text:
        return
    ctx.inserts.append(
        {"insertText": {"location": _loc(ctx, ctx.current_index), "text": text}}
    )
    ctx.current_index += len(text)


def _merge_style(stack: list[dict]) -> dict:
    out: dict = {}
    for layer in stack:
        out.update(layer)
    return out


def _last_ends_nl(ctx: _Ctx) -> bool:
    if not ctx.inserts:
        return True
    return ctx.inserts[-1]["insertText"]["text"].endswith("\n")


def _process(tok: Token, ctx: _Ctx) -> None:  # noqa: C901, PLR0912 — token dispatch
    t = tok.type
    if t == "heading_open":
        ctx.heading_level = int(tok.tag[1])
        ctx.para_start = ctx.current_index
    elif t == "heading_close":
        if ctx.para_start is not None and ctx.heading_level:
            lvl = min(ctx.heading_level, 6)
            ctx.para_ranges.append(
                (ctx.para_start, ctx.current_index, f"HEADING_{lvl}")
            )
        _insert(ctx, "\n")
        ctx.heading_level = None
        ctx.para_start = None
    elif t == "paragraph_open":
        if not ctx.list_stack:
            ctx.para_start = ctx.current_index
    elif t == "paragraph_close":
        if not _last_ends_nl(ctx):
            _insert(ctx, "\n")
        if ctx.open_items:
            ctx.list_items[ctx.open_items[-1]]["end"] = ctx.current_index - 1
        ctx.para_start = None
    elif t == "inline":
        for child in tok.children or []:
            _process(child, ctx)
    elif t == "text":
        if not tok.content:
            return
        s = ctx.current_index
        _insert(ctx, tok.content)
        style = _merge_style(ctx.style_stack)
        if style:
            ctx.text_ranges.append((s, ctx.current_index, style))
    elif t == "softbreak":
        _insert(ctx, " ")
    elif t == "hardbreak":
        _insert(ctx, "\n")
    elif t == "strong_open":
        ctx.style_stack.append({"bold": True})
    elif t == "em_open":
        ctx.style_stack.append({"italic": True})
    elif t == "s_open":
        ctx.style_stack.append({"strikethrough": True})
    elif t in ("strong_close", "em_close", "s_close", "link_close"):
        if ctx.style_stack:
            ctx.style_stack.pop()
    elif t == "link_open":
        href = (tok.attrs or {}).get("href")
        ctx.style_stack.append({"link": href} if href else {})
    elif t == "code_inline":
        s = ctx.current_index
        _insert(ctx, tok.content)
        ctx.text_ranges.append((s, ctx.current_index, {"code": True}))
    elif t in ("fence", "code_block"):
        body = tok.content.rstrip("\n")
        if not _last_ends_nl(ctx):
            _insert(ctx, "\n")
        s = ctx.current_index
        _insert(ctx, body + "\n")
        ctx.text_ranges.append((s, s + len(body), {"code": True}))
    elif t == "bullet_list_open":
        ctx.list_stack.append("bullet")
    elif t == "ordered_list_open":
        ctx.list_stack.append("ordered")
    elif t in ("bullet_list_close", "ordered_list_close"):
        if ctx.list_stack:
            ctx.list_stack.pop()
    elif t == "list_item_open":
        depth = len(ctx.list_stack) - 1
        if depth > 0:
            _insert(ctx, "\t" * depth)
        preset = (
            "NUMBERED_DECIMAL_NESTED"
            if ctx.list_stack and ctx.list_stack[-1] == "ordered"
            else "BULLET_DISC_CIRCLE_SQUARE"
        )
        ctx.list_items.append(
            {
                "start": ctx.current_index,
                "end": None,
                "preset": preset,
                "level": depth,
            }
        )
        ctx.open_items.append(len(ctx.list_items) - 1)
    elif t == "list_item_close":
        idx = ctx.open_items.pop()
        item = ctx.list_items[idx]
        if item["end"] is None:
            item["end"] = (
                ctx.current_index - 1 if _last_ends_nl(ctx) else ctx.current_index
            )
        if not _last_ends_nl(ctx):
            _insert(ctx, "\n")
    elif t == "blockquote_open":
        ctx.para_start = ctx.current_index
    elif t == "blockquote_close":
        if ctx.para_start is not None:
            ctx.para_ranges.append((ctx.para_start, ctx.current_index, "_QUOTE"))
        ctx.para_start = None


def _finalize(ctx: _Ctx) -> None:
    for s, e, style in ctx.text_ranges:
        ts: dict[str, Any] = {}
        fields: list[str] = []
        if style.get("bold"):
            ts["bold"] = True
            fields.append("bold")
        if style.get("italic"):
            ts["italic"] = True
            fields.append("italic")
        if style.get("strikethrough"):
            ts["strikethrough"] = True
            fields.append("strikethrough")
        if style.get("code"):
            ts["weightedFontFamily"] = {"fontFamily": CODE_FONT}
            ts["backgroundColor"] = {"color": {"rgbColor": CODE_BG_RGB}}
            fields += ["weightedFontFamily", "backgroundColor"]
        if style.get("link"):
            ts["link"] = {"url": style["link"]}
            fields.append("link")
        if ts:
            ctx.formats.append(
                {
                    "updateTextStyle": {
                        "range": _range(ctx, s, e),
                        "textStyle": ts,
                        "fields": ",".join(fields),
                    }
                }
            )

    for s, e, style in ctx.para_ranges:
        if style == "_QUOTE":
            ctx.formats.append(
                {
                    "updateParagraphStyle": {
                        "range": _range(ctx, s, e),
                        "paragraphStyle": {
                            "indentStart": {"magnitude": 36, "unit": "PT"}
                        },
                        "fields": "indentStart",
                    }
                }
            )
        else:
            ctx.formats.append(
                {
                    "updateParagraphStyle": {
                        "range": _range(ctx, s, e),
                        "paragraphStyle": {"namedStyleType": style},
                        "fields": "namedStyleType",
                    }
                }
            )

    items = sorted(
        [i for i in ctx.list_items if i["end"] and i["end"] > i["start"]],
        key=lambda i: i["start"],
    )
    merged: list[dict] = []
    for it in items:
        if (
            merged
            and merged[-1]["preset"] == it["preset"]
            and it["start"] <= merged[-1]["end"] + 1
        ):
            merged[-1]["end"] = max(merged[-1]["end"], it["end"])
        else:
            merged.append(dict(it))
    # Apply bottom-up: createParagraphBullets consumes leading tabs and shifts
    # indices below the range.
    for m in sorted(merged, key=lambda x: -x["start"]):
        ctx.formats.append(
            {
                "createParagraphBullets": {
                    "range": _range(ctx, m["start"], m["end"]),
                    "bulletPreset": m["preset"],
                }
            }
        )


def render_content_to_requests(
    content: str, tab_id: str, starting_index: int = 1
) -> list[dict]:
    """Convert markdown to Google Docs batchUpdate requests for one tab.

    Inserts run sequentially from ``starting_index`` (default 1, the
    start of an empty body); styling runs after. Coverage:
    H1–H6, ``**bold**``, ``*italic*``, ``~~strike~~``, ``\\`inline code\\```,
    fenced code blocks, links (including bare URLs via linkify),
    bulleted/numbered lists (nested), blockquotes, soft/hard breaks.
    Tables and images are deferred. Use ``starting_index > 1`` when
    appending to an existing tab body — pass the body's current
    end index minus 1 to insert before the trailing newline.
    """
    if not content or not content.strip():
        return []
    ctx = _Ctx(tab_id=tab_id, current_index=starting_index)
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})
    md.enable("strikethrough")
    for tok in md.parse(content):
        _process(tok, ctx)
    _finalize(ctx)
    return ctx.inserts + ctx.formats
