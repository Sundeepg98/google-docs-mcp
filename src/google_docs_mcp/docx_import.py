"""Wave E: lossless .docx → native-tabbed Google Doc.

Pipeline:
  1. Drive uploads + converts .docx (preserves tables, shading, borders,
     images, equations — full Word fidelity, Drive's converter handles it)
  2. We identify split points by walking the converted doc's paragraph
     stream looking for the configured heading style
  3. REST API creates empty nested tab shells (addDocumentTab with
     parentTabId) — one per split point, deeply nested per ``children``
  4. We POST the split spec to the user's deployed Apps Script Web App,
     which uses ``Element.copy()`` + ``Body.appendXxx(copy)`` to move
     content from the primary tab into the new shells — the only path
     that preserves drawings, equations, tables, and cell shading
     because no REST request type can re-emit those losslessly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypedDict
from urllib import error as urlerror
from urllib import request as urlrequest

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import get_webapp_url
from .docs_api import (
    MAX_NESTING_DEPTH,
    TabSpec,
    add_tabs_to_doc,
)
from .drive_api import upload_and_convert_docx

SplitBy = Literal["heading_1", "heading_2", "page_break", "auto"]
_STYLE_FOR_SPLIT = {
    "heading_1": "HEADING_1",
    "heading_2": "HEADING_2",
}


class _SplitPoint(TypedDict):
    title: str
    icon_emoji: str | None
    # List of [startChild, endChild] index ranges into the source body
    # (inclusive on both ends). Multiple ranges support the future
    # "parent keeps gaps between children" semantics; flat detection
    # always produces a single contiguous range per split.
    ranges: list[tuple[int, int]]
    children: list["_SplitPoint"]


def convert_docx_to_tabbed_doc(
    creds: Credentials,
    docx_path: Path,
    split_by: SplitBy = "heading_1",
    title: str | None = None,
) -> dict:
    """Upload a .docx, convert, then restructure into nested tabs.

    Returns ``{"doc_id", "url", "tabs": [...], "split_strategy_used"}``.
    """
    webapp_url = get_webapp_url()
    if not webapp_url:
        raise RuntimeError(
            "Apps Script Web App URL not configured. "
            "Run `google-docs-mcp setup-apps-script` for setup instructions, "
            "then save the deployment URL with "
            "`google-docs-mcp configure-webapp <URL>`."
        )

    # 1. Drive upload + lossless conversion.
    converted = upload_and_convert_docx(creds, docx_path, title=title)
    doc_id = converted["doc_id"]

    # 2. Find split points in the converted doc's primary tab body.
    docs = build("docs", "v1", credentials=creds)
    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    body_content = fetched["tabs"][0]["documentTab"]["body"]["content"]

    splits, strategy_used = _detect_splits(body_content, split_by)
    if not splits:
        return {
            "doc_id": doc_id,
            "url": converted["url"],
            "tabs": [],
            "split_strategy_used": strategy_used,
            "note": (
                "No split points found; doc is left as a single-tab "
                f"conversion of {docx_path.name}."
            ),
        }

    # 3. Cap nesting depth defensively — _detect_splits won't currently
    # produce nested splits, but a future strategy might.
    max_depth = _max_depth(splits)
    if max_depth >= MAX_NESTING_DEPTH:
        raise RuntimeError(
            f"Detected {max_depth + 1} nesting levels; "
            f"Google Docs UI allows at most {MAX_NESTING_DEPTH}."
        )

    # 4. REST creates the empty tab shells under the primary tab.
    primary_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]
    shell_specs = [_split_to_tabspec(s) for s in splits]
    add_tabs_to_doc(
        creds, doc_id, shell_specs, parent_tab_id=primary_tab_id
    )

    # 5. Apps Script moves content into the shells with full fidelity.
    payload = {
        "docId": doc_id,
        "splitTree": _splits_to_json(splits),
    }
    response = _call_webapp(webapp_url, payload)

    if not response.get("success", False):
        raise RuntimeError(
            f"Apps Script restructure failed at stage "
            f"'{response.get('stage', 'unknown')}': "
            f"{response.get('error', 'no error message returned')}"
        )

    return {
        "doc_id": doc_id,
        "url": converted["url"],
        "tabs": response.get("tabs", []),
        "moved_children": response.get("movedChildren", 0),
        "warnings": response.get("warnings", []),
        "split_strategy_used": strategy_used,
    }


def _detect_splits(
    body_content: list[dict], split_by: SplitBy
) -> tuple[list[_SplitPoint], str]:
    """Walk the body and emit split points per strategy.

    ``auto`` tries ``heading_1`` → ``heading_2`` → ``page_break`` and
    returns the first non-empty result.
    """
    if split_by == "auto":
        for strategy in ("heading_1", "heading_2", "page_break"):
            splits, _ = _detect_splits(body_content, strategy)  # type: ignore[arg-type]
            if splits:
                return splits, strategy
        return [], "auto"

    # Filter to elements that DocumentApp.Body.getChild() exposes.
    # ``sectionBreak`` is a REST-only structural element (it describes the
    # section's page properties) and Apps Script does NOT count it as a
    # body child. If we leave it in, our indices run one ahead of what
    # Apps Script sees and the last range gets rejected as out-of-bounds.
    docapp_children = [
        elem for elem in body_content if "sectionBreak" not in elem
    ]

    splits: list[_SplitPoint] = []
    target_style = _STYLE_FOR_SPLIT.get(split_by)

    for child_idx, elem in enumerate(docapp_children):
        para = elem.get("paragraph")
        if para is None:
            if splits:
                lo, _hi = splits[-1]["ranges"][-1]
                splits[-1]["ranges"][-1] = (lo, child_idx)
            continue

        is_split = False
        title_text = ""

        if split_by in ("heading_1", "heading_2"):
            style = para.get("paragraphStyle", {})
            if style.get("namedStyleType") == target_style:
                is_split = True
                title_text = _extract_paragraph_text(para)
        elif split_by == "page_break":
            for pe in para.get("elements", []):
                if "pageBreak" in pe:
                    is_split = True
                    title_text = f"Page {len(splits) + 2}"
                    break

        if is_split:
            splits.append(
                _SplitPoint(
                    title=(title_text or f"Section {len(splits) + 1}")[:80].strip()
                    or f"Section {len(splits) + 1}",
                    icon_emoji=None,
                    ranges=[(child_idx, child_idx)],
                    children=[],
                )
            )
        elif splits:
            lo, _hi = splits[-1]["ranges"][-1]
            splits[-1]["ranges"][-1] = (lo, child_idx)

    return splits, split_by


def _extract_paragraph_text(para: dict) -> str:
    """Concatenate the visible text in a paragraph's element runs."""
    return "".join(
        pe.get("textRun", {}).get("content", "")
        for pe in para.get("elements", [])
    ).strip()


