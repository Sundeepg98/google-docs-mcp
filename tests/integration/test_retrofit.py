"""Retrofit: docx with banner markers → all markers matched.

Guards the run-fragmentation fix from v0.15.1. If text extraction
regresses (stops walking w:sym, <w:tab>, etc.) or normalization
drops smart-quote folding, this test catches it.
"""
from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.live


def _build_pathological_docx() -> bytes:
    """Build a .docx with no Heading 1s and pathological run boundaries."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def add_fragmented(doc, parts):
        p = OxmlElement("w:p")
        for part in parts:
            r = OxmlElement("w:r")
            if part == "NBSP":
                sym = OxmlElement("w:sym")
                sym.set(qn("w:char"), "00A0")
                r.append(sym)
            else:
                t = OxmlElement("w:t")
                t.text = part
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                r.append(t)
            p.append(r)
        doc.element.body.insert(-1, p)

    doc = Document()
    # 4 sections with progressively nastier fragmentation.
    add_fragmented(doc, ["Section", " ", "One"])              # plain split
    doc.add_paragraph("body of section one")
    add_fragmented(doc, ["Sec", "tion", "NBSP", "Two"])       # NBSP via w:sym
    doc.add_paragraph("body two")
    add_fragmented(doc, ["Smart “Quotes” Three"])             # smart quotes
    doc.add_paragraph("body three")
    add_fragmented(doc, ["Em—dash Section Four"])             # em-dash
    doc.add_paragraph("body four")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_retrofit_matches_all_pathological_markers(live_creds, tmp_path):
    """All 4 markers in the test doc must match in a single retrofit call."""
    from google_docs_mcp.drive_api import trash_drive_file
    from google_docs_mcp.retrofit import retrofit_existing_docx

    docx_bytes = _build_pathological_docx()
    docx_path = tmp_path / "pathological.docx"
    docx_path.write_bytes(docx_bytes)

    # Caller types plain ASCII for the smart-quote / em-dash sections;
    # the normalization layer should reconcile them.
    markers = [
        {"marker_text": "Section One", "tab_title": "One"},
        {"marker_text": "Section Two", "tab_title": "Two"},
        {"marker_text": 'Smart "Quotes" Three', "tab_title": "Three"},
        {"marker_text": "Em-dash Section Four", "tab_title": "Four"},
    ]

    result = retrofit_existing_docx(
        live_creds, markers=markers, docx_path=docx_path,
        title="retrofit_regression_test",
    )
    try:
        info = result.get("retrofit") or {}
        matched = info.get("markers_matched")
        missed = info.get("markers_missed") or []
        assert matched == 4, (
            f"expected all 4 markers matched, got {matched}; missed={missed!r}. "
            "Likely run-fragmentation or typographic-fold regression."
        )
        assert missed == [], f"unexpected misses: {missed}"
    finally:
        if result.get("doc_id"):
            try:
                trash_drive_file(live_creds, result["doc_id"])
            except Exception:
                pass
