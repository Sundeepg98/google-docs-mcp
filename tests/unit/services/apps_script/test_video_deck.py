"""Tests for services/apps_script/video_deck.py (PR-Δ11).

``as_generate_video_deck`` is the RENDER half of the slides-to-video
pipeline — a use-case tool COMPOSING the PR-Δ7 bound-script primitive.

Base-tier redesign: the generated ``renderFrames()`` POSTs each PNG to
the appscriptly server's signed frame-staging endpoint (no Drive folder,
no drive.readonly). The tool mints a batch id + signed upload URL, bakes
them into the script, and returns ``frames_batch_id`` for the downstream
``as_encode_video`` call.
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
from appscriptly.services.apps_script import video_deck
from appscriptly.services.apps_script.video_deck import (
    build_video_deck_script,
)

_SLIDES_MIME = "application/vnd.google-apps.presentation"

_PID = "DECK-XYZ"
_UPLOAD_BASE = "https://example.fly.dev/upload/frames/BATCHaaaaaaaaaaaaaaaa"
_UPLOAD_TOKEN = "1999999999.deadbeefcafe"


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Scope-aware creds patch + the env the tool needs to mint a batch
    (resolve_runtime_oauth_config + keys.get_key('signed_url'))."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-bearer-token-32-characters-x")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS_JSON",
        json.dumps({"web": {"client_id": "x", "client_secret": "y"}}),
    )


def _make_script_stub() -> MagicMock:
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "DECK1",
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
        "id": "DECK1", "name": "deck", "mimeType": mimetype,
    }
    return drive


@pytest.fixture
def with_slides_container():
    drive = _make_drive_stub(_SLIDES_MIME)
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        yield drive, script


# ---------------------------------------------------------------------
# Pure script generation — build_video_deck_script (base-tier)
# ---------------------------------------------------------------------


def _gen() -> str:
    return build_video_deck_script(_PID, _UPLOAD_BASE, _UPLOAD_TOKEN)


def test_script_has_onopen_video_menu():
    src = _gen()
    assert "function onOpen() {" in src
    assert "SlidesApp.getUi()" in src
    assert "createMenu('Video')" in src
    assert ".addItem('Render frames', 'renderFrames')" in src


def test_script_has_renderframes_function():
    src = _gen()
    assert "function renderFrames() {" in src
    assert "SlidesApp.getActivePresentation()" in src
    assert "getSlides()" in src


def test_script_calls_getthumbnail_at_large_size():
    src = _gen()
    assert "Slides.Presentations.Pages.getThumbnail(" in src
    assert "'thumbnailProperties.thumbnailSize': THUMBNAIL_SIZE" in src
    assert "var THUMBNAIL_SIZE = 'LARGE';" in src


def test_script_posts_frames_to_server_not_drive():
    """The base-tier handoff: POST each PNG to the server, NOT to Drive."""
    src = _gen()
    assert "DriveApp.createFolder" not in src
    assert "folder.createFile" not in src
    assert "manifest.json" not in src
    assert "UrlFetchApp.fetch(thumbnail.contentUrl)" in src
    assert "method: 'post'" in src
    assert "contentType: 'image/png'" in src
    assert "payload: response.getBlob().getBytes()" in src


def test_script_injects_signed_upload_target():
    src = _gen()
    assert f"var UPLOAD_BASE = {json.dumps(_UPLOAD_BASE)};" in src
    assert f"var UPLOAD_TOKEN = {json.dumps(_UPLOAD_TOKEN)};" in src
    assert "UPLOAD_BASE + '/' + frameNumber" in src
    assert "encodeURIComponent(UPLOAD_TOKEN)" in src


def test_script_checks_upload_http_status():
    src = _gen()
    assert "getResponseCode()" in src
    assert "throw new Error" in src


def test_script_embeds_presentation_id_as_js_literal():
    src = _gen()
    assert f"var PRESENTATION_ID = {json.dumps(_PID)};" in src


def test_script_escapes_injected_values_safely():
    nasty = '";os.exit();//'
    src = build_video_deck_script(_PID, _UPLOAD_BASE, nasty)
    assert json.dumps(nasty) in src


def test_script_generation_is_deterministic():
    assert _gen() == _gen()


def test_script_ends_with_trailing_newline():
    assert _gen().endswith("\n")


# ---------------------------------------------------------------------
# Manifest scope derivation — base-tier render scopes (no drive at all)
# ---------------------------------------------------------------------


def test_manifest_declares_render_scopes_without_drive():
    from appscriptly.services.apps_script.api import build_manifest

    manifest = build_manifest({"oauth_scopes": video_deck._RENDER_SCOPES})
    scopes = manifest["oauthScopes"]
    assert "https://www.googleapis.com/auth/presentations" in scopes
    assert "https://www.googleapis.com/auth/script.external_request" in scopes
    assert "https://www.googleapis.com/auth/drive.file" not in scopes
    assert "https://www.googleapis.com/auth/drive.readonly" not in scopes
    assert manifest["runtimeVersion"] == "V8"


# ---------------------------------------------------------------------
# Tool happy-path
# ---------------------------------------------------------------------


def test_tool_happy_path_returns_envelope(with_slides_container):
    result = video_deck.as_generate_video_deck(presentation_id="DECK1")
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["presentation_id"] == "DECK1"
    assert result["render_function"] == "renderFrames"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    assert isinstance(result["frames_batch_id"], str) and result["frames_batch_id"]


def test_tool_binds_via_parent_id(with_slides_container):
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "DECK1"


def test_tool_pushes_renderframes_body(with_slides_container):
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert any("function renderFrames()" in f["source"] for f in code_files)


def test_tool_pushed_script_posts_to_the_returned_batch(with_slides_container):
    """The batch id in the envelope must match the upload URL baked into the
    deployed script."""
    _drive, script = with_slides_container
    result = video_deck.as_generate_video_deck(presentation_id="DECK1")
    batch_id = result["frames_batch_id"]
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code = next(f for f in files if f["type"] == "SERVER_JS")["source"]
    assert f"/upload/frames/{batch_id}" in code


def test_tool_pushes_manifest_without_drive_scope(with_slides_container):
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    scopes = parsed["oauthScopes"]
    assert "https://www.googleapis.com/auth/script.external_request" in scopes
    assert "https://www.googleapis.com/auth/drive.readonly" not in scopes
    assert "https://www.googleapis.com/auth/drive.file" not in scopes
    assert "__plan__" not in parsed


def test_tool_activation_note_says_frames_not_generated(with_slides_container):
    result = video_deck.as_generate_video_deck(presentation_id="DECK1")
    note = result["activation_note"]
    assert "NOT generated yet" in note
    assert "Render frames" in note or "renderFrames" in note
    assert result["frames_batch_id"] in note
    assert result["frames_expected"] is None


# ---------------------------------------------------------------------
# Slides-only container guard
# ---------------------------------------------------------------------


def test_tool_rejects_docs_container():
    drive = _make_drive_stub("application/vnd.google-apps.document")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="requires a Google Slides"):
            video_deck.as_generate_video_deck(presentation_id="DOC1")
        create_body_calls = [
            c for c in script.projects().create.call_args_list if "body" in c.kwargs
        ]
        assert not create_body_calls


def test_tool_rejects_sheets_container():
    drive = _make_drive_stub("application/vnd.google-apps.spreadsheet")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="requires a Google Slides"):
            video_deck.as_generate_video_deck(presentation_id="SHEET1")


# ---------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------


def test_tool_rejects_empty_presentation_id(with_slides_container):
    with pytest.raises(ValueError, match="presentation_id cannot be empty"):
        video_deck.as_generate_video_deck(presentation_id="   ")


# ---------------------------------------------------------------------
# API error path + creds canary
# ---------------------------------------------------------------------


def test_tool_api_httperror_maps_to_tool_error():
    drive = _make_drive_stub(_SLIDES_MIME)
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
            video_deck.as_generate_video_deck(presentation_id="DECK1")


def test_tool_resolves_creds_via_scope_aware_path(with_slides_container, monkeypatch):
    """Because the tool declares scopes=GAS_BOUND_SCOPES, the decorator
    resolves creds via the scope-aware auth.load_credentials path."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
