"""Recipe registry: the 13 bound-automation generators expressed as DATA.

Wave 2 (ROADMAP recipe lever). Every ``as_install_*`` bound generator is a
thin composition over the SAME spine wave 1 already built — the shared mint
(``_lifecycle.mint_bound_automation``), the shared manifest builder
(``api.build_manifest``), the shared observability seams
(``_observability``), and the shared activation contract
(``activation.build_activation_fields``). The generators differ only in a
handful of DATA points: which pure ``build_*`` produces the ``.gs`` body,
which small manifest-plan dict feeds ``build_manifest``, which container
kind they bind to, whether they thread the failure reporter, which
installable-trigger handler names they record, and what activation model
they carry.

This module tabulates those data points as one ``RecipeSpec`` per recipe and
exposes a pure ``render`` that reproduces a generator's ``(script_body,
manifest, handler_functions)`` from ``(spec, params)``. It registers NO
tools (underscore-prefixed, so tool auto-discovery skips it, exactly like
``_lifecycle`` / ``_observability``).

**The byte-identity contract (this wave's spine).** For a fixed param set,
``render(spec, params)`` MUST equal the CURRENT generator's output
byte-for-byte — the ``.gs`` body, the manifest dict (including the private
``__plan__`` echo ``build_manifest`` adds), AND the recorded handler names.
The per-entry identity pins in
``tests/unit/services/apps_script/test_recipes_registry.py`` capture what
the REAL tool passes to ``mint_bound_automation`` and assert equality, so an
entry that drifts from its generator fails loudly. Until a later stream adds
value on top, the registry must not change one byte of any generated
artifact.

**Design notes.**

- The pure ``build_*`` builders are the genuine codegen (hundreds of lines
  of ``.gs`` synthesis); the registry REFERENCES them (never duplicates
  them) via LAZY imports inside the ``build`` callables. Lazy import is
  deliberate: it keeps this module import-acyclic even after the wrapper
  migration (Stream 3) makes the generator modules import ``render`` from
  here — the generator import resolves at call time, by which point both
  modules are fully loaded.
- The shared, leaf-level seams (``build_manifest`` / ``container_data_scope``
  from ``api``; ``add_mail_scope`` / ``guard_name_for`` from
  ``_observability``) are imported at module top: those modules never import
  a generator or this module, so there is no cycle.
- The generated per-script OAuth scope URLs and the Tasks advanced-service
  block are held as local constants / a local transform here (leaf values
  and an ~8-line merge). Re-declaring them keeps the registry self-contained
  and the S3 import graph acyclic; the identity pins guarantee they match
  every generator exactly.
- Scope-neutral: every scope named here lands ONLY in a GENERATED manifest
  via ``build_manifest``; the registry NEVER touches ``auth.WORKSPACE_SCOPES``
  or the connector's consent. The restricted-scope guard in ``build_manifest``
  is untouched (no recipe declares a restricted scope).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from appscriptly.services.apps_script._observability import (
    add_mail_scope,
    guard_name_for,
)
from appscriptly.services.apps_script.api import (
    build_manifest,
    container_data_scope,
)
from appscriptly.tool_schemas import (
    AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
    AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA,
    AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA,
    AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA,
    AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA,
    AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA,
    AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA,
    AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA,
    AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA,
    AS_INSTALL_SHEET_MENU_OUTPUT_SCHEMA,
    AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA,
    AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA,
    AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA,
)

# A params bag is a plain dict — the recipe's install inputs (the keys the
# wrapper tool's signature would carry, minus the injected ``creds``).
Params = dict[str, Any]


# ---------------------------------------------------------------------
# Generated-manifest OAuth scope URLs (leaf constants; identity-pinned).
# Each mirrors the same-named constant in its generator module. They land
# ONLY in a GENERATED manifest via build_manifest, never in the connector's
# own consent. (MAIL_SCOPE is threaded via the imported add_mail_scope.)
# ---------------------------------------------------------------------
_SCRIPTAPP_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
_CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
_FORMS_SCOPE = "https://www.googleapis.com/auth/forms"
_PRESENTATIONS_SCOPE = "https://www.googleapis.com/auth/presentations"
_URLFETCH_SCOPE = "https://www.googleapis.com/auth/script.external_request"

# The Tasks advanced-service dependency block task_rollover merges into its
# manifest AFTER build_manifest (build_manifest emits only timeZone /
# runtimeVersion / oauthScopes). Mirrors task_rollover._TASKS_ADVANCED_SERVICE.
_TASKS_ADVANCED_SERVICE = {
    "userSymbol": "Tasks",
    "serviceId": "tasks",
    "version": "v1",
}

# The first ``function NAME(`` declaration in a caller-authored body — the
# name the installable-trigger classes wire (via guard_name_for) and record.
# Identical to the ``_extract_handler_name`` regex the generator builders use.
_FUNCTION_NAME_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _first_function_name(body: str) -> str:
    """Return the first ``function NAME(`` declaration name in ``body``.

    Mirrors the generator builders' ``_extract_handler_name``: a trigger
    handler must be a NAMED declaration (``ScriptApp.newTrigger`` takes the
    handler name as a string). Raises ``ValueError`` when none is found.
    """
    match = _FUNCTION_NAME_RE.search(body)
    if match is None:
        raise ValueError(
            "expected a NAMED function declaration (e.g. "
            "`function handler(e) { ... }`) to derive the trigger handler."
        )
    return match.group(1)


def _with_tasks_advanced_service(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``manifest`` with the Tasks advanced service enabled.

    Mirrors ``task_rollover._with_tasks_advanced_service`` byte-for-byte:
    merges (not overwrites) ``dependencies.enabledAdvancedServices`` and
    de-dups by ``serviceId`` so it is idempotent. ``set_project_content``
    serializes ``dependencies`` through (it strips only ``__plan__``).
    """
    merged = dict(manifest)
    deps = dict(merged.get("dependencies") or {})
    services = list(deps.get("enabledAdvancedServices") or [])
    if not any(
        s.get("serviceId") == _TASKS_ADVANCED_SERVICE["serviceId"]
        for s in services
    ):
        services.append(dict(_TASKS_ADVANCED_SERVICE))
    deps["enabledAdvancedServices"] = services
    merged["dependencies"] = deps
    return merged


def _project_name(template: str) -> Callable[[Params], str]:
    """Build the default-project-name deriver for a recipe.

    Returns ``params['name']`` when the caller supplied one, else
    ``template`` formatted with the params (e.g. ``"{schedule}"`` /
    ``"{menu_title}"``). ``str.format`` does a single non-recursive pass, so
    a value containing ``{`` cannot inject a placeholder. Templates use the
    ASCII hyphen only (never an em/en dash — the name is consent-visible).
    """

    def derive(params: Params) -> str:
        supplied = params.get("name")
        if supplied:
            return str(supplied)
        return template.format(**params)

    return derive


# ---------------------------------------------------------------------
# The recipe entry format + the pure render.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class RecipeSpec:
    """One bound-automation generator, expressed as data.

    Fields with a codegen role (``build`` / ``manifest_plan`` /
    ``manifest_transform`` / ``handler_derivation`` / ``pre_mint``) are the
    parts ``render`` composes; the rest are metadata a wrapper / an inventory
    browse tool reads. The callables take a ``Params`` bag (the recipe's
    install inputs) and are PURE except ``pre_mint`` (video_deck's server-side
    batch + HMAC-token mint).
    """

    name: str  # ledger / on_conflict / tool key (== the wrapper tool name)
    title: str  # human title for a browse tool (NO em/en dash)
    summary: str  # one-line description for a browse tool (NO em/en dash)
    container_kind: str  # sheets | docs | slides | forms
    build: Callable[[Params], str]  # pure .gs builder (wraps the existing build_*)
    manifest_plan: Callable[[Params, str], dict[str, Any] | None]  # build_manifest INPUT
    observability: str  # "reporter" | "none"
    activation_model: str  # from lifecycle_tools._ACTIVATION_MODEL (one source now)
    activation_function: str | None  # installTrigger | renderFrames | gradeResponses | ...
    project_name: Callable[[Params], str]  # default project-name deriver (NO em/en dash)
    input_schema: dict[str, Any]  # JSON schema for the install params
    output_schema: dict[str, Any]  # the existing AS_*_OUTPUT_SCHEMA constant
    example_params: tuple[Params, ...]  # representative + edge inputs the harness renders
    version: str  # recipe codegen version (bumped when a builder changes)
    # Optional codegen hooks (most recipes leave these None):
    handler_derivation: Callable[[Params], list[str]] | None = None  # D/E trigger classes
    manifest_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None  # task_rollover
    pre_mint: Callable[[Params], Params] | None = None  # video_deck ONLY (batch + token)


class RenderResult(NamedTuple):
    """The three generated artifacts ``render`` reproduces for a recipe.

    ``manifest`` is the ``build_manifest`` OUTPUT — it carries the private
    ``__plan__`` echo (``set_project_content`` strips it at push time and
    ``compute_automation_hash`` strips it for hashing), exactly as the
    generator hands it to ``mint_bound_automation``.
    """

    script_body: str
    manifest: dict[str, Any]
    handler_functions: list[str]


def render(spec: RecipeSpec, params: Params) -> RenderResult:
    """Reproduce ``(script_body, manifest, handler_functions)`` for a recipe.

    PURE. Runs the recipe's ``build`` (the ``.gs`` body), feeds its
    ``manifest_plan`` to ``build_manifest`` (then an optional
    ``manifest_transform``), and derives the installable-trigger handler
    names (empty for the non-trigger classes). Does NOT run ``pre_mint`` —
    the wrapper applies that impure hook first (video_deck only) so ``render``
    stays deterministic; pass the already-augmented params.
    """
    script_body = spec.build(params)
    manifest = build_manifest(spec.manifest_plan(params, spec.container_kind))
    if spec.manifest_transform is not None:
        manifest = spec.manifest_transform(manifest)
    handlers = (
        spec.handler_derivation(params)
        if spec.handler_derivation is not None
        else []
    )
    return RenderResult(
        script_body=script_body,
        manifest=manifest,
        handler_functions=list(handlers),
    )


# ---------------------------------------------------------------------
# Params validation + container-id derivation. Registry-level helpers that
# operate on a recipe's input_schema (metadata, NOT the byte-identity
# contract). Shared by the generic installer (``as_install_recipe``) and the
# update regeneration path (``as_update_automation``) so both validate an
# install-params bag against the recipe's declared inputs the same way.
# ---------------------------------------------------------------------


def container_param_of(spec: RecipeSpec) -> str:
    """Return the params key holding the recipe's container Drive id.

    That is the FIRST required input of every recipe (``doc_id`` / ``sheet_id``
    / ``form_id`` / ``presentation_id``) -- the value the typed wrapper hands
    ``mint_bound_automation`` as ``container_id``. The generic installer derives
    ``container_id`` this way; the per-recipe mapping is pinned in
    ``test_install_recipe`` so a future recipe that reorders its required list
    cannot silently mis-bind.
    """
    return spec.input_schema["required"][0]


def _param_type_ok(value: Any, declared: str | None) -> bool:
    """True if ``value`` satisfies the JSON-schema ``declared`` primitive type.

    Returns True for an unknown / unmapped type (only the primitives the recipe
    input schemas actually use are checked; presence + non-null are enforced by
    the caller). ``integer`` / ``number`` reject ``bool`` explicitly because
    ``bool`` is an ``int`` subclass.
    """
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    simple: dict[str, type] = {
        "string": str, "array": list, "object": dict, "boolean": bool,
    }
    expected = simple.get(declared) if declared else None
    return expected is None or isinstance(value, expected)


def required_param_offenders(spec: RecipeSpec, params: Params) -> list[str]:
    """One offender string per REQUIRED input that is missing, null, or the
    wrong JSON type in ``params``; an empty list means every required input is
    valid.

    Optional inputs are not checked (they may legitimately be absent or null).
    Each offender names the key -- and, for a type error, the expected vs actual
    type. This is the shared validation the generic installer surfaces before an
    install AND ``as_update_automation`` surfaces before a recipe regeneration,
    so a bad input becomes a clean ``ValueError`` naming the offender instead of
    a cryptic ``TypeError`` from deep inside a generator builder.
    """
    schema = spec.input_schema
    props = schema.get("properties", {})
    offenders: list[str] = []
    for key in schema.get("required", []):
        declared = props.get(key, {}).get("type")
        if key not in params or params[key] is None:
            offenders.append(f"{key} (required, must not be null)")
        elif not _param_type_ok(params[key], declared):
            offenders.append(
                f"{key} (expected {declared}, got {type(params[key]).__name__})"
            )
    return offenders


# ---------------------------------------------------------------------
# build callables (LAZY imports of the pure generator builders).
# Each returns the exact .gs body the generator produces for these params.
# ---------------------------------------------------------------------


def _build_doc_menu(p: Params) -> str:
    from appscriptly.services.apps_script.doc_menu import build_menu_script

    return build_menu_script(p["menu_title"], p["items"])


def _build_sheet_menu(p: Params) -> str:
    from appscriptly.services.apps_script.sheet_menu import build_menu_script

    return build_menu_script(p["menu_title"], p["items"])


def _build_slides_menu(p: Params) -> str:
    from appscriptly.services.apps_script.slides_menu import build_menu_script

    return build_menu_script(p["menu_title"], p["items"])


def _build_custom_function(p: Params) -> str:
    from appscriptly.services.apps_script.custom_function import (
        build_custom_function_script,
    )

    return build_custom_function_script(
        p["function_name"], p["function_body"], p.get("description")
    )


def _build_dashboard(p: Params) -> str:
    from appscriptly.services.apps_script.sheet_dashboard import (
        build_dashboard_script_body,
    )

    return build_dashboard_script_body(
        p["refresh_function_body"], p["schedule"], p["hour"], p.get("dashboard_note")
    )[0]


def _build_calendar_sync(p: Params) -> str:
    from appscriptly.services.apps_script.sheet_dashboard import (
        build_dashboard_script_body,
    )

    return build_dashboard_script_body(
        p["sync_function_body"], p["schedule"], p["hour"], p.get("sync_note")
    )[0]


def _build_task_rollover(p: Params) -> str:
    from appscriptly.services.apps_script.sheet_dashboard import (
        build_dashboard_script_body,
    )

    return build_dashboard_script_body(
        p["task_function_body"], p["schedule"], p["hour"], p.get("task_note")
    )[0]


def _build_edit_trigger(p: Params) -> str:
    from appscriptly.services.apps_script.edit_trigger import (
        build_edit_trigger_script_body,
    )

    return build_edit_trigger_script_body(
        p["handler_function_body"], p["sheet_id"], p.get("handler_note")
    )[0]


def _build_form_handler(p: Params) -> str:
    from appscriptly.services.apps_script.form_handler import (
        build_form_handler_script_body,
    )

    return build_form_handler_script_body(
        p["handler_function_body"], p["form_id"], p.get("handler_note")
    )[0]


def _build_grade(p: Params) -> str:
    from appscriptly.services.apps_script.grade_form_responses import (
        build_grade_script_body,
    )

    return build_grade_script_body(p["scoring_function_body"], p["menu_title"])[0]


def _build_refresh(p: Params) -> str:
    from appscriptly.services.apps_script.refresh_linked_slides import (
        build_refresh_script_body,
    )

    return build_refresh_script_body(p["menu_title"])


def _build_video_deck(p: Params) -> str:
    from appscriptly.services.apps_script.video_deck import build_video_deck_script

    return build_video_deck_script(
        p["presentation_id"], p["upload_base_url"], p["upload_token"]
    )


# ---------------------------------------------------------------------
# manifest_plan callables — the small high-level dict each generator hands
# build_manifest. Scope ORDER matches the generator exactly (build_manifest
# prepends capability-derived scopes, then these explicit ones, add_mail last).
# ---------------------------------------------------------------------


def _plan_menu(p: Params, kind: str) -> dict[str, Any]:
    return {
        "menu": [
            {"name": it["label"], "function_name": it["function_name"]}
            for it in p["items"]
        ],
        "oauth_scopes": add_mail_scope([container_data_scope(kind)]),
    }


def _plan_bare(p: Params, kind: str) -> None:
    # custom_function: bare V8 manifest, no scope (build_manifest(None)).
    return None


def _time_trigger_plan(
    *extra_scopes: str,
) -> Callable[[Params, str], dict[str, Any]]:
    """A time-trigger manifest plan: script.scriptapp is DERIVED from the
    ``time`` trigger; ``extra_scopes`` (calendar / tasks / none) precede the
    container data scope, then add_mail_scope appends the reporter's send scope.
    """

    def plan(p: Params, kind: str) -> dict[str, Any]:
        return {
            "triggers": [{"type": "time", "schedule": p["schedule"]}],
            "oauth_scopes": add_mail_scope(
                [*extra_scopes, container_data_scope(kind)]
            ),
        }

    return plan


def _plan_edit_trigger(p: Params, kind: str) -> dict[str, Any]:
    # onEdit is NOT a capability build_manifest derives scriptapp for (only
    # "time" is), so scriptapp is declared explicitly alongside the data scope.
    return {
        "triggers": [{"type": "edit"}],
        "oauth_scopes": add_mail_scope(
            [_SCRIPTAPP_SCOPE, container_data_scope(kind)]
        ),
    }


def _plan_form_handler(p: Params, kind: str) -> dict[str, Any]:
    # onFormSubmit has no build_manifest trigger type; scriptapp + the Form's
    # data scope are declared explicitly (no triggers/menu key -> empty plan).
    return {
        "oauth_scopes": add_mail_scope(
            [_SCRIPTAPP_SCOPE, container_data_scope(kind)]
        )
    }


def _plan_contact_sync(p: Params, kind: str) -> dict[str, Any]:
    return {
        "oauth_scopes": add_mail_scope(
            [_SCRIPTAPP_SCOPE, _CONTACTS_SCOPE, container_data_scope(kind)]
        )
    }


def _plan_grade(p: Params, kind: str) -> dict[str, Any]:
    # onOpen menu -> script.container.ui (derived); submitGrades needs FULL forms.
    return {
        "menu": [{"name": "Grade responses", "function_name": "gradeResponses"}],
        "oauth_scopes": add_mail_scope([_FORMS_SCOPE]),
    }


def _plan_refresh(p: Params, kind: str) -> dict[str, Any]:
    return {
        "menu": [
            {"name": "Refresh linked slides", "function_name": "refreshLinkedSlides"}
        ],
        "oauth_scopes": add_mail_scope([_PRESENTATIONS_SCOPE]),
    }


def _plan_video_deck(p: Params, kind: str) -> dict[str, Any]:
    # The onOpen "Video" menu is NOT declared to build_manifest (no container.ui);
    # the renderer needs Slides read + UrlFetch (getThumbnail + POST).
    return {
        "oauth_scopes": add_mail_scope([_PRESENTATIONS_SCOPE, _URLFETCH_SCOPE])
    }


# ---------------------------------------------------------------------
# handler_derivation — the installable-trigger handler names the ledger
# records (the GUARD wrapper name, so uninstall's self-disarm reaper matches).
# ---------------------------------------------------------------------


def _handlers_from(body_key: str) -> Callable[[Params], list[str]]:
    def derive(p: Params) -> list[str]:
        return [guard_name_for(_first_function_name(p[body_key]))]

    return derive


# ---------------------------------------------------------------------
# pre_mint — video_deck ONLY. Impure: mints a server-side frames batch + a
# user-bound single-use HMAC upload token, then injects them into params so
# the pure builder can embed them. Mirrors as_generate_video_deck steps 3-4.
# ---------------------------------------------------------------------


def _video_deck_pre_mint(p: Params) -> Params:
    from appscriptly.credentials import current_user_id_or_none
    from appscriptly.oauth_google import resolve_runtime_oauth_config
    from appscriptly.services.apps_script._frames_staging import (
        new_batch_id,
        sign_frames_batch,
    )

    server_base_url = resolve_runtime_oauth_config()["base_url"]
    batch_id = new_batch_id()
    upload_token = sign_frames_batch(batch_id, user_id=current_user_id_or_none())
    upload_base_url = f"{server_base_url}/upload/frames/{batch_id}"
    return {
        **p,
        "batch_id": batch_id,
        "upload_token": upload_token,
        "upload_base_url": upload_base_url,
    }


# ---------------------------------------------------------------------
# Shared JSON-schema fragments + a builder for the per-recipe input schemas.
# These describe the install params (the wrapper tool's signature minus creds)
# for a browse tool / a future generic installer; they are not part of the
# byte-identity contract.
# ---------------------------------------------------------------------

_ON_CONFLICT_PROP: dict[str, Any] = {
    "type": "string",
    "enum": ["new", "replace", "skip"],
    "default": "new",
    "description": (
        "What to do when this recipe already installed on this container: "
        "new (fresh install), replace (uninstall prior first), or skip "
        "(reuse the existing install)."
    ),
}
_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "description": "Optional Apps Script project title; defaults to a generated name.",
}
_MENU_ITEMS_PROP: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "description": "Menu items to install.",
    "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Text shown in the menu."},
            "function_name": {
                "type": "string",
                "description": "The .gs handler the item runs (a valid JS identifier).",
            },
            "function_body": {
                "type": "string",
                "description": "The .gs statements the handler runs (may be empty).",
            },
        },
        "required": ["label", "function_name"],
    },
}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------
# Representative example params (the harness renders these; the identity pins
# also drive them through the real tools). Bodies are valid named JS
# declarations; ids are realistic Drive-id shapes.
# ---------------------------------------------------------------------

