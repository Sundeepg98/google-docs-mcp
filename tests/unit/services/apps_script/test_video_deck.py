"""Tests for services/apps_script/video_deck.py (PR-Δ11).

``as_generate_video_deck`` is the RENDER half of the slides-to-video
pipeline — a use-case tool COMPOSING the PR-Δ7 bound-script primitive.
Coverage splits into:

  * **Pure script generation** (``build_video_deck_script``) — the
    ``onOpen`` "Video" menu + ``renderFrames`` loop are emitted correctly
    and safely (getThumbnail at LARGE, UrlFetch, folder/file creation,
    manifest write, zero-padded frame names, JS-string escaping,
    determinism).
  * **Manifest scope derivation** — reusing #138's ``build_manifest`` with
    an ``oauth_scopes`` list declares presentations / drive.file /
    script.external_request.
  * **Slides-only container guard** — the tool validates the container is
    a Slides deck (via ``auto_detect_container_kind``) and rejects Docs /
    Sheets before any project is created.
  * **Validation** — empty presentation_id / bad frame_prefix / blank
    folder override raise ValueError before any API call.
  * **Tool happy-path** — end-to-end at the ``@workspace_tool(creds=True,
    scopes=...)`` boundary via ``InMemoryGoogleAPIClient``, incl. the
    HONEST activation_note (frames not yet generated).

Fixture pattern copied from the sibling apps_script tests: because the
tool DECLARES ``scopes=GAS_BOUND_SCOPES``, the decorator takes the
SCOPE-AWARE credential path (``auth.load_credentials`` in stdio test
mode), so the fixture patches ``auth.load_credentials`` (NOT just
``_get_credentials_fn``). And because this tool AUTO-DETECTS the container
kind (to validate it's Slides), a Drive stub IS needed (contrast with
the doc_menu/sheet_dashboard fixtures which know their kind).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from google_docs_mcp import decorators
from google_docs_mcp.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from google_docs_mcp.services.apps_script import video_deck
from google_docs_mcp.services.apps_script.video_deck import (
    build_video_deck_script,
)

_SLIDES_MIME = "application/vnd.google-apps.presentation"


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Scope-aware creds patch (same as the sibling apps_script tests).

    This tool declares scopes, so resolution flows through
    ``auth.load_credentials(..., extra_scopes=scopes)`` in stdio test
    mode — patch THAT, plus the no-scope fallback for belt-and-suspenders.
    """
    from google_docs_mcp import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """Apps Script v1 stub: create / updateContent / versions /
    deployments pre-wired to plausible defaults."""
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
    """Drive resolves the container to a Slides deck; Apps Script stub
    wired for the full create→push→deploy flow."""
    drive = _make_drive_stub(_SLIDES_MIME)
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        yield drive, script


# ---------------------------------------------------------------------
# Pure script generation — build_video_deck_script
# ---------------------------------------------------------------------


def test_script_has_onopen_video_menu():
    """The generated body defines onOpen and creates a "Video" menu with
    a "Render frames" item via SlidesApp.getUi()."""
    src = build_video_deck_script("DECK1", "frames-folder", "frame")
    assert "function onOpen() {" in src
    assert "SlidesApp.getUi()" in src
    assert "createMenu('Video')" in src
    assert ".addItem('Render frames', 'renderFrames')" in src


def test_script_has_renderframes_function():
    """The generated body defines the renderFrames() export loop."""
    src = build_video_deck_script("DECK1", "frames-folder", "frame")
    assert "function renderFrames() {" in src
    assert "SlidesApp.getActivePresentation()" in src
    assert "getSlides()" in src


def test_script_calls_getthumbnail_at_large_size():
    """renderFrames calls the advanced Slides service getThumbnail at
    LARGE size (1600px) per slide."""
    src = build_video_deck_script("DECK1", "frames-folder", "frame")
    assert "Slides.Presentations.Pages.getThumbnail(" in src
    assert "'thumbnailProperties.thumbnailSize': THUMBNAIL_SIZE" in src
    assert "var THUMBNAIL_SIZE = 'LARGE';" in src


def test_script_fetches_content_url_and_creates_files():
    """The renderer fetches the PNG bytes from the thumbnail contentUrl
    via UrlFetchApp and saves a file per slide to a created Drive folder."""
    src = build_video_deck_script("DECK1", "frames-folder", "frame")
    assert "UrlFetchApp.fetch(thumbnail.contentUrl)" in src
    assert "DriveApp.createFolder(OUTPUT_FOLDER_NAME)" in src
    assert "folder.createFile(blob)" in src


