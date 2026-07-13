"""Tests for services/apps_script/grade_form_responses.py (GAS parity).

``as_grade_form_responses`` composes the PR-Δ7 bound-script primitive into
a "push computed grades onto quiz responses" tool — a bound script whose
``gradeResponses()`` runs the canonical grading loop and calls
``Form.submitGrades()``. The caller supplies the per-question scorer; the
tool owns the submitGrades choreography. It's an ON-DEMAND action.

The LOAD-BEARING test here is the scope guard: ``submitGrades`` needs the
FULL ``forms`` scope, which must live in the GENERATED bound script's
manifest ONLY — and NOT in appscriptly's own consent
(``auth.WORKSPACE_SCOPES``). That invariant is asserted explicitly below.

Coverage:
  * **Pure script generation** (``build_grade_script_body``) — the scorer
    is embedded; gradeResponses runs the response×item loop, calls the
    scorer, withItemGrade, then submitGrades; onOpen menu wired.
  * **Scope guard** — the GENERATED manifest declares the full forms
    scope; appscriptly's own consent gains NO new scope.
  * **Forms-rejection lift** — binds directly to the Form ID without
    auto_detect_container_kind.
  * **Tool happy-path** — envelope incl. honest run-state + manifest_scope.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.apps_script import grade_form_responses as gfr
from appscriptly.services.apps_script.grade_form_responses import (
    build_grade_script_body,
)

_FORMS_SCOPE = "https://www.googleapis.com/auth/forms"

# A valid caller-authored per-question scorer (named function declaration).
_SCORER = (
    "function scoreItem(itemResponse, item) {\n"
    "  if (itemResponse.getResponse() === '42') { itemResponse.setScore(1); }\n"
    "  else { itemResponse.setScore(0); }\n"
    "}"
)


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "FORM1",
    }
    script.projects().updateContent().execute.return_value = {}
    script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEPLOY-1",
    }
    return script


@pytest.fixture
def with_script_client():
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


# ---------------------------------------------------------------------
# Pure script generation — build_grade_script_body
# ---------------------------------------------------------------------


def test_script_embeds_the_callers_scorer_and_parses_its_name():
    body, scorer = build_grade_script_body(_SCORER)
    assert scorer == "scoreItem"
    assert "function scoreItem(itemResponse, item) {" in body
    assert "itemResponse.setScore(1);" in body


def test_script_runs_the_grading_loop_and_submits_grades():
    body, _ = build_grade_script_body(_SCORER)
    assert "function gradeResponses() {" in body
    assert "FormApp.getActiveForm()" in body
    assert "form.getResponses()" in body
    assert "getGradableResponseForItem(item)" in body
    # calls the caller's scorer, attaches, then submits
    assert "scoreItem(itemResponse, item);" in body
    assert "response.withItemGrade(itemResponse);" in body
    assert "form.submitGrades(responses);" in body


def test_script_skips_unanswered_items():
    """A null gradable response (item the response didn't answer) is
    skipped — no scorer call, no crash."""
    body, _ = build_grade_script_body(_SCORER)
    assert "if (itemResponse === null) {" in body
    assert "continue;" in body


def test_script_has_onopen_menu_pointing_at_grade_function():
    body, _ = build_grade_script_body(_SCORER, "Quiz Tools")
    assert "function onOpen(e) {" in body
    assert "FormApp.getUi()" in body
    assert 'createMenu("Quiz Tools")' in body
    assert '.addItem("Grade responses", "gradeResponses")' in body


def test_unnamed_scorer_rejected():
    with pytest.raises(ValueError, match="NAMED function declaration"):
        build_grade_script_body("const scoreItem = (ir, it) => {};")


def test_script_is_deterministic():
    assert build_grade_script_body(_SCORER) == build_grade_script_body(_SCORER)


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_empty_form_id_rejected(with_script_client, bad_id):
    with pytest.raises(ValueError, match="form_id cannot be empty"):
        gfr.as_grade_form_responses(
            form_id=bad_id, scoring_function_body=_SCORER,
        )


@pytest.mark.parametrize("bad_body", ["", "   "])
def test_empty_scorer_rejected(with_script_client, bad_body):
    with pytest.raises(ValueError, match="scoring_function_body cannot be empty"):
        gfr.as_grade_form_responses(
            form_id="FORM1", scoring_function_body=bad_body,
        )


def test_unnamed_scorer_rejected_at_tool_boundary(with_script_client):
    with pytest.raises(ValueError, match="NAMED function declaration"):
        gfr.as_grade_form_responses(
            form_id="FORM1",
            scoring_function_body="const f = (ir, it) => {};",
        )


def test_validation_failure_makes_no_api_call(with_script_client):
    with pytest.raises(ValueError):
        gfr.as_grade_form_responses(
            form_id="", scoring_function_body=_SCORER,
        )
    create_body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_body_calls


def test_grade_returns_unified_activation_contract(with_script_client):
    """Stream 3: the legacy run_required / run_instructions aliases survive
    AND the unified activation_* fields are present, naming gradeResponses
    (an on-demand action, so activation = one run)."""
    result = gfr.as_grade_form_responses(
        form_id="FORM1", scoring_function_body=_SCORER,
    )
    # Legacy aliases preserved (back-compat).
    assert result["run_required"] is True
    assert "run_instructions" in result
    # Unified canonical fields (build_activation_fields).
    assert result["activation_required"] is True
    assert result["activation_function"] == "gradeResponses"
    assert result["activation_url"] == result["project_url"]
    assert result["activation_url"].endswith("/edit")
    assert "gradeResponses" in result["activation_instructions"]


# ---------------------------------------------------------------------
# SCOPE GUARD (load-bearing) — full forms scope in GENERATED manifest only
# ---------------------------------------------------------------------


def test_generated_manifest_declares_full_forms_scope(with_script_client):
    """submitGrades needs the FULL forms scope — it must land in the
    GENERATED bound script's manifest oauthScopes."""
    gfr.as_grade_form_responses(
        form_id="FORM1", scoring_function_body=_SCORER,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert _FORMS_SCOPE in parsed["oauthScopes"]
    # the onOpen menu also derives the UI scope
    assert (
        "https://www.googleapis.com/auth/script.container.ui"
        in parsed["oauthScopes"]
    )
    assert "__plan__" not in parsed


def test_tool_declares_only_baseline_gas_scopes_not_full_forms():
    """The TOOL itself declares only GAS_BOUND_SCOPES (script.projects +
    script.deployments) for appscriptly's own consent — NOT the full forms
    scope. The full forms scope is the bound script's, not appscriptly's.
    This is the verify-LAST guard at the tool-annotation level: scopes are
    stamped onto ``tool.annotations.scopes`` (machine-readable surface)."""
    import asyncio

    from appscriptly.server import mcp
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    # sanity: the full forms WRITE scope is not even in GAS_BOUND_SCOPES.
    assert _FORMS_SCOPE not in GAS_BOUND_SCOPES

    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == "as_grade_form_responses")
    declared = list(getattr(tool.annotations, "scopes", []) or [])
    assert declared, "as_grade_form_responses must declare its scopes"
    assert _FORMS_SCOPE not in declared, (
        "as_grade_form_responses must NOT declare the full forms scope on "
        "appscriptly's own consent — it belongs in the generated manifest."
    )
    assert set(declared) == set(GAS_BOUND_SCOPES)


def test_appscriptly_own_consent_has_no_forms_write_scope():
    """The crux: appscriptly's OWN consent (auth.WORKSPACE_SCOPES) must NOT
    contain the full forms WRITE scope. Only the read-only
    forms.responses.readonly (+ forms.body) baseline scopes are present;
    the WRITE scope this tool's generated script needs lives in the
    generated manifest, never in appscriptly's consent."""
    from appscriptly import auth

    assert _FORMS_SCOPE not in auth.WORKSPACE_SCOPES
    # sanity: the read-only response scope IS baseline (so reads work)
    assert (
        "https://www.googleapis.com/auth/forms.responses.readonly"
        in auth.WORKSPACE_SCOPES
    )


# ---------------------------------------------------------------------
# Tool happy-path
# ---------------------------------------------------------------------


def test_happy_path_returns_honest_run_state(with_script_client):
    result = gfr.as_grade_form_responses(
        form_id="FORM1", scoring_function_body=_SCORER,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["form_id"] == "FORM1"
    assert result["grade_function"] == "gradeResponses"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert result["graded_count"] is None
    assert result["run_required"] is True
    assert "gradeResponses" in result["run_instructions"]
    assert result["manifest_scope"] == _FORMS_SCOPE


def test_binds_directly_to_form_without_auto_detect(with_script_client):
    """The Forms-rejection lift: bind directly to the Form ID, never
    calling Drive auto_detect_container_kind (which rejects Forms). The
    only service used is script v1 — no drive v3 call."""
    gfr.as_grade_form_responses(
        form_id="FORM1", scoring_function_body=_SCORER,
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "FORM1"


def test_pushes_grader_as_server_js(with_script_client):
    gfr.as_grade_form_responses(
        form_id="FORM1", scoring_function_body=_SCORER,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files
    src = code_files[-1]["source"]
    assert "form.submitGrades(responses);" in src


# ---------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------


def test_api_httperror_maps_to_tool_error():
    script = MagicMock(name="script-v1-stub-erroring")
    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            gfr.as_grade_form_responses(
                form_id="FORM1", scoring_function_body=_SCORER,
            )