_EX_SHEET_ID = "1AbC_dEfG-hIjKlMnOpQrStUvWxYz0123456789"
_EX_DOC_ID = "1DoC_iD00-abcdefghijklmnopqrstuvwxyz01234"
_EX_FORM_ID = "1FoRm_iD-abcdefghijklmnopqrstuvwxyz01234"
_EX_PRES_ID = "1PrEs_iD-abcdefghijklmnopqrstuvwxyz01234"

# A multi-item menu that stresses label escaping. It carries every edge the
# now-dropped hand-written Class B harness cases exercised (S3), so those cases
# can be removed without losing coverage: a comma INSIDE a label (exercises the
# non-greedy .addItem-target extraction the harness's menu-integrity gate uses),
# embedded double-quotes + '<' + a backslash + a non-ASCII char (exercises the
# JS-string escaping in the menu builder), an empty handler body (a no-op
# handler is legal), and a '$' in an identifier (a valid JS identifier char).
# Paired with a single-item menu below.
_EX_MENU_ITEMS_MULTI: list[dict[str, str]] = [
    {
        "label": "Recompute totals, now",
        "function_name": "recomputeTotals",
        "function_body": "SpreadsheetApp.getActive().toast('done');",
    },
    {
        "label": 'Quote "block" & <b> \\ café',
        "function_name": "insertBlock",
        "function_body": "var s = 'ok';\nLogger.log(s);",
    },
    {
        "label": "Placeholder",
        "function_name": "third_item$",
        "function_body": "",
    },
]
_EX_MENU_ITEMS_SINGLE: list[dict[str, str]] = [
    {
        "label": "Refresh",
        "function_name": "refreshData",
        "function_body": "Logger.log('refresh');",
    },
]