def test_script_names_frames_with_prefix_and_zero_padding():
    """Frames are named <prefix>_001.png — the prefix is embedded and the
    1-based index zero-padded to 3 digits."""
    src = build_video_deck_script("DECK1", "frames-folder", "myframe")
    assert 'var FRAME_PREFIX = "myframe";' in src
    # zero-pad logic + .png suffix assembly present
    assert "('000' + frameNumber).slice(-3)" in src
    assert "FRAME_PREFIX + '_' + padded + '.png'" in src


def test_script_writes_ordered_manifest():
    """A manifest.json is written listing the frames in order (frameCount
    + frames array) for the downstream encode step."""
    src = build_video_deck_script("DECK1", "frames-folder", "frame")
    assert "'manifest.json'" in src
    assert "frameCount: frames.length" in src
    assert "frames: frames" in src
    assert "'application/json'" in src


def test_script_embeds_presentation_id_as_js_literal():
    """The deck ID is embedded as a JS string literal for the explicit
    getThumbnail presentationId argument."""
    src = build_video_deck_script("DECK-XYZ", "frames-folder", "frame")
    assert 'var PRESENTATION_ID = "DECK-XYZ";' in src


def test_script_escapes_folder_name_safely():
    """A folder name with a quote can't break out of the JS string
    literal (json.dumps escaping)."""
    src = build_video_deck_script("DECK1", 'evil" + injection', "frame")
    # The dangerous quote is escaped inside the literal; no raw break-out.
    assert 'var OUTPUT_FOLDER_NAME = "evil\\" + injection";' in src


def test_script_generation_is_deterministic():
    """Same inputs → byte-identical output (pure function)."""
    a = build_video_deck_script("DECK1", "frames", "frame")
    b = build_video_deck_script("DECK1", "frames", "frame")
    assert a == b


def test_script_ends_with_trailing_newline():
    """Conventional trailing newline for source files."""
    src = build_video_deck_script("DECK1", "frames", "frame")
    assert src.endswith("\n")


# ---------------------------------------------------------------------
# Manifest scope derivation
# ---------------------------------------------------------------------


def test_manifest_declares_render_scopes():
    """The tool builds a manifest declaring the three scopes the renderer
    exercises (presentations + drive.file + script.external_request)."""
    from google_docs_mcp.services.apps_script.api import build_manifest

    manifest = build_manifest({"oauth_scopes": video_deck._RENDER_SCOPES})
    scopes = manifest["oauthScopes"]
    assert "https://www.googleapis.com/auth/presentations" in scopes
    assert "https://www.googleapis.com/auth/drive.file" in scopes
    assert "https://www.googleapis.com/auth/script.external_request" in scopes
    # Always V8 + a timeZone (the #138 manifest invariant).
    assert manifest["runtimeVersion"] == "V8"
    assert "timeZone" in manifest


# ---------------------------------------------------------------------
# Tool happy-path
# ---------------------------------------------------------------------


def test_tool_happy_path_returns_envelope(with_slides_container):
    """End-to-end: validate Slides → create → push → deploy → envelope."""
    result = video_deck.as_generate_video_deck(presentation_id="DECK1")
    assert result["script_id"] == "SCRIPT-1"
    assert result["deployment_id"] == "DEPLOY-1"
    assert result["presentation_id"] == "DECK1"
    assert result["render_function"] == "renderFrames"
    assert result["project_url"] == "https://script.google.com/d/SCRIPT-1/edit"
    # Default folder name when not supplied.
    assert result["output_folder_name"] == "appscriptly video frames"


def test_tool_binds_via_parent_id(with_slides_container):
    """The create call passes parentId=presentation_id — the binding."""
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "DECK1"


def test_tool_pushes_renderframes_body(with_slides_container):
    """The deployed .gs (pushed via updateContent) contains renderFrames."""
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert any("function renderFrames()" in f["source"] for f in code_files)


def test_tool_pushes_manifest_with_render_scopes(with_slides_container):
    """The pushed appsscript.json declares the render scopes (and no
    internal __plan__ key leaks into the manifest)."""
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert (
        "https://www.googleapis.com/auth/script.external_request"
        in parsed["oauthScopes"]
    )
    assert "__plan__" not in parsed


