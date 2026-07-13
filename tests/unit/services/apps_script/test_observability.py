"""Unit tests for generated-code failure observability (gap #5).

Covers the shared ``_observability`` helpers AND asserts the mechanism (a
try / report-by-mail / rethrow wrapper) is actually present in every class's
GENERATED ``.gs`` output. The wrapper must never swallow the error (it
rethrows) and the reporter must use MailApp with the send-only, non-restricted
``script.send_mail`` scope. The generated output is also kept parse-valid
(a plain, syntactically simple wrapper) so Stream 1's static-parse harness
passes; here we assert structure with substring checks.
"""
from __future__ import annotations

import pytest

from appscriptly.services.apps_script import (
    doc_menu,
    edit_trigger,
    form_handler,
    grade_form_responses,
    refresh_linked_slides,
    sheet_dashboard,
    sheet_menu,
    slides_menu,
    video_deck,
)
from appscriptly.services.apps_script import _observability as obs
from appscriptly.services.apps_script.api import is_restricted_scope
from appscriptly.services.gas_deploy.api import (
    inject_error_reporting,
    inject_webapp_hmac_guard,
)

_EM_DASH = "—"
_EN_DASH = "–"

# Representative inputs for each generator that runs a caller-authored body.
_MENU_ITEMS = [
    {
        "label": "Insert block",
        "function_name": "insertBlock",
        "function_body": "DocumentApp.getActiveDocument().getBody().appendParagraph('hi');",
    },
    # An empty body is legal (a no-op handler) — it must still wrap cleanly.
    {"label": "No op", "function_name": "noop", "function_body": ""},
]


def _all_generated_scripts() -> dict[str, str]:
    """Every generated ``.gs`` that carries a caller-run handler (the classes
    this pass wraps). Keyed by a human label for clear failure messages."""
    dash, _ = sheet_dashboard.build_dashboard_script_body(
        "function refreshDashboard() { rebuild(); }", "daily", 6
    )
    edit, _ = edit_trigger.build_edit_trigger_script_body(
        "function onSheetEdit(e) { stamp(e.range); }", "SHEET_1"
    )
    form, _ = form_handler.build_form_handler_script_body(
        "function onSubmit(e) { route(e.response); }", "FORM_1"
    )
    grade, _ = grade_form_responses.build_grade_script_body(
        "function scoreItem(ir, it) { ir.setScore(1); }", "Quiz"
    )
    return {
        "doc_menu": doc_menu.build_menu_script("Tools", _MENU_ITEMS),
        "sheet_menu": sheet_menu.build_menu_script("Tools", _MENU_ITEMS),
        "slides_menu": slides_menu.build_menu_script("Tools", _MENU_ITEMS),
        "sheet_dashboard": dash,
        "edit_trigger": edit,
        "form_handler": form,
        "grade": grade,
        "refresh": refresh_linked_slides.build_refresh_script_body("Deck"),
        "video_deck": video_deck.build_video_deck_script(
            "PID_1", "https://srv/upload/frames/b1", "tok_1"
        ),
    }


# --------------------------------------------------------------------------
# The mail scope: send-only, NOT restricted (restricted-guard invariant)
# --------------------------------------------------------------------------


def test_mail_scope_is_the_send_only_scope():
    assert obs.MAIL_SCOPE == "https://www.googleapis.com/auth/script.send_mail"


def test_mail_scope_is_not_restricted():
    """The whole scope-neutrality story depends on this: script.send_mail is
    NOT a Google RESTRICTED scope, so it passes build_manifest's guard and
    keeps the generated automation no-CASA."""
    assert is_restricted_scope(obs.MAIL_SCOPE) is False


# --------------------------------------------------------------------------
# add_mail_scope
# --------------------------------------------------------------------------


def test_add_mail_scope_appends_when_absent():
    assert obs.add_mail_scope(["a", "b"]) == ["a", "b", obs.MAIL_SCOPE]


def test_add_mail_scope_none_yields_just_the_scope():
    assert obs.add_mail_scope(None) == [obs.MAIL_SCOPE]


def test_add_mail_scope_is_idempotent_and_order_stable():
    already = ["x", obs.MAIL_SCOPE, "y"]
    assert obs.add_mail_scope(already) == already  # no duplicate appended


