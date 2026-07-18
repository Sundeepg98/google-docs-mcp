"""Stream S5 - deterministic update: server-side recipe regeneration.

The wave-2 lifecycle win. An automation installed FROM a registry recipe
records ``(recipe, params)`` at mint (Stream S5), so ``as_update_automation``
can regenerate its ``.gs`` + manifest from the CURRENT codegen with NO caller
re-authoring -- the deterministic fleet-refresh path. These pin:

  * a real installer records ``recipe`` + ``params`` in the ledger;
  * regenerating with NO ``script_body`` picks up a bumped registry template
    (new content hash) and reports ``regenerated_from_recipe: true``;
  * a matching-codegen regeneration is an idempotent ``unchanged`` no-op;
  * ``params`` overrides merge over the recorded params and are re-stored;
  * regeneration detects a scope the live deployment lacks (needs_reactivation);
  * the regeneration manifest goes through the SAME restricted-scope guard as
    an install;
  * a recipe-LESS row (raw ``as_generate_bound_script``) keeps the
    caller-supplied-body path unchanged, reporting ``regenerated_from_recipe:
    false``;
  * a ``video_deck`` row (a per-install single-use token) is refused in place.

The Apps Script primitives are faked (monkeypatch) so the tests exercise the
tool + recipe registry + real ledger, not the Google API plumbing. The fake's
``set_project_content`` reflects the pushed manifest as the live content, so a
regeneration's scope-change detection reads back what an install actually
deployed.
"""
from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from appscriptly import auth, automation_ledger, decorators
from appscriptly.services.apps_script import _lifecycle, sheet_dashboard
from appscriptly.services.apps_script._lifecycle import _ledger_user_id
from appscriptly.services.apps_script._recipes import RECIPES, RecipeSpec
from appscriptly.services.apps_script.api import (
    container_data_scope,
    is_restricted_scope,
)
from appscriptly.services.apps_script.lifecycle_tools import as_update_automation
from appscriptly.services.apps_script.sheet_dashboard import (
    as_install_sheet_dashboard,
)

_CREDS = object()


@pytest.fixture(autouse=True)
def stub_creds(monkeypatch):
    """Stop the creds=True envelope from launching real OAuth."""
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: _CREDS)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: _CREDS)


class _FakeApi:
    """Fakes the Apps Script primitives; reflects pushed content as live."""

    def __init__(self) -> None:
        self._n = 0
        self.pushed: list[tuple] = []
        self.content_by_script: dict[str, dict] = {}

    def create_bound_project(self, creds, container_id, name):
        self._n += 1
        return {"scriptId": f"SID{self._n}"}

    def set_project_content(self, creds, script_id, body, manifest):
        self.pushed.append((script_id, body, manifest))
        # Reflect the push as the live content (minus the private __plan__
        # echo, exactly like the real set_project_content), so a later
        # regeneration's scope-change detection reads back reality.
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


_DASH_PARAMS = dict(
    sheet_id="SHEET1",
    refresh_function_body=(
        "function refreshDashboard() { SpreadsheetApp.getActive(); }"
    ),
    schedule="daily",
    hour=9,
)


def _bump_builder(monkeypatch, prefix: str = "// codegen v2 bump\n") -> None:
    """Simulate a codegen bump: the dashboard builder now prepends a line.

    Patches the module-level ``build_dashboard_script_body`` the recipe's
    ``build`` lazily imports, so a REGENERATION emits new content while the
    (already-completed) install used the original codegen.
    """
    real = sheet_dashboard.build_dashboard_script_body

    def bumped(body, schedule, hour, note=None):
        script, handler = real(body, schedule, hour, note)
        return prefix + script, handler

    monkeypatch.setattr(sheet_dashboard, "build_dashboard_script_body", bumped)


# ---------------------------------------------------------------------
# (d) A real installer records recipe + params in the ledger.
# ---------------------------------------------------------------------


def test_install_records_recipe_and_params_in_the_ledger(fake_api):
    result = as_install_sheet_dashboard(**_DASH_PARAMS)
    row = automation_ledger.get_automation(result["script_id"])
    assert row["recipe"] == "as_install_sheet_dashboard"
    stored = json.loads(row["params_json"])
    assert stored["sheet_id"] == "SHEET1"
    assert stored["schedule"] == "daily"
    assert stored["hour"] == 9
    assert stored["refresh_function_body"] == _DASH_PARAMS["refresh_function_body"]


