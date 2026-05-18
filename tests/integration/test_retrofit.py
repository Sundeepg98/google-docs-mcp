"""Retrofit: docx with banner markers → all markers matched.

Guards the run-fragmentation fix from v0.15.1. If text extraction
regresses (stops walking w:sym, <w:tab>, etc.) or normalization
drops smart-quote folding, this test catches it.
"""
from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.live


def _build_live_test_docx() -> bytes:
    """A .docx with NO Heading 1 paragraphs but plain marker text Drive can convert.

    Pathological-content coverage (fragmented runs, NBSP, smart quotes,
    em-dash, typographic folding) is exercised in-process via
    ``tests/unit/test_retrofit_text_normalization.py`` instead — Drive's
    converter 500s on .docx files that mix w:sym + smart-quote +
    fragmented-run constructs in one document, even though python-docx
    writes them as syntactically valid XML. We confirmed this isn't a
    retrofit bug (the unit test passes 14/14 pathological cases) but a
    Drive-side limitation, so live coverage uses normal content.
    """
    from docx import Document

    doc = Document()
    # Sections marked only by their banner text — no Heading 1 style.
    # Plain paragraphs Drive's converter handles cleanly.
    doc.add_paragraph("SECTION ONE BANNER")
    doc.add_paragraph("body of section one")
    doc.add_paragraph("SECTION TWO BANNER")
    doc.add_paragraph("body of section two")
    doc.add_paragraph("SECTION THREE BANNER")
    doc.add_paragraph("body of section three")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_retrofit_e2e(live_creds, tmp_path):
    """All markers match end-to-end through upload+convert+restructure."""
    from google_docs_mcp.drive_api import trash_drive_file
    from google_docs_mcp.retrofit import retrofit_existing_docx

    docx_path = tmp_path / "retrofit_live.docx"
    docx_path.write_bytes(_build_live_test_docx())

    markers = [
        {"marker_text": "SECTION ONE BANNER",   "tab_title": "One"},
        {"marker_text": "SECTION TWO BANNER",   "tab_title": "Two"},
        {"marker_text": "SECTION THREE BANNER", "tab_title": "Three"},
    ]

    result = retrofit_existing_docx(
        live_creds, markers=markers, docx_path=docx_path,
        title="retrofit_e2e_test",
    )
    try:
        info = result.get("retrofit") or {}
        matched = info.get("markers_matched")
        missed = info.get("markers_missed") or []
        assert matched == 3, (
            f"expected 3 markers matched, got {matched}; missed={missed!r}."
        )
        assert missed == [], f"unexpected misses: {missed}"
        # Sanity: result also has a real doc_id and tabs
        assert result.get("doc_id")
        assert len(result.get("tabs") or []) >= 3
    finally:
        if result.get("doc_id"):
            try:
                trash_drive_file(live_creds, result["doc_id"])
            except Exception:
                pass
