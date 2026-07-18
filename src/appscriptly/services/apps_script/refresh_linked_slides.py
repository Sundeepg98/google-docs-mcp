"""``as_refresh_linked_slides`` — sync linked slides from their source deck.

GAS service-parity. A *use-case* tool layered on the PR-Δ7 bound-script
generator primitive (``as_generate_bound_script`` /
``services/apps_script/api.py``). It installs a bound Apps Script into a
Google Slides presentation whose ``refreshLinkedSlides()`` walks every
slide (``presentation.getSlides()``) and calls ``slide.refreshSlide()``
on each slide that is LINKED to a source slide — pulling the latest
content from a master/source deck into this (client) deck.

**Why this can't be done over REST.** The Slides REST API has no
"refresh linked slide" operation — linked-slide refresh is an
Apps-Script-only capability (``Slide.refreshSlide()`` on a slide whose
``getSlideLinkingMode()`` is ``LINKED``). So a bound script is the ONLY
way to programmatically re-sync a client deck to its master. This is the
master-deck → client-deck sync the prompt calls out as a REST gap.

**On-demand, not a trigger.** Unlike ``sheet_dashboard`` /
``edit_trigger`` / ``form_handler`` (which wire installable TRIGGERS that
need a one-time ``installTrigger`` run), refreshing linked slides is an
*action* the user invokes when they want to pull the latest from the
source deck. So the generated script exposes:

  1. ``refreshLinkedSlides()`` — the runnable function (walks slides,
     refreshes the linked ones, returns the count); and
  2. an ``onOpen`` custom menu ("<menu_title>" → "Refresh linked slides")
     so it's one-click from the deck's menu bar.

Like the ``as_generate_video_deck`` render half, the deploy WIRES the
function but does not RUN it — refreshing happens when the user clicks the
menu item (or runs the function in the editor). The return payload is
HONEST about this: ``run_required`` is ``True`` with ``run_instructions``,
and ``refreshed_count`` is ``null`` (the linked-slide count is only known
once the function runs — the tool never reads the deck).

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py`` (``build_manifest`` →
``create_bound_project`` → ``set_project_content`` → ``create_deployment``).
This module's OWN contribution is the ``.gs`` body synthesis.

**Scope note (verify-LAST).** The tool DECLARES only ``GAS_BOUND_SCOPES``
(``script.projects`` + ``script.deployments``) for appscriptly's OWN
consent — both already baseline-granted, so NO new consent scope. The
generated script's scopes — ``script.container.ui`` (the onOpen menu) and
``presentations`` (``refreshSlide`` operates on the bound deck) — live
ONLY in the GENERATED manifest (derived/declared via ``build_manifest``),
never in ``auth.WORKSPACE_SCOPES``. (``presentations`` happens to already
be a baseline scope for the gslides_* tools, but here it is carried by the
bound script's manifest, not added to appscriptly's consent by this PR.)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    reporter_helper_source as _reporter_helper_source,
    wrap_generated_body as _wrap_generated_body,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    render as _render,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA

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


# The default menu title for the one-click refresh action. Callers may
# override via menu_title.
_DEFAULT_MENU_TITLE = "Presentation Tools"

# The runnable function name the generated script exposes + the menu item
# points at. Fixed (not caller-supplied) so the return contract + tests
# reference one source of truth.
_REFRESH_FUNCTION = "refreshLinkedSlides"

# The menu item label that invokes the refresh function.
_MENU_ITEM_LABEL = "Refresh linked slides"


def _js_string(value: str) -> str:
    """Render a Python str as a safe JS string literal.

    Uses ``json.dumps`` indirectly via manual escaping for the small set
    of chars that matter here (the menu title). Drive IDs / titles can't
    break out of the literal.
    """
    import json

    return json.dumps(value)


def build_refresh_script_body(menu_title: str = _DEFAULT_MENU_TITLE) -> str:
    """Synthesize the ``.gs`` body for the linked-slide refresher (PURE).

    Deterministic, no I/O. Produces a script with:

      * ``onOpen`` — adds a ``menu_title`` menu with one
        "Refresh linked slides" item pointing at ``refreshLinkedSlides``;
      * ``refreshLinkedSlides()`` — walks ``getSlides()``, and for each
        slide whose ``getSlideLinkingMode()`` is
        ``SlidesApp.SlideLinkingMode.LINKED`` calls ``refreshSlide()``,
        counting the refreshed ones; logs + returns the count (handy when
        run from the editor).

    Args:
        menu_title: the menu's display label (embedded as a JS string
            literal). Defaults to ``"Presentation Tools"``.

    Returns:
        The complete ``.gs`` source as a string, trailing newline
        included.
    """
    title_literal = _js_string(menu_title)
    item_literal = _js_string(_MENU_ITEM_LABEL)

    # The refreshLinkedSlides body. Wrapped (below) in the appscriptly
    # failure reporter so a refresh error is emailed to the owner, not just
    # buried in the execution log; the wrapper rethrows so the run still
    # records as failed (gap #5). onOpen is NOT wrapped: it is a simple
    # trigger that runs in the limited-auth context (no MailApp there).
    refresh_inner = f"""\
  var presentation = SlidesApp.getActivePresentation();
  var slides = presentation.getSlides();
  var refreshed = 0;

  for (var i = 0; i < slides.length; i++) {{
    var slide = slides[i];
    // Only LINKED slides can be refreshed; UNLINKED / NOT_LINKED_IMAGE
    // slides have no source to pull from, so skip them.
    if (slide.getSlideLinkingMode() ===
        SlidesApp.SlideLinkingMode.LINKED) {{
      slide.refreshSlide();
      refreshed++;
    }}
  }}

  Logger.log("Refreshed " + refreshed + " linked slide(s).");
  return refreshed;"""

    return f"""\
