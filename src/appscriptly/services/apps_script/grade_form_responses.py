"""``as_grade_form_responses`` — push computed grades onto quiz responses.

GAS service-parity. A *use-case* tool layered on the PR-Δ7 bound-script
generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). It installs a bound Apps Script into a
Google Form (quiz) whose ``gradeResponses()`` computes per-question scores
and calls ``FormApp.getActiveForm().submitGrades(responses)`` — pushing
the grades onto the submitted responses so respondents see their scores.

**Why this needs Apps Script (REST gap + the WRITE side).** The Forms REST
API can READ responses (``forms.responses.list`` / ``.get`` — the
``gforms_list_responses`` / ``gforms_get_response`` tools) but cannot
SUBMIT GRADES back onto them. Grading is an Apps-Script-only capability
(``Form.submitGrades(FormResponse[])`` built from
``ItemResponse.setScore`` + ``FormResponse.withItemGrade``). So a bound
script is the only programmatic path to push computed scores onto quiz
responses — this tool is the WRITE counterpart to the read-only response
tools.

**Caller authors the scoring; the tool owns the choreography.** The
generated ``gradeResponses()`` runs the canonical Apps Script grading
double-loop — for each response, for each item, get the gradable
``ItemResponse``, hand it to the caller's per-question scorer, attach it
via ``withItemGrade``, then ``submitGrades`` the batch. The caller
supplies ONLY the per-question scoring logic as a named function (which
question gets what score) — the dangerous/domain part stays caller-authored
(same trust model as the other apps_script generators' bodies); the
tool guarantees the correct ``submitGrades`` sequence around it.

**On-demand, not a trigger.** Grading is an *action* you invoke once a
quiz has responses (or to re-grade after changing the key). Unlike the
reactive trigger tools, there is no ``installTrigger`` — the generated
script exposes ``gradeResponses()`` (runnable) plus an ``onOpen`` menu
("<menu_title>" → "Grade responses") for one-click. The deploy WIRES the
grader but does not RUN it; the return payload is HONEST:
``run_required`` is ``True`` with ``run_instructions``, and
``graded_count`` is ``null`` (the count is only known once it runs — the
tool never reads the form).

**⚠️ Scope (verify-LAST — this is the load-bearing part).**
``Form.submitGrades`` requires the FULL ``https://www.googleapis.com/
auth/forms`` scope (the read-only ``forms.responses.readonly`` baseline
scope is NOT enough to WRITE grades). Crucially, that full ``forms`` scope
lives ONLY in the GENERATED bound script's manifest (declared via
``build_manifest``'s ``oauth_scopes``) — it is authorized by the user the
first time they run ``gradeResponses`` in the editor (the bound script's
OWN one-time consent). This tool DECLARES only ``GAS_BOUND_SCOPES``
(``script.projects`` + ``script.deployments``) for appscriptly's OWN
consent — both already baseline-granted. So this tool adds NO new scope to
appscriptly's own consent / OAuth-verification scope set. This is exactly
how ``form_handler.py`` lands ``script.scriptapp`` in the generated
manifest without touching ``auth.WORKSPACE_SCOPES``. (We bind DIRECTLY to
the Form ID and never call ``auto_detect_container_kind``, which rejects
Forms — same Forms-rejection lift as ``form_handler``.)
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script.api import build_manifest as _build_manifest
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA

# Imported for parity with the sibling apps_script tools; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# The default menu title for the one-click grade action. Callers may
# override via menu_title.
_DEFAULT_MENU_TITLE = "Quiz Tools"

# The runnable function name the generated script exposes + the menu item
# points at. Fixed (not caller-supplied) so the return contract + tests
# reference one source of truth.
_GRADE_FUNCTION = "gradeResponses"

# The menu item label that invokes the grade function.
_MENU_ITEM_LABEL = "Grade responses"

# Form.submitGrades requires the FULL forms scope to WRITE grades (the
# read-only forms.responses.readonly baseline scope cannot write). This is
# declared in the GENERATED manifest only — NOT added to appscriptly's own
# consent (see the module docstring's scope note).
_FORMS_SCOPE = "https://www.googleapis.com/auth/forms"


def _extract_handler_name(scoring_function_body: str) -> str:
    """Pull the function name out of a ``function NAME(...) {...}`` body.

    The caller supplies ``scoring_function_body`` as a named Apps Script
    function declaration (e.g. ``function scoreItem(itemResponse, item) {
    ... }``). The generated ``gradeResponses()`` calls that function by
    name per gradable item, so we parse the declared name out of the body.

    PURE — no I/O, deterministic. Matches the FIRST top-level
    ``function <name>(`` declaration (an arrow function or bare expression
    can't be referenced by name, so we reject those).

    Args:
        scoring_function_body: the ``.gs`` source for the per-question
            scorer — a named ``function`` declaration.

    Returns:
        The scorer function's name (e.g. ``"scoreItem"``).

    Raises:
        ValueError: no named ``function`` declaration found.
    """
    match = re.search(
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(",
        scoring_function_body,
    )
    if match is None:
        raise ValueError(
            "scoring_function_body must be a NAMED function declaration "
            "(e.g. `function scoreItem(itemResponse, item) { ... }`) — its "
            "name is called per gradable item by the generated "
            "`gradeResponses()`. Arrow functions and bare expressions "
            "can't be referenced by name. Got a body with no "
            "`function <name>(` declaration."
        )
    return match.group(1)


def build_grade_script_body(
    scoring_function_body: str,
    menu_title: str = _DEFAULT_MENU_TITLE,
) -> tuple[str, str]:
    """Synthesize the ``.gs`` body for the response grader (PURE).

    Deterministic, no I/O. Assembles a script with:

      1. the caller's per-question scorer verbatim (a named function that
         receives an ``ItemResponse`` + its ``Item`` and sets the score on
         the ``ItemResponse`` via ``setScore(...)`` — and optionally
         ``setFeedback(...)``);
      2. an ``onOpen`` menu ("<menu_title>" → "Grade responses") pointing
         at ``gradeResponses``;
      3. ``gradeResponses()`` — the canonical Apps Script grading loop:
         for each ``form.getResponses()``, for each ``form.getItems()``,
         get the gradable ``ItemResponse``
         (``getGradableResponseForItem``), pass it (+ the item) to the
         caller's scorer, attach it via ``response.withItemGrade(...)``,
         then ``form.submitGrades(responses)`` to persist. Skips items a
         response didn't answer (a null gradable response). Returns the
         number of responses graded.

    Args:
        scoring_function_body: the caller's named per-question scorer.
            Must declare the function by name.
        menu_title: the menu's display label (embedded as a JS string
            literal). Defaults to ``"Quiz Tools"``.

    Returns:
        ``(script_body, scorer_name)`` — the assembled ``.gs`` source and
        the parsed scorer-function name.

    Raises:
        ValueError: ``scoring_function_body`` has no named ``function``
            declaration (from ``_extract_handler_name``).
    """
    import json

    scorer = _extract_handler_name(scoring_function_body)
    title_literal = json.dumps(menu_title)
    item_literal = json.dumps(_MENU_ITEM_LABEL)

    grade_fn = f"""\
