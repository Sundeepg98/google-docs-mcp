"""Automation lifecycle: mint-with-ledger, on_conflict, and uninstall.

The shared orchestration layer the ``as_*`` installers funnel their
create/push/deploy through so that EVERY mint writes a ledger row in the
same flow (a mint without a row is undiscoverable, per Stream-0 finding
S0-1) and so ``on_conflict`` behaves uniformly across the whole family.
Kept underscore-prefixed so tool auto-discovery skips it (it registers no
tools — the two lifecycle TOOLS live in ``lifecycle_tools.py``).

Three capabilities:

- ``mint_bound_automation`` — the create_bound_project -> set_project_content
  -> create_deployment trio + the ledger write, with ``on_conflict``
  (``new`` mints fresh, ``replace`` uninstalls prior installs of the same
  (tool, container) then mints, ``skip`` reuses the newest prior install
  instead of minting). Installers call this in place of the inline trio.

- ``uninstall_automation`` — undeploy every deployment + push a disarmed
  body + forget the ledger row. **Uninstall is FUNDAMENTALLY PARTIAL by
  proof (S0-4):** the Apps Script API has no ``projects.delete`` and the
  connector's ``drive.file`` grant cannot see or trash a script project,
  so the project FILE always lingers; and installable triggers can only be
  removed from INSIDE the script. The disarm handles the trigger reality
  honestly with a self-disarming stub (see ``build_disarm_script``).

- ``build_disarm_script`` — the pure inert-body generator.

Scope-neutral: this layer only exercises ``script.projects`` +
``script.deployments`` (both baseline-granted); every WORK scope lives in
the GENERATED per-script manifest, never appscriptly's own consent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from googleapiclient.errors import HttpError

from appscriptly import automation_ledger
from appscriptly.credentials import current_user_id_or_none
from appscriptly.services.apps_script.api import (
    create_bound_project as _create_bound_project,
    create_deployment as _create_deployment,
    delete_deployment as _delete_deployment,
    get_project_content as _get_project_content,
    list_deployments as _list_deployments,
    set_project_content as _set_project_content,
)
from appscriptly.setup_state import compute_content_hash

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

# The stdio / single-tenant install has no OAuth ``sub`` (the operator
# runs locally). The ledger still tracks their automations under a stable
# sentinel key so ``as_list_installed_automations`` / uninstall work
# identically in stdio mode.
_STDIO_LOCAL_USER = "__stdio_local__"

# The three on_conflict policies (mirrors the convert path's T2.3 shape).
#   new     — always mint a fresh automation (historical behavior).
#   replace — uninstall any prior install of the same (tool, container),
#             then mint fresh (stops the S0-3 littering, no orphans).
#   skip    — if a prior install of the same (tool, container) exists,
#             return it unchanged instead of minting a duplicate.
VALID_ON_CONFLICT: frozenset[str] = frozenset({"new", "replace", "skip"})
DEFAULT_ON_CONFLICT = "new"

# A valid Apps Script (JS) function identifier. Handler names pulled from
# the ledger are re-validated against this before being embedded in the
# disarm body — a malformed name (tampered ledger) is skipped, never
# injected. Mirrors sheet_menu._JS_IDENTIFIER_RE.
_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# Names the disarm stub defines itself; a ledger handler colliding with
# one of these is skipped (the stub already covers it).
_DISARM_RESERVED = frozenset({"onOpen", "__mcpDisarmAllTriggers"})

# The disarmed project keeps ONLY the trigger-management scope so its
# self-reaper can delete leftover installable triggers on their next fire.
# It drops every WORK scope the original automation carried. This is
# already-granted for any activated trigger (activation required it), so
# pushing it prompts no new consent.
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"
_DISARM_MANIFEST: dict[str, Any] = {
    "timeZone": "Etc/UTC",
    "runtimeVersion": "V8",
    "oauthScopes": [_TRIGGER_SCOPE],
}


def _ledger_user_id() -> str:
    """The ledger key for the calling user (HTTP sub, or the stdio sentinel)."""
    return current_user_id_or_none() or _STDIO_LOCAL_USER


def validate_on_conflict(on_conflict: str) -> str:
    """Return ``on_conflict`` if valid, else raise a clear ValueError.

    The canonical definition of the three policies, with their edge-case
    caveats (both non-blocking review riders on the Stream-2 PR):

    - ``new``: always install a fresh automation (may leave duplicates).
    - ``replace``: uninstall any prior install of the same (tool, container)
      FIRST, then install fresh. TRANSIENT WINDOW: between the disarm of the
      old install and the new one becoming live there is a gap where NEITHER
      works, and for the trigger / menu-action classes the new install still
      needs its one-time activation (Run + Allow) before it fires, so the
      automation is dormant from the disarm until the user activates the
      replacement. Prefer ``new`` if you need zero-downtime overlap.
    - ``skip``: if a prior install of the same (tool, container) exists,
      return it UNCHANGED instead of installing a duplicate. NO LIVENESS
      CHECK: ``skip`` trusts the ledger row and does NOT probe whether that
      prior install is still deployed / activated / healthy (there is no
      cheap per-automation liveness signal, a bound automation has no /exec
      to GET, and a processes read needs its own API call). If the prior
      install may have been deleted or broken, use ``replace`` (or ``new``)
      to force a fresh, known-good install rather than ``skip``.

    Raised as ``ValueError`` so the ``@workspace_tool`` envelope renders it
    as a user-facing ``ToolError`` (pre-validation, before any API call).
    """
    if on_conflict not in VALID_ON_CONFLICT:
        raise ValueError(
            f"on_conflict must be one of {sorted(VALID_ON_CONFLICT)} "
            f"('new' = always install a fresh automation, 'replace' = "
            f"uninstall any prior install on this container first, 'skip' = "
            f"reuse the existing install instead of adding a duplicate); "
            f"got {on_conflict!r}."
        )
    return on_conflict


def compute_automation_hash(script_body: str, manifest_dict: dict[str, Any]) -> str:
    """Content hash of a generated automation (manifest + ``.gs`` body).

    Mirrors ``setup_apps_script``'s use of
    ``setup_state.compute_content_hash`` so the whole codebase hashes
    generated Apps Script content one way. The private ``__plan__`` echo
    ``build_manifest`` adds is stripped first so the hash matches what
    ``set_project_content`` actually pushes (it strips ``__plan__`` too) —
    otherwise the recorded hash would never equal a re-derivation.
    """
    manifest_for_hash = {
        k: v for k, v in manifest_dict.items() if k != "__plan__"
    }
    return compute_content_hash(
        manifest_for_hash, {"Code": script_body}
    )


def resolve_install_conflict(
    creds: Credentials,
    *,
    tool: str,
    container_id: str | None,
    on_conflict: str,
) -> tuple[dict[str, Any] | None, int]:
    """Apply ``on_conflict`` BEFORE a mint. Returns ``(skip_row, replaced)``.

    Shared by ``mint_bound_automation`` (bound installers) and the
    standalone ``as_deploy_web_app`` path so ``on_conflict`` behaves
    identically across the whole installer family:

    - ``skip`` -> ``(newest_prior_row, 0)`` if a prior install of the same
      (tool, container) exists, else ``(None, 0)``. The caller returns the
      prior install instead of minting a duplicate.
    - ``replace`` -> ``(None, N)`` after uninstalling the N prior installs
      (undeploy + disarm + forget each). The caller then mints fresh.
    - ``new`` -> ``(None, 0)``. The caller mints fresh unconditionally.
    """
    validate_on_conflict(on_conflict)
    user_id = _ledger_user_id()
    if on_conflict == "skip":
        prior = automation_ledger.find_automations(user_id, tool, container_id)
        return (prior[0] if prior else None), 0
    replaced = 0
    if on_conflict == "replace":
        for row in automation_ledger.find_automations(
            user_id, tool, container_id
        ):
            uninstall_automation(
                creds,
                row["script_id"],
                handler_functions=row.get("handler_functions"),
                forget=True,
            )
            replaced += 1
    return None, replaced


@dataclass(frozen=True)
class MintResult:
    """Outcome of ``mint_bound_automation``.

    ``reused`` is True only for ``on_conflict='skip'`` when a prior install
    was returned instead of minting. ``replaced`` counts the prior installs
    uninstalled for ``on_conflict='replace'``. The installer plugs
    ``script_id`` / ``deployment_id`` into its normal return shape so a
    reused/replaced result is schema-identical to a fresh install.
    """

    script_id: str
    deployment_id: str
    reused: bool = False
    replaced: int = 0


def mint_bound_automation(
    creds: Credentials,
    *,
    tool: str,
    container_id: str,
    container_kind: str | None,
    project_name: str,
    script_body: str,
    manifest_dict: dict[str, Any],
    on_conflict: str = DEFAULT_ON_CONFLICT,
    handler_functions: Sequence[str] = (),
    deploy_description: str | None = None,
) -> MintResult:
    """Mint a bound automation (or reuse/replace a prior one) + record it.

    The shared body every ``as_*`` bound installer routes through in place
    of the inline create_bound_project -> set_project_content ->
    create_deployment trio. ``on_conflict`` is honored BEFORE the mint;
    the ledger row is written IN THE SAME FLOW as the deploy (S0-1: a mint
    without a row is undiscoverable).

    Args:
        creds: OAuth credentials carrying ``script.projects`` +
            ``script.deployments`` (baseline).
        tool: the installer tool name (the ledger + on_conflict key).
        container_id: Drive ID of the bound container (or a stable
            identity string for a standalone deploy). Part of the
            on_conflict key.
        container_kind: ``docs`` / ``sheets`` / ``slides`` / etc. — stored
            for the inventory listing.
        project_name: title for the new Apps Script project.
        script_body: the generated ``.gs`` source (caller-authored).
        manifest_dict: the manifest from ``build_manifest`` (its private
            ``__plan__`` echo is stripped by ``set_project_content``).
        on_conflict: ``new`` / ``replace`` / ``skip`` (validated).
        handler_functions: installable-trigger handler names (Classes
            D/E) recorded so uninstall can self-disarm them; empty
            otherwise.
        deploy_description: optional deployment description; a plain
            default is used when omitted.

    Returns:
        A ``MintResult``. For ``skip`` with a prior install, no API call is
        made and ``reused=True``.
    """
    skip_row, replaced = resolve_install_conflict(
        creds, tool=tool, container_id=container_id, on_conflict=on_conflict
    )
    if skip_row is not None:
        return MintResult(
            script_id=skip_row["script_id"],
            deployment_id=skip_row.get("deployment_id") or "",
            reused=True,
            replaced=0,
        )
    user_id = _ledger_user_id()

    # Mint: create the bound project, push the body + manifest, deploy.
    project = _create_bound_project(creds, container_id, project_name)
    script_id = project["scriptId"]
    _set_project_content(creds, script_id, script_body, manifest_dict)
    deployment = _create_deployment(
        creds,
        script_id,
        description=deploy_description or f"{project_name} initial deploy",
    )
    deployment_id = deployment["deploymentId"]

    # Ledger write — SAME flow as the mint. A failure here surfaces (the
    # automation would otherwise be undiscoverable); it is a local SQLite
    # insert on the same volume user_store uses, so a failure signals a
    # genuinely broken data plane, not a routine error to swallow.
    automation_ledger.record_automation(
        user_id=user_id,
        script_id=script_id,
        tool=tool,
        container_id=container_id,
        container_kind=container_kind,
        deployment_id=deployment_id,
        project_url=f"https://script.google.com/d/{script_id}/edit",
        content_hash=compute_automation_hash(script_body, manifest_dict),
        handler_functions=handler_functions,
    )

    return MintResult(
        script_id=script_id,
        deployment_id=deployment_id,
        reused=False,
        replaced=replaced,
    )


# ---------------------------------------------------------------------
# Update path (Stream 5): re-push current codegen to the EXISTING project
# ---------------------------------------------------------------------

# A top-level ``function NAME(`` declaration (for naming the re-activation
# function when an update adds a scope). Same shape sheet_dashboard uses.
_JS_FUNCTION_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")


def _manifest_scopes(content: dict[str, Any]) -> set[str]:
    """Extract the ``oauthScopes`` set from a projects.getContent payload.

    Reads the manifest file (name ``appsscript``, type ``JSON``) out of the
    live content and returns its declared scopes. A missing / unparseable
    manifest yields an empty set (treated as "no scopes known", so the update
    reports every new scope as an addition, the safe over-warn direction).
    """
    for f in content.get("files", []):
        if f.get("name") == "appsscript" and f.get("type") == "JSON":
            try:
                manifest = json.loads(f.get("source") or "{}")
            except (ValueError, TypeError):
                return set()
            scopes = manifest.get("oauthScopes")
            return set(scopes) if isinstance(scopes, list) else set()
    return set()


def _reactivation_function(script_body: str) -> str:
    """Name the function the user runs once to re-Allow an added scope.

    Prefers ``installTrigger`` (the trigger classes define it, and re-running
    it re-authorizes + re-installs the trigger); otherwise the first
    top-level function declaration in the pushed body; otherwise a generic
    placeholder. Used only to fill the activation instructions when an update
    adds a scope (the editor exposes no function-level deep link, per
    activation.py).
    """
    if "function installTrigger(" in script_body:
        return "installTrigger"
    m = _JS_FUNCTION_DECL_RE.search(script_body)
    return m.group(1) if m else "any function"


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of ``update_automation``.

    ``status`` is ``updated`` when the content changed and was re-pushed, or
    ``unchanged`` when the new content hashed identically to what is deployed
    (no re-push). ``added_scopes`` lists OAuth scopes the new manifest
    declares that the live deployment did not, and ``needs_reactivation`` is
    True exactly when that list is non-empty (the user must Run + Allow once
    to grant them).
    """

    script_id: str
    deployment_id: str
    status: str
    content_hash_before: str | None
    content_hash_after: str
    added_scopes: list[str]
    needs_reactivation: bool


