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

**PR-Δ5 (commercial-ready engineering) — optional GCP project linking.**
The ``GCP_PROJECT_NUMBER`` env var, when set, augments the manifest
with a ``cloudPlatform.projectId`` block (Apps Script API expects
the project NUMBER, despite the field name's lexical mismatch). Doing
so links every Apps Script execution into Cloud Logging under that
GCP project, providing an enterprise-grade audit trail (the SOC 2 +
compliance path). When unset (the default), the manifest is identical
to pre-PR-Δ5 — zero behavior change for personal users.

See ``docs/runbooks/gcp-project-linking.md`` for the operator
workflow + verification steps.
"""
from __future__ import annotations

import json
import logging
import os
from enum import Enum
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from google.auth.credentials import Credentials

from . import config, setup_state, user_store
from .apps_script_hmac import (
    generate_hmac_key,
    inject_hmac_into_source,
)
from .auth import (
    default_data_dir,
    load_credentials,
    load_service_account_credentials,
)
from .services.gas_deploy import AppsScriptClient, GAS_DEPLOY_SCOPES
from .services.gas_deploy.api import WebAppDeployment

log = logging.getLogger("appscriptly.setup_apps_script")

# The .gs script ships in the package itself (the file copied into
# the wheel by hatchling). Reading from __file__'s dir means it works
# whether installed via pipx, pip -e, or inside a Docker image.
RESTRUCTURE_GS_PATH = Path(__file__).parent / "restructure.gs"


def _build_files(hmac_key: str) -> dict[str, str]:
    """Read ``restructure.gs`` and template the per-user HMAC key into it.

    Returns the ``{filename: source}`` mapping pushed to Apps Script. The
    key is baked into the deployed script (``MCP_HMAC_KEY`` / the
    ``MCP_HMAC_REQUIRED`` flag) so ``doPost`` can authenticate every request
    — see ``apps_script_hmac.inject_hmac_into_source``.

    Centralized so BOTH the content-hash computation and the actual push use
    the SAME injected source. The hash MUST be computed over the injected
    text (not the raw template) so it is stable across re-runs for a user
    whose key is unchanged, and so a key ROTATION correctly changes the hash
    and triggers a fresh deploy of the script carrying the new key.
    """
    raw = RESTRUCTURE_GS_PATH.read_text(encoding="utf-8")
    return {SCRIPT_FILENAME: inject_hmac_into_source(raw, hmac_key)}

PROJECT_TITLE = "appscriptly / restructure"
# PR-Δ5.5 (2026-05-27): renamed from ``"google-docs-mcp / restructure"``.
# Affects NEW installs only — Apps Script project titles aren't part
# of the content_hash (compute_content_hash takes only (manifest,
# files)), so existing users' projects retain their original title
# in Drive. The setup-state ledger keys by script_id, not by title,
# so the rename has no behavioral effect on existing deploys; users
# searching Drive for "appscriptly" after the rename see new installs
# only. Existing users who care about Drive-title consistency can
# manually rename via the Apps Script editor — not worth churning
# every existing project automatically (a re-create would cost the
# user re-authorization + the prior project's deployment URLs).
SCRIPT_FILENAME = "Restructure"

_BASE_MANIFEST = {
    "timeZone": "Etc/GMT",
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
    "webapp": {
        # USER_DEPLOYING: the script runs as the OAuth user who deployed
        # it (the chat user in cloud mode, the operator in stdio mode) —
        # so it can only touch docs THAT USER owns. ANYONE_ANONYMOUS:
        # required so our server can reach the /exec endpoint (the server
        # POSTs with no Google sign-in). The "anyone" surface is no longer
        # gated by URL secrecy alone: as of v2.0c every request is
        # authenticated by a per-user HMAC signature verified in
        # restructure.gs::doPost (provisioned + templated in below; signed
        # by docx_import._call_webapp). See apps_script_hmac.py and
        # THREAT_MODEL.md §4 row 5.
        "executeAs": "USER_DEPLOYING",
        "access": "ANYONE_ANONYMOUS",
    },
}


def _build_manifest(gcp_project_number: str | None) -> dict:
    """Construct the appsscript.json manifest, optionally linked to GCP.

    When ``gcp_project_number`` is None (the default, personal-user
    case), returns the base manifest verbatim — bit-for-bit identical
    to the pre-PR-Δ5 ``_MANIFEST`` constant. When supplied, augments
    with a ``cloudPlatform`` block so every Apps Script execution
    surfaces logs in the named GCP project's Cloud Logging.

    The Apps Script API expects the GCP project NUMBER (the numeric
    integer ID Google assigns at project-creation time), NOT the
    project ID (the human-readable string). The manifest field is
    confusingly named ``projectId`` but takes the number — verified
    against Google's documented schema:
    https://developers.google.com/apps-script/manifest#cloudplatform

    Args:
        gcp_project_number: GCP project number as a string (numeric
            content but typed as ``str`` for env-var consistency).
            Pass ``None`` to omit the link (default / personal use).

    Returns:
        A new dict — never mutates ``_BASE_MANIFEST``. The
        ``cloudPlatform`` key is only present when the project number
        is supplied.
    """
    # Defensive copy. The caller's downstream uses (content_hash,
    # push_files) consume the dict; if we mutated the module-level
    # constant, two consecutive calls with different env-var values
    # would silently corrupt each other.
    manifest = dict(_BASE_MANIFEST)
    # Copy the nested webapp dict too — same reason.
    manifest["webapp"] = dict(_BASE_MANIFEST["webapp"])

    if gcp_project_number is not None:
        # Per Google's documented schema (URL above), the manifest
        # field is ``cloudPlatform.projectId`` even though the value
        # is the project NUMBER. Storing as str matches the env-var
        # source type; Apps Script's JSON parser handles either.
        manifest["cloudPlatform"] = {"projectId": gcp_project_number}

    return manifest


def _resolve_gcp_project_number() -> str | None:
    """Read ``GCP_PROJECT_NUMBER`` env var; return stripped value or None.

    Single read site so the env-var name + falsy-value semantics are
    pinned in one place. An empty / whitespace-only value is treated
    as unset (no link) rather than as an explicit empty-link request,
    matching the convention for the rest of this repo's env vars.
    """
    raw = os.environ.get("GCP_PROJECT_NUMBER", "").strip()
    return raw or None


def _current_manifest() -> dict:
    """Resolve the manifest at call time (reads ``GCP_PROJECT_NUMBER``).

    PR-Δ5: replaces the pre-existing module-level ``_MANIFEST`` constant
    so the env var is read each time the pipeline runs rather than
    once at import. Two reasons:

      1. Tests can monkeypatch ``GCP_PROJECT_NUMBER`` per-test without
         needing to reload the module.
      2. Operators flipping the env var without restarting (e.g. via
         a Fly secret update + soft-reload) see the change on the
         next pipeline run.

    Both call sites in ``_execute_setup_with_ledger`` —
    ``push_files`` and ``setup_state.compute_content_hash`` — must
    use the SAME manifest within a single run; binding via a local
    at the top of the function (rather than two separate calls) is
    the contract.
    """
    return _build_manifest(_resolve_gcp_project_number())


# Backward-compat surface for in-repo callers that referenced the
# pre-PR-Δ5 module-level ``_MANIFEST`` constant. The snapshot is
# captured at import time and reflects whatever ``GCP_PROJECT_NUMBER``
# happened to be set to then — fine for static assertions (e.g.
# ``test_doc_cohesion`` checks the webapp.access mode, which doesn't
# vary by GCP linking) but WRONG for any caller that needs the
# env-var-driven behavior. New code should call ``_current_manifest()``
# directly. The constant is preserved instead of deleted so the
# import line in ``test_doc_cohesion`` keeps working without a
# rewrite — that test's contract (README cohesion vs the manifest)
# is orthogonal to PR-Δ5.
_MANIFEST = _current_manifest()


class WebAppHealth(Enum):
    """Classified liveness of a deployed ``/exec`` Web App endpoint.

    Deliberately three-way, NOT a bool: a transient network failure
    (``UNKNOWN``) must never be conflated with a deployment that
    definitively does not serve (``DEAD``), or a flaky network would
    make setup thrash working deployments.
    """

    HEALTHY = "healthy"
    DEAD = "dead"
    UNKNOWN = "unknown"


# Health-probe GET budget. A live doGet answers in single-digit seconds
# (including the /exec 302 hop); anything slower is treated as a
# transient network problem (UNKNOWN), never as deployment death.
_PROBE_TIMEOUT_SECONDS = 10.0
# A healthy doGet reply is ~100 bytes of JSON; Google's access page is
# tens of KB of HTML. Cap the read so classification never slurps an
# unbounded body.
_PROBE_MAX_BODY_BYTES = 65536


def probe_webapp_health(
    url: str, *, timeout: float = _PROBE_TIMEOUT_SECONDS
) -> WebAppHealth:
    """GET a deployment's ``/exec`` URL and classify what answered.

    A LIVE deployment answers ``doGet`` with a small JSON payload —
    ``restructure.gs::doGet`` returns ``{ok: true, ...}`` exactly so the
    URL can be health-checked. A DECAYED deployment never reaches the
    script: Google itself answers with an HTML access-denied page (HTTP
    403). Observed live in prod (2026-07): after the deploying user
    revokes then re-grants OAuth consent, the OLD deployment's
    authorization is severed — its ``/exec`` serves the 403 door page —
    while a brand-new project deployed under the current grant serves
    fine (same account, same ``ANYONE_ANONYMOUS`` access, same minute).

    Classification:

    - ``HEALTHY`` — HTTP 200 and the body parses as JSON (the script ran).
    - ``DEAD`` — a definitive 4xx (the 403 door page, a 404 for a
      deleted deployment) or a 200 whose body is HTML/non-JSON (a Google
      interstitial, not ``doGet``). Will not recover on its own.
    - ``UNKNOWN`` — transport trouble (timeout, DNS/connection failure)
      or a retryable server-side status (5xx / 429). Says nothing about
      the deployment; callers must treat it as "reuse the cache."

    Stdlib-only (urllib) so it works identically in the slim container
    and the local stdio install. Redirects are followed (urllib's
    default) — a live ``/exec`` GET 302-hops to
    ``script.googleusercontent.com`` before serving the JSON.
    """
    req = urlrequest.Request(url, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            body = resp.read(_PROBE_MAX_BODY_BYTES).decode(
                "utf-8", errors="replace"
            )
    except urlerror.HTTPError as e:
        # 5xx / 429 are server-side or throttling blips — retryable, so
        # not proof the deployment is gone. Any other 4xx (401/403/404)
        # is Google definitively refusing the URL: the door page.
        if e.code >= 500 or e.code == 429:
            return WebAppHealth.UNKNOWN
        return WebAppHealth.DEAD
    except (OSError, HTTPException):
        # URLError (DNS, refused connection) and socket timeouts are
        # OSError subclasses; HTTPException covers protocol-level
        # garbage (e.g. BadStatusLine) urlopen can leak through.
        return WebAppHealth.UNKNOWN
    try:
        json.loads(body)
    except ValueError:
        # 200 but not JSON: the request terminated at a Google HTML
        # page (sign-in / error interstitial), not at doGet.
        return WebAppHealth.DEAD
    return WebAppHealth.HEALTHY


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
    - Cached deployment URL must still SERVE (``probe_webapp_health``).
      A DEAD ``/exec`` — deployment decay — clears and re-provisions a
      fresh project; the replacement is verified with ONE re-probe
      (raises if still dead, no retry loop). A probe that can't reach
      the network (UNKNOWN) reuses the cache, never re-cuts.

    On success, every step's result is persisted before the next
    starts — so a mid-pipeline crash leaves a resumable ledger
    instead of an orphan Apps Script project.
    """
    client = AppsScriptClient(creds)
    state = get_state()

    # --- Reset checks ---
    # Set when the cached deployment probed DEAD and we re-provision;
    # the freshly cut deployment then gets one verification re-probe.
    recut_after_dead_probe = False
    if not _state_matches_target(state, content_hash, impersonate):
        clear_state()
        save_state_partial({"content_hash": content_hash, "impersonate": impersonate})
        state = {"content_hash": content_hash, "impersonate": impersonate}
    elif "script_id" in state and not client.script_exists(state["script_id"]):
        clear_state()
        save_state_partial({"content_hash": content_hash, "impersonate": impersonate})
        state = {"content_hash": content_hash, "impersonate": impersonate}
    elif "url" in state and probe_webapp_health(state["url"]) is WebAppHealth.DEAD:
        # Deployment decay: the ledger says "deployed" but /exec answers
        # Google's 403 access page, so requests never reach doPost and
        # reconstructing from cache would report "ready" for a dead
        # endpoint. Full re-provision — a fresh project + deployment is
        # the PROVEN-healthy repair (re-deploying on the possibly severed
        # old script is not). clear_state() keeps the HMAC key by design
        # in both ledger backends (it lives outside the ledger fields),
        # so the re-cut script carries the user's same stable key.
        clear_state()
        save_state_partial({"content_hash": content_hash, "impersonate": impersonate})
        state = {"content_hash": content_hash, "impersonate": impersonate}
        recut_after_dead_probe = True

    # --- Step 1: projects.create ---
    if "script_id" not in state:
        state["script_id"] = client.create_project(PROJECT_TITLE)
        save_state_partial({"script_id": state["script_id"]})

    # --- Step 2/3: projects.updateContent + versions.create ---
    # (Pushed in the same conditional — pushing is idempotent at the
    # API level, but if version_number is already set we've passed
    # this point and shouldn't redo either operation.)
    # PR-Δ5: manifest is resolved at call time so the GCP_PROJECT_NUMBER
    # env var (optional) flows into ``cloudPlatform.projectId``. Bound
    # to a local so the push body matches whatever the caller's
    # content_hash was computed against (the two must use the same
    # manifest within a single run).
    if "version_number" not in state:
        manifest = _current_manifest()
        client.push_files(state["script_id"], manifest=manifest, files=files)
        state["version_number"] = client.create_version(
            state["script_id"],
            description="initial deploy via setup-apps-script-auto",
        )
        save_state_partial({"version_number": state["version_number"]})

    # --- Step 4: projects.deployments.create ---
    # Entry-point config (executeAs, access) is declared in the manifest
    # and pushed via push_files; the deployment body must NOT include
    # entryPoints (Apps Script API rejects it).
    if "deployment_id" not in state:
        deployment = client.deploy_webapp(
            state["script_id"], state["version_number"],
            description="google-docs-mcp restructure webapp",
        )
        save_state_partial({
            "deployment_id": deployment.deployment_id,
            "url": deployment.url,
        })
        if (
            recut_after_dead_probe
            and probe_webapp_health(deployment.url) is WebAppHealth.DEAD
        ):
            # ONE verification probe on the replacement, no retry loop.
            # UNKNOWN is accepted (a network blip must not fail an
            # otherwise complete install). The fresh ledger stays
            # persisted, so the next run re-probes and can repair again.
            raise RuntimeError(
                "Apps Script Web App is still not serving after a full "
                f"re-provision: GET {deployment.url} does not answer "
                "with the script's JSON health response. The previous "
                "deployment had decayed (Google returned its access "
                "page instead of running the script), so a brand new "
                "project and deployment were cut, but the replacement "
                "is unreachable too. This points at an account-level "
                "problem, for example the Apps Script API being "
                "disabled for the account or a stale Google grant. "
                "Re-authorize Google access and re-run the "
                "as_install_automation tool."
            )
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

    # v2.0c: provision (read-or-generate) the HMAC key for THIS local
    # deployment before building the files / content hash, then template it
    # into the deployed script so doPost can authenticate requests. Reusing
    # the persisted key keeps re-runs idempotent; a key rotation changes the
    # content_hash and triggers a fresh deploy carrying the new key.
    hmac_key = config.load().get("apps_script_hmac_key")
    if not hmac_key:
        hmac_key = generate_hmac_key()
        config.save({"apps_script_hmac_key": hmac_key})
    files = _build_files(hmac_key)
    # PR-Δ5: manifest resolved at call time so the optional
    # GCP_PROJECT_NUMBER env var participates in the content_hash. If
    # the operator flips GCP linking on/off between runs, the hash
    # change correctly triggers a re-deploy (matches the existing
    # "manifest changed" reset logic in _execute_setup_with_ledger).
    content_hash = setup_state.compute_content_hash(_current_manifest(), files)

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
    # NB: apps_script_hmac_key is deliberately NOT in this map. It must
    # SURVIVE a ledger clear (hash mismatch / manual script delete) so a
    # user keeps one stable key across re-deploys — the key is provisioned
    # separately in setup_apps_script_for_user (read-or-generate) and
    # cleared only by clear_state(user_id) at consent revocation.
}


