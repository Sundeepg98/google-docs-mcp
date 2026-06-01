"""Co-located unit tests for services/gas_deploy/api.py (Apps Script REST wrapper).

Pure-mock coverage of the create -> push -> version -> deploy flow,
including the response-parsing edge cases that would be painful to
notice live (e.g. ``deploy_webapp`` raises if the response is missing
``entryPoints[].webApp.url`` — Apps Script API has reshuffled fields
before).

**M3 Phase C (v2.1.5):** moved from ``tests/unit/test_gas_deploy.py``
to its co-located home at ``tests/unit/services/gas_deploy/test_api.py``
when the corresponding source file moved from ``gas_deploy/client.py``
to ``services/gas_deploy/api.py``. Mirrors the layout established by
PR #95 (docs) and PR #96 (drive).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_script_svc():
    """Yield a fake script_v1 service via the M2 GoogleAPIClient port.

    **v2.1.2 (M2)**: pre-v2.1.2 this fixture used
    ``patch("appscriptly.services.gas_deploy.api.get_service")``, which
    required knowing exactly which module imported ``get_service``.
    The ``with_google_api_client`` + ``InMemoryGoogleAPIClient``
    pattern (introduced in this PR's M2 port) routes through the
    single facade — no import-binding awareness needed.
    """
    from appscriptly.google_api_client import (
        InMemoryGoogleAPIClient,
        with_google_api_client,
    )

    svc = MagicMock()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): svc,
    })):
        yield svc


def test_create_project_returns_script_id(mock_script_svc):
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().create().execute.return_value = {
        "scriptId": "ABC123", "title": "foo"
    }
    client = AppsScriptClient(MagicMock())
    assert client.create_project("foo") == "ABC123"


def test_push_files_sends_manifest_plus_files(mock_script_svc):
    """The pushed payload must include the manifest as JSON + every file as SERVER_JS."""
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().updateContent().execute.return_value = {}
    client = AppsScriptClient(MagicMock())
    client.push_files(
        "SCRIPT_ID",
        manifest={"timeZone": "Etc/GMT", "webapp": {"executeAs": "USER_DEPLOYING"}},
        files={"Code": "function doPost(e) { return e; }"},
    )

    # Inspect the actual body that was passed to updateContent()
    call = mock_script_svc.projects().updateContent.call_args
    sent_files = call.kwargs["body"]["files"]
    by_name = {f["name"]: f for f in sent_files}

    assert "appsscript" in by_name, "manifest file missing from updateContent"
    assert by_name["appsscript"]["type"] == "JSON"
    parsed_manifest = json.loads(by_name["appsscript"]["source"])
    assert parsed_manifest["webapp"]["executeAs"] == "USER_DEPLOYING"

    assert "Code" in by_name, "Code file missing from updateContent"
    assert by_name["Code"]["type"] == "SERVER_JS"
    assert "doPost" in by_name["Code"]["source"]


def test_create_version_returns_int(mock_script_svc):
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().versions().create().execute.return_value = {
        "versionNumber": 3
    }
    client = AppsScriptClient(MagicMock())
    assert client.create_version("SID", "desc") == 3


def test_deploy_webapp_extracts_url_from_entry_points(mock_script_svc):
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP123",
        "entryPoints": [
            {
                "entryPointType": "WEB_APP",
                "webApp": {
                    "url": "https://script.google.com/macros/s/abc/exec",
                    "entryPointConfig": {"access": "MYSELF", "executeAs": "USER_DEPLOYING"},
                },
            }
        ],
    }
    client = AppsScriptClient(MagicMock())
    d = client.deploy_webapp("SCRIPT_ID", 1)
    assert d.script_id == "SCRIPT_ID"
    assert d.deployment_id == "DEP123"
    assert d.version == 1
    assert d.url == "https://script.google.com/macros/s/abc/exec"


def test_deploy_webapp_raises_when_url_missing(mock_script_svc):
    """If Apps Script API returns a deployment without a webApp.url,
    that's an API contract break — fail loudly, don't silently produce
    an empty URL.
    """
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP123",
        "entryPoints": [
            {"entryPointType": "ADD_ON", "addOn": {}}  # no webApp
        ],
    }
    client = AppsScriptClient(MagicMock())
    with pytest.raises(RuntimeError, match="no webApp.url"):
        client.deploy_webapp("SCRIPT_ID", 1)


def test_deploy_webapp_body_does_not_include_entryPoints(mock_script_svc):
    """v1.1.1 regression guard. Apps Script API rejects entryPoints in
    the deployments.create body:

        HttpError 400: Invalid JSON payload received.
        Unknown name "entryPoints": Cannot find field.

    entryPoints belong in the manifest (appsscript.json), pushed via
    push_files. The deployment body must carry ONLY versionNumber,
    manifestFileName, and description.
    """
    from appscriptly.services.gas_deploy import AppsScriptClient

    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "D", "entryPoints": [{"webApp": {"url": "u"}}],
    }
    client = AppsScriptClient(MagicMock())
    client.deploy_webapp("S", 1, description="test")
    body = mock_script_svc.projects().deployments().create.call_args.kwargs["body"]

    assert "entryPoints" not in body, (
        "deployments.create body includes entryPoints — "
        "Apps Script API will reject with 400 'Unknown name entryPoints'"
    )
    assert body == {
        "versionNumber": 1,
        "manifestFileName": "appsscript",
        "description": "test",
    }


def test_gas_deploy_scopes_constant_lists_required_scopes():
    """Smoke test on the public scope list — must include the script.*
    scopes we know are required, and the drive.file scope projects.create
    needs.
    """
    from appscriptly.services.gas_deploy import GAS_DEPLOY_SCOPES

    required_fragments = ["script.projects", "script.deployments", "drive.file"]
    for fragment in required_fragments:
        assert any(fragment in s for s in GAS_DEPLOY_SCOPES), (
            f"{fragment} missing from GAS_DEPLOY_SCOPES: {GAS_DEPLOY_SCOPES}"
        )


# ---------------------------------------------------------------------
# build_webapp_manifest — pure manifest assembly (ROADMAP 59)
# ---------------------------------------------------------------------


def test_build_webapp_manifest_default_shape():
    """Default = USER_DEPLOYING + ANYONE_ANONYMOUS (the webhook case),
    V8 runtime, with a webapp block — mirrors the runtime installer's
    _BASE_MANIFEST shape."""
    from appscriptly.services.gas_deploy.api import build_webapp_manifest

    m = build_webapp_manifest()
    assert m["runtimeVersion"] == "V8"
    assert m["webapp"] == {
        "executeAs": "USER_DEPLOYING",
        "access": "ANYONE_ANONYMOUS",
    }
    assert "timeZone" in m


def test_build_webapp_manifest_honors_overrides():
    from appscriptly.services.gas_deploy.api import build_webapp_manifest

    m = build_webapp_manifest(execute_as="USER_ACCESSING", access="MYSELF")
    assert m["webapp"] == {"executeAs": "USER_ACCESSING", "access": "MYSELF"}


def test_build_webapp_manifest_rejects_bad_execute_as():
    from appscriptly.services.gas_deploy.api import build_webapp_manifest

    with pytest.raises(ValueError, match="execute_as must be one of"):
        build_webapp_manifest(execute_as="EVERYONE")


def test_build_webapp_manifest_rejects_bad_access():
    from appscriptly.services.gas_deploy.api import build_webapp_manifest

    with pytest.raises(ValueError, match="access must be one of"):
        build_webapp_manifest(access="PUBLIC")


# ---------------------------------------------------------------------
# deploy_web_app_project — full create->push->version->deploy orchestration
# ---------------------------------------------------------------------


@pytest.fixture
def stub_full_deploy(mock_script_svc):
    """Wire the whole create -> push -> version -> deploy chain to
    plausible responses so deploy_web_app_project completes end-to-end."""
    mock_script_svc.projects().create().execute.return_value = {
        "scriptId": "NEW-SID", "title": "hook",
    }
    mock_script_svc.projects().updateContent().execute.return_value = {}
    mock_script_svc.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP-1",
        "entryPoints": [
            {"webApp": {"url": "https://script.google.com/macros/s/x/exec"}}
        ],
    }
    return mock_script_svc


def test_deploy_web_app_project_happy_path(stub_full_deploy):
    """End-to-end: returns a WebAppDeployment with the live /exec URL."""
    from appscriptly.services.gas_deploy.api import deploy_web_app_project

    d = deploy_web_app_project(
        MagicMock(),
        script_body="function doPost(e){ return ContentService.createTextOutput('ok'); }",
        title="Stripe hook",
    )
    assert d.script_id == "NEW-SID"
    assert d.deployment_id == "DEP-1"
    assert d.version == 1
    assert d.url == "https://script.google.com/macros/s/x/exec"


def test_deploy_web_app_project_pushes_webapp_manifest(stub_full_deploy):
    """The pushed manifest must declare the webapp entry point (the
    deploy config lives in the manifest, not the deploy body)."""
    from appscriptly.services.gas_deploy.api import deploy_web_app_project

    deploy_web_app_project(
        MagicMock(),
        script_body="function doGet(e){ return ContentService.createTextOutput('hi'); }",
        title="Webhook",
        access="MYSELF",
    )
    call = stub_full_deploy.projects().updateContent.call_args
    by_name = {f["name"]: f for f in call.kwargs["body"]["files"]}
    manifest = json.loads(by_name["appsscript"]["source"])
    assert manifest["webapp"]["access"] == "MYSELF"
    # The doGet body is pushed as a SERVER_JS file.
    assert any(
        f["type"] == "SERVER_JS" and "doGet" in f["source"]
        for f in call.kwargs["body"]["files"]
    )


def test_deploy_web_app_project_rejects_empty_body():
    from appscriptly.services.gas_deploy.api import deploy_web_app_project

    with pytest.raises(ValueError, match="script_body cannot be empty"):
        deploy_web_app_project(MagicMock(), script_body="  ", title="T")


def test_deploy_web_app_project_rejects_body_without_handler():
    """A Web App with neither doGet nor doPost has no HTTP entry point —
    rejected client-side rather than deploying a dead endpoint."""
    from appscriptly.services.gas_deploy.api import deploy_web_app_project

    with pytest.raises(ValueError, match="doGet.*doPost|doGet\\(e\\) or doPost"):
        deploy_web_app_project(
            MagicMock(),
            script_body="function helper(){ return 1; }",
            title="No handler",
        )


def test_deploy_web_app_project_rejects_blank_title():
    from appscriptly.services.gas_deploy.api import deploy_web_app_project

    with pytest.raises(ValueError, match="title cannot be empty"):
        deploy_web_app_project(
            MagicMock(),
            script_body="function doPost(e){ return e; }",
            title="   ",
        )
