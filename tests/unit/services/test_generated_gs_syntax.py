"""Static-validation harness for every ``as_*`` generator's produced ``.gs``.

WHY THIS EXISTS (inventory gap #4): every other unit test asserts SUBSTRINGS
of the generated Apps Script string (e.g. ``assert "function installTrigger()"
in body``); none ever parses the generated ``.gs`` AS CODE. So a generator
that emits syntactically broken JavaScript, or an ``onOpen`` custom menu whose
``.addItem`` points at a function it never defines, ships green and only
explodes when the USER runs it in the Apps Script editor at 3am.

WHAT IT DOES: renders each generator's ``.gs`` output for representative
params (all automation classes, edge cases: multi-item menus with
label-escaping stress, every schedule kind, the HMAC-injected web-app
variant) and enforces two gates:

  1. SYNTAX -- validates each rendered source with ``node --check`` (V8
     parse, NO execution). Because nothing runs, undefined Apps Script
     globals (``DocumentApp`` / ``SpreadsheetApp`` / ``ScriptApp`` / ...) are
     expected and fine; only genuine ``SyntaxError``s fail. Apps Script is
     V8 / ES-compatible for SYNTAX purposes, so ``node --check`` is a
     faithful gate. If a construct were valid Apps Script yet tripped node it
     would be allowlisted here with an explicit comment -- none is needed
     today. node is the same dependency the existing HMAC behavioral test
     already relies on (``tests/js/verify_hmac_behavior.test.mjs``).

  2. MENU INTEGRITY -- for generators that build an ``onOpen`` custom menu,
     every ``.addItem(label, "fn")`` target must be DEFINED in the same
     source. This closes the undefined-symbol class for the one place it is
     cheaply derivable from the generated output (the menu wiring): a menu
     that points at a function it never defines throws "Script function not
     found" the moment the user clicks the item.

WHAT IS OUT OF SCOPE (deliberately, not an omission):
  * Class A ``as_generate_bound_script`` emits NO server-generated ``.gs``
    -- the caller authors the whole body -- so there is nothing here to
    statically validate; its manifest is JSON, validated by the manifest
    builder tests.
  * ``encode_video`` runs server-side ffmpeg; it produces no ``.gs``.
  * The static bundled ``restructure.gs`` (Class I) is already parsed under
    node by ``tests/unit/test_restructure_gs_verify_behavior.py`` (its vm
    sandbox throws on a syntax error), so it is not re-checked here.

NODE HANDLING: skipped LOUDLY when node is absent locally; in CI (the tests
workflow provisions Node) a missing node FAILS instead of skipping, so this
gate can never silently degrade to a no-op on the runner it exists to protect.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# The generator builders under test. All are PURE (params -> .gs string), so
# the harness renders them DIRECTLY -- no Google API, no mocking, no deploy.
from appscriptly.services.apps_script.custom_function import (
    build_custom_function_script,
)
from appscriptly.services.apps_script.doc_menu import (
    build_menu_script as build_doc_menu_script,
)
from appscriptly.services.apps_script.edit_trigger import (
    build_edit_trigger_script_body,
)
from appscriptly.services.apps_script.form_handler import (
    build_form_handler_script_body,
)
from appscriptly.services.apps_script.grade_form_responses import (
    build_grade_script_body,
)
from appscriptly.services.apps_script.refresh_linked_slides import (
    build_refresh_script_body,
)
from appscriptly.services.apps_script.sheet_dashboard import (
    build_dashboard_script_body,
)
from appscriptly.services.apps_script.sheet_menu import (
    build_menu_script as build_sheet_menu_script,
)
from appscriptly.services.apps_script.slides_menu import (
    build_menu_script as build_slides_menu_script,
)
from appscriptly.services.apps_script.video_deck import build_video_deck_script
from appscriptly.services.gas_deploy.api import inject_webapp_hmac_guard

_NODE = shutil.which("node")
_IN_CI = bool(os.environ.get("CI"))


def _require_node() -> str:
    """Return the node executable, or skip (local) / fail (CI).

    Locally we skip loudly when node is not installed. In CI the tests
    workflow provisions Node, so a missing node there would let the syntax
    gate silently pass -- we FAIL instead, refusing to let the gate become a
    no-op on the runner it is meant to protect.
    """
    if _NODE is not None:
        return _NODE
    reason = (
        "node not on PATH; the generated-.gs static-validation harness needs "
        "Node's V8 parser (`node --check`)."
    )
    if _IN_CI:
        pytest.fail(reason + " CI must provide Node for this gate.")
    pytest.skip(reason)


# --- representative caller-authored bodies (the "trusted" inputs the tools
# accept from Claude; the trigger/dashboard/grade builders REQUIRE a named
# function declaration, and the custom-function builder requires the body to
# define its named function). ---

_REFRESH_BODY = (
    "function refreshDashboard() {\n"
    "  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];\n"
    "  sheet.getRange('A1').setValue(new Date());\n"
    "}"
)
_EDIT_BODY = (
    "function onEditHandler(e) {\n"
    "  if (e && e.range) { e.range.setNote('edited ' + new Date()); }\n"
    "}"
)
_SUBMIT_BODY = (
    "function onSubmitHandler(e) {\n"
    "  Logger.log('submission: ' + JSON.stringify(e && e.namedValues));\n"
    "}"
)
_SCORER_BODY = (
    "function scoreItem(itemResponse, item) {\n"
    "  if (itemResponse.getResponse() === '42') { itemResponse.setScore(1); }\n"
    "  return itemResponse;\n"
    "}"
)
_CUSTOM_FN_BODY = (
    "function BRAND_CHECK(input) {\n"
    "  return String(input).toUpperCase().indexOf('ACME') >= 0;\n"
    "}"
)
_DOPOST_BODY = (
    "function doPost(e) {\n"
    "  var data = JSON.parse((e && e.postData && e.postData.contents) || '{}');\n"
    "  return ContentService\n"
    "    .createTextOutput(JSON.stringify({ok: true, echo: data}))\n"
    "    .setMimeType(ContentService.MimeType.JSON);\n"
    "}"
)
_DOPOST_WITH_EXTRAS = (
    "function doGet(e) {\n"
    "  return ContentService.createTextOutput('hi');\n"
    "}\n\n"
    "function helper_(x) { return x * 2; }\n\n"
    "function doPost(e) {\n"
    "  return ContentService.createTextOutput(String(helper_(21)));\n"
    "}"
)
# 64 lowercase hex chars -- the shape generate_hmac_key() mints.
_HMAC_KEY = "ab" * 32

# Multi-item menu with escaping stress: a comma INSIDE a label (exercises the
# non-greedy addItem-target extraction), embedded double-quotes + '<' + a
# backslash + a non-ASCII char (exercises the JSON string escaping in the
# generator), an empty body (a no-op handler is legal), and a '$' in an
# identifier (a valid JS identifier char).
_MENU_ITEMS_MULTI = [
    {
        "label": "Insert name, date",
        "function_name": "sayHi",
        "function_body": "DocumentApp.getUi().alert('hi');",
    },
    {
        "label": 'Quote "block" & <b> \\ café',
        "function_name": "insertBlock",
        "function_body": "var s = 'ok';\nLogger.log(s);",
    },
    {
        "label": "Empty handler",
        "function_name": "third_item$",
        "function_body": "",
    },
]
_MENU_ITEMS_SINGLE = [
    {
        "label": "Refresh",
        "function_name": "refreshData",
        "function_body": "SpreadsheetApp.getActive().toast('done');",
    },
]

# Realistic-shaped Drive / Slides IDs (embedded as JS string literals by the
# builders). Content is [A-Za-z0-9_-] like real Drive IDs.
_SHEET_ID = "1AbC_dEfG-hIjKlMnOpQrStUvWxYz0123456789"
_FORM_ID = "1FoRm_iD-abcdefghijklmnopqrstuvwxyz01234"
_PRES_ID = "1PrEs_iD-abcdefghijklmnopqrstuvwxyz01234"


@dataclass(frozen=True)
class GsCase:
    """One rendered generator output to validate.

    ``id`` is the readable pytest parametrize id; ``label`` names the tool /
    automation class for failure messages; ``source`` is the rendered ``.gs``;
    ``expect_menu`` marks cases that build an ``onOpen`` menu (subject to the
    menu-integrity gate, which also asserts the menu is non-empty).
    """

    id: str
    label: str
    source: str
    expect_menu: bool = False


def _cases() -> list[GsCase]:
    """Render every generator once per representative param set (PURE)."""
    cases: list[GsCase] = []

    # CLASS B -- custom-menu installers. Same builder shape, three UI
    # namespaces (DocumentApp / SpreadsheetApp / SlidesApp). Multi-item (with
    # escaping stress) + single item.
    for svc, builder in (
        ("doc", build_doc_menu_script),
        ("sheet", build_sheet_menu_script),
        ("slides", build_slides_menu_script),
    ):
        cases.append(
            GsCase(
                id=f"B-{svc}_menu-multi",
                label=f"as_install_{svc}_menu (multi-item menu)",
                source=builder("appscriptly Tools", _MENU_ITEMS_MULTI),
                expect_menu=True,
            )
        )
        cases.append(
            GsCase(
                id=f"B-{svc}_menu-single",
                label=f"as_install_{svc}_menu (single item)",
                source=builder("Tools", _MENU_ITEMS_SINGLE),
                expect_menu=True,
            )
        )

    # CLASS C -- custom spreadsheet function (@customfunction JSDoc prepend).
    # Second case stresses the JSDoc description escaping with a literal '*/'
    # and embedded quotes (a naive prepend would close the comment early and
    # deploy the rest as live code -> node --check would catch it).
    cases.append(
        GsCase(
            id="C-custom_function-nodesc",
            label="as_install_custom_function (no description)",
            source=build_custom_function_script("BRAND_CHECK", _CUSTOM_FN_BODY),
        )
    )
    cases.append(
        GsCase(
            id="C-custom_function-desc-escape",
            label="as_install_custom_function (description with */ escape)",
            source=build_custom_function_script(
                "BRAND_CHECK",
                _CUSTOM_FN_BODY,
                description='Checks brand mention. Edge: */ and "quotes".',
            ),
        )
    )

    # CLASS D -- time-driven installers. build_dashboard_script_body is SHARED
    # VERBATIM by as_install_sheet_dashboard / as_install_calendar_sync /
    # as_install_task_rollover (they import it as _build_time_trigger_script_body
    # and differ only in generated-MANIFEST scope, not in the .gs). One case
    # per schedule kind, with hour boundaries (0 / 9 / 23).
    for schedule, hour in (("daily", 9), ("hourly", 0), ("weekly", 23)):
        body, _handler = build_dashboard_script_body(
            _REFRESH_BODY,
            schedule,
            hour,
            dashboard_note="Rebuilds the dashboard tab.\nEdge */ in a note.",
        )
        cases.append(
            GsCase(
                id=f"D-dashboard-{schedule}",
                label=(
                    "as_install_sheet_dashboard/calendar_sync/task_rollover "
                    f"({schedule})"
                ),
                source=body,
            )
        )

    # CLASS E -- reactive-trigger installers. edit_trigger (onEdit) +
    # form_handler (onFormSubmit; the body is also REUSED VERBATIM by
    # as_install_contact_sync).
    edit_body, _ = build_edit_trigger_script_body(
        _EDIT_BODY, _SHEET_ID, handler_note="React to edits."
    )
    cases.append(
        GsCase(
            id="E-edit_trigger",
            label="as_install_edit_trigger (onEdit)",
            source=edit_body,
        )
    )
    form_body, _ = build_form_handler_script_body(
        _SUBMIT_BODY, _FORM_ID, handler_note="React to submissions."
    )
    cases.append(
        GsCase(
            id="E-form_handler",
            label="as_install_form_handler / as_install_contact_sync",
            source=form_body,
        )
    )

    # CLASS F -- on-demand run tools (onOpen menu + action). grade + refresh,
    # each with the default and a custom (quote-bearing) menu title.
    grade_default, _ = build_grade_script_body(_SCORER_BODY)
    cases.append(
        GsCase(
            id="F-grade-default-menu",
            label="as_grade_form_responses (default menu)",
            source=grade_default,
            expect_menu=True,
        )
    )
    grade_custom, _ = build_grade_script_body(
        _SCORER_BODY, menu_title='Quiz "Pro" Tools'
    )
    cases.append(
        GsCase(
            id="F-grade-custom-menu",
            label="as_grade_form_responses (custom menu title)",
            source=grade_custom,
            expect_menu=True,
        )
    )
    cases.append(
        GsCase(
            id="F-refresh-default-menu",
            label="as_refresh_linked_slides (default menu)",
            source=build_refresh_script_body(),
            expect_menu=True,
        )
    )
    cases.append(
        GsCase(
            id="F-refresh-custom-menu",
            label="as_refresh_linked_slides (custom menu title)",
            source=build_refresh_script_body(menu_title='Deck "Sync" Tools'),
            expect_menu=True,
        )
    )

    # CLASS G -- slides->video renderer (onOpen menu + renderFrames + signed
    # server POST). The un-injected doGet/doPost path deploys caller code
    # verbatim; this builder is the generator's contribution.
    cases.append(
        GsCase(
            id="G-video_deck",
            label="as_generate_video_deck (render half)",
            source=build_video_deck_script(
                _PRES_ID,
                "https://mcp.appscriptly.com/upload/frames/batch_abc123",
                "tok_" + "de" * 20,
            ),
            expect_menu=True,
        )
    )

    # CLASS H -- standalone web app, HMAC-INJECTED variant (the generator's
    # contribution for a public ANYONE_ANONYMOUS app: it renames the caller's
    # doPost and prepends a signed-request guard). Simple doPost + a body with
    # doGet/helper/doPost so the rename targets only the entry point.
    cases.append(
        GsCase(
            id="H-webapp-hmac-simple",
            label="as_deploy_web_app HMAC guard (simple doPost)",
            source=inject_webapp_hmac_guard(_DOPOST_BODY, _HMAC_KEY),
        )
    )
    cases.append(
        GsCase(
            id="H-webapp-hmac-extras",
            label="as_deploy_web_app HMAC guard (doGet + helper + doPost)",
            source=inject_webapp_hmac_guard(_DOPOST_WITH_EXTRAS, _HMAC_KEY),
        )
    )

    return cases


_GS_CASES = _cases()
_MENU_CASES = [c for c in _GS_CASES if c.expect_menu]

# A top-level ``function NAME(`` declaration. Every generated menu handler is
# emitted in this form (Class B wraps each item body as ``function fn() {..}``;
# grade/refresh/video declare gradeResponses/refreshLinkedSlides/renderFrames),
# so this is sufficient for the menu-integrity check.
_FUNCTION_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")

# ``.addItem(<label literal>, <"fn"|'fn'>)`` -- the function name is a
# validated JS identifier, always quoted, immediately before the ')'. The
# non-greedy label match tolerates commas / quotes inside the label literal,
# and the anchor on the closing paren means only the true second arg matches.
# BOUNDARY: this is a textual scan, so a handler body that literally contained
# a `.addItem(x, "fn")` call would also be read as a menu target. No generated
# shape does that (handler bodies here don't build menus), so it is a
# theoretical false-positive, not a real one; if a future generator emits
# nested .addItem calls inside a handler, scope this to the onOpen block.
_ADDITEM_TARGET_RE = re.compile(
    r"\.addItem\(.+?,\s*(['\"])([A-Za-z_$][A-Za-z0-9_$]*)\1\s*\)"
)


def _declared_function_names(source: str) -> set[str]:
    return set(_FUNCTION_DECL_RE.findall(source))


def _menu_target_names(source: str) -> set[str]:
    return {m.group(2) for m in _ADDITEM_TARGET_RE.finditer(source)}


def _node_check(node: str, source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``node --check`` on ``source`` written to a temp ``.js`` file.

    The ``.js`` extension makes node parse it as a CommonJS script, where
    top-level ``function`` / ``var`` declarations are legal (no ESM needed) --
    the same top-level shape Apps Script uses.
    """
    gs_file = tmp_path / "generated.js"
    gs_file.write_text(source, encoding="utf-8")
    return subprocess.run(
        [node, "--check", str(gs_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("case", _GS_CASES, ids=[c.id for c in _GS_CASES])
def test_generated_gs_is_syntactically_valid(case: GsCase, tmp_path: Path):
    """Every generator's rendered ``.gs`` must PARSE as JavaScript.

    Parse-only (``node --check``): undefined Apps Script globals are expected
    and fine; only genuine ``SyntaxError``s fail.
    """
    node = _require_node()
    result = _node_check(node, case.source, tmp_path)
    assert result.returncode == 0, (
        f"Generated .gs for {case.label} is NOT valid JavaScript.\n"
        f"node --check reported:\n{result.stderr}\n"
        f"--- generated source ---\n{case.source}"
    )


@pytest.mark.parametrize("case", _MENU_CASES, ids=[c.id for c in _MENU_CASES])
def test_generated_menu_targets_are_defined(case: GsCase):
    """Every ``onOpen`` ``.addItem(label, "fn")`` target must be DEFINED in
    the same source.

    Catches the undefined-symbol class where it is cheaply derivable from the
    generated output: a menu that points at a function it never defines throws
    "Script function not found" the instant the user clicks the item.
    """
    targets = _menu_target_names(case.source)
    declared = _declared_function_names(case.source)
    assert targets, (
        f"{case.label}: expected an onOpen menu with .addItem targets but "
        f"found none. The menu-target extractor may be broken.\n{case.source}"
    )
    missing = targets - declared
    assert not missing, (
        f"{case.label}: menu item(s) reference function(s) {sorted(missing)} "
        f"that are NOT defined in the generated source (defined: "
        f"{sorted(declared)}). Clicking the item would fail with 'Script "
        f"function not found'.\n--- generated source ---\n{case.source}"
    )