# ---------------------------------------------------------------------
# (b) A recipe row regenerates WITHOUT a body and picks up a bumped template.
# ---------------------------------------------------------------------


def test_recipe_row_regenerates_at_current_codegen_without_a_body(
    fake_api, monkeypatch
):
    result = as_install_sheet_dashboard(**_DASH_PARAMS)
    sid = result["script_id"]
    hash_v1 = automation_ledger.get_automation(sid)["content_hash"]

    _bump_builder(monkeypatch)

    # NO script_body: the server regenerates from the recipe at current codegen.
    out = as_update_automation(script_id=sid)

    assert out["regenerated_from_recipe"] is True
    assert out["status"] == "updated"
    assert out["content_hash_after"] != hash_v1
    # A body-only bump changes no scopes -> no re-Allow needed.
    assert out["needs_reactivation"] is False
    # The ledger row carries the new hash on the SAME project (never a new id).
    row = automation_ledger.get_automation(sid)
    assert row["script_id"] == sid
    assert row["content_hash"] == out["content_hash_after"]
    assert row["recipe"] == "as_install_sheet_dashboard"
    # The pushed body is the bumped SERVER codegen, not a caller-authored one.
    update_pushes = [b for s, b, _ in fake_api.pushed if s == sid]
    assert update_pushes[-1].startswith("// codegen v2 bump")


def test_regeneration_without_a_codegen_change_is_an_idempotent_noop(fake_api):
    result = as_install_sheet_dashboard(**_DASH_PARAMS)
    sid = result["script_id"]
    pushes_after_install = len([p for p in fake_api.pushed if p[0] == sid])

    # Same recipe, same recorded params, same codegen -> byte-identical.
    out = as_update_automation(script_id=sid)

    assert out["regenerated_from_recipe"] is True
    assert out["status"] == "unchanged"
    assert out["needs_reactivation"] is False
    assert out["content_hash_before"] == out["content_hash_after"]
    # Nothing was re-pushed.
    assert len([p for p in fake_api.pushed if p[0] == sid]) == pushes_after_install


def test_params_override_merges_over_recorded_params_and_restores(fake_api):
    result = as_install_sheet_dashboard(**_DASH_PARAMS)  # daily @ 9
    sid = result["script_id"]

    out = as_update_automation(
        script_id=sid, params={"schedule": "weekly", "hour": 8}
    )

    assert out["regenerated_from_recipe"] is True
    # schedule/hour change the generated .gs -> new content.
    assert out["status"] == "updated"
    # The merged params are re-stored: unchanged keys preserved, overrides applied.
    stored = json.loads(automation_ledger.get_automation(sid)["params_json"])
    assert stored["schedule"] == "weekly"
    assert stored["hour"] == 8
    assert stored["sheet_id"] == "SHEET1"
    assert stored["refresh_function_body"] == _DASH_PARAMS["refresh_function_body"]


def test_regeneration_reports_a_scope_the_live_deployment_lacks(
    fake_api, monkeypatch
):
    """A codegen bump whose deployed version predates a scope it now needs:
    regenerating both re-pushes the new body AND flags the missing scope."""
    result = as_install_sheet_dashboard(**_DASH_PARAMS)
    sid = result["script_id"]
    data_scope = container_data_scope("sheets")

    # Simulate an OLDER live deployment: drop the container data scope from the
    # manifest the update reads back (as if it predated the PR-G fix).
    live = json.loads(fake_api.content_by_script[sid]["files"][0]["source"])
    live["oauthScopes"] = [s for s in live["oauthScopes"] if s != data_scope]
    fake_api.content_by_script[sid]["files"][0]["source"] = json.dumps(live)
    # Bump the body so the content genuinely changes (status=updated).
    _bump_builder(monkeypatch)

    out = as_update_automation(script_id=sid)

    assert out["regenerated_from_recipe"] is True
    assert out["status"] == "updated"
    assert out["needs_reactivation"] is True
    assert data_scope in out["added_scopes"]
    # The shared activation fields are handed back for the one-time re-Allow.
    assert out["activation_required"] is True
    assert out["activation_url"].endswith(f"/d/{sid}/edit")
    assert "Run once" in out["activation_instructions"]