def test_tool_activation_note_says_frames_not_generated(with_slides_container):
    """HONEST activation: the return note must say frames are NOT yet
    generated and name the one-time render step (mirrors #140's caveat)."""
    result = video_deck.as_generate_video_deck(presentation_id="DECK1")
    note = result["activation_note"]
    assert "NOT generated yet" in note
    assert "Render frames" in note or "renderFrames" in note
    # frames_expected is null on deploy (count only known after render).
    assert result["frames_expected"] is None


def test_tool_custom_folder_name_used(with_slides_container):
    """A supplied output_folder_name is sanitized + threaded into both the
    envelope and the generated script."""
    _drive, script = with_slides_container
    result = video_deck.as_generate_video_deck(
        presentation_id="DECK1", output_folder_name="My Video Frames",
    )
    assert result["output_folder_name"] == "My Video Frames"
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code = next(f for f in files if f["type"] == "SERVER_JS")["source"]
    assert '"My Video Frames"' in code


def test_tool_custom_frame_prefix_threaded(with_slides_container):
    """A custom frame_prefix lands in the generated script."""
    _drive, script = with_slides_container
    video_deck.as_generate_video_deck(
        presentation_id="DECK1", frame_prefix="slide",
    )
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code = next(f for f in files if f["type"] == "SERVER_JS")["source"]
    assert 'var FRAME_PREFIX = "slide";' in code


# ---------------------------------------------------------------------
# Slides-only container guard
# ---------------------------------------------------------------------


def test_tool_rejects_docs_container():
    """A Google Doc is not a video deck — reject before any project."""
    drive = _make_drive_stub("application/vnd.google-apps.document")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="requires a Google Slides"):
            video_deck.as_generate_video_deck(presentation_id="DOC1")
        # No project should have been created.
        create_body_calls = [
            c for c in script.projects().create.call_args_list if "body" in c.kwargs
        ]
        assert not create_body_calls


def test_tool_rejects_sheets_container():
    """A Google Sheet is not a video deck — reject."""
    drive = _make_drive_stub("application/vnd.google-apps.spreadsheet")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="requires a Google Slides"):
            video_deck.as_generate_video_deck(presentation_id="SHEET1")


def test_tool_rejects_form_container():
    """A Form (not even in the Doc/Sheet/Slides set) is rejected by the
    underlying auto_detect_container_kind with its own message."""
    drive = _make_drive_stub("application/vnd.google-apps.form")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError):
            video_deck.as_generate_video_deck(presentation_id="FORM1")


# ---------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------


def test_tool_rejects_empty_presentation_id(with_slides_container):
    with pytest.raises(ValueError, match="presentation_id cannot be empty"):
        video_deck.as_generate_video_deck(presentation_id="   ")


def test_tool_rejects_bad_frame_prefix(with_slides_container):
    """A frame_prefix with spaces / dots / separators is rejected (it's a
    filename stem embedded into <prefix>_001.png)."""
    with pytest.raises(ValueError, match="frame_prefix"):
        video_deck.as_generate_video_deck(
            presentation_id="DECK1", frame_prefix="bad prefix.png",
        )


def test_tool_rejects_folder_name_that_sanitizes_to_empty(with_slides_container):
    """An output_folder_name made entirely of path separators / control
    chars becomes empty after sanitize and is rejected."""
    with pytest.raises(ValueError, match="output_folder_name"):
        video_deck.as_generate_video_deck(
            presentation_id="DECK1", output_folder_name="///",
        )


def test_tool_validation_runs_before_container_check(with_slides_container):
    """Cheap client-side validation (frame_prefix) fires before the Drive
    auto-detect round-trip — a bad prefix doesn't even hit the API."""
    _drive, _script = with_slides_container
    with pytest.raises(ValueError, match="frame_prefix"):
        video_deck.as_generate_video_deck(
            presentation_id="DECK1", frame_prefix="has space",
        )


# ---------------------------------------------------------------------
# API error path + creds canary
# ---------------------------------------------------------------------


def test_tool_api_httperror_maps_to_tool_error():
    """An Apps Script HttpError on create → the @workspace_tool envelope
    translates it to ToolError."""
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
    """Canary: the @workspace_tool(creds=True, scopes=...) decorator MUST
    resolve credentials before delegating. Because this tool declares
    scopes, resolution flows through auth.load_credentials with the
    declared scopes as extra_scopes."""
    from google_docs_mcp import auth
    from google_docs_mcp.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    video_deck.as_generate_video_deck(presentation_id="DECK1")
    assert len(calls) == 1
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