# --------------------------------------------------------------------------
# reporter helper
# --------------------------------------------------------------------------


def test_reporter_function_name_matches_source():
    src = obs.reporter_helper_source()
    assert f"function {obs.REPORTER_FUNCTION}(" in src


def test_reporter_uses_mailapp_to_the_effective_user():
    src = obs.reporter_helper_source()
    assert "MailApp.sendEmail(" in src
    assert "Session.getEffectiveUser().getEmail()" in src


def test_reporter_email_carries_the_one_click_editor_url():
    """Rider (a): the failure email ends with a one-click editor link derived
    from ``ScriptApp.getScriptId()`` so the owner jumps straight to the
    project. Derived DEFENSIVELY (its own try) so a getScriptId() failure on a
    minimal manifest degrades to the generic footer rather than dropping the
    whole email (best-effort is preserved)."""
    src = obs.reporter_helper_source()
    assert "ScriptApp.getScriptId()" in src
    assert "https://script.google.com/d/" in src
    assert "/edit" in src
    # The URL derivation is guarded by its own catch so it can never abort the
    # send (a menu class has no scriptapp scope; if getScriptId() ever threw
    # there, the email must still go out).
    assert "catch (idErr)" in src


def test_reporter_is_best_effort_and_never_throws():
    """The reporter body is itself wrapped in try/catch so a reporting
    failure (no scope, quota) can never mask the original error."""
    src = obs.reporter_helper_source()
    # It swallows its OWN failure (the reporter must not rethrow) ...
    assert "catch (reportErr)" in src
    # ... and contains no throw STATEMENT (only the caller wrappers rethrow;
    # the word "rethrows" in the prose is fine, a `throw ` statement is not).
    assert "throw " not in src


# --------------------------------------------------------------------------
# the three wrapper shapes
# --------------------------------------------------------------------------


def test_guarded_function_block_wraps_reports_and_rethrows():
    block = obs.guarded_function_block("doThing", "doWork();")
    assert block.startswith("function doThing() {")
    assert "try {" in block
    assert "doWork();" in block
    assert f'{obs.REPORTER_FUNCTION}("doThing", __appscriptlyErr__);' in block
    assert "throw __appscriptlyErr__;" in block


def test_guarded_function_block_handles_empty_body():
    block = obs.guarded_function_block("noop", "")
    assert "function noop() {" in block
    assert "try {" in block
    assert "throw __appscriptlyErr__;" in block


def test_wrap_generated_body_wraps_reports_and_rethrows():
    wrapped = obs.wrap_generated_body("renderFrames", "  var x = 1;\n  return x;")
    assert "try {" in wrapped
    assert "return x;" in wrapped
    assert f'{obs.REPORTER_FUNCTION}("renderFrames", __appscriptlyErr__);' in wrapped
    assert "throw __appscriptlyErr__;" in wrapped


def test_guarded_delegator_delegates_to_caller_and_rethrows():
    src, name = obs.guarded_delegator("refreshDashboard")
    assert name == "__appscriptlyGuarded_refreshDashboard__"
    assert f"function {name}(e) {{" in src
    assert "return refreshDashboard(e);" in src
    assert f'{obs.REPORTER_FUNCTION}("refreshDashboard", __appscriptlyErr__);' in src
    assert "throw __appscriptlyErr__;" in src


def test_guarded_entry_point_keeps_exact_name_and_delegates():
    src = obs.guarded_entry_point("doPost", "__appscriptlyUserDoPost")
    assert src.startswith("function doPost(e) {")
    assert "return __appscriptlyUserDoPost(e);" in src
    assert f'{obs.REPORTER_FUNCTION}("doPost", __appscriptlyErr__);' in src
    assert "throw __appscriptlyErr__;" in src


def test_injected_helpers_have_no_em_or_en_dashes():
    """No em/en dashes in anything appscriptly AUTHORS here (a hard project
    guardrail). Only the injected parts are checked — the pre-existing
    templates may still carry dashes in their own comments."""
    authored = [
        obs.reporter_helper_source(),
        obs.guarded_function_block("f", "g();"),
        obs.wrap_generated_body("f", "g();"),
        obs.guarded_delegator("f")[0],
        obs.guarded_entry_point("doGet", "__x"),
    ]
    for src in authored:
        assert _EM_DASH not in src
        assert _EN_DASH not in src


