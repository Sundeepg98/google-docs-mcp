"""One-command Apps Script Web App setup for google-docs-mcp.

Wraps the generic ``gas_deploy`` plumbing with this project's specifics:
which .gs script to deploy (``restructure.gs``), what title to give
the project, what manifest settings to use, and where to save the
resulting URL.

Two entry points by transport:

- ``setup_apps_script_auto`` — single-tenant local CLI path. Loads
  OAuth/SA creds from disk, runs the 4-step pipeline with the
  ``setup_state`` JSON-file ledger, writes the resulting URL to
  ``config.json`` (read at runtime by ``docx_import``).

- ``setup_apps_script_for_user`` — multi-tenant cloud MCP-tool path.
  Caller supplies creds (resolved from the per-user OAuth flow); the
  ``user_store`` row for that user is the ledger AND the runtime
  destination — no shared local config.

Both share ``_execute_setup_with_ledger`` for the 4 API calls + the
hash-mismatch / script-deleted reset logic. The only difference is
where state lives.

This is the DOMAIN-SPECIFIC layer. ``gas_deploy/`` is the GENERIC
layer that could be extracted. The dividing line: anything that
mentions ``restructure.gs`` or ``google-docs-mcp`` lives here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from google.auth.credentials import Credentials

from . import config, setup_state, user_store
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


def _execute_setup_with_ledger(
    *,
    creds: Credentials,
    files: dict[str, str],
    content_hash: str,
    impersonate: str | None,
    get_state: Callable[[], dict],
    save_state_partial: Callable[[dict], None],
    clear_state: Callable[[], None],
) -> WebAppDeployment:
    """Run the 4-step setup with a pluggable persistence backend.

    Ledger callbacks operate on an INTERNAL state shape:
        {content_hash, impersonate, script_id, version_number,
         deployment_id, url}

    Whatever storage layer the caller uses (local JSON file or
    per-user SQLite row), they translate to/from this shape.

    Reset logic (lifted verbatim from the v1.0.1 ledger fix):
    - Cached state's content_hash + impersonate must match the
      target. If not, clear and start fresh.
    - Cached script_id must still exist in Drive. If user manually
      deleted it, clear and start fresh.

    On success, every step's result is persisted before the next
    starts — so a mid-pipeline crash leaves a resumable ledger
    instead of an orphan Apps Script project.
    """
    client = AppsScriptClient(creds)
    state = get_state()

    # --- Reset checks ---
    if not _state_matches_target(state, content_hash, impersonate):
        clear_state()
        save_state_partial({"content_hash": content_hash, "impersonate": impersonate})
        state = {"content_hash": content_hash, "impersonate": impersonate}
    elif "script_id" in state and not client.script_exists(state["script_id"]):
        clear_state()
        save_state_partial({"content_hash": content_hash, "impersonate": impersonate})
        state = {"content_hash": content_hash, "impersonate": impersonate}

    # --- Step 1: projects.create ---
    if "script_id" not in state:
        state["script_id"] = client.create_project(PROJECT_TITLE)
        save_state_partial({"script_id": state["script_id"]})

    # --- Step 2/3: projects.updateContent + versions.create ---
    # (Pushed in the same conditional — pushing is idempotent at the
    # API level, but if version_number is already set we've passed
    # this point and shouldn't redo either operation.)
    if "version_number" not in state:
        client.push_files(state["script_id"], manifest=_MANIFEST, files=files)
        state["version_number"] = client.create_version(
            state["script_id"],
            description="initial deploy via setup-apps-script-auto",
        )
        save_state_partial({"version_number": state["version_number"]})

    # --- Step 4: projects.deployments.create ---
    if "deployment_id" not in state:
        deployment = client.deploy_webapp(
            state["script_id"], state["version_number"],
            description="google-docs-mcp restructure webapp",
            execute_as="USER_DEPLOYING",
            access="MYSELF",
        )
        save_state_partial({
            "deployment_id": deployment.deployment_id,
            "url": deployment.url,
        })
        return deployment

    # All steps already done in a prior run — reconstruct from cache.
    return WebAppDeployment(
        script_id=state["script_id"],
        deployment_id=state["deployment_id"],
        version=state["version_number"],
        url=state["url"],
    )


def _state_matches_target(
    state: dict, content_hash: str, impersonate: str | None
) -> bool:
    """True if cached state was written for the same target.

    Lifted from the v1.0.1 ``setup_state.state_matches_target`` so
    both ledger backends share the same notion of "same setup."
    """
    return (
        state.get("content_hash") == content_hash
        and state.get("impersonate") == impersonate
    )


# ---------------------------------------------------------------------
# Entry point 1: single-tenant local CLI (existing v1.0.1 behavior)
# ---------------------------------------------------------------------


def setup_apps_script_auto(
    data_dir: Path | None = None,
    *,
    service_account_key: Path | None = None,
    impersonate_user: str | None = None,
) -> WebAppDeployment:
    """End-to-end local setup: create project, push, deploy, save URL.

    Two auth modes:

    - **OAuth (default)**: triggers a one-time browser consent on first
      run if the cached token doesn't already cover Apps Script scopes.
      Subsequent runs are headless. Right for individual developers
      using the MCP on their own machine.

    - **Service Account + DWD** (opt-in): pass ``service_account_key``
      + ``impersonate_user``. Truly headless from the first call.
      Requires Google Workspace + admin who's enabled DWD for the SA's
      Client ID against the GAS_DEPLOY_SCOPES.

    Idempotent: re-running after a crash resumes from the first
    incomplete step (see ``setup_state.py`` for the ledger).

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
        creds = load_credentials(data_dir, extra_scopes=GAS_DEPLOY_SCOPES)

    files = {SCRIPT_FILENAME: RESTRUCTURE_GS_PATH.read_text(encoding="utf-8")}
    content_hash = setup_state.compute_content_hash(_MANIFEST, files)

    def _get_state() -> dict:
        return dict(setup_state.load_state(data_dir))

    def _save_partial(updates: dict) -> None:
        current = dict(setup_state.load_state(data_dir))
        current.update(updates)
        setup_state.save_state(data_dir, current)  # type: ignore[arg-type]

    def _clear() -> None:
        setup_state.clear_state(data_dir)

    deployment = _execute_setup_with_ledger(
        creds=creds,
        files=files,
        content_hash=content_hash,
        impersonate=impersonate_user,
        get_state=_get_state,
        save_state_partial=_save_partial,
        clear_state=_clear,
    )

    # Persist the URL so the local runtime can find it without manual
    # config. (Idempotent — always-overwrite is fine since state is the
    # source of truth.)
    cfg = config.load()
    cfg["apps_script_webapp_url"] = deployment.url
    cfg["apps_script_script_id"] = deployment.script_id
    cfg["apps_script_deployment_id"] = deployment.deployment_id
    config.save(cfg)

    return deployment


