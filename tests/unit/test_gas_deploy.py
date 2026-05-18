"""Unit tests for the gas_deploy sub-package (Apps Script REST wrapper).

Pure-mock coverage of the create -> push -> version -> deploy flow,
including the response-parsing edge cases that would be painful to
notice live (e.g. ``deploy_webapp`` raises if the response is missing
``entryPoints[].webApp.url`` — Apps Script API has reshuffled fields
before).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_script_svc():
    """Yield a fake script_v1 build() with a mock projects() chain."""
    with patch("google_docs_mcp.gas_deploy.client.build") as build_mock:
        svc = MagicMock()
        build_mock.return_value = svc
        yield svc


def test_create_project_returns_script_id(mock_script_svc):
    from google_docs_mcp.gas_deploy import AppsScriptClient

    mock_script_svc.projects().create().execute.return_value = {
        "scriptId": "ABC123", "title": "foo"
    }
    client = AppsScriptClient(MagicMock())
    assert client.create_project("foo") == "ABC123"


def test_push_files_sends_manifest_plus_files(mock_script_svc):
    """The pushed payload must include the manifest as JSON + every file as SERVER_JS."""
    from google_docs_mcp.gas_deploy import AppsScriptClient

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
    from google_docs_mcp.gas_deploy import AppsScriptClient

    mock_script_svc.projects().versions().create().execute.return_value = {
        "versionNumber": 3
    }
    client = AppsScriptClient(MagicMock())
    assert client.create_version("SID", "desc") == 3


def test_deploy_webapp_extracts_url_from_entry_points(mock_script_svc):
    from google_docs_mcp.gas_deploy import AppsScriptClient

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
    from google_docs_mcp.gas_deploy import AppsScriptClient

    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP123",
        "entryPoints": [
            {"entryPointType": "ADD_ON", "addOn": {}}  # no webApp
        ],
    }
    client = AppsScriptClient(MagicMock())
    with pytest.raises(RuntimeError, match="no webApp.url"):
        client.deploy_webapp("SCRIPT_ID", 1)


def test_deploy_webapp_passes_executeAs_and_access(mock_script_svc):
    from google_docs_mcp.gas_deploy import AppsScriptClient

    mock_script_svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "D", "entryPoints": [{"webApp": {"url": "u"}}],
    }
    client = AppsScriptClient(MagicMock())
    client.deploy_webapp("S", 1, execute_as="USER_ACCESSING", access="DOMAIN")
    body = mock_script_svc.projects().deployments().create.call_args.kwargs["body"]
    web_cfg = body["entryPoints"][0]["webApp"]
    assert web_cfg["executeAs"] == "USER_ACCESSING"
    assert web_cfg["access"] == "DOMAIN"


def test_gas_deploy_scopes_constant_lists_required_scopes():
    """Smoke test on the public scope list — must include the script.*
    scopes we know are required, and the drive.file scope projects.create
    needs.
    """
    from google_docs_mcp.gas_deploy import GAS_DEPLOY_SCOPES

    required_fragments = ["script.projects", "script.deployments", "drive.file"]
    for fragment in required_fragments:
        assert any(fragment in s for s in GAS_DEPLOY_SCOPES), (
            f"{fragment} missing from GAS_DEPLOY_SCOPES: {GAS_DEPLOY_SCOPES}"
        )