_EX_REFRESH_BODY = (
    "function refreshDashboard() {\n"
    "  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];\n"
    "  sheet.getRange('A1').setValue(new Date());\n"
    "}"
)
_EX_SYNC_BODY = (
    "function syncEvents() {\n"
    "  var cal = CalendarApp.getDefaultCalendar();\n"
    "  cal.createEvent('Event', new Date(), new Date());\n"
    "}"
)
_EX_TASK_BODY = (
    "function rollOverTasks() {\n"
    "  var lists = Tasks.Tasklists.list().items || [];\n"
    "}"
)
_EX_EDIT_BODY = (
    "function onSheetEdit(e) {\n"
    "  if (e && e.range) { e.range.setNote('edited ' + new Date()); }\n"
    "}"
)
_EX_SUBMIT_BODY = (
    "function onSubmit(e) {\n"
    "  Logger.log('submission: ' + JSON.stringify(e && e.namedValues));\n"
    "}"
)
_EX_CONTACT_BODY = (
    "function onSubmit(e) {\n"
    "  ContactsApp.createContact('First', 'Last', 'person@example.com');\n"
    "}"
)
_EX_SCORER_BODY = (
    "function scoreItem(itemResponse, item) {\n"
    "  if (itemResponse.getResponse() === '42') { itemResponse.setScore(1); }\n"
    "}"
)
_EX_CUSTOM_FN_BODY = (
    "function BRAND_CHECK(input) {\n"
    "  return String(input).toUpperCase().indexOf('ACME') >= 0;\n"
    "}"
)


