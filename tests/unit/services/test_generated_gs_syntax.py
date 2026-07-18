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

DRIVEN OFF THE RECIPE REGISTRY (Stream S2/S3): the cases are the UNION of two
sources -- a set derived by iterating the recipe registry (``_registry_cases``:
every ``RECIPES`` entry rendered through the registry's pure ``render`` for each
of its ``example_params``) AND a small set of hand-written cases (``_cases``)
for the two generators that are NOT recipes (the standalone ``as_deploy_web_app``
HMAC guard + the ``as_uninstall_automation`` disarm stub -- see the note above
``_cases``). Iterating the registry is the structural close of this gap: a new
recipe row is parse-gated + menu-integrity-gated with ZERO edits here,
``expect_menu`` is DERIVED from the entry's ``activation_model`` (never
hand-flagged), and ``test_every_recipe_is_covered_by_the_harness`` fails if a
registered recipe escapes the collection (empty ``example_params`` or a dropped
entry). Since Stream S3 the recipe wrappers delegate to ``render``, so the
former hand-written Class B-G cases would only re-exercise the same ``build_*``
the registry cases already cover; they were dropped, leaving the registry as the
single source for every recipe generator's ``.gs``.

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

# The two NON-recipe generators still hand-written here (see the note above
# ``_cases``): ``as_deploy_web_app``'s HMAC guard (a deploy PRIMITIVE, not a
# bound recipe) and the uninstall disarm stub. Both are PURE (params -> .gs
# string). Every RECIPE generator's ``.gs`` (Classes B-G) is covered via the
# registry (``_registry_cases``), whose ``render`` lazily imports the
# ``build_*`` builders -- so they are no longer imported directly here.
from appscriptly.services.apps_script._lifecycle import build_disarm_script
from appscriptly.services.gas_deploy.api import inject_webapp_hmac_guard

# The recipe registry (Stream S1): the 13 bound generators expressed as DATA,
# plus a pure ``render`` that reproduces each generator's ``.gs`` body. The
# harness ALSO iterates this registry (``_registry_cases``) so every registered
# recipe is parse-gated + menu-integrity-gated AUTOMATICALLY -- a new recipe row
# is gated with ZERO edits to this file, and a recipe that escapes the
# collection fails the completeness pin below.
from appscriptly.services.apps_script._recipes import RECIPES, render

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


# --- the caller-authored web-app bodies for the Class H HMAC-guard cases (the
# "trusted" doPost / doGet source as_deploy_web_app wraps with a signed-request
# guard). The recipe generators' inputs (menus, triggers, dashboards, scorers,
# ...) now live on each RecipeSpec's ``example_params`` and reach this harness
# through ``_registry_cases``. ---

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


@dataclass(frozen=True)
class GsCase:
    """One rendered generator output to validate.

    ``id`` is the readable pytest parametrize id; ``label`` names the tool /
    automation class for failure messages; ``source`` is the rendered ``.gs``;
    ``expect_menu`` marks cases that build an ``onOpen`` menu (subject to the
    menu-integrity gate, which also asserts the menu is non-empty); ``recipe``
    is the ``RECIPES`` key for registry-derived cases (``None`` for the
    hand-written generator / lifecycle cases) -- the completeness pin reads it
    to prove every registered recipe produced at least one case.
    """

    id: str
    label: str
    source: str
    expect_menu: bool = False
    recipe: str | None = None


def _cases() -> list[GsCase]:
    """Render the two NON-recipe generators (Class H + LIFECYCLE).

    Every RECIPE generator's ``.gs`` (Classes B-G) is covered by
    ``_registry_cases`` -- the wrappers delegate to the registry's ``render``
    (Stream S3), so a hand-written case here would exercise the same ``build_*``
    twice. What remains are the two generators that are NOT recipes (no
    ``RECIPES`` entry) and so are unreachable via the registry: the standalone
    web-app HMAC guard (a deploy PRIMITIVE) and the uninstall disarm stub.
    """
    cases: list[GsCase] = []

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

    # LIFECYCLE -- the inert stub as_uninstall_automation pushes over a
    # disarmed automation (Stream 2). It redefines each recorded trigger
    # handler as a self-reaper (deletes all project triggers on next fire),
    # so a broken stub would leave an installed trigger firing-and-erroring
    # forever. No menu (its onOpen is a deliberate no-op), so expect_menu
    # stays False. Two shapes: with recorded handlers (Class D/E) and none.
    cases.append(
        GsCase(
            id="X-uninstall-disarm-handlers",
            label="as_uninstall_automation disarm stub (self-disarm handlers)",
            source=build_disarm_script(["refreshDashboard", "onSubmitHandler"]),
        )
    )
    cases.append(
        GsCase(
            id="X-uninstall-disarm-nohandlers",
            label="as_uninstall_automation disarm stub (no handlers)",
            source=build_disarm_script(),
        )
    )

    return cases


# Recipes whose onOpen builds a custom menu are subject to the menu-integrity
# gate. DERIVED from ``activation_model`` (never hand-flagged per case) so a new
# menu-building recipe is menu-gated with no edit here: "menu" = the Class B
# custom menus (doc / sheet / slides); "menu_action" = the Class F/G on-open
# menu actions (grade / refresh / video_deck). ``test_non_menu_cases_define_no_menu``
# self-checks this derivation against the rendered output.
_MENU_ACTIVATION_MODELS = frozenset({"menu", "menu_action"})