// Auto-generated by appscriptly as_refresh_linked_slides.
// Re-syncs every LINKED slide in this presentation from its source slide
// (master-deck -> client-deck sync the Slides REST API cannot do).
// Runs on Google's infrastructure — no Claude in the loop.

/**
 * Adds a "{menu_title}" menu with a one-click "{_MENU_ITEM_LABEL}" item.
 * Runs automatically when the presentation is opened.
 */
function onOpen(e) {{
  SlidesApp.getUi()
    .createMenu({title_literal})
    .addItem({item_literal}, "{_REFRESH_FUNCTION}")
    .addToUi();
}}

/**
 * Refreshes every LINKED slide in this presentation from its source
 * slide. Run this (via the "{_MENU_ITEM_LABEL}" menu item, or the editor
 * Run button) whenever you want to pull the latest content from the
 * source deck — deploying the script does not run it. A failure is
 * emailed to you (best-effort) then rethrown.
 *
 * Returns the number of slides refreshed (handy when run from the
 * editor).
 */
function {_REFRESH_FUNCTION}() {{
{_wrap_generated_body(_REFRESH_FUNCTION, refresh_inner)}}}

{_reporter_helper_source().rstrip()}
"""


@workspace_tool(
    title="Refresh linked slides in a Google Slides presentation",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment — re-running
    # installs a SECOND refresher script bound to the same deck. NOT
    # idempotent (same convention as the other apps_script generators).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA,
)
def as_refresh_linked_slides(
    creds: Credentials,
    presentation_id: str,
    menu_title: str = _DEFAULT_MENU_TITLE,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a "refresh linked slides" automation into a presentation.

    Deploys a *bound* Apps Script into the presentation that, when run,
    walks every slide and calls ``refreshSlide()`` on each one LINKED to a
    source slide — pulling the latest content from a master/source deck
    into this (client) deck. It also installs a one-click menu item so the
    user can trigger the refresh from the deck's menu bar. This composes
    the generic bound-script primitive (``as_generate_bound_script``) for
    a pattern the Slides REST API cannot express.

    USE WHEN: the user maintains a "master" slide deck and one or more
    "client" decks with LINKED slides, and wants the client deck to pull
    the latest from the master on demand — "re-sync my linked slides from
    the template deck", "refresh the linked charts in this deck". For
    decks with NO linked slides this is a no-op (nothing to refresh). To
    CREATE the link, use Slides' UI ("Linked" paste); this tool refreshes
    EXISTING links.

    WHY THIS EXISTS (REST can't do it): refreshing a linked slide is an
    Apps-Script-only capability (``Slide.refreshSlide()``); the Slides
    REST API has no equivalent. A bound script is the only programmatic
    path, so this tool generates + deploys one.

    ON-DEMAND, not a schedule/trigger: refreshing pulls the source deck's
    CURRENT content, so it's an action you invoke when you want the
    latest. The deploy WIRES the ``refreshLinkedSlides`` function + the
    menu but does NOT run it. To refresh: open the returned
    ``project_url`` (or the deck) and either click the
    "Refresh linked slides" menu item or run ``refreshLinkedSlides`` from
    the editor (approve the one-time authorization prompt the first time).
    The return payload says so explicitly — ``run_required`` is ``True``
    with ``run_instructions``, and ``refreshed_count`` is ``null`` (the
    count is only known once the function runs — this tool never reads the
    deck). Do not tell the user their slides are refreshed until they've
    run it.

    Args:
        presentation_id: Drive ID of the Google Slides presentation to
            install the refresher into (the ID part of the deck's URL).
            The bound script is attached to THIS presentation.
        menu_title: the menu's display label in the presentation's menu
            bar (the "Refresh linked slides" item lives under it).
            Defaults to ``"Presentation Tools"``. Non-empty.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated refresher name.
        on_conflict: what to do when a refresher from THIS tool already
            exists on this presentation. "new" (the default) always
            installs a fresh one (which can leave duplicate menus);
            "replace" uninstalls the prior install(s) on this presentation
            first (no duplicate, no orphan); "skip" returns the existing
            install unchanged instead of adding a duplicate. Keyed by (this
            tool, this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, presentation_id, refresh_function,
        project_url, refreshed_count, run_required, run_instructions}``.
        ``refresh_function`` is ``"refreshLinkedSlides"`` (the function to
        run). ``project_url`` deep-links to the script editor.
        ``refreshed_count`` is ``null`` on a successful deploy (the count
        is known only after the function runs). ``run_required`` is
        ``True`` and ``run_instructions`` spells out the one step.

    Raises:
        ValueError: empty ``presentation_id`` / ``menu_title`` (rejected
            before any API call).
        ToolError: any Apps Script / Drive API error — the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``presentation_id`` from the user's URL or a prior
    ``gslides_create_presentation`` call. After this returns, point the
    user at ``project_url`` (or the deck's new menu) to run the refresh.
    (The Apps Script scopes are baseline-granted, so most users won't see
    a second OAuth consent for the deploy itself; the in-editor /
    in-deck run has its own one-time authorization prompt.)
    """
    # 1. Validate inputs client-side (cheap rejection before any I/O).
    if not presentation_id or not presentation_id.strip():
        raise ValueError(
            "presentation_id cannot be empty — pass the Drive ID of the "
            "Google Slides presentation to install the refresher into."
        )
    if not menu_title or not menu_title.strip():
        raise ValueError(
            "menu_title cannot be empty — it's the label shown in the "
            "presentation's menu bar."
        )

    # 2. Codegen via the recipe registry (_recipes.py) — the SINGLE source
    #    for this tool's .gs body + manifest. render() runs the same
    #    build_refresh_script_body (onOpen menu + refreshLinkedSlides walker)
    #    and threads the same manifest plan (script.container.ui from the
    #    onOpen menu + the presentations scope for refreshSlide +
    #    add_mail_scope for the failure reporter); the byte-identity pins
    #    guarantee the output is unchanged.
    spec = _RECIPES["as_refresh_linked_slides"]
    params = {
        "presentation_id": presentation_id,
        "menu_title": menu_title,
        "name": name,
    }
    rendered = _render(spec, params)

    # 3. Deploy via the SAME machinery the #138 primitive uses: create the
    #    bound project (parentId=presentation_id), push the body +
    #    manifest, cut a version + deploy. container_kind is known
    #    ("slides") — no Drive mimeType round-trip needed.
    result = _mint_bound_automation(
        creds,
        tool=spec.name,
        recipe=spec.name,
        recipe_params=params,
        container_id=presentation_id,
        container_kind=spec.container_kind,
        project_name=spec.project_name(params),
        script_body=rendered.script_body,
        manifest_dict=rendered.manifest,
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
        "presentation_id": presentation_id,
        "refresh_function": _REFRESH_FUNCTION,
        "project_url": f"https://script.google.com/d/{script_id}/edit",
        # HONEST run state: the deploy wires the function but does NOT run
        # it, and the tool never reads the deck — so the count is unknown.
        "refreshed_count": None,
        "run_required": True,
        "run_instructions": (
            f"Open the presentation (or the script editor at the "
            f"project_url) and run `{_REFRESH_FUNCTION}` — via the "
            f"\"{_MENU_ITEM_LABEL}\" menu item under \"{menu_title}\", or "
            f"the editor Run button — approving the one-time authorization "
            f"prompt. That pulls the latest content from each linked "
            f"slide's source deck. Re-run it whenever you want to re-sync."
        ),
        # Unified activation contract (Stream 3): run_required /
        # run_instructions are the legacy aliases; these carry the canonical
        # shape. This is an on-demand action, so activation = running the
        # function once.
        **build_activation_fields(
            script_id,
            _REFRESH_FUNCTION,
            (
                f"Open the presentation (or the script editor at the "
                f"activation_url) and run `{_REFRESH_FUNCTION}` once: use the "
                f"\"{_MENU_ITEM_LABEL}\" menu item under \"{menu_title}\", or "
                f"select `{_REFRESH_FUNCTION}` in the editor's function "
                f"dropdown and click Run, then approve the one-time "
                f"authorization prompt. That pulls the latest content from "
                f"each linked slide's source deck. Re-run it whenever you "
                f"want to re-sync."
            ),
        ),
    }
