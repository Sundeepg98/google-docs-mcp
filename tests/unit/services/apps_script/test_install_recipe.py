"""``as_install_recipe`` - the generic catalog-driven installer (Wave 2 / S6a).

The load-bearing property is IDENTITY: installing a recipe by name through
``as_install_recipe`` must hand ``mint_bound_automation`` the EXACT same kwargs
the typed ``as_install_*`` wrapper would -- same ``.gs`` body, manifest, handler
names, container id, project name, on_conflict, and stored recipe params. If the
generic dispatch drifted from a typed wrapper, these fail. Plus:

  * the video-deck renderer (the sole ``pre_mint`` recipe) is refused, pointing
    at its typed tool, BEFORE any mint;
  * an unknown recipe / a missing, null, wrong-typed, or unknown-key param is
    rejected with a clean ValueError before any API call;
  * omitted optional inputs take the SAME default the typed signature applies;
  * a row minted by the generic installer records ``(recipe, params)`` and is
    regenerable by ``as_update_automation`` (S5 semantics preserved).

Mirrors ``test_recipes_registry`` (the ``_capturing_mint`` + creds-stub pattern)
and ``test_deterministic_update`` (the ``_FakeApi`` ledger harness).
"""
from __future__ import annotations

import importlib
import json

import pytest
from fastmcp.exceptions import ToolError

from appscriptly import auth, automation_ledger, decorators
from appscriptly.services.apps_script import _lifecycle, install_recipe
from appscriptly.services.apps_script._lifecycle import MintResult
from appscriptly.services.apps_script._recipes import RECIPES, container_param_of
from appscriptly.services.apps_script.install_recipe import as_install_recipe
from appscriptly.services.apps_script.lifecycle_tools import as_update_automation

# recipe name -> the generator module its typed wrapper lives in.
_MODULE: dict[str, str] = {
    "as_install_doc_menu": "doc_menu",
    "as_install_sheet_menu": "sheet_menu",
    "as_install_slides_menu": "slides_menu",
    "as_install_custom_function": "custom_function",
    "as_install_sheet_dashboard": "sheet_dashboard",
    "as_install_calendar_sync": "calendar_sync",
    "as_install_task_rollover": "task_rollover",
    "as_install_edit_trigger": "edit_trigger",
    "as_install_form_handler": "form_handler",
    "as_install_contact_sync": "contact_sync",
    "as_grade_form_responses": "grade_form_responses",
    "as_refresh_linked_slides": "refresh_linked_slides",
    "as_generate_video_deck": "video_deck",
}
_VIDEO_DECK = "as_generate_video_deck"
_NON_VIDEO = [n for n in _MODULE if n != _VIDEO_DECK]

# The container-id param key each recipe carries (the ORACLE for
# container_param_of). Every one is the recipe's first required input; pinned
# here so a reorder that breaks the generic installer's container binding fails.
_CONTAINER_PARAM: dict[str, str] = {
    "as_install_doc_menu": "doc_id",
    "as_install_sheet_menu": "sheet_id",
    "as_install_slides_menu": "presentation_id",
    "as_install_custom_function": "sheet_id",
    "as_install_sheet_dashboard": "sheet_id",
    "as_install_calendar_sync": "sheet_id",
    "as_install_task_rollover": "sheet_id",
    "as_install_edit_trigger": "sheet_id",
    "as_install_form_handler": "form_id",
    "as_install_contact_sync": "form_id",
    "as_grade_form_responses": "form_id",
    "as_refresh_linked_slides": "presentation_id",
    "as_generate_video_deck": "presentation_id",
}

# The kwargs whose equality across the two dispatch paths IS the identity
# contract (handler_functions is compared separately, normalizing absent==[]).
_IDENTITY_FIELDS = (
    "tool", "recipe", "container_id", "container_kind", "project_name",
    "script_body", "manifest_dict", "on_conflict", "recipe_params",
)


@pytest.fixture(autouse=True)
def stub_creds(monkeypatch):
    """Resolve the @workspace_tool(creds=True) envelope without real OAuth.

    Both dispatch paths declare scopes=GAS_BOUND_SCOPES, so creds resolution
    flows through the scope-aware stdio path (auth.load_credentials) and the
    default path (decorators._get_credentials_fn); patch both (mirrors
    test_recipes_registry / test_deterministic_update).
    """
    from unittest.mock import MagicMock

    creds = MagicMock(name="stub-creds")
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: creds)
    return creds


