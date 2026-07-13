"""Tests for services/apps_script/form_handler.py (ROADMAP_SPECS #8).

``as_install_form_handler`` is a USE-CASE tool composing the PR-Δ7
bound-script primitive: it installs a reactive **installable**
``onFormSubmit`` trigger bound to a Google Form. This is the path that
LIFTS the Forms hard-rejection: the generic primitive's
``auto_detect_container_kind`` rejects Forms (no menu/sidebar/onEdit
surface), but a Form's one reactive surface — the submit trigger — is real,
so this purpose-built tool binds DIRECTLY to the Form ID (never calling
auto_detect_container_kind) to unlock it. The deploy machinery (create /
push / deploy) is REUSED from the primitive's api.py and is already covered
by ``test_api.py``; this file covers THIS module's own contributions:

  * pure ``.gs`` body synthesis — the caller's handler function +
    a generated ``installTrigger()`` with dedup-then-create logic wiring
    ``ScriptApp.newTrigger(h).forForm(id).onFormSubmit().create()``;
  * handler-name extraction from the handler function body;
  * the derived manifest scope (an installable onFormSubmit trigger ⇒
    ``script.scriptapp``, supplied via oauth_scopes);
  * input validation (empty form_id / empty-or-unnamed body) → error;
  * the tool happy-path end-to-end at the @workspace_tool boundary,
    including the HONEST trigger-activation state in the return payload;
  * that the Forms rejection IS lifted here (a Form ID deploys cleanly,
    no auto_detect_container_kind call) while it STAYS in force for the
    generic api.auto_detect_container_kind path.

Fixture pattern mirrors ``test_sheet_dashboard.py``.
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
from appscriptly.services.apps_script import form_handler
from appscriptly.services.apps_script.form_handler import (
    _extract_handler_name,
    build_form_handler_script_body,
)

_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# A representative caller-supplied onFormSubmit handler body.
_HANDLER_FN = (
    "function onSubmit(e) {\n"
    "  var resp = e.response;\n"
    "  MailApp.sendEmail('me@example.com', 'New submission', resp.getId());\n"
    "}"
)


# ---------------------------------------------------------------------
# Creds fixture — patch the SCOPE-AWARE path (this tool declares scopes)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=GAS_BOUND_SCOPES) envelope doesn't
    launch real OAuth."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(form_handler, "_get_credentials", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """An Apps Script v1 stub wired for create→push→deploy."""
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "FORM-1",
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
def with_form_container():
    """Apps Script stub wired for the full create→push→deploy flow.

    Crucially: NO Drive stub is registered. The tool binds directly to the
    Form ID and must NEVER call auto_detect_container_kind (which would hit
    the Drive API and reject the Form). If the tool tried to detect, the
    InMemoryGoogleAPIClient would raise on the missing ('drive','v3') key —
    so a clean deploy here is itself proof the Forms-reject path is bypassed.
    """
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


def _last_pushed_files(script: MagicMock) -> list[dict]:
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    return body_calls[-1].kwargs["body"]["files"]


def _pushed_code_source(script: MagicMock) -> str:
    files = _last_pushed_files(script)
    return next(f for f in files if f["type"] == "SERVER_JS")["source"]


def _pushed_manifest(script: MagicMock) -> dict:
    files = _last_pushed_files(script)
    return json.loads(next(f for f in files if f["type"] == "JSON")["source"])


# =====================================================================
# Pure helper: handler extraction
# =====================================================================


def test_extract_handler_name_parses_function_name():
    assert _extract_handler_name(_HANDLER_FN) == "onSubmit"


def test_extract_handler_name_rejects_unnamed_function():
    with pytest.raises(ValueError, match="NAMED function declaration"):
        _extract_handler_name("const f = (e) => { route(e); };")


# =====================================================================
# Pure helper: full script-body synthesis
# =====================================================================


def test_build_script_body_includes_handler_function_verbatim():
    body, handler = build_form_handler_script_body(_HANDLER_FN, "FORM-1")
    assert handler == "onSubmit"
    assert "function onSubmit(e)" in body
    assert "e.response" in body


def test_build_script_body_defines_install_trigger_function():
    body, _ = build_form_handler_script_body(_HANDLER_FN, "FORM-1")
    assert "function installTrigger()" in body
    # The handler is now a guarded wrapper (observability: emails the owner
    # on failure, then rethrows) that delegates to the caller's onSubmit.
    assert "ScriptApp.newTrigger(handlerName)" in body
    assert 'var handlerName = "__appscriptlyGuarded_onSubmit__"' in body
    assert "return onSubmit(e);" in body


def test_build_script_body_wires_for_form_on_form_submit():
    """The synthesized installTrigger must bind the handler to THIS form via
    forForm(id).onFormSubmit().create() — the load-bearing shape from the
    spec."""
    body, _ = build_form_handler_script_body(_HANDLER_FN, "FORM-XYZ")
    assert ".forForm(" in body
    assert '"FORM-XYZ"' in body
    assert ".onFormSubmit()" in body
    assert ".create();" in body


def test_build_script_body_dedupes_existing_triggers_before_create():
    body, _ = build_form_handler_script_body(_HANDLER_FN, "FORM-1")
    assert "ScriptApp.getProjectTriggers()" in body
    assert "ScriptApp.deleteTrigger(" in body
    assert body.index("deleteTrigger") < body.index("newTrigger(handlerName)")


def test_build_script_body_embeds_handler_note_as_comment():
    body, _ = build_form_handler_script_body(
        _HANDLER_FN, "FORM-1", handler_note="route to CRM"
    )
    assert "// route to CRM" in body


def test_build_script_body_note_cannot_break_out_of_comment():
    body, _ = build_form_handler_script_body(
        _HANDLER_FN, "FORM-1", handler_note="evil */ doEvil();"
    )
    assert "*/ doEvil();" not in body
    assert "* /" in body


def test_build_script_body_form_id_with_quote_is_escaped():
    body, _ = build_form_handler_script_body(_HANDLER_FN, 'a"b')
    assert 'forForm("a\\"b")' in body


# =====================================================================
# Tool happy-path (end-to-end at the @workspace_tool boundary)
# =====================================================================


def test_install_form_handler_happy_path_returns_envelope(with_form_container):
    result = form_handler.as_install_form_handler(
        form_id="FORM-1",
        handler_function_body=_HANDLER_FN,
    )
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["form_id"] == "FORM-1"
    assert result["trigger_type"] == "onFormSubmit"
    assert result["trigger_handler"] == "onSubmit"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"


def test_install_form_handler_lifts_forms_rejection(with_form_container):
    """THE load-bearing test for this PR: a Form ID deploys CLEANLY through
    this tool. The generic primitive rejects Forms; this purpose-built path
    binds directly to the Form (no auto_detect_container_kind), so the
    deploy succeeds and binds via parentId=form_id. (The fixture registers
    NO Drive stub — if the tool had tried to auto-detect the container kind
    it would have raised on the missing drive client; a clean return proves
    the reject path is bypassed.)"""
    result = form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    assert result["form_id"] == "FORM-1"
    # Bound directly to the Form via parentId — no kind-detection round-trip.
    body_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "FORM-1"


def test_generic_auto_detect_still_rejects_forms():
    """The Forms rejection is lifted ONLY on this purpose-built path — the
    generic api.auto_detect_container_kind STILL rejects a Form mimeType
    (so menu/sidebar/edit tools that route through it keep rejecting Forms).
    """
    from appscriptly.services.apps_script.api import auto_detect_container_kind

    drive = MagicMock(name="drive-v3-stub")
    drive.files().get().execute.return_value = {
        "id": "FORM-1",
        "name": "Survey",
        "mimeType": "application/vnd.google-apps.form",
    }
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
    })):
        with pytest.raises(ValueError, match="not a"):
            auto_detect_container_kind(MagicMock(name="creds"), "FORM-1")


def test_install_form_handler_reports_honest_trigger_state(with_form_container):
    """The deploy wires but does NOT activate the trigger — the payload
    must say so (trigger_active False, activation_required True, with an
    instruction)."""
    result = form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    assert result["trigger_active"] is False
    assert result["activation_required"] is True
    assert "installTrigger" in result["activation_instructions"]


def test_install_form_handler_pushes_synthesized_body(with_form_container):
    form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    source = _pushed_code_source(with_form_container)
    assert "function onSubmit(e)" in source
    assert "function installTrigger()" in source
    assert ".forForm(" in source
    assert ".onFormSubmit()" in source


def test_install_form_handler_manifest_declares_trigger_scope(with_form_container):
    """An installable onFormSubmit trigger ⇒ the manifest must declare
    script.scriptapp. And the internal __plan__ echo must be stripped."""
    form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )
    manifest = _pushed_manifest(with_form_container)
    assert _TRIGGER_SCOPE in manifest["oauthScopes"]
    # Observability (gap #5): the failure reporter's send-only mail scope.
    assert (
        "https://www.googleapis.com/auth/script.send_mail"
        in manifest["oauthScopes"]
    )
    assert "__plan__" not in manifest


def test_install_form_handler_uses_custom_name_when_given(with_form_container):
    form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
        name="Submission Router",
    )
    body_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "Submission Router"


# =====================================================================
# Validation errors (rejected before any API call)
# =====================================================================


def test_install_form_handler_rejects_empty_form_id(with_form_container):
    with pytest.raises(ValueError, match="form_id cannot be empty"):
        form_handler.as_install_form_handler(
            form_id="   ", handler_function_body=_HANDLER_FN,
        )
    create_calls = [
        c for c in with_form_container.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_calls


def test_install_form_handler_rejects_empty_handler_body(with_form_container):
    with pytest.raises(ValueError, match="handler_function_body cannot be empty"):
        form_handler.as_install_form_handler(
            form_id="FORM-1", handler_function_body="   ",
        )


def test_install_form_handler_rejects_unnamed_handler_body(with_form_container):
    with pytest.raises(ValueError, match="NAMED function declaration"):
        form_handler.as_install_form_handler(
            form_id="FORM-1",
            handler_function_body="const f = (e) => { route(e); };",
        )


# =====================================================================
# API error path → ToolError (standard creds=True envelope)
# =====================================================================


def test_install_form_handler_api_httperror_maps_to_tool_error():
    script = MagicMock(name="script-v1-stub-erroring")
    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            form_handler.as_install_form_handler(
                form_id="FORM-1", handler_function_body=_HANDLER_FN,
            )


# =====================================================================
# Decorator-envelope cross-check: scope-aware creds resolution fires
# =====================================================================


def test_install_form_handler_resolves_creds_via_scope_aware_path(
    with_form_container, monkeypatch
):
    """Canary: because this tool DECLARES scopes, resolution flows through
    the scope-aware path with the tool's declared scopes as extra_scopes."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    form_handler.as_install_form_handler(
        form_id="FORM-1", handler_function_body=_HANDLER_FN,
    )

    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