# --------------------------------------------------------------------------
# the mechanism is present in EVERY generated class
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(_all_generated_scripts()))
def test_every_generated_script_carries_the_failure_reporter(label):
    script = _all_generated_scripts()[label]
    # The reporter is defined EXACTLY once (every wrapper calls the same one).
    assert script.count(f"function {obs.REPORTER_FUNCTION}(") == 1
    assert "MailApp.sendEmail(" in script


@pytest.mark.parametrize("label", sorted(_all_generated_scripts()))
def test_every_generated_script_rethrows_the_original_error(label):
    script = _all_generated_scripts()[label]
    # A caller wrapper is present that reports THEN rethrows (never swallows).
    assert f"{obs.REPORTER_FUNCTION}(" in script
    assert "throw __appscriptlyErr__;" in script


def test_trigger_classes_target_the_guard_not_the_raw_handler():
    """D/E: the installable trigger must target the guard wrapper, and the
    guard must delegate to the caller's handler (which stays verbatim)."""
    dash, _ = sheet_dashboard.build_dashboard_script_body(
        "function refreshDashboard() { rebuild(); }", "hourly", 0
    )
    assert 'var handlerName = "__appscriptlyGuarded_refreshDashboard__"' in dash
    assert "return refreshDashboard(e);" in dash
    # The caller's function is still present verbatim (not renamed/mangled).
    assert "function refreshDashboard() { rebuild(); }" in dash


def test_menu_handlers_are_each_wrapped():
    """B: every menu-item handler body runs inside the reporter wrapper."""
    script = doc_menu.build_menu_script("Tools", _MENU_ITEMS)
    # Each handler function keeps its name and now contains a try + rethrow.
    assert "function insertBlock() {" in script
    assert "function noop() {" in script
    assert script.count("throw __appscriptlyErr__;") == 2  # one per handler
    # The caller body still runs inside its handler.
    assert "appendParagraph('hi');" in script


# --------------------------------------------------------------------------
# Class H — web app doGet/doPost (gas_deploy)
# --------------------------------------------------------------------------


def test_inject_error_reporting_wraps_dopost():
    out = inject_error_reporting(
        "function doPost(e) { return ContentService.createTextOutput(handle(e)); }"
    )
    assert f"function {obs.REPORTER_FUNCTION}(" in out
    assert "function doPost(e) {" in out  # the guarded entry keeps its name
    assert "function __appscriptlyUserDoPost(e)" in out  # caller renamed
    assert "return __appscriptlyUserDoPost(e);" in out
    assert "throw __appscriptlyErr__;" in out


def test_inject_error_reporting_wraps_doget():
    out = inject_error_reporting("function doGet(e) { return page(e); }")
    assert "function doGet(e) {" in out
    assert "function __appscriptlyUserDoGet(e)" in out
    assert "return __appscriptlyUserDoGet(e);" in out


def test_inject_error_reporting_noop_without_entry_points():
    src = "function helper() { return 1; }"
    assert inject_error_reporting(src) == src


def test_error_reporting_composes_under_the_hmac_guard():
    """Applied error-reporting FIRST then the HMAC guard: the HMAC doPost must
    stay OUTERMOST (verifies before any user code), delegating through the
    error-report guard (renamed __mcpUserDoPost) to the caller."""
    body = "function doPost(e) { return ContentService.createTextOutput(handle(e)); }"
    layered = inject_webapp_hmac_guard(inject_error_reporting(body), "a" * 64)
    # HMAC is the outermost doPost (auth check present + delegates onward).
    assert "__mcpVerifyWebappHmac(e)" in layered
    assert "return __mcpUserDoPost(e);" in layered
    # The reporter + caller are still present underneath.
    assert f"function {obs.REPORTER_FUNCTION}(" in layered
    assert "function __appscriptlyUserDoPost(e)" in layered
    assert "throw __appscriptlyErr__;" in layered
