"""``as_list_recipes`` - browse the installable automation recipe catalog.

A recipe is one of appscriptly's built-in bound-automation patterns (a
custom menu, a scheduled dashboard refresh, a reactive onEdit / onFormSubmit
handler, a form grader, ...). Every recipe is a DATA entry in the internal
registry (``services/apps_script/_recipes.py``), and this tool projects that
registry into a compact, read-only catalog so a caller can discover what is
installable and, for each one, which typed ``as_install_*`` tool actually
installs it.

**Why a browse tool.** The install surface is a family of typed tools
(``as_install_doc_menu``, ``as_install_sheet_dashboard``, ...); each carries a
rich per-field input schema on its own signature. This catalog is the index
over that family: list the recipes, read the one-line summary + params
summary + activation model, then call the matching ``installer_tool``. It is
pure-local (it reads the in-process registry, makes no Google API call, needs
no credentials), so it is safe to call any time to orient.

**Why a separate feature file.** Same convention as the other apps_script
tools (``processes.py``, ``check_activation.py``, ...): each tool lives in its
own non-underscore leaf module so ``server.py``'s auto-discovery walk imports
it (running the ``@workspace_tool`` decorator, which registers it) with no
central edit. The registry itself lives in the discovery-skipped
``_recipes.py`` (underscore-prefixed, registers no tools).
"""
from __future__ import annotations

from typing import Any

from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._recipes import RECIPES, RecipeSpec
from appscriptly.tool_schemas import AS_LIST_RECIPES_OUTPUT_SCHEMA

# One honest line per activation model (the ``activation_model`` values a
# RecipeSpec carries). Mirrors the authoritative prose in
# ``lifecycle_tools._ACTIVATION_MODEL``'s comment; the trigger classes
# (scheduled_trigger / reactive_trigger, the D/E generator classes) require a
# one-time in-editor Run + Allow before they ever fire, and this legend says
# so plainly rather than implying an install alone makes them live. ASCII
# hyphen only (this text is surfaced to callers).
_ACTIVATION_MODEL_MEANINGS: dict[str, str] = {
    "menu": (
        "A custom menu that appears in the file's menu bar when the file is "
        "opened; each item authorizes on its first click. No editor setup."
    ),
    "menu_action": (
        "An on-demand menu item that runs (and authorizes) on its first click "
        "from the file's menu. No editor setup."
    ),
    "scheduled_trigger": (
        "A time-driven trigger. It needs a one-time activation in the Apps "
        "Script editor before it fires: open the script, Run the install "
        "function once, then click Allow."
    ),
    "reactive_trigger": (
        "An installable onEdit / onFormSubmit trigger. It needs a one-time "
        "activation in the Apps Script editor before it fires: open the "
        "script, Run the install function once, then click Allow."
    ),
    "custom_function": (
        "A spreadsheet =FUNCTION() usable in a cell; it resolves after a "
        "one-time reload of the sheet. No editor setup."
    ),
}

# Fallback meaning for an activation model with no registered line (defensive:
# a future recipe class should add its meaning above, but the catalog must
# never omit or crash on one).
_UNKNOWN_ACTIVATION_MEANING = (
    "See the recipe's installer tool for how to activate this automation."
)


def _params_summary(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a recipe's input schema to a compact per-param summary.

    One entry per property in declaration order (which matches the installer
    tool's argument order): ``{name, type, required}``. This is a SUMMARY for
    discovery; the matching ``as_install_*`` tool carries the full per-field
    typed schema (enums, defaults, nested item shapes) on its own signature.
    """
    properties = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    return [
        {
            "name": pname,
            "type": pspec.get("type", "string"),
            "required": pname in required,
        }
        for pname, pspec in properties.items()
    ]


def _catalog_entry(spec: RecipeSpec) -> dict[str, Any]:
    """Project one ``RecipeSpec`` to its public catalog shape.

    ``installer_tool`` is the discovery pointer: the name of the typed tool a
    caller invokes to install this recipe. It equals ``name`` today (each
    recipe is exposed as its own ``as_install_*`` wrapper); it is surfaced as a
    distinct field so the "list then install" contract is explicit and stable.
    """
    return {
        "name": spec.name,
        "installer_tool": spec.name,
        "title": spec.title,
        "summary": spec.summary,
        "version": spec.version,
        "container_kind": spec.container_kind,
        "activation_model": spec.activation_model,
        "params": _params_summary(spec.input_schema),
    }


@workspace_tool(
    title="List the installable automation recipes",
    service="apps_script",
    readonly=True,
    destructive=False,
    idempotent=True,
    # Pure-local read of the in-process recipe registry - no Google API call
    # (like server_guide / as_list_installed_automations). openWorldHint=False.
    external=False,
    # creds=False: reads only the static registry; no user identity or Google
    # credentials involved.
    creds=False,
    output_schema=AS_LIST_RECIPES_OUTPUT_SCHEMA,
)
def as_list_recipes() -> dict:
    """List appscriptly's installable automation recipes (the install catalog).

    A recipe is a built-in bound-automation pattern; each is installed by a
    typed ``as_install_*`` tool. This is the discovery surface over that
    family: it enumerates every recipe from the internal registry so you can
    pick one and call the matching installer, WITHOUT a Google API call or any
    authorization (it reads only the in-process registry).

    USE WHEN: the user asks "what automations can you set up?", "what recipes
    are available?", or you want to find the right installer for a pattern
    (a scheduled refresh, a custom menu, an onFormSubmit handler, a quiz
    grader) before calling it. After picking a recipe, call its
    ``installer_tool`` (e.g. ``as_install_sheet_dashboard``) with that tool's
    own typed arguments.

    Returns ``{recipes, count, activation_models}``:

    - ``recipes`` - one entry per recipe, in a stable grouped order. Each
      carries: ``name`` (the recipe id), ``installer_tool`` (the typed tool to
      CALL to install it - equal to ``name`` today), ``title`` and ``summary``
      (human-readable, one line each), ``version`` (the recipe's codegen
      version), ``container_kind`` (``sheets`` / ``docs`` / ``slides`` /
      ``forms`` - the file type it binds to), ``activation_model`` (a key into
      ``activation_models``), and ``params`` - a summary of the installer's
      inputs as ``{name, type, required}`` in argument order (the installer
      tool carries the full per-field schema).
    - ``count`` - the number of recipes.
    - ``activation_models`` - a legend mapping each activation model present in
      the catalog to one honest line about what it takes to make that
      automation live. The trigger classes (``scheduled_trigger`` /
      ``reactive_trigger``) need a one-time Run + Allow in the Apps Script
      editor before they fire; ``menu`` / ``menu_action`` / ``custom_function``
      activate on first use with no editor step. Surface this to the user so
      they know an install may not be the last step.

    This catalog is DATA-DRIVEN: a recipe added to the registry appears here
    automatically, with no change to this tool.
    """
    recipes = [_catalog_entry(spec) for spec in RECIPES.values()]
    models_present = sorted({spec.activation_model for spec in RECIPES.values()})
    activation_models = {
        model: _ACTIVATION_MODEL_MEANINGS.get(model, _UNKNOWN_ACTIVATION_MEANING)
        for model in models_present
    }
    return {
        "recipes": recipes,
        "count": len(recipes),
        "activation_models": activation_models,
    }
