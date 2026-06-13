"""Markdown → Google Docs ``batchUpdate`` renderer (v2.2.1 — R14 #8 split).

Walks the ``markdown-it-py`` token stream to emit ``insertText``
requests forward (with index tracking) and collects style ranges;
formatting requests run after inserts so indices stay stable.
Adapted from the pattern used in ``a-bonus/google-docs-mcp`` (MIT,
TypeScript) — re-implemented in Python, not vendored.

Pure functions — no Google SDK imports. Every function in this module
returns a ``list[dict]`` of batch-update request payloads. ``api.py``
holds the Google API call sites that actually POST those payloads.

Extracted from ``services/docs/api.py`` as part of the R14 #8 split
that closes audit Gap #1. See ``tab_tree.py`` module docstring for
the full split rationale + the R6 UTF-16 unblock context.

**R6 UTF-16 bug** (FIXED): the ``_insert`` helper advances the index
by UTF-16 code-unit count, not Python code points, because Google Docs
measures positions in UTF-16 code units. A character above the BMP
(e.g. ``"𝐀"`` U+1D400) counts as 1 in Python but 2 in UTF-16; using
``len(text)`` left every subsequent index off-by-one. The fix
(``len(text.encode("utf-16-le")) // 2``) lives in ``_insert`` below;
its regression test lives in ``test_markdown_render.py``. The
isolation of this module is what made that fix independently testable.

**Request-payload helpers** also live here: ``_tab_properties``,
``_rename_tab_request``, ``_add_tab_request``, ``_plain_text_requests``.
They're paired with the markdown renderer because they all produce
the same ``list[dict]`` shape that ``api.py`` consumes via
``docs.documents().batchUpdate(body={"requests": [...]})``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# typing_extensions for backward compat: NotRequired was added to typing
# in 3.11, and pydantic requires typing_extensions.TypedDict (not
# typing.TypedDict) for proper schema generation on Python < 3.12.
from typing_extensions import NotRequired, TypedDict

from markdown_it import MarkdownIt
from markdown_it.token import Token

CODE_FONT = "Roboto Mono"
CODE_BG_RGB = {"red": 0.945, "green": 0.957, "blue": 0.965}  # #F1F3F4


class TabSpec(TypedDict):
    title: str
    content: str
    icon_emoji: NotRequired[str | None]
    content_format: NotRequired[Literal["markdown", "text"]]
    children: NotRequired[list["TabSpec"]]


# ---------------------------------------------------------------------
# Tab-management request payload builders
# ---------------------------------------------------------------------


def _tab_properties(tab: TabSpec, *, include_title: bool = True) -> dict:
    """Build the ``tabProperties`` dict for a TabSpec.

    Pure dict construction. ``include_title=False`` is for cases where
    the caller is patching properties on an existing tab and ``title``
    would be a no-op overwrite (the Google API tolerates it, but
    sending the field forces an unnecessary round-trip on the field
    mask).
    """
    props: dict[str, Any] = {}
    if include_title:
        props["title"] = tab["title"]
    icon = tab.get("icon_emoji")
    if icon:
        props["iconEmoji"] = icon
    return props


def _rename_tab_request(tab_id: str, tab: TabSpec) -> dict:
    """Build a ``updateDocumentTabProperties`` request for one tab.

    Field mask is computed from the keys actually present in
    ``_tab_properties``, so unset fields don't appear in the mask
    (preventing accidental clears).
    """
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
    """Build an ``addDocumentTab`` request.

    Omits ``parentTabId`` when ``parent_tab_id is None`` so the new
    tab lands at the root level. With a parent, the new tab becomes
    that parent's last child.
    """
    props = _tab_properties(tab)
    if parent_tab_id:
        props["parentTabId"] = parent_tab_id
    return {"addDocumentTab": {"tabProperties": props}}


def _plain_text_requests(content: str, tab_id: str) -> list[dict]:
    """Build a single ``insertText`` request at index 1 of a tab.

    Used by ``content_format="text"`` callers that want to skip
    markdown rendering entirely. Empty content returns an empty list
    (no-op request — batchUpdate rejects empty insertText).
    """
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


# ---------------------------------------------------------------------
# Markdown renderer — state machine
# ---------------------------------------------------------------------


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
    # R6 (FIXED — see module docstring): Google Docs addresses positions
    # in UTF-16 code units, not Python code points. Above-BMP characters
    # (emoji, math-alphanumerics) are surrogate pairs = 1 code point but 2
    # UTF-16 units, so len(text) would drift every later index. Counting
    # utf-16-le bytes // 2 gives the exact UTF-16 unit count.
    ctx.current_index += len(text.encode("utf-16-le")) // 2


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
        # End is current_index - 1 (excludes the trailing "\n" just
        # inserted) rather than s + len(body): _insert advances by UTF-16
        # units, so above-BMP body content needs the same unit basis here.
        ctx.text_ranges.append((s, ctx.current_index - 1, {"code": True}))
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


# ---------------------------------------------------------------------
# Markdown-table parsing (pure helper for gdocs_insert_markdown_table)
# ---------------------------------------------------------------------


def _split_table_row(line: str) -> list[str]:
    """Split one GFM table row into cell strings.

    Handles escaped pipes (``\\|`` → literal ``|`` inside a cell) and
    strips the optional leading/trailing ``|`` plus per-cell
    whitespace. A line like ``| a | b\\|c | `` → ``["a", "b|c"]``.
    """
    # Temporarily protect escaped pipes, split on bare pipes, restore.
    protected = line.replace("\\|", "\x00")
    # Drop one leading and one trailing unescaped pipe if present.
    stripped = protected.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [c.replace("\x00", "|").strip() for c in stripped.split("|")]
    return cells


def _is_table_separator(line: str) -> bool:
    """True if ``line`` is a GFM header/body separator (``|---|:--:|``).

    Each cell between pipes must be hyphens with optional leading/
    trailing colons (alignment markers) and surrounding whitespace.
    """
    cells = _split_table_row(line)
    if not cells:
        return False
    for cell in cells:
        c = cell.strip()
        if not c:
            return False
        if not re.fullmatch(r":?-+:?", c):
            return False
    return True


def parse_markdown_table(markdown: str) -> dict[str, Any]:
    """Parse a GFM markdown table into ``{rows, columns, cells}``.

    ``cells`` is a list of rows, each a list of cell strings, INCLUDING
    the header row (so ``rows`` counts header + body). Every row is
    padded / truncated to ``columns`` (= the header's cell count) so the
    grid is rectangular — Docs tables are strictly rectangular.

    Args:
        markdown: A markdown table — a header row, a separator row
            (``|---|---|``), then zero or more body rows. Leading/
            trailing blank lines are ignored.

    Returns:
        ``{"rows": int, "columns": int, "cells": list[list[str]]}``.

    Raises:
        ValueError: no recognizable table (missing separator, fewer
            than the header+separator lines, or zero columns).
    """
    lines = [ln for ln in markdown.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(
            "not a markdown table — need at least a header row and a "
            "'|---|---|' separator row."
        )
    if not _is_table_separator(lines[1]):
        raise ValueError(
            "second non-blank line must be a separator row like "
            "'|---|---|' (hyphens, optional alignment colons)."
        )
    header = _split_table_row(lines[0])
    columns = len(header)
    if columns == 0:
        raise ValueError("table header has zero columns.")

    body = [_split_table_row(ln) for ln in lines[2:]]
    grid: list[list[str]] = [header]
    for row in body:
        # Pad short rows, truncate long ones → rectangular grid.
        normalized = (row + [""] * columns)[:columns]
        grid.append(normalized)
    return {"rows": len(grid), "columns": columns, "cells": grid}
