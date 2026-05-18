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
from . import setup_state
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

    gs_source = RESTRUCTURE_GS_PATH.read_text(encoding="utf-8")
    files = {SCRIPT_FILENAME: gs_source}

    # Idempotency: resume a previous interrupted run if the inputs
    # match. See setup_state.py for the full rationale.
    content_hash = setup_state.compute_content_hash(_MANIFEST, files)
    state = setup_state.load_state(data_dir)

    if not setup_state.state_matches_target(state, content_hash, impersonate_user):
        # Different setup target (content changed or different
        # impersonate user) — start fresh.
        state = {
            "content_hash": content_hash,
            "impersonate": impersonate_user,
        }
        setup_state.save_state(data_dir, state)
    elif "script_id" in state and not client.script_exists(state["script_id"]):
        # The cached project was manually deleted between runs.
        # Clear that entry and any downstream state; redo from step 1.
        state = {
            "content_hash": content_hash,
            "impersonate": impersonate_user,
        }
        setup_state.save_state(data_dir, state)

    # Step 1: projects.create
    if "script_id" not in state:
        state["script_id"] = client.create_project(PROJECT_TITLE)
        setup_state.save_state(data_dir, state)

    # Step 2: projects.updateContent
    # (Idempotent at the API level — re-pushing the same files is a
    # no-op. Skip only if we've already passed step 3, since
    # versions.create is what we'd be redoing otherwise.)
    if "version_number" not in state:
        client.push_files(
            state["script_id"], manifest=_MANIFEST, files=files,
        )

    # Step 3: projects.versions.create
    if "version_number" not in state:
        state["version_number"] = client.create_version(
            state["script_id"],
            description="initial deploy via setup-apps-script-auto",
        )
        setup_state.save_state(data_dir, state)

    # Step 4: projects.deployments.create
    if "deployment_id" not in state:
        deployment = client.deploy_webapp(
            state["script_id"], state["version_number"],
            description="google-docs-mcp restructure webapp",
            execute_as="USER_DEPLOYING",
            access="MYSELF",
        )
        state["deployment_id"] = deployment.deployment_id
        state["url"] = deployment.url
        setup_state.save_state(data_dir, state)
    else:
        # All steps already completed in a prior run — just reconstruct
        # the result object from cached state.
        deployment = WebAppDeployment(
            script_id=state["script_id"],
            deployment_id=state["deployment_id"],
            version=state["version_number"],
            url=state["url"],
        )

    # Persist the URL so the runtime can find it without manual config.
    # (Idempotent — always-overwrite is fine since state is the truth.)
    cfg = config.load()
    cfg["apps_script_webapp_url"] = state["url"]
    cfg["apps_script_script_id"] = state["script_id"]
    cfg["apps_script_deployment_id"] = state["deployment_id"]
    config.save(cfg)

    return deployment