def _capturing_mint(captured: dict):
    """A ``mint_bound_automation`` replacement recording its kwargs; returns a
    schema-valid stub (no project is created)."""

    def fake_mint(creds, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return MintResult(script_id="SID-1", deployment_id="DEPLOY-1")

    return fake_mint


def _assert_mint_kwargs_identical(captured_generic: dict, captured_typed: dict, name: str):
    for field in _IDENTITY_FIELDS:
        assert captured_generic[field] == captured_typed[field], (
            f"{name}: mint kwarg {field!r} differs between as_install_recipe "
            f"and the typed wrapper."
        )
    assert (captured_generic.get("handler_functions") or []) == (
        captured_typed.get("handler_functions") or []
    ), f"{name}: handler_functions differ."


# ---------------------------------------------------------------------
# container_param_of oracle
# ---------------------------------------------------------------------


def test_container_param_of_matches_the_known_container_key():
    """The generic installer derives container_id from container_param_of; pin
    it against the known per-recipe container key so a reorder cannot mis-bind."""
    assert set(_CONTAINER_PARAM) == set(RECIPES)
    for name, spec in RECIPES.items():
        assert container_param_of(spec) == _CONTAINER_PARAM[name], name


# ---------------------------------------------------------------------
# IDENTITY: as_install_recipe(name, params) == the typed wrapper, at the mint.
# ---------------------------------------------------------------------

_ID_CASES = [
    (name, i)
    for name in _NON_VIDEO
    for i in range(len(RECIPES[name].example_params))
]


@pytest.mark.parametrize(
    ("name", "index"),
    _ID_CASES,
    ids=[f"{name}-{i}" for name, i in _ID_CASES],
)
def test_generic_install_is_byte_identical_to_the_typed_wrapper(name, index, monkeypatch):
    """as_install_recipe(name, example_params) hands the mint the SAME kwargs
    the typed as_install_* wrapper does -- byte-identical .gs / manifest /
    handlers / container / project name / on_conflict / stored recipe params."""
    spec = RECIPES[name]
    example = dict(spec.example_params[index])
    typed_mod = importlib.import_module(f"appscriptly.services.apps_script.{_MODULE[name]}")

    # custom_function validates its container is a Sheet via a Drive lookup
    # before minting (the generic path does not); stub it for the typed call.
    if name == "as_install_custom_function":
        monkeypatch.setattr(typed_mod, "_auto_detect_container_kind", lambda creds, cid: "sheets")

    captured_generic: dict = {}
    captured_typed: dict = {}
    monkeypatch.setattr(install_recipe, "_mint_bound_automation", _capturing_mint(captured_generic))
    monkeypatch.setattr(typed_mod, "_mint_bound_automation", _capturing_mint(captured_typed))

    # Generic path: install by NAME with the example params (a fresh copy).
    as_install_recipe(recipe=name, params=dict(example))
    # Typed path: call the wrapper with the example params as kwargs.
    getattr(typed_mod, name)(**example)

    _assert_mint_kwargs_identical(captured_generic, captured_typed, name)


def test_omitted_optionals_take_the_same_default_as_the_typed_signature(monkeypatch):
    """A MINIMAL params bag (only the required inputs) must produce the exact
    mint the typed wrapper produces from its signature defaults -- so the
    schema defaults the generic path applies mirror the Python defaults.

    Covers a schema-defaulted optional (slides_menu.menu_title = 'Presentation
    Tools') and two more (sheet_dashboard.schedule='daily', hour=6), plus the
    None-default optionals (name / *_note)."""
    cases = {
        "as_install_slides_menu": {
            "presentation_id": "PRES1",
            "items": [
                {"label": "Refresh", "function_name": "refreshData", "function_body": "Logger.log('r');"}
            ],
        },
        "as_install_sheet_dashboard": {
            "sheet_id": "SHEET1",
            "refresh_function_body": "function refreshDashboard() { SpreadsheetApp.getActive(); }",
        },
    }
    for name, minimal in cases.items():
        typed_mod = importlib.import_module(
            f"appscriptly.services.apps_script.{_MODULE[name]}"
        )
        captured_generic: dict = {}
        captured_typed: dict = {}
        monkeypatch.setattr(install_recipe, "_mint_bound_automation", _capturing_mint(captured_generic))
        monkeypatch.setattr(typed_mod, "_mint_bound_automation", _capturing_mint(captured_typed))

        as_install_recipe(recipe=name, params=dict(minimal))
        getattr(typed_mod, name)(**minimal)

        _assert_mint_kwargs_identical(captured_generic, captured_typed, name)


def test_on_conflict_rides_in_params_reaches_mint_and_is_not_stored(monkeypatch):
    """on_conflict is a declared input, but (like the typed wrappers) it is
    handed to the mint SEPARATELY and never stored in the recipe params."""
    captured: dict = {}
    monkeypatch.setattr(install_recipe, "_mint_bound_automation", _capturing_mint(captured))

    as_install_recipe(
        recipe="as_install_sheet_menu",
        params={
            "sheet_id": "S",
            "menu_title": "M",
            "items": [{"label": "L", "function_name": "fn", "function_body": "Logger.log(1);"}],
            "on_conflict": "replace",
        },
    )
    assert captured["on_conflict"] == "replace"
    assert "on_conflict" not in captured["recipe_params"]


# ---------------------------------------------------------------------
# video_deck (the sole pre_mint recipe) is refused BEFORE any mint.
# ---------------------------------------------------------------------


def test_video_deck_recipe_is_refused_pointing_at_the_typed_tool(monkeypatch):
    """A pre_mint recipe (video_deck: a per-install single-use token) cannot be
    installed generically -- refused with a ToolError naming the typed tool,
    before any mint/pre_mint runs (matches as_update_automation's posture)."""
    def _explode(*a, **k):
        raise AssertionError("mint must not run for a refused recipe")

    monkeypatch.setattr(install_recipe, "_mint_bound_automation", _explode)

    with pytest.raises(ToolError, match="as_generate_video_deck"):
        as_install_recipe(recipe=_VIDEO_DECK, params={"presentation_id": "P"})


def test_only_the_pre_mint_recipe_is_refused():
    """The refusal is the GENERAL pre_mint predicate, not a video_deck string
    check: exactly the recipes carrying a pre_mint hook are the refused set."""
    refused = {n for n, s in RECIPES.items() if s.pre_mint is not None}
    assert refused == {_VIDEO_DECK}


# ---------------------------------------------------------------------
# Input validation (all before any API call).
# ---------------------------------------------------------------------


def test_unknown_recipe_lists_valid_names():
    with pytest.raises(ValueError) as exc:
        as_install_recipe(recipe="as_install_nope", params={})
    msg = str(exc.value)
    assert "as_install_nope" in msg
    assert "as_install_sheet_dashboard" in msg  # a real name is offered


def test_non_dict_params_is_rejected():
    with pytest.raises(ValueError, match="must be an object"):
        as_install_recipe(recipe="as_install_sheet_menu", params=["not", "a", "dict"])


def test_missing_required_param_is_rejected(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("mint must not run when validation fails")

    monkeypatch.setattr(install_recipe, "_mint_bound_automation", _explode)
    # sheet_dashboard requires refresh_function_body.
    with pytest.raises(ValueError) as exc:
        as_install_recipe(recipe="as_install_sheet_dashboard", params={"sheet_id": "S"})
    msg = str(exc.value)
    assert "refresh_function_body" in msg
    assert "as_install_sheet_dashboard" in msg


def test_wrong_typed_required_param_is_rejected():
    with pytest.raises(ValueError) as exc:
        as_install_recipe(
            recipe="as_install_sheet_dashboard",
            params={"sheet_id": "S", "refresh_function_body": ["not", "a", "string"]},
        )
    assert "expected string" in str(exc.value)


def test_unknown_param_key_is_rejected():
    with pytest.raises(ValueError, match="unknown key") as exc:
        as_install_recipe(
            recipe="as_install_sheet_dashboard",
            params={
                "sheet_id": "S",
                "refresh_function_body": "function r(){}",
                "scheduel": "daily",  # typo for schedule
            },
        )
    assert "scheduel" in str(exc.value)


def test_malformed_nested_input_is_a_clean_error_not_a_raw_keyerror(monkeypatch):
    """A nested-shape mistake the top-level required/type check cannot see (a
    menu item missing function_body) surfaces as a clean ValueError naming the
    typed tool, NOT a raw KeyError from deep in the builder -- and no mint runs."""
    def _explode(*a, **k):
        raise AssertionError("mint must not run when the code generator rejects")

    monkeypatch.setattr(install_recipe, "_mint_bound_automation", _explode)

    with pytest.raises(ValueError) as exc:
        as_install_recipe(
            recipe="as_install_sheet_menu",
            params={
                "sheet_id": "S",
                "menu_title": "M",
                "items": [{"label": "L", "function_name": "fn"}],  # no function_body
            },
        )
    msg = str(exc.value)
    assert "code generator rejected" in msg
    assert "as_install_sheet_menu" in msg  # points at the typed tool


# ---------------------------------------------------------------------
# S5: a generic-installed row records recipe+params and is regenerable.
# ---------------------------------------------------------------------


class _FakeApi:
    """Fakes the Apps Script primitives; reflects pushed content as live
    (mirrors test_deterministic_update._FakeApi)."""

    def __init__(self) -> None:
        self._n = 0
        self.pushed: list[tuple] = []
        self.content_by_script: dict[str, dict] = {}

    def create_bound_project(self, creds, container_id, name):
        self._n += 1
        return {"scriptId": f"SID{self._n}"}

    def set_project_content(self, creds, script_id, body, manifest):
        self.pushed.append((script_id, body, manifest))
        live_manifest = {k: v for k, v in manifest.items() if k != "__plan__"}
        self.content_by_script[script_id] = {
            "files": [
                {"name": "appsscript", "type": "JSON",
                 "source": json.dumps(live_manifest)},
                {"name": "Code", "type": "SERVER_JS", "source": body},
            ]
        }
        return {}

    def create_deployment(self, creds, script_id, description):
        return {"deploymentId": f"DEP-{script_id}"}

    def list_deployments(self, creds, script_id):
        return [{"deploymentId": f"DEP-{script_id}",
                 "deploymentConfig": {"versionNumber": 1}}]

    def delete_deployment(self, creds, script_id, deployment_id):
        pass

    def get_project_content(self, creds, script_id):
        return self.content_by_script.get(
            script_id,
            {"files": [{"name": "appsscript", "type": "JSON",
                        "source": json.dumps({"oauthScopes": []})}]},
        )


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(_lifecycle, "_create_bound_project", api.create_bound_project)
    monkeypatch.setattr(_lifecycle, "_set_project_content", api.set_project_content)
    monkeypatch.setattr(_lifecycle, "_create_deployment", api.create_deployment)
    monkeypatch.setattr(_lifecycle, "_list_deployments", api.list_deployments)
    monkeypatch.setattr(_lifecycle, "_delete_deployment", api.delete_deployment)
    monkeypatch.setattr(_lifecycle, "_get_project_content", api.get_project_content)
    return api


def test_generic_install_records_recipe_params_and_is_regenerable(fake_api):
    """(d) A row minted by as_install_recipe records (recipe, params) exactly
    like a typed install, so as_update_automation can regenerate it server-side
    with no caller body (S5 semantics work on rows the generic path creates)."""
    params = {
        "sheet_id": "SHEETX",
        "refresh_function_body": "function refreshDashboard() { SpreadsheetApp.getActive(); }",
        "schedule": "daily",
        "hour": 7,
    }
    out = as_install_recipe(recipe="as_install_sheet_dashboard", params=dict(params))
    sid = out["script_id"]
    assert out["recipe"] == "as_install_sheet_dashboard"
    assert out["container_id"] == "SHEETX"
    assert out["activation_model"] == "scheduled_trigger"
    assert "one-time activation" in out["message"]  # honest trigger caveat

    row = automation_ledger.get_automation(sid)
    assert row["recipe"] == "as_install_sheet_dashboard"
    stored = json.loads(row["params_json"])
    assert stored["sheet_id"] == "SHEETX"
    assert stored["schedule"] == "daily"
    assert stored["hour"] == 7
    assert stored["refresh_function_body"] == params["refresh_function_body"]

    # S5: regenerate from the recorded recipe with NO body. Same codegen ->
    # unchanged, and regenerated_from_recipe is True (the deterministic path).
    upd = as_update_automation(script_id=sid)
    assert upd["regenerated_from_recipe"] is True
    assert upd["status"] == "unchanged"
