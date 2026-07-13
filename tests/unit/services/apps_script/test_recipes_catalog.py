"""Tests for ``as_list_recipes`` (Wave 2 / S4) - the recipe catalog browse tool.

The tool projects the internal recipe registry (``_recipes.RECIPES``) into a
read-only discovery surface. These tests pin the response shape, the
registry -> catalog projection, the activation-model legend (honest about the
trigger classes' editor Run + Allow ritual), the em/en-dash hard rule, and the
DISCRIMINATING property the brief asks for: a recipe added to the registry
appears in the catalog with NO change to the tool code.
"""
from __future__ import annotations

# Import server first so decorators.register() has run and auto-discovery
# registered the tool (mirrors the repo's tool tests; also exercises the
# boot-count floor, which now expects 141).
from appscriptly.server import mcp  # noqa: F401
from appscriptly.services.apps_script._recipes import RECIPES, RecipeSpec
from appscriptly.services.apps_script.recipes_catalog import (
    _UNKNOWN_ACTIVATION_MEANING,
    as_list_recipes,
)
from appscriptly.tool_schemas import AS_LIST_RECIPES_OUTPUT_SCHEMA

_ENTRY_KEYS = {
    "name",
    "installer_tool",
    "title",
    "summary",
    "version",
    "container_kind",
    "activation_model",
    "params",
}


def test_catalog_lists_every_recipe():
    out = as_list_recipes()
    assert set(out) == {"recipes", "count", "activation_models"}
    assert out["count"] == len(out["recipes"]) == len(RECIPES)
    names = {e["name"] for e in out["recipes"]}
    assert names == set(RECIPES)


def test_entry_shape_and_installer_pointer():
    out = as_list_recipes()
    by_name = {e["name"]: e for e in out["recipes"]}
    for name, spec in RECIPES.items():
        entry = by_name[name]
        assert set(entry) == _ENTRY_KEYS
        # installer_tool is the discovery pointer a caller invokes to install
        # the recipe (== the recipe name today).
        assert entry["installer_tool"] == name == spec.name
        assert entry["title"] == spec.title
        assert entry["summary"] == spec.summary
        assert entry["version"] == spec.version
        assert entry["container_kind"] == spec.container_kind
        assert entry["activation_model"] == spec.activation_model


def test_params_summary_matches_input_schema():
    out = as_list_recipes()
    by_name = {e["name"]: e for e in out["recipes"]}
    for name, spec in RECIPES.items():
        props = spec.input_schema.get("properties") or {}
        required = set(spec.input_schema.get("required") or [])
        params = by_name[name]["params"]
        # One entry per property, in declaration (== argument) order.
        assert [p["name"] for p in params] == list(props)
        for p in params:
            assert p["required"] == (p["name"] in required)
            assert p["type"] == props[p["name"]].get("type", "string")


def test_activation_models_legend_covers_present_models_and_is_honest():
    out = as_list_recipes()
    legend = out["activation_models"]
    present = {spec.activation_model for spec in RECIPES.values()}
    # Exactly the models the catalog actually contains - no more, no less.
    assert set(legend) == present
    assert all(v.strip() for v in legend.values())
    # Honest about the one-time editor Run + Allow the trigger classes (the
    # D/E generator classes) need before they ever fire.
    for model in ("scheduled_trigger", "reactive_trigger"):
        assert model in legend, f"the current registry should contain {model}"
        text = legend[model]
        assert "Run" in text and "Allow" in text


def test_no_em_or_en_dashes_in_user_visible_text():
    out = as_list_recipes()
    blobs = list(out["activation_models"].values())
    for entry in out["recipes"]:
        blobs += [entry["title"], entry["summary"]]
    em_dash, en_dash = chr(0x2014), chr(0x2013)
    for text in blobs:
        assert em_dash not in text, f"em dash in user-visible catalog text: {text!r}"
        assert en_dash not in text, f"en dash in user-visible catalog text: {text!r}"


def test_return_satisfies_output_schema_required_keys():
    out = as_list_recipes()
    for key in AS_LIST_RECIPES_OUTPUT_SCHEMA["required"]:
        assert key in out
    entry_schema = AS_LIST_RECIPES_OUTPUT_SCHEMA["properties"]["recipes"]["items"]
    for entry in out["recipes"]:
        for key in entry_schema["required"]:
            assert key in entry
    param_required = entry_schema["properties"]["params"]["items"]["required"]
    for entry in out["recipes"]:
        for param in entry["params"]:
            for key in param_required:
                assert key in param


def test_catalog_is_data_driven_new_entry_appears(monkeypatch):
    """DISCRIMINATING: a recipe added to the registry surfaces in the catalog
    with NO edit to the tool code, and a novel activation model gets the
    fallback legend line. This is the discovery-surface contract - the tool
    is a pure projection of RECIPES."""
    dummy = RecipeSpec(
        name="as_install_dummy_widget",
        title="Dummy widget installer",
        summary="A synthetic recipe used only to prove the catalog is data-driven.",
        container_kind="sheets",
        build=lambda p: "",
        manifest_plan=lambda p, kind: None,
        observability="none",
        activation_model="dummy_model",
        activation_function=None,
        project_name=lambda p: "dummy",
        input_schema={
            "type": "object",
            "properties": {
                "sheet_id": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["sheet_id"],
        },
        output_schema={},
        example_params=(),
        version="9",
    )
    monkeypatch.setitem(RECIPES, dummy.name, dummy)

    out = as_list_recipes()
    by_name = {e["name"]: e for e in out["recipes"]}
    assert dummy.name in by_name, "a registry entry did not surface in the catalog"
    entry = by_name[dummy.name]
    assert entry["installer_tool"] == dummy.name
    assert entry["version"] == "9"
    assert entry["container_kind"] == "sheets"
    assert entry["activation_model"] == "dummy_model"
    assert entry["params"] == [
        {"name": "sheet_id", "type": "string", "required": True},
        {"name": "count", "type": "integer", "required": False},
    ]
    # The legend is dynamic: a model with no registered meaning gets the
    # defensive fallback rather than being omitted or crashing.
    assert out["activation_models"]["dummy_model"] == _UNKNOWN_ACTIVATION_MEANING