def update_automation(
    creds: Credentials,
    script_id: str,
    *,
    script_body: str,
    manifest_dict: dict[str, Any],
    handler_functions: Sequence[str],
    row: dict[str, Any],
) -> UpdateResult:
    """Re-push CURRENT codegen to the EXISTING project (consent-preserving).

    Closes gap #6 (stale generated-code drift): when the caller regenerates a
    bound automation's ``.gs`` (a codegen fix, a scope correction, an added
    step), this pushes the new content + a fresh version + deployment onto
    the SAME ``script_id``. It NEVER mints a new project, so the user's
    existing per-script authorization (and any installed trigger) is
    preserved, exactly like PR-D's GONE-heal redeploys on the surviving
    project.

    Refreshes the ledger row's ``content_hash`` + ``handler_functions`` +
    ``deployment_id`` (keeping ``tool`` / ``container`` / ``created_at``).

    Scope-change detection: reads the LIVE manifest (``projects.getContent``)
    before pushing and compares its scopes to the new manifest's. Any scope
    the new content ADDS means the user must Run + Allow once to grant it,
    surfaced as ``needs_reactivation`` + ``added_scopes`` (the tool layer
    turns that into the shared activation fields).

    No-op fast path: if the new content hashes identically to what is
    deployed, returns ``status='unchanged'`` WITHOUT re-pushing.

    ``row`` is the ledger row for ``script_id`` (already fetched +
    ownership-checked by the caller).
    """
    old_hash = row.get("content_hash")
    new_hash = compute_automation_hash(script_body, manifest_dict)
    new_scopes = set(manifest_dict.get("oauthScopes") or [])

    # Read the live manifest scopes BEFORE any push, to detect an addition.
    # A gone project (404) or a read blip yields an empty old set, which
    # reports every new scope as added (the safe over-warn direction).
    try:
        old_scopes = _manifest_scopes(_get_project_content(creds, script_id))
    except HttpError:
        old_scopes = set()
    added_scopes = sorted(new_scopes - old_scopes)

    if new_hash == old_hash:
        # Identical content already deployed, nothing to push. (Same hash
        # implies the same manifest, so added_scopes is empty here.)
        return UpdateResult(
            script_id=script_id,
            deployment_id=row.get("deployment_id") or "",
            status="unchanged",
            content_hash_before=old_hash,
            content_hash_after=new_hash,
            added_scopes=added_scopes,
            needs_reactivation=bool(added_scopes),
        )

    # Push the new content + cut a fresh version + deployment on the SAME
    # project (consent-preserving; no new scriptId).
    _set_project_content(creds, script_id, script_body, manifest_dict)
    deployment = _create_deployment(
        creds, script_id, description="appscriptly update"
    )
    new_deployment_id = deployment["deploymentId"]

    # Refresh the ledger row (UPSERT on script_id preserves created_at).
    automation_ledger.record_automation(
        user_id=row["user_id"],
        script_id=script_id,
        tool=row["tool"],
        container_id=row.get("container_id"),
        container_kind=row.get("container_kind"),
        deployment_id=new_deployment_id,
        project_url=row.get("project_url")
        or f"https://script.google.com/d/{script_id}/edit",
        exec_url=row.get("exec_url"),
        content_hash=new_hash,
        handler_functions=handler_functions,
    )

    return UpdateResult(
        script_id=script_id,
        deployment_id=new_deployment_id,
        status="updated",
        content_hash_before=old_hash,
        content_hash_after=new_hash,
        added_scopes=added_scopes,
        needs_reactivation=bool(added_scopes),
    )