def _max_depth(splits: list[_SplitPoint]) -> int:
    if not splits:
        return -1
    return 1 + max(
        (_max_depth(s["children"]) for s in splits), default=-1
    )


def _split_to_tabspec(split: _SplitPoint) -> TabSpec:
    """Convert a split point into a TabSpec for tab-shell creation.

    Tab shells are created empty — content moves in via Apps Script.
    """
    spec: TabSpec = {"title": split["title"], "content": ""}
    if split["icon_emoji"]:
        spec["icon_emoji"] = split["icon_emoji"]
    if split["children"]:
        spec["children"] = [_split_to_tabspec(c) for c in split["children"]]
    return spec


def _splits_to_json(splits: list[_SplitPoint]) -> list[dict]:
    """Serialize splits for the Apps Script payload — JSON-friendly keys."""
    return [
        {
            "title": s["title"],
            "iconEmoji": s["icon_emoji"] or "",
            "ranges": [[lo, hi] for lo, hi in s["ranges"]],
            "children": _splits_to_json(s["children"]),
        }
        for s in splits
    ]


def _call_webapp(url: str, payload: dict) -> dict:
    """POST JSON to the Apps Script Web App and parse the JSON reply."""
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Apps Script Web App returned HTTP {e.code}: {body[:500]}"
        ) from e
    except urlerror.URLError as e:
        raise RuntimeError(
            f"Could not reach Apps Script Web App at {url}: {e.reason}"
        ) from e

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "Apps Script Web App did not return JSON. "
            "Common cause: the script throws an uncaught error before "
            "the JSON response is built, so Google returns an HTML "
            "error page. First 500 chars of response:\n"
            f"{body[:500]}"
        ) from e