# ---------------------------------------------------------------------
# THE REGISTRY. One RecipeSpec per bound generator. as_deploy_web_app and
# as_generate_bound_script stay OUT (a deploy primitive / a generic passthrough
# with no template to parameterize).
# ---------------------------------------------------------------------

RECIPES: dict[str, RecipeSpec] = {}


def _register(spec: RecipeSpec) -> None:
    RECIPES[spec.name] = spec


# ---- Class B: custom menus (doc / sheet / slides) -------------------

_register(
    RecipeSpec(
        name="as_install_doc_menu",
        title="Custom menu in a Google Doc",
        summary="Install a persistent onOpen custom menu into a Google Doc, one handler per item.",
        container_kind="docs",
        build=_build_doc_menu,
        manifest_plan=_plan_menu,
        observability="reporter",
        activation_model="menu",
        activation_function=None,
        project_name=_project_name("appscriptly doc menu - {menu_title}"),
        input_schema=_schema(
            {
                "doc_id": {"type": "string", "description": "Drive ID of the Google Doc."},
                "menu_title": {"type": "string", "description": "Menu label in the Doc menu bar."},
                "items": _MENU_ITEMS_PROP,
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["doc_id", "menu_title", "items"],
        ),
        output_schema=AS_INSTALL_DOC_MENU_OUTPUT_SCHEMA,
        example_params=(
            {"doc_id": _EX_DOC_ID, "menu_title": "Contract Tools", "items": _EX_MENU_ITEMS_MULTI},
            {"doc_id": _EX_DOC_ID, "menu_title": "Tools", "items": _EX_MENU_ITEMS_SINGLE},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_sheet_menu",
        title="Custom menu in a Google Sheet",
        summary="Install a persistent onOpen custom menu into a Google Sheet, one handler per item.",
        container_kind="sheets",
        build=_build_sheet_menu,
        manifest_plan=_plan_menu,
        observability="reporter",
        activation_model="menu",
        activation_function=None,
        project_name=_project_name("appscriptly sheet menu - {menu_title}"),
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "menu_title": {"type": "string", "description": "Menu label in the Sheet menu bar."},
                "items": _MENU_ITEMS_PROP,
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "menu_title", "items"],
        ),
        output_schema=AS_INSTALL_SHEET_MENU_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "menu_title": "Budget Tools", "items": _EX_MENU_ITEMS_MULTI},
            {"sheet_id": _EX_SHEET_ID, "menu_title": "Tools", "items": _EX_MENU_ITEMS_SINGLE},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_slides_menu",
        title="Custom menu in a Google Slides presentation",
        summary="Install a persistent onOpen custom menu into a Google Slides deck, one handler per item.",
        container_kind="slides",
        build=_build_slides_menu,
        manifest_plan=_plan_menu,
        observability="reporter",
        activation_model="menu",
        activation_function=None,
        project_name=_project_name("appscriptly slides menu - {menu_title}"),
        input_schema=_schema(
            {
                "presentation_id": {"type": "string", "description": "Drive ID of the presentation."},
                "items": _MENU_ITEMS_PROP,
                "menu_title": {
                    "type": "string",
                    "default": "Presentation Tools",
                    "description": "Menu label in the deck menu bar.",
                },
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["presentation_id", "items"],
        ),
        output_schema=AS_INSTALL_SLIDES_MENU_OUTPUT_SCHEMA,
        example_params=(
            {"presentation_id": _EX_PRES_ID, "menu_title": "Presentation Tools", "items": _EX_MENU_ITEMS_MULTI},
            {"presentation_id": _EX_PRES_ID, "menu_title": "Deck Tools", "items": _EX_MENU_ITEMS_SINGLE},
        ),
        version="1",
    )
)