/**
 * Adds a "{menu_title}" menu with a one-click "{_MENU_ITEM_LABEL}" item.
 * Runs automatically when the form editor is opened.
 */
function onOpen(e) {{
  FormApp.getUi()
    .createMenu({title_literal})
    .addItem({item_literal}, "{_GRADE_FUNCTION}")
    .addToUi();
}}

/**
 * Computes per-question scores for every submitted response and pushes
 * them onto the responses via Form.submitGrades(). Run this (via the
 * "{_MENU_ITEM_LABEL}" menu item, or the editor Run button) once the quiz
 * has responses — deploying the script does not run it. Re-run to
 * re-grade after changing the scorer/key.
 *
 * Calls {scorer}(itemResponse, item) per gradable item; that function
 * (you authored it) sets the score on the itemResponse via setScore(...).
 *
 * Returns the number of responses graded (handy when run from the
 * editor).
 */
function {_GRADE_FUNCTION}() {{
  var form = FormApp.getActiveForm();
  var responses = form.getResponses();
  var items = form.getItems();
  var graded = 0;

  for (var r = 0; r < responses.length; r++) {{
    var response = responses[r];
    var didGrade = false;
    for (var i = 0; i < items.length; i++) {{
      var item = items[i];
      // getGradableResponseForItem returns null when this response did
      // not answer the item (or the item isn't gradable) — skip those.
      var itemResponse = response.getGradableResponseForItem(item);
      if (itemResponse === null) {{
        continue;
      }}
      // Caller-authored: set the score (and optional feedback) on the
      // itemResponse for this question.
      {scorer}(itemResponse, item);
      response.withItemGrade(itemResponse);
      didGrade = true;
    }}
    if (didGrade) {{
      graded++;
    }}
  }}

  // Persist the grades onto the submitted responses in one call.
  form.submitGrades(responses);
  Logger.log("Graded " + graded + " response(s).");
  return graded;
}}
"""

    body = f"{scoring_function_body.rstrip()}\n\n{grade_fn}"
    return body, scorer


@workspace_tool(
    title="Push computed grades onto Google Form quiz responses",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment — re-running
    # installs a SECOND grader script bound to the same Form. NOT
    # idempotent (same convention as the other apps_script generators).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA,
)
def as_grade_form_responses(
    creds: Credentials,
    form_id: str,
    scoring_function_body: str,
    menu_title: str = _DEFAULT_MENU_TITLE,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a grader that pushes computed scores onto quiz responses.

    Deploys a *bound* Apps Script into a Google Form (quiz) that, when
    run, computes per-question scores for every submitted response and
    calls ``Form.submitGrades()`` to push them onto the responses (so
    respondents see their scores). It also installs a one-click menu item.
    This composes the generic bound-script primitive
    (``as_generate_bound_script``) for the WRITE side of quiz grading,
    which the Forms REST API cannot do.

    USE WHEN: the user has a Google Form quiz with submitted responses and
    wants to push computed grades onto them — "auto-grade this quiz",
    "give 2 points for each correct answer and submit the grades". For
    READING responses (not grading), use ``gforms_list_responses`` /
    ``gforms_get_response`` (no script needed). To send a confirmation on
    each NEW submission, use ``as_install_form_handler``.

    WHY THIS EXISTS (REST can't do it): the Forms REST API can read
    responses but cannot SUBMIT GRADES. Grading is Apps-Script-only
    (``Form.submitGrades``), so this tool generates + deploys a bound
    grader.

    YOU AUTHOR THE SCORING; THE TOOL OWNS THE CHOREOGRAPHY. Supply
    ``scoring_function_body`` — a NAMED function that scores ONE question's
    response, e.g.::

        function scoreItem(itemResponse, item) {
          // itemResponse: the respondent's ItemResponse for this question
          // item: the Item (use item.getTitle() to branch per question)
          if (itemResponse.getResponse() === '42') {
            itemResponse.setScore(1);
          } else {
            itemResponse.setScore(0);
          }
        }

    The generated ``gradeResponses()`` runs the canonical grading loop
    (every response × every gradable item → your scorer →
    ``withItemGrade`` → ``submitGrades``). You decide the scoring; the tool
    guarantees the correct submit sequence.

    ON-DEMAND, not a trigger: grading reads the CURRENT responses, so it's
    an action you invoke (or re-invoke after changing the key). The deploy
    WIRES ``gradeResponses`` + the menu but does NOT run it. To grade: open
    the returned ``project_url`` (or the form editor) and either click the
    "Grade responses" menu item or run ``gradeResponses`` from the editor.
    The FIRST run prompts a one-time authorization for the full ``forms``
    scope (needed to WRITE grades — see the scope note below). The return
    payload says so explicitly — ``run_required`` is ``True`` with
    ``run_instructions``, and ``graded_count`` is ``null`` (the count is
    only known once it runs — this tool never reads the form). Do not tell
    the user the responses are graded until they've run it.

    SCOPE NOTE: writing grades needs the full ``forms`` scope, which lives
    ONLY in the generated bound script's manifest (authorized when the user
    runs the grader). This tool itself adds NO new scope to appscriptly's
    consent — it only uses the baseline Apps Script management scopes for
    the deploy.

    Args:
        form_id: Drive ID of the Google Form (quiz) to install the grader
            into (the ID part of the Form's edit URL). The bound script is
            attached to THIS Form.
        scoring_function_body: the ``.gs`` source for the per-question
            scorer as a NAMED function declaration, e.g.
            ``"function scoreItem(itemResponse, item) { ... }"``. Claude
            authors this — it sets the score on the passed ``ItemResponse``
            via ``setScore(...)`` (and optionally ``setFeedback(...)``).
            Required; empty / unnamed bodies are rejected. It receives the
            gradable ``ItemResponse`` and its ``Item``.
        menu_title: the menu's display label in the form editor's menu bar
            (the "Grade responses" item lives under it). Defaults to
            ``"Quiz Tools"``. Non-empty.
        name: OPTIONAL title for the new Apps Script project. Defaults to a
            generated grader name.
        on_conflict: what to do when a grader from THIS tool already exists
            on this Form. "new" (the default) always installs a fresh one
            (which can leave duplicate menus); "replace" uninstalls the
            prior install(s) on this Form first (no duplicate, no orphan);
            "skip" returns the existing install unchanged instead of adding
            a duplicate. Keyed by (this tool, this container) via
            appscriptly's automation ledger; the response adds
            ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, form_id, grade_function, project_url,
        graded_count, run_required, run_instructions, manifest_scope}``.
        ``grade_function`` is ``"gradeResponses"`` (the function to run).
        ``project_url`` deep-links to the script editor. ``graded_count``
        is ``null`` on a successful deploy (known only after the function
        runs). ``run_required`` is ``True`` and ``run_instructions``
        spells out the one step. ``manifest_scope`` is the full ``forms``
        scope the GENERATED script declares (reported for transparency —
        it's the bound script's scope, not appscriptly's consent).

    Raises:
        ValueError: empty ``form_id`` / ``menu_title``, or an empty /
            unnamed ``scoring_function_body`` (rejected before any API
            call).
        ToolError: any Apps Script / Drive API error — the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``form_id`` from the user's Form edit URL or a prior
    ``gforms_create_form`` call. After this returns, point the user at
    ``project_url`` (or the form's new menu) to run the grader. (The Apps
    Script scopes are baseline-granted, so most users won't see a second
    OAuth consent for the deploy itself; the in-editor run has its own
    one-time authorization for the full ``forms`` scope.)
    """
    # 1. Validate inputs client-side (cheap rejection before any I/O).
    if not form_id or not form_id.strip():
        raise ValueError(
            "form_id cannot be empty — pass the Drive ID of the Google "
            "Form (quiz) to install the grader into."
        )
    if not menu_title or not menu_title.strip():
        raise ValueError(
            "menu_title cannot be empty — it's the label shown in the "
            "form editor's menu bar."
        )
    if not scoring_function_body or not scoring_function_body.strip():
        raise ValueError(
            "scoring_function_body cannot be empty — pass the .gs source "
            "for the per-question scorer as a named function declaration "
            "(e.g. `function scoreItem(itemResponse, item) { ... }`)."
        )

    # 2. Synthesize the .gs body (caller's scorer + onOpen menu +
    #    gradeResponses loop). _extract_handler_name (inside) also rejects
    #    an unnamed function.
    script_body, scorer = build_grade_script_body(
        scoring_function_body, menu_title
    )

    # 3. Build the manifest. The onOpen menu derives script.container.ui;
    #    submitGrades needs the FULL forms scope, declared via oauth_scopes.
    #    Both land in the GENERATED manifest only — never in appscriptly's
    #    own consent (the load-bearing verify-LAST guarantee).
    manifest_dict = _build_manifest(
        {
            "menu": [
                {"name": _MENU_ITEM_LABEL, "function_name": _GRADE_FUNCTION}
            ],
            "oauth_scopes": [_FORMS_SCOPE],
        }
    )

    # 4. Default the project name when not supplied.
    project_name = name or "appscriptly form grader"

    # 5. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=form_id), push the body + manifest, cut a
    #    version + deploy. We bind DIRECTLY to the Form ID and never call
    #    auto_detect_container_kind (which rejects Forms) — same
    #    Forms-rejection lift as form_handler.
    result = _mint_bound_automation(
        creds,
        tool="as_grade_form_responses",
        container_id=form_id,
        container_kind="forms",
        project_name=project_name,
        script_body=script_body,
        manifest_dict=manifest_dict,
        on_conflict=on_conflict,
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "form_id": form_id,
        "grade_function": _GRADE_FUNCTION,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
        # HONEST run state: the deploy wires the grader but does NOT run
        # it, and the tool never reads the form — so the count is unknown.
        "graded_count": None,
        "run_required": True,
        "run_instructions": (
            f"Open the form editor (or the script editor at the "
            f"project_url) and run `{_GRADE_FUNCTION}` — via the "
            f"\"{_MENU_ITEM_LABEL}\" menu item under \"{menu_title}\", or "
            f"the editor Run button — approving the one-time authorization "
            f"prompt (the full `forms` scope, needed to write grades). "
            f"That pushes the computed scores onto the submitted "
            f"responses. Re-run to re-grade."
        ),
        # Unified activation contract (Stream 3): run_required /
        # run_instructions are the legacy aliases; these carry the canonical
        # shape. This is an on-demand action, so activation = running the
        # function once.
        **build_activation_fields(
            script_id,
            _GRADE_FUNCTION,
            (
                f"Open the form (or the script editor at the activation_url) "
                f"and run `{_GRADE_FUNCTION}` once: use the "
                f"\"{_MENU_ITEM_LABEL}\" menu item under \"{menu_title}\", or "
                f"select `{_GRADE_FUNCTION}` in the editor's function "
                f"dropdown and click Run, then approve the one-time "
                f"authorization prompt (the full `forms` scope, needed to "
                f"write grades). That pushes the computed scores onto the "
                f"submitted responses. Re-run to re-grade."
            ),
        ),
        # Transparency: the scope the GENERATED bound script declares to
        # write grades. It is the bound script's manifest scope, NOT a
        # scope added to appscriptly's own OAuth consent.
        "manifest_scope": _FORMS_SCOPE,
    }