def _deployment_is_head(deployment: dict[str, Any]) -> bool:
    """True for the implicit ``@HEAD`` deployment (no cut version).

    ``deployments.list`` always returns a HEAD deployment that tracks the
    latest saved content rather than a versioned deployment; it is not
    independently deletable, so uninstall skips it. Identified by the
    ABSENCE of a ``versionNumber`` in its ``deploymentConfig``.
    """
    config = deployment.get("deploymentConfig") or {}
    return "versionNumber" not in config


def uninstall_automation(
    creds: Credentials,
    script_id: str,
    *,
    handler_functions: Sequence[str] | None = None,
    forget: bool = True,
) -> dict[str, Any]:
    """Uninstall an automation as far as the connector's scopes allow.

    HONEST, PARTIAL by proof (S0-4). Does three things, then reports each
    truthfully:

      1. **Undeploy** every versioned deployment (``script.deployments``) —
         removes web-app ``/exec`` endpoints and published versions.
      2. **Disarm** the code: replace ALL project files with an inert stub
         (``build_disarm_script``). Menus stop rebuilding (onOpen becomes a
         no-op), custom functions vanish, and any installable trigger this
         automation created now points at a self-disarming handler that
         deletes every project trigger on its next fire.
      3. **Forget** the ledger row so the automation leaves the inventory.

    What it CANNOT do (and says so): delete the Apps Script PROJECT FILE —
    there is no ``projects.delete`` and the ``drive.file`` grant cannot
    trash a script project. The response includes the editor URL so the
    user can remove the file manually.

    Args:
        creds: OAuth credentials carrying ``script.projects`` +
            ``script.deployments``.
        script_id: the project to uninstall.
        handler_functions: the installable-trigger handler names to
            self-disarm. When None, they are read from the ledger row.
        forget: drop the ledger row when done (default True). ``replace``
            passes True; a direct uninstall of an unrecorded script also
            forgets (a no-op if absent).

    Returns:
        ``{script_id, status, undeployed_count, undeploy_errors,
        content_disarmed, ledger_forgotten, project_file_removed,
        project_url, message}``. ``status`` is ``uninstalled`` normally or
        ``already_gone`` when the project no longer exists.
    """
    if handler_functions is None:
        row = automation_ledger.get_automation(script_id)
        stored = (row or {}).get("handler_functions")
        handlers: Sequence[str] = stored if isinstance(stored, list) else []
    else:
        handlers = handler_functions

    project_url = f"https://script.google.com/d/{script_id}/edit"
    undeployed = 0
    undeploy_errors: list[str] = []
    project_gone = False

    # 1. Undeploy every versioned deployment (best-effort, per-deployment).
    try:
        deployments = _list_deployments(creds, script_id)
    except HttpError as e:
        if e.status_code == 404:
            project_gone = True
            deployments = []
        else:
            raise
    for dep in deployments:
        if _deployment_is_head(dep):
            continue
        dep_id = dep.get("deploymentId")
        if not dep_id:
            continue
        try:
            _delete_deployment(creds, script_id, dep_id)
            undeployed += 1
        except HttpError as e:  # noqa: PERF203 — per-deployment best effort
            undeploy_errors.append(f"{dep_id}: {e.status_code}")

    # 2. Disarm the content (replace all files with the inert stub).
    content_disarmed = False
    if not project_gone:
        disarm_body = build_disarm_script(handlers)
        try:
            _set_project_content(
                creds, script_id, disarm_body, dict(_DISARM_MANIFEST)
            )
            content_disarmed = True
        except HttpError as e:
            if e.status_code == 404:
                project_gone = True
            else:
                raise

    # 3. Forget the ledger row (the automation leaves the inventory).
    ledger_forgotten = False
    if forget:
        ledger_forgotten = automation_ledger.forget_automation(script_id)

    if project_gone:
        message = (
            "This automation's Apps Script project no longer exists in your "
            "Google account (it was already deleted). It has been removed "
            "from your appscriptly inventory."
        )
        status = "already_gone"
    else:
        message = (
            "Automation uninstalled. Its deployments were removed and its "
            "code was replaced with an inert stub, so it no longer runs: "
            "menus stop appearing, custom functions stop resolving, and any "
            "scheduled or reactive trigger deletes itself the next time it "
            "would have fired. ONE thing remains that only you can do: the "
            "Apps Script project FILE still exists in your account (Google "
            "provides no way for appscriptly to delete a script project). "
            f"To remove it completely, open {project_url} and use File > "
            "Move to trash. You can also delete any leftover trigger there "
            "under the clock icon (Triggers)."
        )
        status = "uninstalled"

    return {
        "script_id": script_id,
        "status": status,
        "undeployed_count": undeployed,
        "undeploy_errors": undeploy_errors,
        "content_disarmed": content_disarmed,
        "ledger_forgotten": ledger_forgotten,
        "project_file_removed": False,
        "project_url": project_url,
        "message": message,
    }


