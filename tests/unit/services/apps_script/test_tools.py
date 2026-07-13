"""Per-tool behavior tests for services/apps_script/tools.py (PR-Δ7).

Mirrors ``tests/unit/services/slides/test_tools.py`` /
``tests/unit/services/sheets/test_tools.py``: drive the tool end-to-end
at the ``@workspace_tool(creds=True)`` decorator boundary, using the
``InMemoryGoogleAPIClient`` + monkeypatched creds-resolution fixture
pattern. Both the Drive (container detection) and Apps Script (create /
updateContent / versions / deployments) HTTP boundaries are stubbed.

The api-shape coverage (request bodies, parentId binding, version-then-
deploy sequence, manifest serialization) lives in ``test_api.py`` +
``test_manifest_builder.py``; this file covers the tool-layer
orchestration: container-kind auto-detection vs override, the assembled
return envelope, and the error paths (invalid container → ToolError,
API HttpError → ToolError).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.apps_script import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.

    IMPORTANT — this tool declares ``scopes=GAS_BOUND_SCOPES``, so its
    decorator takes the SCOPE-AWARE resolution path
    (``decorators._resolve_credentials_for_scopes(scopes)``), NOT the
    plain ``_get_credentials_fn`` path the no-scope sheets/slides tools
    use. In stdio test context (``current_user_id_or_none()`` is None),
    that path calls ``auth.load_credentials(default_data_dir(),
    extra_scopes=scopes)`` — which would launch a REAL OAuth consent flow
    and hang. We patch ``auth.load_credentials`` (the deferred-import
    target inside the decorator) to return the stub instead. The other
    two patches keep the no-scope path covered too (belt-and-suspenders /
    in case a future refactor flips the branch)."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """An Apps Script v1 stub with create / updateContent / versions /
    deployments pre-wired to plausible defaults."""
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "C1",
    }
    script.projects().updateContent().execute.return_value = {}
    script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEPLOY-1",
    }
    return script


def _make_drive_stub(mimetype: str) -> MagicMock:
    drive = MagicMock(name="drive-v3-stub")
    drive.files().get().execute.return_value = {
        "id": "C1", "name": "container", "mimeType": mimetype,
    }
    return drive


@pytest.fixture
def with_docs_container():
    """Drive resolves the container to a Google Doc; Apps Script stub
    wired for the full create→push→deploy flow."""
    drive = _make_drive_stub("application/vnd.google-apps.document")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        yield drive, script


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_generate_bound_script_happy_path_returns_envelope(with_docs_container):
    """End-to-end: auto-detect kind → create → push → deploy → envelope."""
    result = tools.as_generate_bound_script(
        container_id="C1",
        script_body="function onOpen(){ SpreadsheetApp.getUi(); }",
    )
    assert result == {
        "script_id": "SCRIPT-1",
        "deployment_id": "DEPLOY-1",
        "on_conflict": "new",
        "reused_existing": False,
        "replaced_count": 0,
        "container_id": "C1",
        "container_kind": "docs",
        "project_url": "https://script.google.com/d/SCRIPT-1/edit",
    }


def test_generate_bound_script_auto_detects_container_kind(with_docs_container):
    """With no container_kind passed, the tool reads the Drive mimeType
    to resolve it (here: docs)."""
    drive, _script = with_docs_container
    result = tools.as_generate_bound_script(
        container_id="C1", script_body="function x(){}",
    )
    assert result["container_kind"] == "docs"
    # Drive was consulted for detection.
    fileid_calls = [
        c for c in drive.files().get.call_args_list if "fileId" in c.kwargs
    ]
    assert fileid_calls, "Drive get was not called for container detection"


def test_generate_bound_script_honors_explicit_container_kind(with_docs_container):
    """When container_kind is supplied, the tool trusts it and SKIPS the
    Drive detection round-trip entirely."""
    drive, _script = with_docs_container
    result = tools.as_generate_bound_script(
        container_id="C1",
        script_body="function x(){}",
        container_kind="sheets",  # override (even though Drive says docs)
    )
    assert result["container_kind"] == "sheets"
    # Drive detection must NOT have been invoked with a fileId.
    fileid_calls = [
        c for c in drive.files().get.call_args_list if "fileId" in c.kwargs
    ]
    assert not fileid_calls, (
        "Drive detection ran despite an explicit container_kind override"
    )


def test_generate_bound_script_binds_via_parent_id(with_docs_container):
    """The create call must pass parentId=container_id — the BINDING."""
    _drive, script = with_docs_container
    tools.as_generate_bound_script(container_id="C1", script_body="function x(){}")
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "C1"


def test_generate_bound_script_passes_script_body_to_content(with_docs_container):
    """The caller's .gs body must reach updateContent as a SERVER_JS file."""
    _drive, script = with_docs_container
    tools.as_generate_bound_script(
        container_id="C1", script_body="function customWork(){ return 42; }",
    )
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert any("customWork" in f["source"] for f in code_files)


