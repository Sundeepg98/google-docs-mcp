"""One-command Apps Script Web App setup for google-docs-mcp.

Wraps the generic ``gas_deploy`` plumbing with this project's specifics:
which .gs script to deploy (``restructure.gs``), what title to give
the project, what manifest settings to use, and where to save the
resulting URL.

This is the DOMAIN-SPECIFIC layer. ``gas_deploy/`` is the GENERIC
layer that could be extracted. The dividing line: anything that
mentions ``restructure.gs`` or ``google-docs-mcp`` lives here.
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .auth import (
    default_data_dir,
    load_credentials,
    load_service_account_credentials,
)
from .gas_deploy import AppsScriptClient, GAS_DEPLOY_SCOPES
from .gas_deploy.client import WebAppDeployment

# The .gs script ships in the package itself (the file copied into
# the wheel by hatchling). Reading from __file__'s dir means it works
# whether installed via pipx, pip -e, or inside a Docker image.
RESTRUCTURE_GS_PATH = Path(__file__).parent / "restructure.gs"

PROJECT_TITLE = "google-docs-mcp / restructure"
SCRIPT_FILENAME = "Restructure"

_MANIFEST = {
    "timeZone": "Etc/GMT",
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
    "webapp": {
        "executeAs": "USER_DEPLOYING",
        "access": "MYSELF",
    },
}


def setup_apps_script_auto(
    data_dir: Path | None = None,
    *,
    service_account_key: Path | None = None,
    impersonate_user: str | None = None,
) -> WebAppDeployment:
    """End-to-end: create project, push restructure.gs, deploy, save URL.

    Two auth modes:

    - **OAuth (default)**: triggers a one-time browser consent on first
      run if the cached token doesn't already cover Apps Script scopes.
      Subsequent runs are headless. Right for individual developers
      using the MCP on their own machine.

    - **Service Account + DWD** (opt-in): pass ``service_account_key``
      + ``impersonate_user``. Truly headless from the first call.
      Requires Google Workspace + admin who's enabled DWD for the SA's
      Client ID against the GAS_DEPLOY_SCOPES. Right for CI, server-
      side batch processing, IT-managed multi-user provisioning. NOT
      usable for personal @gmail.com (no Admin Console = no DWD).

    Returns the ``WebAppDeployment`` (scriptId, deploymentId, version,
    /exec URL). The URL is also persisted to the local config so
    ``gdocs_tab_existing_doc`` and retrofit pick it up automatically.
    """
    data_dir = data_dir or default_data_dir()

    if service_account_key is not None:
        if not impersonate_user:
            raise ValueError(
                "service_account_key requires impersonate_user — the "
                "Workspace user the SA acts as (and who'll own the "
                "resulting Apps Script project)."
            )
        creds = load_service_account_credentials(
            service_account_key, impersonate_user, GAS_DEPLOY_SCOPES,
        )
    else:
        # OAuth path: load runtime creds + extended scopes. Re-consents
        # if cached token doesn't cover Apps Script scopes.
        creds = load_credentials(data_dir, extra_scopes=GAS_DEPLOY_SCOPES)

    client = AppsScriptClient(creds)

    script_id = client.create_project(PROJECT_TITLE)

    gs_source = RESTRUCTURE_GS_PATH.read_text(encoding="utf-8")
    client.push_files(
        script_id,
        manifest=_MANIFEST,
        files={SCRIPT_FILENAME: gs_source},
    )

    version = client.create_version(
        script_id, description="initial deploy via setup-apps-script-auto"
    )

    deployment = client.deploy_webapp(
        script_id, version,
        description="google-docs-mcp restructure webapp",
        execute_as="USER_DEPLOYING",
        access="MYSELF",
    )

    # Persist the URL so the runtime can find it without manual config.
    cfg = config.load()
    cfg["apps_script_webapp_url"] = deployment.url
    cfg["apps_script_script_id"] = script_id
    cfg["apps_script_deployment_id"] = deployment.deployment_id
    config.save(cfg)

    return deployment
