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
    # CASA-free growth — execution-history read (processes.py). Read-only
    # observability over script.processes (SENSITIVE, no CASA); companion
    # to the create+deploy levers.
    "as_list_script_processes",
    # Stream 3 — activation verification (check_activation.py). Read-only:
    # answers "is this deployed automation live yet?" via a web-app probe
    # or an execution-history read. Same script.processes scope.
    "as_check_activation",
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
    # GAS service-parity — Sheets custom menu (sheet_menu.py); the Sheets
    # analogue of as_install_doc_menu (SpreadsheetApp.getUi()).
    "as_install_sheet_menu",
    # GAS service-parity — Slides custom menu (slides_menu.py); the Slides
    # analogue of as_install_doc_menu (SlidesApp.getUi()).
    "as_install_slides_menu",
    # GAS service-parity — refresh linked slides (refresh_linked_slides.py);
    # getSlides()→refreshSlide() master-deck→client-deck sync REST can't do.
    "as_refresh_linked_slides",
    # GAS service-parity — push computed grades onto quiz responses
    # (grade_form_responses.py); FormApp.submitGrades(), full forms scope in
    # the GENERATED manifest only (not appscriptly's own consent).
    "as_grade_form_responses",
    # PR-Δ11 — slides-to-video RENDER half (video_deck.py).
    "as_generate_video_deck",
    # PR-Δ12 — slides-to-video ENCODE half, server-side ffmpeg
    # compute (encode_video.py); the only apps_script tool that is
    # NOT a bound-script generator.
    "as_encode_video",
    # GAS service-parity — Calendar: time-driven Sheet→Calendar event
    # sync (calendar_sync.py); CalendarApp on a time trigger, full
    # calendar scope in the GENERATED manifest only (not appscriptly's
    # own consent).
    "as_install_calendar_sync",
    # GAS service-parity — Tasks: time-driven Tasks orchestration
    # (task_rollover.py) via the Tasks ADVANCED service; full tasks scope
    # + the advanced-service dependency in the GENERATED manifest only.
    "as_install_task_rollover",
    # GAS service-parity — Contacts: reactive onFormSubmit contact
    # create/sync (contact_sync.py); ContactsApp, full contacts scope in
    # the GENERATED manifest only; binds directly to a Form (lifts the
    # Forms rejection, same as form_handler).
    "as_install_contact_sync",
})
