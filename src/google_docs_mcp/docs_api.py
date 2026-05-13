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
from typing import Any, Literal, NotRequired, TypedDict

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


def render_content_to_requests(content: str, tab_id: str) -> list[dict]:
    """Convert markdown to Google Docs batchUpdate requests for one tab.

    Inserts run sequentially from index 1; styling runs after. Coverage:
    H1–H6, ``**bold**``, ``*italic*``, ``~~strike~~``, ``\\`inline code\\```,
    fenced code blocks, links (including bare URLs via linkify),
    bulleted/numbered lists (nested), blockquotes, soft/hard breaks.
    Tables and images are deferred.
    """
    if not content or not content.strip():
        return []
    ctx = _Ctx(tab_id=tab_id)
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})
    md.enable("strikethrough")
    for tok in md.parse(content):
        _process(tok, ctx)
    _finalize(ctx)
    return ctx.inserts + ctx.formats
