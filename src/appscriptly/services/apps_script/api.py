"""Bound Apps Script generation — internal logic + Apps Script REST calls.

PR-Δ7. The generic primitive every later feature PR (slides-for-video,
sheets dashboards, docs menus) builds on: generate a *container-bound*
Apps Script project, push a ``.gs`` body + manifest into it, and deploy
it — all in one orchestrated tool call. The container automation then
lives in the user's Workspace and runs without Claude in the loop.

**Distinct from ``services/gas_deploy``.** ``gas_deploy`` is the runtime
*bootstrap* — it creates ONE standalone Apps Script Web App per user so
the lossless-retrofit path has a backend. This module creates a NEW
*bound* project per container (``projects.create`` with a ``parentId``),
which is what enables ``Ui.createMenu`` menus, ``HtmlService`` sidebars,
``onEdit`` simple triggers, and custom Sheets functions tied to that
specific Doc / Sheet / Slides file. Both speak the Apps Script REST API
through the same ``get_service("script", "v1")`` chokepoint; neither
duplicates the other's purpose.

API reference:
  https://developers.google.com/apps-script/api/reference/rest

**Manifest reality check (verified against the official manifest doc).**
Apps Script's ``appsscript.json`` manifest carries ``timeZone``,
``runtimeVersion``, ``oauthScopes``, ``dependencies``, ``addOns``,
``webapp`` — and NOTHING ELSE that's relevant here. Crucially:

  * Custom menus (``Ui.createMenu``) are NOT a manifest field — they're
    created in code from an ``onOpen`` trigger.
  * Sidebars (``HtmlService``) are NOT a manifest field — created in code.
  * Time-driven + ``onEdit`` triggers are NOT manifest fields — they're
    installed in code via ``ScriptApp.newTrigger(...)`` (or, for the
    simple ``onEdit``/``onOpen`` cases, by naming the function so).

So ``build_manifest`` does the one thing the manifest CAN do for these
features: it derives the right ``oauthScopes`` from the requested
capabilities (a menu/sidebar needs ``script.container.ui``; an
installable trigger needs ``script.scriptapp``) and merges them with
any caller-supplied ``oauth_scopes``. The actual menu/trigger/sidebar
wiring lives in the ``.gs`` ``script_body`` the caller supplies — which
is exactly why ``script_body`` is a required argument of the tool.
``build_manifest`` stays a pure function (easy to property-test): same
input dict → same manifest dict, no I/O.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Apps Script's manifest file is conventionally named "appsscript" with
# type JSON. Every project requires exactly one. (Matches the constant
# in services/gas_deploy/api.py — same API contract, kept local so the
# two services don't couple.)
_MANIFEST_FILENAME = "appsscript"

# The default .gs source filename when we push the caller's script body.
# Apps Script shows it as "Code.gs" in the editor; the API ``name`` is
# the extension-less "Code".
_DEFAULT_CODE_FILENAME = "Code"

# Drive mimeType → container "kind" the bound script attaches to. Only
# Docs / Sheets / Slides support container-bound scripts with the
# automation surfaces this primitive targets (menus / sidebars / edit
# triggers). Forms ARE bindable but their automation model is different
# (form-submit triggers, no Ui menu) — out of scope for the generic
# primitive, so we reject with a clear message rather than silently
# producing a script whose menu/sidebar code would no-op.
_MIMETYPE_TO_KIND: dict[str, str] = {
    "application/vnd.google-apps.document": "docs",
    "application/vnd.google-apps.spreadsheet": "sheets",
    "application/vnd.google-apps.presentation": "slides",
}

# Scopes a generated bound script needs depending on which capabilities
# the manifest declares. These go into the manifest's ``oauthScopes`` so
# the user's authorization covers what the .gs code will actually do.
# https://developers.google.com/apps-script/concepts/scopes
_UI_SCOPE = "https://www.googleapis.com/auth/script.container.ui"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"

# Google's RESTRICTED OAuth scopes that this generic generator REFUSES to
# bake into a manifest unless the caller explicitly opts in. Restricted
# scopes (full Gmail + broad Drive) trigger Google's CASA security
# assessment, the 7-day Testing-mode refresh-token cap, and a far larger
# blast radius if the generated automation is ever abused. The shipped
# minimal-scope ``as_install_*`` tools never request these; the open door
# is the generic ``as_generate_bound_script`` / ``as_deploy_web_app``
# passthroughs that accept caller-supplied ``oauth_scopes``.
#
# This is the COMPLETE Google restricted set for the products this MCP can
# touch (NOT the incomplete mirror in tests/unit/test_base_tier_scopes.py).
# Source: https://developers.google.com/identity/protocols/oauth2/
#         production-readiness/restricted-scope-verification
_RESTRICTED_SCOPES: frozenset[str] = frozenset({
    # --- Gmail (full mailbox + every per-action restricted scope) ---
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    # --- Drive (broad / full-content + metadata + activity + scripts) ---
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.activity",
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/drive.scripts",
})


def is_restricted_scope(scope: str) -> bool:
    """True if ``scope`` is one of Google's RESTRICTED OAuth scopes.

    Single read-point for the restricted-set membership so callers
    (``build_manifest`` here, the web-app deploy guard) share one
    definition. Exposed (no underscore) so tests + the gas_deploy guard
    import the SAME source of truth rather than re-listing scopes.
    """
    return scope in _RESTRICTED_SCOPES


def auto_detect_container_kind(creds: Credentials, container_id: str) -> str:
    """Resolve a Drive file ID to its bound-script container kind.

    Reads the file's ``mimeType`` via the Drive API and maps it to one
    of ``"docs"`` / ``"sheets"`` / ``"slides"``. This lets the tool
    accept just a ``container_id`` and figure out the kind itself, so
    callers don't have to know (or get wrong) which Workspace app the
    ID belongs to.

    Args:
        creds: OAuth credentials carrying a Drive read scope (baseline).
        container_id: The Drive file ID of the Doc / Sheet / Slides file
            the script should be bound to.

    Returns:
        ``"docs"``, ``"sheets"``, or ``"slides"``.

    Raises:
        ValueError: the file's mimeType is not a Doc / Sheet / Slides —
            i.e. it's a Form, Folder, PDF, raw upload, etc. The message
            names the offending mimeType so the caller can correct the
            ID. (Forms are technically bindable but their automation
            model — no Ui menu, form-submit triggers only — doesn't fit
            this generic menu/sidebar/edit-trigger primitive.)
        HttpError: from the Drive SDK on 4xx / 5xx (e.g. 404 for a bad
            ID) — propagated to the tool-layer envelope.
    """
    drive = get_service("drive", "v3", credentials=creds)
    # Pure read — classifying the container type is idempotent; wrap so a
    # transient 429/5xx during detection doesn't fail the whole generate.
    meta = execute_with_retry(
        lambda: drive.files().get(
            fileId=container_id, fields="id,name,mimeType"
        ).execute(),
        idempotent=True,
        op_name="drive.files.get.container_kind",
    )
    mime = meta.get("mimeType", "")
    kind = _MIMETYPE_TO_KIND.get(mime)
    if kind is None:
        raise ValueError(
            f"Drive file {container_id!r} (mimeType {mime!r}) is not a "
            f"Google Doc, Sheet, or Slides file. Bound Apps Script "
            f"generation supports those three container types only "
            f"(they're the ones that host custom menus, sidebars, and "
            f"edit triggers). Forms, folders, PDFs, and raw uploads "
            f"aren't supported — pass the ID of a Doc / Sheet / Slides "
            f"file, or use the standalone runtime installer "
            f"(gdocs_install_automation) for non-container automation."
        )
    return kind


def build_manifest(
    manifest_dict: dict[str, Any] | None,
    timezone: str = "Etc/UTC",
    *,
    allow_restricted_scopes: bool = False,
) -> dict[str, Any]:
    """Translate an operator-friendly manifest dict into ``appsscript.json``.

    PURE function — no I/O, deterministic. ``manifest_dict`` is a small
    high-level description of what the generated script does; this maps
    it to the actual Apps Script manifest fields, ALWAYS emitting a
    valid ``runtimeVersion`` (``"V8"``) + ``timeZone``.

    Supported keys in ``manifest_dict`` (all optional):

      * ``menu`` — list of ``{name, function_name}``. Each entry is a
        custom-menu item the generated ``onOpen`` code will add. The
        manifest itself has no menu field (menus are code), so the only
        manifest effect is requiring the ``script.container.ui`` scope.
        The entries are validated for shape + echoed back under a
        private ``__plan__`` key (see "Plan echo" below) so the caller /
        generated body knows what to wire.
      * ``triggers`` — list of ``{type: "time"|"edit", ...}``. Same
        deal: triggers are installed in code (``ScriptApp.newTrigger``),
        not declared in the manifest, so the manifest effect is
        requiring ``script.scriptapp`` (for installable triggers) and/or
        ``script.container.ui``. Validated + echoed.
      * ``sidebar_html`` — str of HTML for an ``HtmlService`` sidebar.
        Manifest effect: requires ``script.container.ui``. Echoed.
      * ``oauth_scopes`` — list[str] of explicit scopes to add to the
        manifest's ``oauthScopes`` (union'd with the capability-derived
        ones above). Order-stable + de-duplicated.

    Args:
        manifest_dict: the high-level description, or ``None`` / ``{}``
            for a bare manifest (V8 + timeZone, no extra scopes).
        timezone: the script's ``timeZone`` (a TZ database ZoneId, e.g.
            ``"America/New_York"``). Defaults to ``"Etc/UTC"``.
        allow_restricted_scopes: opt-in to permit Google RESTRICTED scopes
            (full Gmail / broad Drive — see ``_RESTRICTED_SCOPES``) in
            ``oauth_scopes``. Defaults to ``False``: a restricted scope is
            REJECTED with a ``ValueError`` so the generic generator can't
            silently mint an automation with full-mailbox / full-Drive
            authority. Set ``True`` ONLY after surfacing the consequences to
            the user (CASA assessment, 7-day Testing refresh-token cap,
            larger blast radius). The shipped minimal-scope ``as_install_*``
            tools never need this.

    Returns:
        A dict ready to ``json.dumps`` as ``appsscript.json``. Always
        contains ``runtimeVersion`` + ``timeZone``. Contains
        ``oauthScopes`` only when at least one scope is required (the
        manifest omits an empty ``oauthScopes`` rather than emitting
        ``[]`` — Apps Script treats absent + empty identically and the
        cleaner shape is nicer to read). Contains a private ``__plan__``
        key echoing the validated menu/trigger/sidebar intent (stripped
        by ``set_project_content`` before the manifest is written — it's
        an internal hand-off, not part of the real manifest schema).

    Raises:
        ValueError: a ``menu`` / ``triggers`` entry is malformed (e.g.
            a menu item missing ``name`` or ``function_name``; a trigger
            with an unknown ``type``); ``oauth_scopes`` is not a list of
            strings; or — unless ``allow_restricted_scopes=True`` — any
            entry in ``oauth_scopes`` is a Google RESTRICTED scope. Cheap
            client-side rejection with a message that names the bad entry.
    """
    src = dict(manifest_dict or {})

    menu = _validate_menu(src.get("menu"))
    triggers = _validate_triggers(src.get("triggers"))
    sidebar_html = src.get("sidebar_html")
    if sidebar_html is not None and not isinstance(sidebar_html, str):
        raise ValueError(
            f"sidebar_html must be a string of HTML, got "
            f"{type(sidebar_html).__name__}."
        )

    # Derive the capability scopes the manifest must declare so the
    # generated code's menu/sidebar/trigger calls are authorized.
    derived_scopes: list[str] = []
    if menu or sidebar_html:
        derived_scopes.append(_UI_SCOPE)
    if any(t["type"] == "time" for t in triggers):
        # Installable time-driven triggers require script.scriptapp.
        derived_scopes.append(_TRIGGER_SCOPE)

    explicit_scopes = src.get("oauth_scopes") or []
    if not isinstance(explicit_scopes, list) or not all(
        isinstance(s, str) for s in explicit_scopes
    ):
        raise ValueError(
            "oauth_scopes must be a list of scope-URL strings."
        )

    # RESTRICTED-scope guard. By default a generic generator must NOT bake
    # a full-Gmail / broad-Drive scope into the manifest — that would arm
    # the generated automation with restricted authority (and pull the whole
    # OAuth app into CASA + the 7-day Testing refresh cap). Reject unless the
    # caller explicitly opted in after surfacing the consequences.
    if not allow_restricted_scopes:
        restricted = [s for s in explicit_scopes if is_restricted_scope(s)]
        if restricted:
            raise ValueError(
                "oauth_scopes contains Google RESTRICTED scope(s): "
                f"{sorted(set(restricted))}. Restricted scopes (full Gmail / "
                "broad Drive) require Google's CASA security assessment, cap "
                "refresh tokens at 7 days in Testing mode, and greatly widen "
                "the blast radius of the generated automation. They are "
                "refused by default. If the user genuinely needs one, re-call "
                "with allow_restricted_scopes=True AFTER telling them: this "
                "automation will be able to access that restricted data, and "
                "granting it triggers Google's restricted-scope verification."
            )

    # Order-stable de-dup: derived first (predictable), then explicit.
    oauth_scopes = _dedup_preserve_order([*derived_scopes, *explicit_scopes])

    manifest: dict[str, Any] = {
        "timeZone": timezone,
        "runtimeVersion": "V8",
    }
    if oauth_scopes:
        manifest["oauthScopes"] = oauth_scopes

    # Plan echo: the validated, normalized intent. NOT part of the real
    # appsscript.json schema — set_project_content strips it before the
    # manifest is serialized. It exists so the orchestration layer (and
    # tests) can see what build_manifest understood without re-parsing.
    manifest["__plan__"] = {
        "menu": menu,
        "triggers": triggers,
        "has_sidebar": sidebar_html is not None,
    }
    return manifest


def create_bound_project(
    creds: Credentials, container_id: str, name: str
) -> dict[str, Any]:
    """Create a container-bound Apps Script project via ``projects.create``.

    Binding happens by passing ``parentId=container_id`` — per the Apps
    Script API, that attaches the new script project to the given Drive
    file (Doc / Sheet / Slides), making it a *bound* script rather than a
    standalone one.

    NOT idempotent: each call creates a brand-new project (re-running
    yields a duplicate script bound to the same container). Wrapped with
    ``execute_with_retry(idempotent=False)`` so a transient error is
    surfaced rather than blindly replayed into duplicate projects.

    Args:
        creds: OAuth credentials carrying ``script.projects``.
        container_id: the Drive ID of the Doc / Sheet / Slides to bind to.
        name: the title for the new Apps Script project.

    Returns:
        The created ``Project`` resource (raw API response) — includes
        ``scriptId``, ``title``, ``parentId``, timestamps.

    Raises:
        HttpError: from the Apps Script SDK on 4xx / 5xx — propagated.
    """
    script = get_service("script", "v1", credentials=creds)
    return execute_with_retry(
        lambda: script.projects().create(
            body={"title": name, "parentId": container_id},
        ).execute(),
        idempotent=False,  # create → duplicate on replay; never retry
        op_name="script.projects.create",
    )


def set_project_content(
    creds: Credentials,
    script_id: str,
    script_body: str,
    manifest_dict: dict[str, Any],
) -> dict[str, Any]:
    """Push the ``.gs`` source + manifest into a project via ``updateContent``.

    ``projects.updateContent`` replaces the project's ENTIRE file list
    atomically — any file not in the request disappears. For our
    push-everything-from-scratch flow that's exactly right: we send the
    manifest (``appsscript.json``) + one ``.gs`` file built from
    ``script_body``.

    Idempotent: pushing the same body + manifest to the same project
    twice produces identical content. Wrapped with
    ``execute_with_retry(idempotent=True)``.

    Args:
        creds: OAuth credentials carrying ``script.projects``.
        script_id: the project's scriptId (from ``create_bound_project``).
        script_body: the ``.gs`` source as a string. Becomes a
            ``SERVER_JS`` file named ``Code``.
        manifest_dict: the manifest dict from ``build_manifest``. The
            private ``__plan__`` key (if present) is STRIPPED here — it's
            an internal hand-off, not a real manifest field, and Apps
            Script would reject an unknown top-level manifest key.

    Returns:
        The ``Content`` resource the API echoes back (scriptId + files).

    Raises:
        ValueError: ``script_body`` is empty / whitespace. Cheap
            client-side rejection — an empty script is never intended.
        HttpError: from the Apps Script SDK on 4xx / 5xx — propagated.
    """
    if not script_body or not script_body.strip():
        raise ValueError(
            "script_body cannot be empty — pass the .gs source for the "
            "bound automation (at minimum an onOpen / function the "
            "menu or trigger references)."
        )

    # Strip the internal plan echo before serializing — it's not a real
    # appsscript.json field. Copy so we don't mutate the caller's dict.
    manifest = {k: v for k, v in manifest_dict.items() if k != "__plan__"}

    files = [
        {
            "name": _MANIFEST_FILENAME,
            "type": "JSON",
            "source": json.dumps(manifest, indent=2),
        },
        {
            "name": _DEFAULT_CODE_FILENAME,
            "type": "SERVER_JS",
            "source": script_body,
        },
    ]

    script = get_service("script", "v1", credentials=creds)
    return execute_with_retry(
        lambda: script.projects().updateContent(
            scriptId=script_id,
            body={"files": files},
        ).execute(),
        idempotent=True,  # same content → same result; safe to retry
        op_name="script.projects.updateContent",
    )


def create_deployment(
    creds: Credentials, script_id: str, description: str
) -> dict[str, Any]:
    """Cut a version + create a deployment via the Apps Script API.

    A deployment must reference an immutable VERSION of the project's
    content, so this does two API calls: ``projects.versions.create``
    (snapshot current content → versionNumber) then
    ``projects.deployments.create`` (deploy that version). This mirrors
    the version-then-deploy sequence ``gas_deploy``'s ``deploy_webapp``
    uses; the difference is a bound script needs no ``webapp`` entry
    point — it runs from the container's menus / triggers, not a ``/exec``
    URL.

    Version creation is idempotent in the sense that re-snapshotting
    yields another version of the SAME content; deployment creation is
    NOT idempotent (each call mints a new deploymentId). Both are wrapped
    with ``execute_with_retry(idempotent=False)`` — the conservative
    floor, since a partially-applied deploy shouldn't be blindly
    replayed.

    Args:
        creds: OAuth credentials carrying ``script.deployments`` (+
            ``script.projects`` for the version snapshot).
        script_id: the project's scriptId.
        description: human-readable description for both the version and
            the deployment.

    Returns:
        The created ``Deployment`` resource (raw API response) —
        includes ``deploymentId`` and ``deploymentConfig``.

    Raises:
        HttpError: from the Apps Script SDK on 4xx / 5xx — propagated.
    """
    script = get_service("script", "v1", credentials=creds)

    version_resp = execute_with_retry(
        lambda: script.projects().versions().create(
            scriptId=script_id,
            body={"description": description},
        ).execute(),
        idempotent=False,
        op_name="script.projects.versions.create",
    )
    version_number = version_resp["versionNumber"]

    return execute_with_retry(
        lambda: script.projects().deployments().create(
            scriptId=script_id,
            body={
                "versionNumber": version_number,
                "manifestFileName": _MANIFEST_FILENAME,
                "description": description,
            },
        ).execute(),
        idempotent=False,  # each deploy mints a new deploymentId
        op_name="script.projects.deployments.create",
    )


def list_deployments(creds: Credentials, script_id: str) -> list[dict[str, Any]]:
    """List a project's deployments via ``projects.deployments.list``.

    Returns the raw ``Deployment`` resources (each carries ``deploymentId``
    and ``deploymentConfig``). The list always includes an implicit
    ``@HEAD`` deployment (its ``deploymentConfig`` has NO ``versionNumber``
    — it tracks the latest saved content rather than a cut version); that
    one is NOT independently deletable and callers should skip it (see
    ``_lifecycle.uninstall_automation``).

    Pure read — wrapped ``idempotent=True`` so a transient 429/5xx during
    the listing retries rather than failing an uninstall.

    Raises:
        HttpError: from the Apps Script SDK on 4xx / 5xx — propagated.
    """
    script = get_service("script", "v1", credentials=creds)
    resp = execute_with_retry(
        lambda: script.projects().deployments().list(
            scriptId=script_id,
        ).execute(),
        idempotent=True,
        op_name="script.projects.deployments.list",
    )
    return list(resp.get("deployments", []))


def delete_deployment(
    creds: Credentials, script_id: str, deployment_id: str
) -> None:
    """Delete one deployment via ``projects.deployments.delete``.

    Used by uninstall to UNDEPLOY an automation (remove its live
    endpoints / published versions). Undeploying is the strongest reap the
    connector's ``script.deployments`` scope allows — the project FILE
    itself cannot be deleted (the Apps Script API has no ``projects.delete``
    and the connector's ``drive.file`` grant cannot see or trash a script
    project; Stream-0 finding S0-4).

    Wrapped ``idempotent=True``: re-deleting after a transient blip is
    safe (a definitive 404 for an already-gone deployment is not retried
    and propagates for the caller's best-effort handling).

    Raises:
        HttpError: from the Apps Script SDK on 4xx / 5xx — propagated
            (the ``@HEAD`` deployment refuses deletion with a 4xx; callers
            skip it rather than attempt it).
    """
    script = get_service("script", "v1", credentials=creds)
    execute_with_retry(
        lambda: script.projects().deployments().delete(
            scriptId=script_id,
            deploymentId=deployment_id,
        ).execute(),
        idempotent=True,
        op_name="script.projects.deployments.delete",
    )


# ---------------------------------------------------------------------
# Private validation helpers (pure)
# ---------------------------------------------------------------------


def _validate_menu(menu: Any) -> list[dict[str, str]]:
    """Validate + normalize the ``menu`` key. Returns a list of
    ``{name, function_name}`` dicts (empty list if ``menu`` is None)."""
    if menu is None:
        return []
    if not isinstance(menu, list):
        raise ValueError(
            f"menu must be a list of {{name, function_name}} entries, "
            f"got {type(menu).__name__}."
        )
    out: list[dict[str, str]] = []
    for i, item in enumerate(menu):
        if not isinstance(item, dict):
            raise ValueError(
                f"menu[{i}] must be a dict with 'name' and "
                f"'function_name', got {type(item).__name__}."
            )
        name = item.get("name")
        fn = item.get("function_name")
        if not name or not isinstance(name, str):
            raise ValueError(
                f"menu[{i}] is missing a non-empty string 'name'."
            )
        if not fn or not isinstance(fn, str):
            raise ValueError(
                f"menu[{i}] is missing a non-empty string "
                f"'function_name' (the .gs function the item runs)."
            )
        out.append({"name": name, "function_name": fn})
    return out


def _validate_triggers(triggers: Any) -> list[dict[str, Any]]:
    """Validate + normalize the ``triggers`` key. Returns a list of
    trigger dicts (empty list if ``triggers`` is None). Each dict keeps
    its ``type`` plus any extra keys the caller supplied (e.g. an
    ``every_hours`` for a time trigger) — this primitive doesn't
    prescribe the full trigger config, only that ``type`` is known."""
    if triggers is None:
        return []
    if not isinstance(triggers, list):
        raise ValueError(
            f"triggers must be a list of {{type, ...}} entries, got "
            f"{type(triggers).__name__}."
        )
    valid_types = {"time", "edit"}
    out: list[dict[str, Any]] = []
    for i, item in enumerate(triggers):
        if not isinstance(item, dict):
            raise ValueError(
                f"triggers[{i}] must be a dict with a 'type', got "
                f"{type(item).__name__}."
            )
        ttype = item.get("type")
        if ttype not in valid_types:
            raise ValueError(
                f"triggers[{i}] has unknown type {ttype!r}; expected "
                f"one of {sorted(valid_types)} "
                f"('time' = time-driven, 'edit' = onEdit)."
            )
        out.append(dict(item))
    return out


def _dedup_preserve_order(items: list[str]) -> list[str]:
    """De-duplicate a list of strings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