# ---- Class C: custom spreadsheet function ---------------------------

_register(
    RecipeSpec(
        name="as_install_custom_function",
        title="Custom =FUNCTION() in a Google Sheet",
        summary="Install a bound Apps Script function usable as =FUNCTION_NAME(...) in a Sheet cell.",
        container_kind="sheets",
        build=_build_custom_function,
        manifest_plan=_plan_bare,
        observability="none",
        activation_model="custom_function",
        activation_function=None,
        project_name=_project_name("appscriptly custom function ({function_name})"),
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "function_name": {"type": "string", "description": "Cell-callable name, e.g. BRAND_CHECK."},
                "function_body": {"type": "string", "description": "JS source defining function_name."},
                "description": {"type": "string", "description": "Optional one-line help woven into the JSDoc."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "function_name", "function_body"],
        ),
        output_schema=AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "function_name": "BRAND_CHECK", "function_body": _EX_CUSTOM_FN_BODY},
            {
                "sheet_id": _EX_SHEET_ID,
                "function_name": "BRAND_CHECK",
                "function_body": _EX_CUSTOM_FN_BODY,
                "description": 'Checks brand mention. Edge: */ and "quotes".',
            },
        ),
        version="1",
    )
)


# ---- Class D: time-driven (dashboard / calendar / task) -------------

