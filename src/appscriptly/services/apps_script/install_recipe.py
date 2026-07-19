"""``as_install_recipe`` - the generic, catalog-driven recipe installer.

One installer that takes a recipe NAME + a ``params`` bag and installs that
recipe, dispatching through the SAME render + mint path the typed
``as_install_*`` wrappers use (a byte-identical result). It is the
programmatic / catalog-driven companion to ``as_list_recipes``: browse the
catalog, then install a chosen recipe by name.

**The typed tools remain first-class.** FastMCP derives each tool's INPUT
schema from its Python signature, so the typed ``as_install_*`` tools carry
the full per-field, self-documenting input contract (enums, defaults, nested
item shapes). A generic ``params: object`` cannot. So for everyday use the
typed tool is preferred; this tool is the escape hatch for programmatic /
catalog-driven installs where the recipe is chosen at runtime by name.

**Validation is registry-level.** Required-field + type checks against the
recipe's declared inputs (the SAME validator ``as_update_automation`` uses for
its regeneration overrides), plus an unknown-key check so a typo is rejected
rather than silently defaulted. It does NOT re-run each typed tool's deeper
pre-flight validation (menu-item identifier checks, a Drive mimeType
round-trip confirming a custom function's container is a Sheet, ...) and it
trusts the caller's container id + kind; a deeper mistake surfaces later as a
clean ``ValueError`` from the code generator or an Apps Script push error,
never as a silent bad install.

**The video-deck renderer is refused here.** It is the sole recipe carrying an
impure per-install ``pre_mint`` hook (a fresh single-use HMAC upload token, a
Slides-container check, and a frames-batch handoff the generic result shape
cannot carry), so it is refused and pointed at its typed tool
``as_generate_video_deck`` -- the same posture ``as_update_automation`` takes
for a video-deck row (a per-install token cannot be reproduced generically).

Own module (an auto-discovered non-underscore leaf) per the
one-tool-per-feature-file convention; the recipe registry itself lives in the
discovery-skipped ``_recipes.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError

from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    RecipeSpec,
    container_param_of as _container_param_of,
    render as _render,
    required_param_offenders as _required_param_offenders,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_RECIPE_OUTPUT_SCHEMA

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

# Activation models whose automations do NOT run until a one-time in-editor
# Run + Allow (the D/E trigger classes). The others activate on first use.
_NEEDS_ACTIVATION_MODELS = frozenset({"scheduled_trigger", "reactive_trigger"})


def _recipe_params_from(spec: RecipeSpec, raw_params: dict[str, Any]) -> dict[str, Any]:
    """Reproduce a typed wrapper's ``params`` dict from a generic params bag.

    A typed ``as_install_*`` tool builds ``params`` as every declared input
    EXCEPT ``on_conflict`` (with its Python signature defaults applied), and
    hands ``on_conflict`` to the mint separately. This rebuilds that exact dict
    from ``raw_params``: for each declared property other than ``on_conflict``,
    take the caller's value, else the schema ``default``, else ``None`` (the
    wrappers' ``None`` default for the optional inputs). The result is the same
    dict the typed wrapper would pass to ``render`` / ``mint`` / the ledger, so
    the generic install is byte-identical to the typed one (pinned in
    ``test_install_recipe``).
    """
    props = spec.input_schema.get("properties", {})
    params: dict[str, Any] = {}
    for key, pspec in props.items():
        if key == "on_conflict":
            continue
        if key in raw_params:
            params[key] = raw_params[key]
        elif "default" in pspec:
            params[key] = pspec["default"]
        else:
            params[key] = None
    return params


def _unknown_keys(spec: RecipeSpec, raw_params: dict[str, Any]) -> list[str]:
    """Params keys the recipe does not declare (a caller-typo guard).

    A typo like ``scheduel`` would otherwise be silently ignored and the
    automation installed with the DEFAULT value -- a quiet wrong install. This
    surfaces it so the caller fixes the key.
    """
    declared = set(spec.input_schema.get("properties", {}))
    return sorted(set(raw_params) - declared)


def _activation_message(spec: RecipeSpec) -> str:
    """A one-line honest activation note keyed off the recipe's model.

    The trigger classes (scheduled_trigger / reactive_trigger) do NOT fire
    until a one-time in-editor Run + Allow; the others activate on first use.
    Compact by design -- the full legend lives in ``as_list_recipes``'
    ``activation_models``.
    """
    if spec.activation_model in _NEEDS_ACTIVATION_MODELS:
        return (
            "Installed. This automation needs a one-time activation before it "
            "fires: open the script editor (project_url), Run the "
            f"{spec.activation_function} function once, then click Allow. Use "
            "as_check_activation to confirm it is live."
        )
    return (
        "Installed. It activates on first use (open or reload the file); no "
        "editor step is required."
    )


@workspace_tool(
    title="Install an automation recipe by name",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call mints a NEW bound project + deployment (same convention as the
    # typed installers it dispatches to); re-running installs another.
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_RECIPE_OUTPUT_SCHEMA,
)
def as_install_recipe(
    creds: Credentials,
    recipe: str,
    params: dict[str, Any],
) -> dict:
    """Install one of appscriptly's automation recipes by name (catalog-driven).

    USE WHEN: you have a recipe name from ``as_list_recipes`` plus its params
    and want to install it programmatically, without calling the specific typed
    tool. For everyday use PREFER the typed ``as_install_*`` tool for the
    recipe (e.g. ``as_install_sheet_dashboard``): its signature documents and
    validates every field, and it may add per-tool pre-flight checks this
    generic path skips. This tool is the catalog-driven companion to
    ``as_list_recipes`` -- browse, then install by name.

    HOW IT WORKS: looks up ``recipe`` in appscriptly's built-in recipe
    registry, validates ``params`` against that recipe's input schema (every
    REQUIRED input present, non-null, and the right type; unknown keys
    rejected), then generates + deploys the automation through the SAME
    machinery the typed installer uses -- a byte-identical result. It records
    the recipe + params so ``as_update_automation`` can later regenerate the
    automation deterministically from the current codegen.

    VALIDATION IS SCHEMA-LEVEL: required-field + type + unknown-key checks
    against the recipe's declared inputs. It does NOT re-run each typed tool's
    deeper pre-flight validation (e.g. a Drive mimeType round-trip) and trusts
    the caller's container id + kind; a deeper mistake surfaces as a clear error
    from the code generator or the Apps Script push, never as a silent bad
    install. Like every installer this requires the one-time Google OAuth grant.

    Args:
        recipe: the recipe name to install, e.g. ``as_install_sheet_dashboard``
            (from ``as_list_recipes`` -- each catalog entry's ``name`` /
            ``installer_tool``). An unknown name is rejected with the list of
            valid names.
        params: the recipe's install inputs, matching that recipe's ``params``
            summary in ``as_list_recipes`` (and the typed tool's arguments).
            Include the container id (``sheet_id`` / ``doc_id`` / ``form_id`` /
            ``presentation_id``), the required bodies, and optionally
            ``on_conflict`` (``new`` / ``replace`` / ``skip``) and a project
            ``name``. Omitted optional inputs take their documented defaults.

    Returns:
        ``{recipe, script_id, deployment_id, container_id, container_kind,
        activation_model, on_conflict, reused_existing, replaced_count,
        project_url, message}``. ``activation_model`` + ``message`` state what
        (if anything) it takes to make the automation live -- surface
        ``message`` to the user, since a trigger automation is NOT live until a
        one-time activation. ``project_url`` deep-links to the script editor.

    Raises:
        ValueError: an unknown recipe name; a ``params`` that is not an object,
            carries unknown keys, or leaves a required input missing / null /
            wrong-typed. Rejected before any API call.
        ToolError: the recipe cannot be installed generically (the video-deck
            renderer, which mints a per-install token -- use
            ``as_generate_video_deck``); or any Apps Script API error.
    """
    spec = _RECIPES.get(recipe)
    if spec is None:
        valid = ", ".join(sorted(_RECIPES))
        raise ValueError(
            f"Unknown recipe {recipe!r}. Call as_list_recipes to browse the "
            f"catalog, then install by name. Valid recipe names: {valid}."
        )

    # A recipe with an impure per-install pre_mint hook (video_deck: a fresh
    # single-use HMAC upload token + a Slides-container check + a frames-batch
    # handoff the generic result shape cannot carry) is refused here and pointed
    # at its typed tool -- the SAME posture as_update_automation takes for a
    # video-deck row. Refused BEFORE any mint / pre_mint runs.
    if spec.pre_mint is not None:
        raise ToolError(
            f"The {recipe!r} recipe mints a per-install upload token and needs "
            f"its own setup, so it cannot be installed through "
            f"as_install_recipe. Install it with its typed tool, "
            f"as_generate_video_deck."
        )

    if not isinstance(params, dict):
        raise ValueError(
            f"params must be an object mapping the recipe's inputs to values, "
            f"got {type(params).__name__}. See the recipe's params in "
            f"as_list_recipes."
        )

    unknown = _unknown_keys(spec, params)
    if unknown:
        valid = ", ".join(spec.input_schema.get("properties", {}))
        raise ValueError(
            f"params for {recipe!r} has unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {valid}. (See this recipe's params in as_list_recipes.)"
        )

    # on_conflict rides in params (it is a declared input) but the typed
    # wrappers hand it to the mint SEPARATELY and never store it in the recipe
    # params -- do the same so recipe_params (and thus the ledger + any later
    # regeneration) are byte-identical to a typed install.
    on_conflict = params.get("on_conflict", "new")
    recipe_params = _recipe_params_from(spec, params)

    # Registry-level required/type validation (the SAME validator
    # as_update_automation applies to its regeneration overrides): a required
    # input that is missing / null / wrong-typed is rejected with a clean
    # ValueError naming the key + recipe, BEFORE any render or API call.
    offenders = _required_param_offenders(spec, recipe_params)
    if offenders:
        raise ValueError(
            f"Cannot install the {recipe!r} recipe: invalid input(s): "
            f"{'; '.join(offenders)}. Fix them and retry (see the recipe's "
            f"params in as_list_recipes)."
        )

    # Render + mint through the EXACT path the typed wrapper uses. render is
    # pure codegen; a rejection here is a malformed NESTED input the top-level
    # required/type check cannot see -- a menu item missing a field, a trigger
    # body that is not a NAMED function -- which the typed tool would catch in
    # its own pre-flight. Surface it as a clean ValueError pointing at that
    # typed tool, never a raw KeyError / TypeError.
    try:
        rendered = _render(spec, recipe_params)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(
            f"Cannot install the {recipe!r} recipe: the code generator "
            f"rejected these inputs ({type(exc).__name__}: {exc}). The typed "
            f"tool {spec.name} validates inputs field-by-field -- call it for a "
            f"precise error, or fix the params (see as_list_recipes)."
        ) from exc
    container_id = recipe_params[_container_param_of(spec)]

    result = _mint_bound_automation(
        creds,
        tool=spec.name,
        recipe=spec.name,
        recipe_params=recipe_params,
        container_id=container_id,
        container_kind=spec.container_kind,
        project_name=spec.project_name(recipe_params),
        script_body=rendered.script_body,
        manifest_dict=rendered.manifest,
        on_conflict=on_conflict,
        handler_functions=rendered.handler_functions,
    )

    return {
        "recipe": spec.name,
        "script_id": result.script_id,
        "deployment_id": result.deployment_id,
        "container_id": container_id,
        "container_kind": spec.container_kind,
        "activation_model": spec.activation_model,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "project_url": f"https://script.google.com/d/{result.script_id}/edit",
        "message": _activation_message(spec),
    }
