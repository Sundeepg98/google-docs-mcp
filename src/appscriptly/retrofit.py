"""Inject Heading 1 markers into a styled .docx without rebuilding it.

Use case: pre-existing approved deliverables (e.g. styled curriculum
docs) where section boundaries are visual table-banners rather than
Heading 1 paragraphs. The converter splits on Heading 1, so these
docs can't be tabbed as-is. Rebuilding from scratch reliably loses
banners, formatting, and embedded artifacts.

This module:
  1. Opens the .docx (which is a ZIP of XML) via python-docx
  2. Finds each ``marker_text`` in the body (paragraphs OR tables) —
     match is Unicode-normalized (NFKC) + whitespace-collapsed +
     case-insensitive by default. Works across fragmented <w:r>
     run boundaries, <w:sym> chars (e.g. NBSP), <w:tab>, <w:br>.
  3. Inserts a synthetic Heading 1 paragraph with ``tab_title`` BEFORE
     the containing block element (paragraph or table)
  4. Saves the modified .docx and hands it to
     ``convert_docx_to_tabbed_doc`` as a normal conversion

No formatting is removed; we only ADD heading paragraphs. The original
visual structure (table banners, colored borders, embedded images) is
preserved exactly.
"""
from __future__ import annotations

import io
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from google.oauth2.credentials import Credentials

from .docx_import import (
    PlaceholderBehavior,
    convert_docx_to_tabbed_doc,
)
from .services.drive.api import DOCX_MIME, GDOC_MIME
from appscriptly.google_clients import get_service

W_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class MarkerSpec(dict):
    """A retrofit marker: where to find it and what to title the new tab."""
    marker_text: str
    tab_title: str