_register(
    RecipeSpec(
        name="as_install_sheet_dashboard",
        title="Scheduled dashboard refresh in a Google Sheet",
        summary="Install a time-driven trigger that runs a refresh function on a daily/hourly/weekly schedule.",
        container_kind="sheets",
        build=_build_dashboard,
        manifest_plan=_time_trigger_plan(),
        observability="reporter",
        activation_model="scheduled_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly dashboard refresh ({schedule})"),
        handler_derivation=_handlers_from("refresh_function_body"),
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "refresh_function_body": {"type": "string", "description": "Named refresh function declaration."},
                "schedule": {"type": "string", "enum": ["daily", "hourly", "weekly"], "default": "daily"},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23, "default": 6},
                "dashboard_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "refresh_function_body"],
        ),
        output_schema=AS_INSTALL_SHEET_DASHBOARD_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "refresh_function_body": _EX_REFRESH_BODY, "schedule": "daily", "hour": 9},
            {"sheet_id": _EX_SHEET_ID, "refresh_function_body": _EX_REFRESH_BODY, "schedule": "hourly", "hour": 0},
            {
                "sheet_id": _EX_SHEET_ID,
                "refresh_function_body": _EX_REFRESH_BODY,
                "schedule": "weekly",
                "hour": 23,
                "dashboard_note": "Rebuilds the dashboard tab.\nEdge */ in a note.",
            },
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_calendar_sync",
        title="Scheduled Sheet-to-Calendar sync",
        summary="Install a time-driven trigger that syncs a Sheet's rows into Google Calendar events.",
        container_kind="sheets",
        build=_build_calendar_sync,
        manifest_plan=_time_trigger_plan(_CALENDAR_SCOPE),
        observability="reporter",
        activation_model="scheduled_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly calendar sync ({schedule})"),
        handler_derivation=_handlers_from("sync_function_body"),
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "sync_function_body": {"type": "string", "description": "Named CalendarApp sync function."},
                "schedule": {"type": "string", "enum": ["daily", "hourly", "weekly"], "default": "daily"},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23, "default": 6},
                "sync_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "sync_function_body"],
        ),
        output_schema=AS_INSTALL_CALENDAR_SYNC_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "sync_function_body": _EX_SYNC_BODY, "schedule": "daily", "hour": 6},
            {"sheet_id": _EX_SHEET_ID, "sync_function_body": _EX_SYNC_BODY, "schedule": "weekly", "hour": 8},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_task_rollover",
        title="Scheduled Google Tasks automation",
        summary="Install a time-driven trigger that runs Tasks advanced-service orchestration on a schedule.",
        container_kind="sheets",
        build=_build_task_rollover,
        manifest_plan=_time_trigger_plan(_TASKS_SCOPE),
        observability="reporter",
        activation_model="scheduled_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly tasks automation ({schedule})"),
        handler_derivation=_handlers_from("task_function_body"),
        manifest_transform=_with_tasks_advanced_service,
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "task_function_body": {"type": "string", "description": "Named Tasks.* orchestration function."},
                "schedule": {"type": "string", "enum": ["daily", "hourly", "weekly"], "default": "daily"},
                "hour": {"type": "integer", "minimum": 0, "maximum": 23, "default": 6},
                "task_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "task_function_body"],
        ),
        output_schema=AS_INSTALL_TASK_ROLLOVER_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "task_function_body": _EX_TASK_BODY, "schedule": "daily", "hour": 6},
            {"sheet_id": _EX_SHEET_ID, "task_function_body": _EX_TASK_BODY, "schedule": "hourly", "hour": 0},
        ),
        version="1",
    )
)