def test_generate_bound_script_uses_custom_name_when_given(with_docs_container):
    """A supplied name becomes the project title on create."""
    _drive, script = with_docs_container
    tools.as_generate_bound_script(
        container_id="C1", script_body="function x(){}", name="My Custom Automation",
    )
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "My Custom Automation"


def test_generate_bound_script_default_name_includes_kind(with_docs_container):
    """Without a name, the default project title references the kind."""
    _drive, script = with_docs_container
    tools.as_generate_bound_script(container_id="C1", script_body="function x(){}")
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert "docs" in body_calls[-1].kwargs["body"]["title"]


def test_generate_bound_script_threads_manifest_capabilities(with_docs_container):
    """A manifest with a menu must produce an appsscript.json that
    declares the UI scope — proving the manifest flows create→content."""
    import json

    _drive, script = with_docs_container
    tools.as_generate_bound_script(
        container_id="C1",
        script_body="function onOpen(){}",
        manifest={"menu": [{"name": "Run", "function_name": "run"}]},
    )
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert (
        "https://www.googleapis.com/auth/script.container.ui"
        in parsed["oauthScopes"]
    )
    # And the internal __plan__ echo was stripped from the real manifest.
    assert "__plan__" not in parsed


# ---------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------


def test_generate_bound_script_invalid_container_raises_tool_error():
    """An unsupported container (a Form) → the ValueError from
    auto_detect bubbles as-is (the decorator envelope only translates
    HttpError; ValueError surfaces directly for the caller). We assert it
    raises and names the problem."""
    drive = _make_drive_stub("application/vnd.google-apps.form")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="not a Google Doc, Sheet, or Slides"):
            tools.as_generate_bound_script(
                container_id="FORM1", script_body="function x(){}",
            )
        # The create call must NOT have fired — detection failed first.
        create_body_calls = [
            c for c in script.projects().create.call_args_list if "body" in c.kwargs
        ]
        assert not create_body_calls


def test_generate_bound_script_api_httperror_maps_to_tool_error():
    """An Apps Script HttpError on create → the @workspace_tool envelope
    translates it to ToolError (the standard creds=True behavior)."""
    drive = _make_drive_stub("application/vnd.google-apps.document")
    script = MagicMock(name="script-v1-stub-erroring")

    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            tools.as_generate_bound_script(
                container_id="C1", script_body="function x(){}",
            )


def test_generate_bound_script_empty_body_rejected(with_docs_container):
    """An empty script_body is rejected (ValueError from
    set_project_content) — caught before any deployment happens."""
    _drive, script = with_docs_container
    with pytest.raises(ValueError, match="script_body cannot be empty"):
        tools.as_generate_bound_script(container_id="C1", script_body="   ")
    # No deployment should have been attempted.
    dep_body_calls = [
        c for c in script.projects().deployments().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not dep_body_calls


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: scope-aware creds resolution is invoked
# ---------------------------------------------------------------------


def test_generate_bound_script_resolves_creds_via_scope_aware_path(
    with_docs_container, monkeypatch
):
    """Canary: the @workspace_tool(creds=True, scopes=...) decorator MUST
    resolve credentials before delegating to the body. Because this tool
    DECLARES scopes, resolution flows through the scope-aware path
    (auth.load_credentials in stdio test mode) rather than the plain
    _get_credentials_fn the no-scope sheets/slides tools use. Assert the
    scope-aware path fires exactly once with the tool's declared scopes
    passed as extra_scopes."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    tools.as_generate_bound_script(container_id="C1", script_body="function x(){}")

    assert len(calls) == 1, (
        "auth.load_credentials was not called exactly once — the "
        "scope-aware decorator path may have changed or the fixture missed."
    )
    # The tool's declared scopes are threaded through as extra_scopes.
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