def retrofit_existing_docx(
    creds: Credentials,
    markers: list[dict],
    docx_path: Path | None = None,
    drive_file_id: str | None = None,
    title: str | None = None,
    icons_by_title: dict[str, str] | None = None,
    placeholder_behavior: PlaceholderBehavior = "delete",
    placeholder_title: str = "Overview",
    placeholder_icon: str = "\U0001f4d1",
    replace_doc_id: str | None = None,
    case_sensitive: bool = False,
) -> dict:
    """Inject heading markers into a styled .docx, then convert to tabs.

    Args:
        markers: List of ``{"marker_text": str, "tab_title": str}`` in
            document order. Match is Unicode-normalized (NFKC) +
            whitespace-collapsed + case-insensitive by default. Works
            across fragmented <w:r> run boundaries (Word frequently
            splits a single visible phrase across multiple runs for
            spell-check, language tags, or rPr changes).
        case_sensitive: Set True to make marker_text matching case-
            sensitive. Default False — Word's autocorrect often
            changes case (e.g. "section x" -> "Section X").
        docx_path / drive_file_id: Source document. Exactly one.
        Other params: pass-through to ``convert_docx_to_tabbed_doc``.

    Returns:
        Same shape as ``convert_docx_to_tabbed_doc``, plus
        ``"retrofit": {"markers_matched": int, "markers_missed":
        [{"marker_text": str, "candidate_blocks": [first 100 chars
        of each block's normalized visible text]}, ...]}``. The
        candidate_blocks list is included so callers can grep for
        near-misses when a marker doesn't match.
    """
    if (docx_path is None) == (drive_file_id is None):
        raise ValueError("Provide exactly one of docx_path or drive_file_id.")
    if not markers:
        raise ValueError("markers cannot be empty")
    for i, m in enumerate(markers):
        if not isinstance(m, dict) or "marker_text" not in m or "tab_title" not in m:
            raise ValueError(
                f"markers[{i}] must be {{'marker_text': str, 'tab_title': str}}"
            )

    # 1. Load the .docx bytes.
    if docx_path is not None:
        if not docx_path.exists():
            raise FileNotFoundError(
                f"docx_path not found: {docx_path}. The 'docx_path' "
                "parameter only works when the MCP server can see the file "
                "on its own filesystem (local stdio MCP — Claude Code / "
                "Claude Desktop). From claude.ai cloud chat the remote "
                "server can't see your sandbox. Upload the .docx to Drive "
                "first (drive.files.create) and pass its id as drive_file_id."
            )
        with open(docx_path, "rb") as f:
            src_bytes = f.read()
    else:
        src_bytes = _fetch_drive_as_docx_bytes(creds, drive_file_id)  # type: ignore[arg-type]

    # 2. Open with python-docx and inject headings.
    doc = Document(io.BytesIO(src_bytes))
    matched, missed_specs = _inject_headings(
        doc, markers, case_sensitive=case_sensitive
    )

    if not matched:
        return {
            "doc_id": None,
            "url": None,
            "retrofit": {
                "markers_matched": 0,
                "markers_missed": missed_specs,
            },
            "error": (
                "None of the marker_text values matched any block in the "
                "document. Check the candidate_blocks list under "
                "retrofit.markers_missed for the actual visible text of "
                "each body block (Unicode-normalized + whitespace-"
                "collapsed). Pick a distinctive substring from one of "
                "those and retry."
            ),
        }

    # 3. Save the modified .docx to a temp file and convert normally.
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        doc.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        result = convert_docx_to_tabbed_doc(
            creds,
            docx_path=tmp_path,
            split_by="heading_1",
            title=title,
            icons_by_title=icons_by_title,
            placeholder_behavior=placeholder_behavior,
            placeholder_title=placeholder_title,
            placeholder_icon=placeholder_icon,
            replace_doc_id=replace_doc_id,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    result["retrofit"] = {
        "markers_matched": matched,
        "markers_missed": missed_specs,
    }
    return result


def _inject_headings(
    doc: Any, markers: list[dict], *, case_sensitive: bool = False
) -> tuple[int, list[dict]]:
    """Walk doc body in order; insert Heading 1 before each marker's block.

    Returns ``(matched_count, missed_specs)`` where ``missed_specs`` is
    a list of ``{"marker_text", "candidate_blocks": [first 100 chars of
    each body block's normalized visible text]}`` to aid debugging.
    """
    body = doc.element.body

    # Pre-extract all block texts once — avoids re-walking the tree per
    # marker. Order preserved so earlier matches don't claim later blocks.
    blocks: list[tuple[Any, str]] = []
    for child in body.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl")):
            blocks.append((child, _extract_visible_text(child)))

    candidate_preview = [text[:100] for _, text in blocks if text]

    matched = 0
    missed_specs: list[dict] = []
    used_block_ids: set[int] = set()

    for spec in markers:
        marker_text: str = spec["marker_text"]
        tab_title: str = spec["tab_title"]
        needle = _normalize(marker_text)
        if not case_sensitive:
            needle = needle.lower()

        target_block: Any = None
        for block, text in blocks:
            if id(block) in used_block_ids:
                continue
            hay = text if case_sensitive else text.lower()
            if needle in hay:
                target_block = block
                break

        if target_block is None:
            missed_specs.append(
                {"marker_text": marker_text, "candidate_blocks": candidate_preview}
            )
            continue

        target_block.addprevious(_build_heading_paragraph(tab_title))
        used_block_ids.add(id(target_block))
        matched += 1

    return matched, missed_specs


def _extract_visible_text(element: Any) -> str:
    """Concatenate ALL visible-text-producing OOXML children of ``element``,
    then NFKC-normalize and collapse whitespace.

    Handles:
      <w:t>     — regular text runs
      <w:tab/>  — tab characters
      <w:br/>   — line breaks
      <w:sym/>  — symbol chars (e.g. NBSP, en-dash) via w:char hex
      <w:cr/>   — carriage return

    Why this matters: Word fragments a visually-contiguous phrase
    across multiple <w:r> runs for spell-check tags, language tags,
    rPr changes, or autocorrect substitutions. Joining only <w:t>
    nodes works for plain text but misses anything inside w:sym
    (commonly used for non-breaking spaces). NFKC also folds smart
    quotes/em-dashes to ASCII so caller marker_text doesn't have to
    match Word's autocorrected punctuation.
    """
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
        elif tag == qn("w:sym"):
            char_hex = node.get(qn("w:char"))
            if char_hex:
                try:
                    parts.append(chr(int(char_hex, 16)))
                except (ValueError, OverflowError):
                    pass
    return _normalize("".join(parts))


def _normalize(s: str) -> str:
    """NFKC + typographic fold + whitespace-collapse.

    Steps:
      1. NFKC: width/compat folding (e.g. ligatures, NBSP -> space).
      2. Typographic fold: Word autocorrects ASCII punctuation into
         typographic equivalents (smart quotes, em/en-dashes, ellipsis).
         Fold those back to ASCII so the caller's marker_text doesn't
         have to know whether Word autocorrected the doc.
      3. Whitespace-collapse: any run of whitespace -> single space;
         strip leading/trailing.
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_TYPOGRAPHIC_FOLD)
    return " ".join(s.split())


# Word autocorrect substitutions -> ASCII originals. Folding these
# means a caller typing plain quotes/dashes matches a doc where Word
# typographically prettified them.
_TYPOGRAPHIC_FOLD = str.maketrans({
    "‘": "'",   # left single quote
    "’": "'",   # right single quote / apostrophe
    "‚": "'",   # single low-9 quote
    "‛": "'",   # single high-reversed-9 quote
    "“": '"',   # left double quote
    "”": '"',   # right double quote
    "„": '"',   # double low-9 quote
    "‟": '"',   # double high-reversed-9 quote
    "–": "-",   # en dash
    "—": "-",   # em dash
    "―": "-",   # horizontal bar
    "−": "-",   # minus sign
    "…": "...", # horizontal ellipsis -> 3 ASCII dots
    "·": ".",   # middle dot
    "•": ".",   # bullet
})


def _build_heading_paragraph(title: str) -> Any:
    """Construct a w:p element styled as Heading 1 with the given text."""
    p = OxmlElement("w:p")

    pPr = OxmlElement("w:pPr")
    pStyle = OxmlElement("w:pStyle")
    pStyle.set(qn("w:val"), "Heading1")
    pPr.append(pStyle)
    p.append(pPr)

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = title
    # Preserve any leading/trailing whitespace literally.
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    p.append(r)

    return p


def _fetch_drive_as_docx_bytes(creds: Credentials, drive_file_id: str) -> bytes:
    """Pull a Drive file as .docx bytes (works for .docx and Google Doc)."""
    drive = get_service("drive", "v3", credentials=creds)
    meta = drive.files().get(fileId=drive_file_id, fields="mimeType").execute()
    mime = meta.get("mimeType")
    if mime == DOCX_MIME:
        return drive.files().get_media(fileId=drive_file_id).execute()
    if mime == GDOC_MIME:
        return drive.files().export(
            fileId=drive_file_id, mimeType=DOCX_MIME
        ).execute()
    raise ValueError(
        f"Drive file {drive_file_id!r} has mimeType {mime!r}. "
        "Expected .docx or Google Doc."
    )
