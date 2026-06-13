"""Co-located tests for services/apps_script/api.py (PR-Δ7).

Mirrors ``tests/unit/services/gas_deploy/test_api.py`` (Apps Script REST
wrapper) + ``tests/unit/services/slides/test_api.py``: exercise the
module via ``with_google_api_client(InMemoryGoogleAPIClient)`` so the
real ``get_service`` chokepoint runs but the Drive + Apps Script HTTP
boundaries are stubbed. No real OAuth, no real API round-trip.

Covers the four api surfaces (manifest builder is in its own file,
``test_manifest_builder.py``):

1. **``auto_detect_container_kind``** — docs / sheets / slides mimeType
   mapping + the ValueError for unsupported types (forms, folders, etc.).
2. **``create_bound_project``** — the ``parentId``-binding create call
   shape + idempotent=False wrapping (it must be invoked exactly once).
3. **``set_project_content``** — the manifest + .gs file push shape,
   the ``__plan__`` strip, and the empty-body ValueError.
4. **``create_deployment``** — the version-then-deploy two-call sequence
   + the deployment request shape.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.apps_script.api import (
    auto_detect_container_kind,
    create_bound_project,
    create_deployment,
    set_project_content,
)


# ---------------------------------------------------------------------
# auto_detect_container_kind — Drive mimeType → kind
# ---------------------------------------------------------------------


@pytest.fixture
def stub_drive():
    """A Drive v3 Resource stub for container-kind detection."""
    drive = MagicMock(name="drive-v3-stub")
    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        yield drive


@pytest.mark.parametrize(
    ("mime", "expected_kind"),
    [
        ("application/vnd.google-apps.document", "docs"),
        ("application/vnd.google-apps.spreadsheet", "sheets"),
        ("application/vnd.google-apps.presentation", "slides"),
    ],
)
def test_auto_detect_container_kind_maps_supported_mimetypes(
    stub_drive, mime, expected_kind
):
    """The three supported container types map to their kind string."""
    stub_drive.files().get().execute.return_value = {
        "id": "C1", "name": "thing", "mimeType": mime,
    }
    assert auto_detect_container_kind(MagicMock(), "C1") == expected_kind


def test_auto_detect_container_kind_passes_file_id_to_drive(stub_drive):
    """The Drive get call must target the container_id the caller passed."""
    stub_drive.files().get().execute.return_value = {
        "mimeType": "application/vnd.google-apps.document",
    }
    auto_detect_container_kind(MagicMock(), "CONTAINER-XYZ")
    # Find the call that actually carried a fileId.
    fileid_calls = [
        c for c in stub_drive.files().get.call_args_list if "fileId" in c.kwargs
    ]
    assert fileid_calls, "no files().get() call captured a fileId"
    assert fileid_calls[-1].kwargs["fileId"] == "CONTAINER-XYZ"


def test_auto_detect_container_kind_rejects_form(stub_drive):
    """A Google Form is bindable in raw Apps Script but doesn't fit this
    menu/sidebar/edit-trigger primitive — reject with a clear message."""
    stub_drive.files().get().execute.return_value = {
        "id": "F1", "name": "Survey",
        "mimeType": "application/vnd.google-apps.form",
    }
    with pytest.raises(ValueError, match="not a Google Doc, Sheet, or Slides"):
        auto_detect_container_kind(MagicMock(), "F1")


def test_auto_detect_container_kind_rejects_folder(stub_drive):
    """A folder is obviously not a script container."""
    stub_drive.files().get().execute.return_value = {
        "mimeType": "application/vnd.google-apps.folder",
    }
    with pytest.raises(ValueError, match="folder"):
        auto_detect_container_kind(MagicMock(), "FOLDER1")


def test_auto_detect_container_kind_rejects_pdf_naming_mimetype(stub_drive):
    """An arbitrary non-Workspace file (PDF) is rejected; the message
    names the offending mimeType so the caller can fix the ID."""
    stub_drive.files().get().execute.return_value = {
        "mimeType": "application/pdf",
    }
    with pytest.raises(ValueError, match="application/pdf"):
        auto_detect_container_kind(MagicMock(), "PDF1")


def test_auto_detect_container_kind_rejects_missing_mimetype(stub_drive):
    """Defensive: a response with no mimeType (shouldn't happen) is
    treated as unsupported rather than KeyError."""
    stub_drive.files().get().execute.return_value = {"id": "X"}
    with pytest.raises(ValueError):
        auto_detect_container_kind(MagicMock(), "X")


# ---------------------------------------------------------------------
# create_bound_project — projects.create with parentId
# ---------------------------------------------------------------------


@pytest.fixture
def stub_script():
    """An Apps Script v1 Resource stub for project + deployment calls."""
    script = MagicMock(name="script-v1-stub")
    with with_google_api_client(InMemoryGoogleAPIClient({("script", "v1"): script})):
        yield script


def test_create_bound_project_passes_title_and_parentid(stub_script):
    """The create body MUST include parentId=container_id — that's what
    BINDS the script to the container (vs a standalone project)."""
    stub_script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT1", "title": "My Automation",
        "parentId": "CONTAINER1",
    }
    create_bound_project(MagicMock(), "CONTAINER1", "My Automation")
    body_calls = [
        c for c in stub_script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls, "no projects().create() call captured a body"
    body = body_calls[-1].kwargs["body"]
    assert body == {"title": "My Automation", "parentId": "CONTAINER1"}


def test_create_bound_project_returns_raw_project_resource(stub_script):
    """Returns the raw API Project resource (caller extracts scriptId)."""
    resp = {
        "scriptId": "SCRIPT-ABC", "title": "T", "parentId": "C",
        "createTime": "2026-05-28T00:00:00Z",
    }
    stub_script.projects().create().execute.return_value = resp
    result = create_bound_project(MagicMock(), "C", "T")
    assert result == resp
    assert result["scriptId"] == "SCRIPT-ABC"


# ---------------------------------------------------------------------
# set_project_content — updateContent with manifest + .gs file
# ---------------------------------------------------------------------


def test_set_project_content_rejects_empty_body(stub_script):
    """An empty / whitespace script_body is a caller bug — reject it
    client-side before the API call."""
    with pytest.raises(ValueError, match="script_body cannot be empty"):
        set_project_content(MagicMock(), "SID", "", {"timeZone": "Etc/UTC"})
    with pytest.raises(ValueError, match="script_body cannot be empty"):
        set_project_content(MagicMock(), "SID", "   \n  ", {"timeZone": "Etc/UTC"})


def test_set_project_content_sends_manifest_and_code_file(stub_script):
    """The pushed payload must include the manifest (appsscript / JSON)
    AND the .gs body (Code / SERVER_JS)."""
    stub_script.projects().updateContent().execute.return_value = {}
    manifest = {"timeZone": "America/New_York", "runtimeVersion": "V8"}
    set_project_content(
        MagicMock(), "SID", "function onOpen(){}", manifest
    )
    body_calls = [
        c for c in stub_script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls, "no updateContent() call captured a body"
    sent_files = body_calls[-1].kwargs["body"]["files"]
    by_name = {f["name"]: f for f in sent_files}

    assert by_name["appsscript"]["type"] == "JSON"
    parsed = json.loads(by_name["appsscript"]["source"])
    assert parsed["timeZone"] == "America/New_York"
    assert parsed["runtimeVersion"] == "V8"

    assert by_name["Code"]["type"] == "SERVER_JS"
    assert by_name["Code"]["source"] == "function onOpen(){}"


def test_set_project_content_passes_script_id(stub_script):
    """updateContent must target the scriptId the caller passed."""
    stub_script.projects().updateContent().execute.return_value = {}
    set_project_content(MagicMock(), "SCRIPT-TARGET", "x()", {"timeZone": "Etc/UTC"})
    scriptid_calls = [
        c for c in stub_script.projects().updateContent.call_args_list
        if "scriptId" in c.kwargs
    ]
    assert scriptid_calls[-1].kwargs["scriptId"] == "SCRIPT-TARGET"


def test_set_project_content_strips_internal_plan_key(stub_script):
    """The private ``__plan__`` echo from build_manifest must NOT be
    serialized into appsscript.json — Apps Script would reject an unknown
    top-level manifest field."""
    stub_script.projects().updateContent().execute.return_value = {}
    manifest = {
        "timeZone": "Etc/UTC",
        "runtimeVersion": "V8",
        "__plan__": {"menu": [{"name": "X", "function_name": "x"}]},
    }
    set_project_content(MagicMock(), "SID", "function x(){}", manifest)
    body_calls = [
        c for c in stub_script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    sent_files = body_calls[-1].kwargs["body"]["files"]
    by_name = {f["name"]: f for f in sent_files}
    parsed = json.loads(by_name["appsscript"]["source"])
    assert "__plan__" not in parsed


def test_set_project_content_does_not_mutate_caller_manifest(stub_script):
    """Stripping ``__plan__`` must operate on a copy — the caller's dict
    is left intact (defensive against surprising aliasing bugs)."""
    stub_script.projects().updateContent().execute.return_value = {}
    manifest = {"timeZone": "Etc/UTC", "__plan__": {"menu": []}}
    set_project_content(MagicMock(), "SID", "f()", manifest)
    assert "__plan__" in manifest  # caller's dict untouched


# ---------------------------------------------------------------------
# create_deployment — versions.create THEN deployments.create
# ---------------------------------------------------------------------


def test_create_deployment_creates_version_then_deployment(stub_script):
    """A deployment references an immutable version, so the function must
    first snapshot a version, then deploy that versionNumber."""
    stub_script.projects().versions().create().execute.return_value = {
        "versionNumber": 7,
    }
    stub_script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP1",
    }
    create_deployment(MagicMock(), "SID", "initial deploy")

    # Version snapshot happened.
    ver_calls = [
        c for c in stub_script.projects().versions().create.call_args_list
        if "scriptId" in c.kwargs
    ]
    assert ver_calls, "versions().create() was never called with a scriptId"
    assert ver_calls[-1].kwargs["scriptId"] == "SID"
    assert ver_calls[-1].kwargs["body"]["description"] == "initial deploy"


def test_create_deployment_deploys_the_created_version_number(stub_script):
    """The deployment body's versionNumber must be the one the version
    snapshot returned (7), not a hardcoded value."""
    stub_script.projects().versions().create().execute.return_value = {
        "versionNumber": 7,
    }
    stub_script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP1",
    }
    create_deployment(MagicMock(), "SID", "desc")

    dep_calls = [
        c for c in stub_script.projects().deployments().create.call_args_list
        if "body" in c.kwargs
    ]
    assert dep_calls, "deployments().create() was never called with a body"
    body = dep_calls[-1].kwargs["body"]
    assert body["versionNumber"] == 7
    assert body["manifestFileName"] == "appsscript"
    assert body["description"] == "desc"


def test_create_deployment_passes_script_id_to_deployment(stub_script):
    """deployments.create must target the right scriptId (path param)."""
    stub_script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    stub_script.projects().deployments().create().execute.return_value = {
        "deploymentId": "D",
    }
    create_deployment(MagicMock(), "SCRIPT-DEPLOY", "desc")
    dep_calls = [
        c for c in stub_script.projects().deployments().create.call_args_list
        if "scriptId" in c.kwargs
    ]
    assert dep_calls[-1].kwargs["scriptId"] == "SCRIPT-DEPLOY"


def test_create_deployment_returns_raw_deployment_resource(stub_script):
    """Returns the raw Deployment resource (caller extracts deploymentId)."""
    stub_script.projects().versions().create().execute.return_value = {
        "versionNumber": 2,
    }
    resp = {"deploymentId": "DEP-XYZ", "deploymentConfig": {"versionNumber": 2}}
    stub_script.projects().deployments().create().execute.return_value = resp
    result = create_deployment(MagicMock(), "SID", "desc")
    assert result == resp
    assert result["deploymentId"] == "DEP-XYZ"
