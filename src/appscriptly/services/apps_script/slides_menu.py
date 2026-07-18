"""``as_install_slides_menu`` — install a custom menu into a presentation.

GAS service-parity. The Slides analogue of ``as_install_doc_menu``
(``doc_menu.py``): a higher-level *feature* tool that composes the PR-Δ7
bound-script primitive (``services/apps_script/api.py``) into a one-call
"install a persistent custom menu" capability — for a Google Slides
presentation instead of a Doc.

**What it is.** Given a presentation ID, a menu title (default
"Presentation Tools"), and a list of menu items (each
``{label, function_name, function_body}``), this:

  1. Generates an ``.gs`` script body — an ``onOpen(e)`` trigger that
     builds the menu via ``SlidesApp.getUi().createMenu(title)``,
     ``.addItem(label, function_name)`` per item, then ``.addToUi()`` —
     PLUS each item's handler function (from its ``function_body``).
  2. Builds the manifest via ``build_manifest({"menu": [...]})`` so the
     ``script.container.ui`` OAuth scope is derived (reused, not
     reimplemented — menus are code, the manifest only carries scopes).
  3. Deploys it as a *bound* script via the SAME machinery
     ``as_generate_bound_script`` uses: ``create_bound_project`` →
     ``set_project_content`` → ``create_deployment``.

After the single deploy the menu appears in that presentation's menu bar
and persists — it runs on Google's infrastructure with no Claude in the
loop. Re-opening the presentation fires ``onOpen`` and re-adds the menu.
(``as_generate_video_deck`` already uses this same ``SlidesApp.getUi()``
onOpen-menu idiom for its "Render frames" item — this tool generalizes it
into a caller-driven menu installer.)

**Container scope.** Slides only. A bound ``SlidesApp.getUi()`` menu is
meaningless in a Doc or Sheet (those use ``DocumentApp`` /
``SpreadsheetApp``); this tool is the Slides-specialized composition.
Because the kind is fixed we bind DIRECTLY to the presentation ID and
never pay the Drive ``auto_detect_container_kind`` round-trip (same
shortcut ``doc_menu`` takes).

**Scope note (verify-LAST).** The tool DECLARES only ``GAS_BOUND_SCOPES``
(``script.projects`` + ``script.deployments``) for appscriptly's OWN
consent — both already baseline-granted, so NO new consent scope. The
menu's ``script.container.ui`` scope lives ONLY in the GENERATED bound
script's manifest (derived by ``build_manifest``), never in
``auth.WORKSPACE_SCOPES`` — identical to ``as_install_doc_menu``.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    guarded_function_block as _guarded_function_block,
    reporter_helper_source as _reporter_helper_source,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    render as _render,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA

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


# The default menu title for a presentation menu (per the service-parity
# spec). Callers may override via the menu_title arg.
_DEFAULT_MENU_TITLE = "Presentation Tools"

# A valid Apps Script (JS) function identifier. Mirrors
# doc_menu._JS_IDENTIFIER_RE.
_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# Apps Script simple-trigger function names a handler must NOT use — the
# generated menu builder owns ``onOpen``; the others are reserved so a
# generated handler can't silently become a trigger.
_RESERVED_FUNCTION_NAMES = frozenset(
    {"onOpen", "onEdit", "onInstall", "onSelectionChange", "doGet", "doPost"}
)


def _js_string(value: str) -> str:
    """Render a Python str as a safe JS string literal (json.dumps).

    Same helper as doc_menu._js_string — JSON string syntax is a subset
    of JS string syntax, so quotes / backslashes / newlines / control
    chars are escaped and a label can't break out of the literal.
    """
    return json.dumps(value)


def build_menu_script(menu_title: str, items: list[dict[str, str]]) -> str:
    """Generate the ``.gs`` source for a Slides custom menu (PURE).

    Produces an ``onOpen(e)`` that builds ``menu_title`` via
    ``SlidesApp.getUi()`` with one ``.addItem(label, function_name)`` per
    entry then ``.addToUi()``, followed by each item's handler function.
    Deterministic: same input → byte-identical output. The ONLY
    difference from ``doc_menu.build_menu_script`` is ``SlidesApp.getUi()``
    in place of ``DocumentApp.getUi()``.

    Args:
        menu_title: the menu's display label in the presentation's menu
            bar.
        items: the validated, normalized item list — each a dict with
            ``label``, ``function_name``, and ``function_body``.

    Returns:
        The complete ``.gs`` source as a string. Labels are JS string
        literals, function names are verified identifiers, handler bodies
        are emitted verbatim inside their function braces.
    """
    lines: list[str] = [
        "// Auto-generated by appscriptly as_install_slides_menu.",
        "// Installs a persistent custom menu into this Google Slides deck.",
        "// Runs on Google's infrastructure — no Claude in the loop.",
        "",
        "function onOpen(e) {",
        "  var ui = SlidesApp.getUi();",
        f"  ui.createMenu({_js_string(menu_title)})",
    ]
    # Chain one .addItem(...) per menu item onto the createMenu builder.
    for item in items:
        label = _js_string(item["label"])
        fn = item["function_name"]  # validated identifier — safe unquoted
        lines.append(f"    .addItem({label}, {_js_string(fn)})")
    lines.append("    .addToUi();")
    lines.append("}")

    # Emit each handler function, WRAPPED in the appscriptly failure
    # reporter: a menu-item handler that throws emails the owner
    # (best-effort) then rethrows, so the failure is not lost (gap #5). The
    # caller-authored body runs inside the try. onOpen is left unwrapped —
    # it is a simple trigger (limited-auth, no MailApp) that only builds
    # the menu.
    for item in items:
        lines.append("")
        lines.append(
            _guarded_function_block(item["function_name"], item["function_body"])
        )

    # Define the failure reporter once — every wrapped handler calls it.
    lines.append("")
    lines.append(_reporter_helper_source().rstrip("\n"))

    return "\n".join(lines) + "\n"


def _validate_items(items: Any) -> list[dict[str, str]]:
    """Validate + normalize the ``items`` arg (PURE).

    Identical contract to doc_menu._validate_items: returns a list of
    ``{label, function_name, function_body}`` dicts; raises ``ValueError``
    on any malformed entry, naming the offending index + missing field.
    """
    if not isinstance(items, list):
        raise ValueError(
            f"items must be a list of "
            f"{{label, function_name, function_body}} entries, got "
            f"{type(items).__name__}."
        )
    if not items:
        raise ValueError(
            "items must contain at least one menu item "
            "(a menu with no items can't be installed)."
        )

    out: list[dict[str, str]] = []
    seen_fns: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"items[{i}] must be a dict with 'label', "
                f"'function_name', and 'function_body', got "
                f"{type(item).__name__}."
            )
        label = item.get("label")
        fn = item.get("function_name")
        body = item.get("function_body")

        if not label or not isinstance(label, str):
            raise ValueError(
                f"items[{i}] is missing a non-empty string 'label' "
                f"(the text shown in the menu)."
            )
        if not fn or not isinstance(fn, str):
            raise ValueError(
                f"items[{i}] is missing a non-empty string "
                f"'function_name' (the .gs function the item runs)."
            )
        if not _JS_IDENTIFIER_RE.match(fn):
            raise ValueError(
                f"items[{i}] function_name {fn!r} is not a valid Apps "
                f"Script function identifier (must start with a letter, "
                f"'_', or '$' and contain only letters, digits, '_', "
                f"or '$')."
            )
        if fn in _RESERVED_FUNCTION_NAMES:
            raise ValueError(
                f"items[{i}] function_name {fn!r} is a reserved Apps "
                f"Script trigger name; the generated menu owns 'onOpen'. "
                f"Pick a different handler name (e.g. "
                f"'{fn}Handler')."
            )
        if fn in seen_fns:
            raise ValueError(
                f"items[{i}] function_name {fn!r} is duplicated — each "
                f"menu item must map to a distinct handler function "
                f"(two functions with the same name collide in the "
                f"generated .gs)."
            )
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ValueError(
                f"items[{i}] function_body must be a string of .gs "
                f"statements, got {type(body).__name__}."
            )

        seen_fns.add(fn)
        out.append({"label": label, "function_name": fn, "function_body": body})
    return out


@workspace_tool(
    title="Install a custom menu into a Google Slides presentation",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment — re-running
    # installs a SECOND menu-builder script bound to the same presentation
    # (it would then show the menu twice on open). NOT idempotent, same
    # convention as as_install_doc_menu / gslides_create_presentation.
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA,
)
def as_install_slides_menu(
    creds: Credentials,
    presentation_id: str,
    items: list[dict[str, str]],
    menu_title: str = _DEFAULT_MENU_TITLE,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a persistent custom menu into a Google Slides presentation.

    USE WHEN: the user wants one-click actions in a specific
    presentation's menu bar that keep working AFTER the conversation ends
    — e.g. "add a 'Refresh charts' menu to this deck" or "give this
    presentation an 'Insert title slide' menu item". The menu installs
    once and persists; re-opening the deck re-shows it. For a ONE-OFF
    edit, use the direct ``gslides_*`` tools instead (no script needed).

    This is the Slides analogue of ``as_install_doc_menu`` — a
    higher-level convenience over ``as_generate_bound_script``: it writes
    the ``onOpen`` menu-builder (via ``SlidesApp.getUi()``) + handler
    boilerplate for you and deploys it as a bound Apps Script — the same
    create→push→deploy machinery the generic primitive uses. The menu and
    its handlers then run on Google's infrastructure, with no Claude in
    the loop.

    Each entry in ``items`` becomes a menu item that runs the given Apps
    Script function:

      * ``label`` — the text shown in the menu (e.g. "Refresh charts").
      * ``function_name`` — the handler function the item invokes. Must
        be a valid JS identifier and not a reserved Apps Script trigger
        name (``onOpen`` is owned by the generated builder). Must be
        unique across items.
      * ``function_body`` — the ``.gs`` statements that run when the item
        is clicked (Claude authors these — e.g.
        ``SlidesApp.getActivePresentation().getSlides()[0]
        .insertTextBox('Hi');``). May be empty for a placeholder handler.

    Args:
        presentation_id: the Drive ID of the Google Slides presentation to
            install the menu into (the ID part of the deck's URL). The
            menu is bound to THIS presentation only.
        items: list of ``{label, function_name, function_body}`` — at
            least one. Validated client-side; a malformed entry is
            rejected before any API call.
        menu_title: the menu's display label in the presentation's menu
            bar. Defaults to ``"Presentation Tools"``. Non-empty.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated name derived from the menu title.
        on_conflict: what to do when an automation from THIS tool already
            exists on this presentation. "new" (the default) always
            installs a fresh one (which can leave duplicate menus);
            "replace" uninstalls the prior install(s) on this presentation
            first (no duplicate, no orphan); "skip" returns the existing
            install unchanged instead of adding a duplicate. The match is
            keyed by (this tool, this container) via appscriptly's
            automation ledger.

    Returns:
        ``{script_id, deployment_id, presentation_id, menu_title,
        item_count, project_url}`` plus ``on_conflict`` (echoed),
        ``reused_existing`` (True when ``on_conflict="skip"`` returned a
        prior install), and ``replaced_count`` (prior installs removed for
        ``on_conflict="replace"``). ``project_url`` deep-links to the
        script editor (``https://script.google.com/d/{script_id}/edit``)
        so the user can inspect / tweak the generated menu + handlers.

    Raises:
        ValueError: empty ``menu_title``, no ``items``, or a malformed
            item (missing/invalid ``label`` / ``function_name`` /
            ``function_body``). Rejected client-side before any API call.
        ToolError: any Apps Script / Drive API error — the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``.

    Choreography: get ``presentation_id`` from the user's URL, from a
    prior ``gslides_create_presentation`` call, or from
    ``gdocs_find_file``. After this returns, open (or reload) the
    presentation — the menu appears in the menu bar. (The first call in a
    session may surface a Google consent prompt for the Apps Script scopes
    if not yet granted; they're in the baseline scope set, so most users
    won't see a second consent.)
    """
    # 1. Validate inputs client-side (cheap rejection before any I/O).
    if not menu_title or not menu_title.strip():
        raise ValueError(
            "menu_title cannot be empty — it's the label shown in the "
            "presentation's menu bar."
        )
    validated_items = _validate_items(items)

    # 2. Codegen via the recipe registry (_recipes.py) — the SINGLE source
    #    for this tool's .gs body + manifest. render() runs the same
    #    build_menu_script and threads the same menu manifest plan
    #    (container_data_scope("slides") + add_mail_scope for the failure
    #    reporter) this tool used to inline; the per-entry byte-identity pins
    #    guarantee the output is unchanged.
    spec = _RECIPES["as_install_slides_menu"]
    params = {
        "presentation_id": presentation_id,
        "menu_title": menu_title,
        "items": validated_items,
        "name": name,
    }
    rendered = _render(spec, params)

    # 3. Deploy via the SAME machinery as as_generate_bound_script:
    #    create bound project → push content → cut version + deploy.
    #    The binding (parentId=presentation_id) attaches the menu to THIS
    #    deck. container_kind is known ("slides") — a SlidesApp menu is
    #    Slides-specific — so no auto-detection round-trip is needed.
    result = _mint_bound_automation(
        creds,
        tool=spec.name,
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
        "menu_title": menu_title,
        "item_count": len(validated_items),
        "project_url": f"https://script.google.com/d/{script_id}/edit",
    }
