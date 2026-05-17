"""Inject Heading 1 markers into a styled .docx without rebuilding it.

Use case: pre-existing approved deliverables (e.g. styled curriculum
docs) where section boundaries are visual table-banners rather than
Heading 1 paragraphs. The converter splits on Heading 1, so these
docs can't be tabbed as-is. Rebuilding from scratch reliably loses
banners, formatting, and embedded artifacts.

This module:
  1. Opens the .docx (which is a ZIP of XML) via python-docx
  2. Finds each ``marker_text`` in the body (paragraphs OR tables)
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
from .drive_api import DOCX_MIME, GDOC_MIME
from googleapiclient.discovery import build

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
) -> dict:
    """Inject heading markers into a styled .docx, then convert to tabs.

    Args:
        markers: List of ``{"marker_text": str, "tab_title": str}`` —
            in document order. Each marker_text is matched (case-
            sensitive substring) against the visible text of each
            body-level block. The first matching block gets a Heading
            1 paragraph with ``tab_title`` inserted BEFORE it.
        docx_path / drive_file_id: Source document. Exactly one.
        Other params: pass-through to ``convert_docx_to_tabbed_doc``.

    Returns:
        Same shape as ``convert_docx_to_tabbed_doc``, plus
        ``"retrofit": {"markers_matched", "markers_missed": [...]}``.
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
            raise FileNotFoundError(f"DOCX file not found: {docx_path}")
        with open(docx_path, "rb") as f:
            src_bytes = f.read()
    else:
        src_bytes = _fetch_drive_as_docx_bytes(creds, drive_file_id)  # type: ignore[arg-type]

    # 2. Open with python-docx and inject headings.
    doc = Document(io.BytesIO(src_bytes))
    matched, missed = _inject_headings(doc, markers)

    if not matched:
        return {
            "doc_id": None,
            "url": None,
            "retrofit": {
                "markers_matched": 0,
                "markers_missed": [m["marker_text"] for m in markers],
            },
            "error": (
                "None of the marker_text values matched any block in the "
                "document. Open the .docx, copy a short distinctive phrase "
                "from each section's banner, and retry."
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
        "markers_missed": missed,
    }
    return result


def _inject_headings(doc: Any, markers: list[dict]) -> tuple[int, list[str]]:
    """Walk doc body in order; insert Heading 1 before each marker's block.

    Returns (matched_count, missed_marker_texts).
    """
    body = doc.element.body
    matched = 0
    missed: list[str] = []

    # Track which blocks have already been "claimed" so two markers
    # don't both target the same block.
    used_block_ids: set[int] = set()

    for spec in markers:
        marker_text: str = spec["marker_text"]
        tab_title: str = spec["tab_title"]
        target_block = _find_block_with_text(body, marker_text, used_block_ids)
        if target_block is None:
            missed.append(marker_text)
            continue
        heading_para = _build_heading_paragraph(tab_title)
        target_block.addprevious(heading_para)
        used_block_ids.add(id(target_block))
        matched += 1

    return matched, missed


def _find_block_with_text(body: Any, needle: str, exclude_ids: set[int]) -> Any:
    """Return the first top-level paragraph or table whose visible text contains ``needle``."""
    for child in body.iterchildren():
        if id(child) in exclude_ids:
            continue
        tag = child.tag
        if tag == f"{{{W_NAMESPACE}}}p" or tag == f"{{{W_NAMESPACE}}}tbl":
            text = "".join(t.text or "" for t in child.iter(qn("w:t")))
            if needle in text:
                return child
    return None


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
    drive = build("drive", "v3", credentials=creds)
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