# ---- Class E: reactive triggers (edit / form / contact) -------------

_register(
    RecipeSpec(
        name="as_install_edit_trigger",
        title="Reactive onEdit trigger in a Google Sheet",
        summary="Install an installable onEdit trigger that runs a handler whenever the Sheet is edited.",
        container_kind="sheets",
        build=_build_edit_trigger,
        manifest_plan=_plan_edit_trigger,
        observability="reporter",
        activation_model="reactive_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly onEdit trigger"),
        handler_derivation=_handlers_from("handler_function_body"),
        input_schema=_schema(
            {
                "sheet_id": {"type": "string", "description": "Drive ID of the Google Sheet."},
                "handler_function_body": {"type": "string", "description": "Named onEdit handler declaration."},
                "handler_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["sheet_id", "handler_function_body"],
        ),
        output_schema=AS_INSTALL_EDIT_TRIGGER_OUTPUT_SCHEMA,
        example_params=(
            {"sheet_id": _EX_SHEET_ID, "handler_function_body": _EX_EDIT_BODY},
            {"sheet_id": _EX_SHEET_ID, "handler_function_body": _EX_EDIT_BODY, "handler_note": "React to edits."},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_form_handler",
        title="Reactive onFormSubmit handler for a Google Form",
        summary="Install an installable onFormSubmit trigger that runs a handler on every submission.",
        container_kind="forms",
        build=_build_form_handler,
        manifest_plan=_plan_form_handler,
        observability="reporter",
        activation_model="reactive_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly onFormSubmit handler"),
        handler_derivation=_handlers_from("handler_function_body"),
        input_schema=_schema(
            {
                "form_id": {"type": "string", "description": "Drive ID of the Google Form."},
                "handler_function_body": {"type": "string", "description": "Named onFormSubmit handler declaration."},
                "handler_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["form_id", "handler_function_body"],
        ),
        output_schema=AS_INSTALL_FORM_HANDLER_OUTPUT_SCHEMA,
        example_params=(
            {"form_id": _EX_FORM_ID, "handler_function_body": _EX_SUBMIT_BODY},
            {"form_id": _EX_FORM_ID, "handler_function_body": _EX_SUBMIT_BODY, "handler_note": "React to submissions."},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_install_contact_sync",
        title="Reactive contact-sync handler for a Google Form",
        summary="Install an onFormSubmit trigger whose handler creates/updates a contact via ContactsApp.",
        container_kind="forms",
        build=_build_form_handler,  # contact_sync reuses form_handler's builder
        manifest_plan=_plan_contact_sync,
        observability="reporter",
        activation_model="reactive_trigger",
        activation_function="installTrigger",
        project_name=_project_name("appscriptly contact sync"),
        handler_derivation=_handlers_from("handler_function_body"),
        input_schema=_schema(
            {
                "form_id": {"type": "string", "description": "Drive ID of the Google Form."},
                "handler_function_body": {"type": "string", "description": "Named ContactsApp handler declaration."},
                "handler_note": {"type": "string", "description": "Optional leading comment in the script."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["form_id", "handler_function_body"],
        ),
        output_schema=AS_INSTALL_CONTACT_SYNC_OUTPUT_SCHEMA,
        example_params=(
            {"form_id": _EX_FORM_ID, "handler_function_body": _EX_CONTACT_BODY},
            {"form_id": _EX_FORM_ID, "handler_function_body": _EX_CONTACT_BODY, "handler_note": "Sync leads to contacts."},
        ),
        version="1",
    )
)


# ---- Class F: on-demand menu actions (grade / refresh) --------------

_register(
    RecipeSpec(
        name="as_grade_form_responses",
        title="Grade Google Form quiz responses",
        summary="Install a bound grader (onOpen menu + gradeResponses) that pushes computed scores onto responses.",
        container_kind="forms",
        build=_build_grade,
        manifest_plan=_plan_grade,
        observability="reporter",
        activation_model="menu_action",
        activation_function="gradeResponses",
        project_name=_project_name("appscriptly form grader"),
        input_schema=_schema(
            {
                "form_id": {"type": "string", "description": "Drive ID of the Google Form (quiz)."},
                "scoring_function_body": {"type": "string", "description": "Named per-question scorer declaration."},
                "menu_title": {"type": "string", "default": "Quiz Tools", "description": "Menu label in the form editor."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["form_id", "scoring_function_body"],
        ),
        output_schema=AS_GRADE_FORM_RESPONSES_OUTPUT_SCHEMA,
        example_params=(
            {"form_id": _EX_FORM_ID, "scoring_function_body": _EX_SCORER_BODY, "menu_title": "Quiz Tools"},
            {"form_id": _EX_FORM_ID, "scoring_function_body": _EX_SCORER_BODY, "menu_title": 'Quiz "Pro" Tools'},
        ),
        version="1",
    )
)

_register(
    RecipeSpec(
        name="as_refresh_linked_slides",
        title="Refresh linked slides in a presentation",
        summary="Install a bound refresher (onOpen menu + refreshLinkedSlides) that re-syncs linked slides.",
        container_kind="slides",
        build=_build_refresh,
        manifest_plan=_plan_refresh,
        observability="reporter",
        activation_model="menu_action",
        activation_function="refreshLinkedSlides",
        project_name=_project_name("appscriptly refresh linked slides"),
        input_schema=_schema(
            {
                "presentation_id": {"type": "string", "description": "Drive ID of the Google Slides presentation."},
                "menu_title": {
                    "type": "string",
                    "default": "Presentation Tools",
                    "description": "Menu label in the deck menu bar.",
                },
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["presentation_id"],
        ),
        output_schema=AS_REFRESH_LINKED_SLIDES_OUTPUT_SCHEMA,
        example_params=(
            {"presentation_id": _EX_PRES_ID, "menu_title": "Presentation Tools"},
            {"presentation_id": _EX_PRES_ID, "menu_title": 'Deck "Sync" Tools'},
        ),
        version="1",
    )
)


# ---- Class G: slides-to-video renderer (pre_mint) -------------------

_register(
    RecipeSpec(
        name="as_generate_video_deck",
        title="Render a Slides deck to video frames",
        summary="Install a bound renderer (onOpen Video menu + renderFrames) that POSTs each slide as a PNG frame.",
        container_kind="slides",
        build=_build_video_deck,
        manifest_plan=_plan_video_deck,
        observability="reporter",
        activation_model="menu_action",
        activation_function="renderFrames",
        project_name=_project_name("appscriptly video deck renderer"),
        pre_mint=_video_deck_pre_mint,
        input_schema=_schema(
            {
                "presentation_id": {"type": "string", "description": "Drive ID of the Google Slides deck to render."},
                "name": _NAME_PROP,
                "on_conflict": _ON_CONFLICT_PROP,
            },
            ["presentation_id"],
        ),
        output_schema=AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
        # example_params carry the POST-pre_mint shape so the harness can render
        # spec.build(params) directly (the pure builder embeds these strings).
        example_params=(
            {
                "presentation_id": _EX_PRES_ID,
                "upload_base_url": "https://mcp.appscriptly.com/upload/frames/testbatch01",
                "upload_token": "1700000000.testnonce.dGVzdA.abc123sig",
            },
        ),
        version="1",
    )
)
