"""Declared tool surface for the apps_script service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. Unlike single-file services, apps_script spreads its tools
across several feature files (the generic primitive in ``tools.py``;
each composing use-case tool in its own module) — but they all carry
``service="apps_script"`` and ALL register, so all are declared here
in one set. The per-tool → source-module mapping for the location
test lives separately in ``test_tool_registration.py``'s
``_APPS_SCRIPT_TOOL_MODULE`` (kept; orthogonal to discovery — it
catches wrong-file definition).
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    # PR-Δ7 — the generic bound-script generator primitive (tools.py).
    "as_generate_bound_script",
    # PR-Δ8 — doc-menu installer (doc_menu.py).
    "as_install_doc_menu",
    # PR-Δ10 — custom =FUNCTION() installer (custom_function.py).
    "as_install_custom_function",
    # PR-Δ9 — scheduled dashboard refresh (sheet_dashboard.py).
    "as_install_sheet_dashboard",
    # ROADMAP_SPECS #8 — reactive onEdit trigger (edit_trigger.py).
    "as_install_edit_trigger",
    # ROADMAP_SPECS #8 — reactive onFormSubmit handler (form_handler.py);
    # lifts the Forms hard-rejection for this one reactive surface.
    "as_install_form_handler",
    # PR-Δ11 — slides-to-video RENDER half (video_deck.py).
    "as_generate_video_deck",
    # PR-Δ12 — slides-to-video ENCODE half, server-side ffmpeg
    # compute (encode_video.py); the only apps_script tool that is
    # NOT a bound-script generator.
    "as_encode_video",
})