def _registry_cases() -> list[GsCase]:
    """Render EVERY recipe registry entry x its ``example_params`` (PURE).

    Iterating ``RECIPES`` (not a hand-list) is the key property: a recipe row
    added to ``_recipes.py`` is parse-gated + menu-integrity-gated here with
    ZERO edits to this file. ``expect_menu`` is derived from the entry's
    ``activation_model`` (see ``_MENU_ACTIVATION_MODELS``), never hand-set.

    ``render`` reproduces the generator's ``.gs`` body (the S1 identity pins
    guarantee it is byte-for-byte the generator's output); it deliberately does
    NOT run ``pre_mint``, so each entry's ``example_params`` already carry any
    post-pre_mint values (video_deck's stubbed upload URL + token) -- exactly as
    the wrapper passes them post-hook. Runs at import, like ``_cases``: a recipe
    whose ``render`` raises fails collection loudly rather than shipping unseen.
    """
    cases: list[GsCase] = []
    for spec in RECIPES.values():
        expect_menu = spec.activation_model in _MENU_ACTIVATION_MODELS
        for i, params in enumerate(spec.example_params):
            cases.append(
                GsCase(
                    id=f"registry-{spec.name}-{i}",
                    label=f"{spec.name} (registry render #{i})",
                    source=render(spec, params).script_body,
                    expect_menu=expect_menu,
                    recipe=spec.name,
                )
            )
    return cases


# The registry cases (``_registry_cases``) exercise every recipe generator's
# ``build_*`` via the registry's ``render``; the hand-written cases (``_cases``)
# cover only the two NON-recipe generators (webapp HMAC guard + disarm stub).
# Done in Stream S3 (the earlier redundancy note): the recipe wrappers now
# delegate to ``render``, so the former Class B-G hand-written cases were
# duplicates of the registry cases and were dropped -- the Class H
# (``as_deploy_web_app`` HMAC guard) and LIFECYCLE (``as_uninstall_automation``
# disarm stub) cases are NOT recipes (no ``RECIPES`` entry) and remain here.
_HANDWRITTEN_CASES = _cases()
_REGISTRY_CASES = _registry_cases()
_GS_CASES = _HANDWRITTEN_CASES + _REGISTRY_CASES
_MENU_CASES = [c for c in _GS_CASES if c.expect_menu]
_NON_MENU_CASES = [c for c in _GS_CASES if not c.expect_menu]

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


def test_every_recipe_is_covered_by_the_harness():
    """COMPLETENESS PIN: every ``RECIPES`` entry is rendered + gated here.

    The two halves of the key property this stream establishes:

      * A new recipe row is gated with ZERO edits to this file -- the cases
        iterate ``RECIPES`` (``_registry_cases``), so adding an entry adds its
        parse + menu-integrity cases automatically.
      * A registered recipe that ESCAPES the harness collection fails HERE
        instead of silently shipping un-parsed. The covered set is derived from
        the actually-collected registry cases; the expected set AND the expected
        COUNT are derived from ``RECIPES`` (never a hand-listed number). So a
        recipe shipped with empty ``example_params`` (0 cases), or one a future
        refactor drops from ``_registry_cases``, trips this pin.
    """
    covered = {c.recipe for c in _REGISTRY_CASES if c.recipe is not None}
    missing = set(RECIPES) - covered
    assert not missing, (
        f"recipe(s) {sorted(missing)} are registered in RECIPES but produced NO "
        f"harness case (empty example_params, or dropped from the collection). "
        f"Every recipe MUST be parse-gated + menu-integrity-gated here with no "
        f"per-recipe edit to this file."
    )
    # Count derived from RECIPES, not hand-listed: exactly one case per
    # example_params entry, across all recipes.
    expected_count = sum(len(spec.example_params) for spec in RECIPES.values())
    assert len(_REGISTRY_CASES) == expected_count, (
        f"registry harness collected {len(_REGISTRY_CASES)} cases but RECIPES "
        f"carries {expected_count} example_params across {len(RECIPES)} recipes "
        f"-- a recipe was skipped, double-counted, or ships no example_params."
    )


@pytest.mark.parametrize("case", _NON_MENU_CASES, ids=[c.id for c in _NON_MENU_CASES])
def test_non_menu_cases_define_no_menu(case: GsCase):
    """Cases NOT flagged as menu-building must declare no ``.addItem`` target.

    This self-checks the ``expect_menu`` derivation (``_MENU_ACTIVATION_MODELS``)
    against the rendered output: if a recipe builds an onOpen menu but is
    mis-derived as non-menu, its menu would silently escape the menu-integrity
    gate. Finding an ``.addItem`` target in a case marked non-menu catches that
    gap -- keeping "menu-integrity where applicable" honestly automatic rather
    than dependent on a hand-set flag.
    """
    targets = _menu_target_names(case.source)
    assert not targets, (
        f"{case.label}: marked NON-menu (expect_menu=False) but the generated "
        f"source declares .addItem menu target(s) {sorted(targets)}. Either the "
        f"activation_model -> menu derivation missed a menu-building recipe or a "
        f"menu leaked into a non-menu generator; the menu-integrity gate would "
        f"NOT cover it.\n--- generated source ---\n{case.source}"
    )