def build_disarm_script(handler_functions: Sequence[str] = ()) -> str:
    """Generate the inert ``.gs`` stub an uninstall pushes over the code.

    PURE — same input, byte-identical output. The stub:

      * redefines ``onOpen`` as a no-op so the custom menu stops rebuilding;
      * defines ``__mcpDisarmAllTriggers`` which deletes every installable
        trigger on the project;
      * for each known handler name, redefines it to call the reaper — so
        an installable trigger that still targets that function deletes all
        project triggers (itself included) on its next fire, then no-ops.

    Handler names are re-validated as JS identifiers before embedding (a
    malformed ledger value is skipped, never injected) and collisions with
    the stub's own function names are skipped.
    """
    seen: set[str] = set()
    handler_defs: list[str] = []
    for name in handler_functions:
        if not isinstance(name, str):
            continue
        if name in _DISARM_RESERVED or name in seen:
            continue
        if not _JS_IDENTIFIER_RE.match(name):
            continue
        seen.add(name)
        handler_defs.append(
            f"function {name}(e) {{ __mcpDisarmAllTriggers(); }}"
        )

    handler_block = ""
    if handler_defs:
        handler_block = (
            "\n// Self-disarming handlers: any installable trigger still\n"
            "// targeting one of these deletes every project trigger on its\n"
            "// next fire, then does nothing.\n"
            + "\n".join(handler_defs)
            + "\n"
        )

    return (
        "// appscriptly: this automation was UNINSTALLED "
        "(as_uninstall_automation).\n"
        "// The original code has been replaced with this inert stub. The\n"
        "// Apps Script project file itself still exists in your account\n"
        "// (Google provides no API to delete a script project, and this\n"
        "// connector cannot trash it); remove it from the editor's File\n"
        "// menu if you want it fully gone.\n"
        "\n"
        "// Menu rebuild disabled: onOpen is now a no-op.\n"
        "function onOpen(e) {}\n"
        "\n"
        "// Removes every installable trigger left on this project.\n"
        "function __mcpDisarmAllTriggers() {\n"
        "  try {\n"
        "    var triggers = ScriptApp.getProjectTriggers();\n"
        "    for (var i = 0; i < triggers.length; i++) {\n"
        "      ScriptApp.deleteTrigger(triggers[i]);\n"
        "    }\n"
        "  } catch (err) {\n"
        "    // Best effort; the stub does no work regardless.\n"
        "  }\n"
        "}\n"
        f"{handler_block}"
    )