# ---------------------------------------------------------------------
# Restricted-scope guard invariant on the regeneration manifest.
# ---------------------------------------------------------------------


def test_regeneration_manifest_goes_through_the_restricted_scope_guard(
    fake_api, monkeypatch
):
    """The regeneration manifest is built by the SAME build_manifest a fresh
    install uses, so its restricted-scope guard rejects a restricted scope in a
    recipe's manifest exactly as an install would (invariant preserved)."""
    restricted = "https://www.googleapis.com/auth/gmail.readonly"
    assert is_restricted_scope(restricted)

    spec = RecipeSpec(
        name="as_test_restricted",
        title="test restricted recipe",
        summary="a synthetic recipe declaring a restricted scope",
        container_kind="sheets",
        build=lambda p: "function onOpen(e){}",
        manifest_plan=lambda p, kind: {"oauth_scopes": [restricted]},
        observability="none",
        activation_model="menu",
        activation_function=None,
        project_name=lambda p: "test restricted",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={},
        example_params=({},),
        version="1",
    )
    monkeypatch.setitem(RECIPES, "as_test_restricted", spec)

    me = _ledger_user_id()
    automation_ledger.record_automation(
        user_id=me, script_id="RST1", tool="as_test_restricted",
        container_id="SHEET1", container_kind="sheets",
        recipe="as_test_restricted", recipe_params={"sheet_id": "SHEET1"},
    )

    # Regenerating renders the manifest through build_manifest, which raises on
    # the restricted scope (allow_restricted_scopes defaults False on render).
    with pytest.raises((ValueError, ToolError), match="(?i)restricted"):
        as_update_automation(script_id="RST1")


# ---------------------------------------------------------------------
# (c) A recipe-LESS row keeps the caller-supplied-body path unchanged.
# ---------------------------------------------------------------------


def test_recipe_less_row_uses_the_caller_supplied_body_path(fake_api):
    me = _ledger_user_id()
    # A raw as_generate_bound_script mint records recipe=NULL.
    automation_ledger.record_automation(
        user_id=me, script_id="RAW1", tool="as_generate_bound_script",
        container_id="DOC1", container_kind="docs", deployment_id="DEP-RAW1",
        content_hash="oldhash", handler_functions=[],
    )
    row = automation_ledger.get_automation("RAW1")
    assert row["recipe"] is None

    # Omitting the body on a recipe-less row is an error (nothing to regenerate).
    with pytest.raises(ValueError, match="not installed from a recipe"):
        as_update_automation(script_id="RAW1")

    # With a body, the caller path runs: regenerated_from_recipe is False.
    out = as_update_automation(
        script_id="RAW1", script_body="function doGet(e){ /* v2 */ }"
    )
    assert out["regenerated_from_recipe"] is False
    assert out["status"] == "updated"
    # The row stays recipe-less (COALESCE preserves the NULL).
    assert automation_ledger.get_automation("RAW1")["recipe"] is None


def test_params_override_rejected_on_the_caller_body_path(fake_api):
    me = _ledger_user_id()
    automation_ledger.record_automation(
        user_id=me, script_id="RAW1", tool="as_generate_bound_script",
        container_id="DOC1", container_kind="docs", content_hash="h",
    )
    with pytest.raises(ValueError, match="params overrides apply only"):
        as_update_automation(
            script_id="RAW1", script_body="function f(){}",
            params={"x": 1},
        )


# ---------------------------------------------------------------------
# video_deck: a per-install token means it cannot be updated in place.
# ---------------------------------------------------------------------


def test_video_deck_row_refuses_in_place_update(fake_api):
    me = _ledger_user_id()
    automation_ledger.record_automation(
        user_id=me, script_id="VID1", tool="as_generate_video_deck",
        container_id="PRES1", container_kind="slides",
        recipe="as_generate_video_deck",
        recipe_params={"presentation_id": "PRES1", "name": None},
    )
    # Refused whether or not a body is passed (a caller cannot author a valid
    # single-use upload token either).
    with pytest.raises(ToolError, match="video-deck renderer"):
        as_update_automation(script_id="VID1")
    with pytest.raises(ToolError, match="video-deck renderer"):
        as_update_automation(
            script_id="VID1", script_body="function renderFrames(){}"
        )