# ---------------------------------------------------------------------
# Entry point 2: multi-tenant cloud MCP tool (v1.1+)
# ---------------------------------------------------------------------


# Field-name translation between the executor's internal vocabulary
# and user_store's column names. Same row holds both setup-ledger
# fields and the runtime-consumed URL/IDs — single source of truth
# per user.
_USER_STORE_FIELD_MAP = {
    "content_hash": "apps_script_content_hash",
    "script_id": "apps_script_script_id",
    "version_number": "apps_script_version_number",
    "deployment_id": "apps_script_deployment_id",
    "url": "apps_script_url",
    # "impersonate" intentionally absent — cloud users authenticate as
    # themselves (their own OAuth tokens); no Workspace-admin DWD path.
}


def setup_apps_script_for_user(
    creds: Credentials, user_id: str,
) -> WebAppDeployment:
    """Deploy a per-user Apps Script Web App, using user_store as ledger.

    The same restructure.gs script (operator's bundled version) is
    deployed under the user's own Drive — so the resulting Web App
    executes as them (USER_DEPLOYING). This is what makes the cloud
    MCP genuinely multi-tenant: each user's document operations run
    against their own Google identity, not a shared operator account.

    Idempotent: resumes a partial run from the user's user_store row
    on retry. Restructure.gs content_hash mismatch (operator updated
    the script) → user's setup-state is cleared and a fresh deploy
    starts on next call. User manually deleted the Apps Script in
    their Drive → detected via script_exists, cleared, fresh deploy.

    Args:
        creds: Google API Credentials for the calling user. Resolve
            via ``credentials.get_credentials_for_user(user_id, ...)``.
        user_id: Stable Google ``sub`` claim — the per-user key.

    Raises whatever the underlying Apps Script REST calls raise (e.g.
    google.auth.exceptions.RefreshError for revoked creds — though
    the caller's credentials resolver should catch that first).
    """
    files = {SCRIPT_FILENAME: RESTRUCTURE_GS_PATH.read_text(encoding="utf-8")}
    content_hash = setup_state.compute_content_hash(_MANIFEST, files)

    def _get_state() -> dict:
        row = user_store.get_state(user_id)
        return {
            internal: row[col]
            for internal, col in _USER_STORE_FIELD_MAP.items()
            if col in row
        }

    def _save_partial(updates: dict) -> None:
        translated: dict[str, object] = {}
        for k, v in updates.items():
            if k == "impersonate":
                continue  # not a user_store field
            mapped = _USER_STORE_FIELD_MAP.get(k)
            if mapped is None:
                continue  # unknown internal field; skip rather than crash
            translated[mapped] = v
        if translated:
            user_store.save_state(user_id, translated)

    def _clear() -> None:
        # NULL the apps_script_* fields but preserve google_creds_json
        # (the user's OAuth tokens — independent of setup state).
        user_store.save_state(user_id, {
            col: None for col in _USER_STORE_FIELD_MAP.values()
        })

    return _execute_setup_with_ledger(
        creds=creds,
        files=files,
        content_hash=content_hash,
        impersonate=None,
        get_state=_get_state,
        save_state_partial=_save_partial,
        clear_state=_clear,
    )