def _resolve_or_create_user_hmac_key(user_id: str) -> str:
    """Return the user's stable HMAC key, generating + persisting if absent.

    Read-or-create: an existing valid ``apps_script_hmac_key`` is reused (so
    re-running setup is idempotent — same key → same script content → same
    content_hash → no needless re-deploy). A user with no key yet (brand new,
    or a legacy row not covered by the migration backfill) gets a fresh
    ``secrets.token_hex(32)`` persisted via the normal validated
    ``user_store.save_state`` path.

    NB: ``user_store.get_state`` drops a persisted key that fails the
    ``_valid_apps_script_hmac_key`` validator (tampering / pre-validator
    install), so a tampered key is transparently re-minted here.
    """
    existing = user_store.get_state(user_id).get("apps_script_hmac_key")
    if existing:
        return existing
    new_key = generate_hmac_key()
    user_store.save_state(user_id, {"apps_script_hmac_key": new_key})
    return new_key


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
    # v2.0c: provision (read-or-generate) the per-user HMAC key BEFORE
    # building the files / content hash. The key is baked into the deployed
    # script so doPost can authenticate requests. Reusing the persisted key
    # on re-runs keeps the content_hash stable (idempotent deploy); a brand-
    # new user gets a fresh key persisted now. The key intentionally lives
    # OUTSIDE the ledger fields (see _USER_STORE_FIELD_MAP) so a ledger
    # reset doesn't rotate it.
    hmac_key = _resolve_or_create_user_hmac_key(user_id)
    files = _build_files(hmac_key)
    # PR-Δ5: manifest resolved at call time so the optional
    # GCP_PROJECT_NUMBER env var participates in the content_hash. If
    # the operator flips GCP linking on/off between runs, the hash
    # change correctly triggers a re-deploy (matches the existing
    # "manifest changed" reset logic in _execute_setup_with_ledger).
    content_hash = setup_state.compute_content_hash(_current_manifest(), files)

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
