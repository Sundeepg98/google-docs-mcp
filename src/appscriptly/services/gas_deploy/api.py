"""Apps Script project + deployment management via Google's REST API.

Generic plumbing: knows nothing about google-docs-mcp's domain. Takes
a Credentials object and lets the caller create a project, push files,
cut a version, and deploy as a Web App — returning the live ``/exec``
URL.

For .gs file content authoring + which scripts to deploy, see callers
(e.g. ``appscriptly.setup_apps_script``).

API reference:
  https://developers.google.com/apps-script/api/reference/rest
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from google.auth.credentials import Credentials  # base type: covers oauth2 + service-account flows
from appscriptly.google_clients import get_service
from appscriptly.services.apps_script._observability import (
    guarded_entry_point as _guarded_entry_point,
    reporter_helper_source as _reporter_helper_source,
)
from googleapiclient.errors import HttpError

# Apps Script's manifest file is conventionally named "appsscript" with
# type JSON. Every project requires exactly one.
_MANIFEST_FILENAME = "appsscript"

# Web App entry-point ``executeAs`` values the Apps Script manifest accepts.
#   USER_DEPLOYING  — the script runs as the deploying OAuth user (acts
#                     with that user's authority / data access).
#   USER_ACCESSING  — runs as whoever invokes the endpoint (requires each
#                     caller to be a Google-authenticated user).
_WEBAPP_EXECUTE_AS = frozenset({"USER_DEPLOYING", "USER_ACCESSING"})

# Web App entry-point ``access`` values (who may invoke the /exec URL).
#   ANYONE_ANONYMOUS — public, no Google sign-in (the webhook case:
#                      Slack / Stripe / external cron POST here).
#   ANYONE           — any Google-signed-in user.
#   DOMAIN           — anyone in the deployer's Workspace domain.
#   MYSELF           — only the deploying user.
_WEBAPP_ACCESS = frozenset({"ANYONE_ANONYMOUS", "ANYONE", "DOMAIN", "MYSELF"})

# Matches a top-level ``function doPost(...)`` declaration so we can rename
# the caller's handler and wrap it with an HMAC guard. Captures the param
# list so the renamed function keeps the caller's signature.
_DOPOST_DECL_RE = re.compile(r"\bfunction\s+doPost\s*\(([^)]*)\)")

# Same, for ``doGet`` — the failure-reporter wrapper (inject_error_reporting)
# guards both entry points; the HMAC guard only covers doPost.
_DOGET_DECL_RE = re.compile(r"\bfunction\s+doGet\s*\(([^)]*)\)")

# The HMAC-verify preamble injected ahead of a caller's ``doPost`` when an
# ANYONE_ANONYMOUS web app is deployed. ``{key}`` is the baked per-deployment
# 64-hex key. The caller's original ``doPost`` is renamed to
# ``__mcpUserDoPost`` and a new ``doPost`` verifies the signature first,
# returning a JSON 401-shaped body on failure WITHOUT calling user code.
# Mirrors restructure.gs::_verifyHmac (same scheme: hex HMAC-SHA256 over
# "<timestamp>.<rawBody>", mcp_ts + mcp_sig QUERY PARAMS, constant-time
# compare, 5-min skew window). Query params, not headers: the Apps Script
# runtime never delivers HTTP request headers to doPost(e), so a
# header-based guard would reject every request and brick the webhook.
_WEBAPP_HMAC_GUARD_TEMPLATE = """\
// === appscriptly: auto-injected HMAC request guard (ANYONE_ANONYMOUS) ===
// This web app is world-reachable, so every POST is authenticated with a
// shared HMAC-SHA256 signature before your handler runs. The signing key was
// generated at deploy time and returned to you as ``hmac_key``. Have the
// caller send, per request, QUERY PARAMS on the /exec URL:
//   POST <exec_url>?mcp_ts=<unix seconds>&mcp_sig=<signature>
//   where mcp_sig = lowercase_hex(HMAC_SHA256(key, timestamp + "." + body))
// Query params are required because Apps Script never exposes HTTP request
// headers to a web app (doPost only receives e.parameter / e.postData).
// A missing/stale/mismatched signature is rejected with stage:'auth' and
// your code is never invoked.
var MCP_WEBAPP_HMAC_KEY = '{key}';
var MCP_WEBAPP_HMAC_MAX_SKEW_SECONDS = 300;

function doPost(e) {{
  var __auth = __mcpVerifyWebappHmac(e);
  if (!__auth.ok) {{
    return ContentService
      .createTextOutput(JSON.stringify({{success: false, stage: 'auth', error: __auth.error}}))
      .setMimeType(ContentService.MimeType.JSON);
  }}
  return __mcpUserDoPost(e);
}}

function __mcpVerifyWebappHmac(e) {{
  if (!MCP_WEBAPP_HMAC_KEY) {{
    return {{ok: false, error: 'server HMAC key not configured'}};
  }}
  var params = (e && e.parameter) || {{}};
  var sig = params.mcp_sig;
  var tsRaw = params.mcp_ts;
  if (!sig || !tsRaw) {{
    return {{ok: false, error: 'missing mcp_sig / mcp_ts query parameter'}};
  }}
  var ts = parseInt(tsRaw, 10);
  if (isNaN(ts)) return {{ok: false, error: 'malformed mcp_ts'}};
  var now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > MCP_WEBAPP_HMAC_MAX_SKEW_SECONDS) {{
    return {{ok: false, error: 'stale or future timestamp'}};
  }}
  var body = (e && e.postData && e.postData.contents) || '';
  var raw = Utilities.computeHmacSha256Signature(tsRaw + '.' + body, MCP_WEBAPP_HMAC_KEY);
  var hex = '';
  for (var i = 0; i < raw.length; i++) {{
    var b = (raw[i] + 256) % 256;
    var h = b.toString(16);
    if (h.length === 1) h = '0' + h;
    hex += h;
  }}
  var got = String(sig).toLowerCase();
  var diff = hex.length ^ got.length;
  var max = Math.max(hex.length, got.length);
  for (var j = 0; j < max; j++) {{
    diff |= (hex.charCodeAt(j) || 0) ^ (got.charCodeAt(j) || 0);
  }}
  if (diff !== 0) return {{ok: false, error: 'signature mismatch'}};
  return {{ok: true}};
}}
// === end appscriptly HMAC guard ===

"""


def inject_webapp_hmac_guard(script_body: str, key: str) -> str:
    """Wrap a caller's ``doPost`` with an HMAC-verify guard for a public app.

    For an ``ANYONE_ANONYMOUS`` web app, the ``/exec`` URL is world-reachable,
    so we don't ship the caller's ``doPost`` unguarded. This:

      1. renames the caller's ``function doPost(<params>)`` to
         ``function __mcpUserDoPost(<params>)``;
      2. prepends a new ``doPost`` that verifies an ``mcp_sig`` /
         ``mcp_ts`` query-param HMAC (key baked in) and only then delegates
         to ``__mcpUserDoPost``, rejecting unsigned/forged/stale requests
         with ``{{success:false, stage:'auth'}}`` before any user code runs.
         The signature travels in the query string because the Apps Script
         runtime never delivers HTTP request headers to ``doPost``.

    Pure function (returns new source). The same scheme as
    ``restructure.gs`` so one signing implementation
    (``apps_script_hmac.compute_signature``) covers both surfaces.

    Args:
        script_body: the caller-supplied ``.gs`` source. MUST contain a
            top-level ``function doPost(...)`` declaration.
        key: the 64-hex per-deployment HMAC key to bake in.

    Raises:
        ValueError: ``key`` is empty, or ``script_body`` has no recognizable
            top-level ``doPost`` declaration to guard (we refuse to deploy a
            public web app we couldn't actually protect, rather than silently
            shipping it unguarded).
    """
    if not key:
        raise ValueError("Refusing to inject an empty HMAC key.")
    if not _DOPOST_DECL_RE.search(script_body):
        raise ValueError(
            "Cannot auto-inject the HMAC guard: no top-level "
            "'function doPost(...)' declaration found in script_body. An "
            "ANYONE_ANONYMOUS web app must have a guardable POST handler — "
            "define doPost(e) as a top-level function, or deploy with a "
            "non-public access mode (DOMAIN / MYSELF)."
        )
    # Rename only the FIRST doPost declaration (the entry point). A second
    # textual "doPost" inside a comment/string is left alone — the regex
    # targets the ``function doPost(`` declaration form specifically.
    renamed = _DOPOST_DECL_RE.sub(
        lambda m: f"function __mcpUserDoPost({m.group(1)})",
        script_body,
        count=1,
    )
    return _WEBAPP_HMAC_GUARD_TEMPLATE.format(key=key) + renamed


def inject_error_reporting(script_body: str) -> str:
    """Wrap a web app's ``doGet`` / ``doPost`` so a throw emails the owner.

    The Class-H arm of the generated-code observability pass (gap #5): a
    failing webhook otherwise surfaces only as an HTTP 500 to the external
    caller, a silent black hole for the DEPLOYING user. This mirrors
    ``inject_webapp_hmac_guard``'s rename-and-delegate shape: each present
    top-level entry point (``doGet`` / ``doPost``) is renamed to
    ``__appscriptlyUser<Entry>`` and a new ``doGet`` / ``doPost`` (the exact
    name Apps Script invokes) delegates to it inside a try / report /
    rethrow. The appscriptly failure reporter (MailApp, best-effort) is
    prepended once; it NEVER swallows the error (it rethrows, so the 500 and
    the execution-log FAILED row are unchanged).

    **Composition with the HMAC guard.** Apply THIS first (on the caller's
    body), then ``inject_webapp_hmac_guard`` for a public app: the HMAC
    ``doPost`` then wraps this reporting ``doPost`` (renamed by HMAC to
    ``__mcpUserDoPost``) which wraps the caller's ``__appscriptlyUserDoPost``
    — so the HMAC check stays the OUTERMOST thing on every request.

    **Scope.** The web-app manifest declares no explicit ``oauthScopes``, so
    Apps Script AUTO-DETECTS scopes from the code at authorization; the
    injected ``MailApp.sendEmail`` is auto-detected too, so
    ``script.send_mail`` rides the SAME per-script consent the user already
    grants for the deployed app — nothing is added to the connector's
    consent, and no explicit manifest scope is needed (adding one would
    suppress auto-detection of the caller's own scopes).

    Pure (returns new source). A no-op (returns the input unchanged) if
    neither entry point is present — ``deploy_web_app_project`` already
    rejects a body with neither ``doGet`` nor ``doPost``.

    Args:
        script_body: the caller-supplied ``.gs`` web-app source.

    Returns:
        The wrapped source: reporter helper + the guard(s) + the renamed
        caller body.
    """
    entries = [
        ("doPost", _DOPOST_DECL_RE, "__appscriptlyUserDoPost"),
        ("doGet", _DOGET_DECL_RE, "__appscriptlyUserDoGet"),
    ]
    wrapped = script_body
    guards: list[str] = []
    for entry_name, regex, user_name in entries:
        if not regex.search(wrapped):
            continue
        # Rename only the FIRST declaration (the entry point); a textual
        # match inside a comment/string is left alone (the regex targets the
        # ``function <entry>(`` declaration form).
        wrapped = regex.sub(
            lambda m, un=user_name: f"function {un}({m.group(1)})",
            wrapped,
            count=1,
        )
        guards.append(_guarded_entry_point(entry_name, user_name))
    if not guards:
        return script_body
    return (
        _reporter_helper_source().rstrip("\n")
        + "\n\n"
        + "\n\n".join(guards)
        + "\n\n"
        + wrapped
    )


@dataclass(frozen=True)
class WebAppDeployment:
    """Result of a successful Web App deployment."""

    script_id: str
    deployment_id: str
    version: int
    url: str  # the live /exec URL


class AppsScriptClient:
    """Thin wrapper around the Apps Script REST API for Web App deployments.

    Use it like::

        client = AppsScriptClient(creds)
        script_id = client.create_project("my project")
        client.push_files(script_id, manifest={...}, files={"Code": gs_source})
        version = client.create_version(script_id, "v1")
        deployment = client.deploy_webapp(
            script_id, version,
            description="v1", execute_as="USER_DEPLOYING", access="MYSELF",
        )
        print(deployment.url)
    """

    def __init__(self, creds: Credentials) -> None:
        self._svc = get_service("script", "v1", credentials=creds)

    def script_exists(self, script_id: str) -> bool:
        """True if the Apps Script project is still reachable.

        Used by the setup-state idempotency layer to detect when a user
        has manually deleted a script from Drive between runs — in
        which case the cached state's ``script_id`` is dead and we
        must start fresh.
        """
        try:
            self._svc.projects().get(scriptId=script_id).execute()
            return True
        except HttpError as e:
            if e.status_code == 404:
                return False
            raise

    def create_project(self, title: str) -> str:
        """Create a new standalone Apps Script project. Returns the scriptId."""
        resp = (
            self._svc.projects()
            .create(body={"title": title})
            .execute()
        )
        return resp["scriptId"]

    def push_files(
        self,
        script_id: str,
        *,
        manifest: dict[str, Any],
        files: dict[str, str],
    ) -> None:
        """Replace the project's full file list with ``manifest`` + ``files``.

        ``manifest`` is the appsscript.json content as a dict (we serialize).
        ``files`` is a ``{filename_without_extension: gs_source}`` mapping;
        each becomes a SERVER_JS file.

        Note: ``updateContent`` replaces ALL files atomically — any file
        not in the call disappears. For our use case (push everything
        from scratch) that's exactly what we want.
        """
        import json

        payload_files = [
            {
                "name": _MANIFEST_FILENAME,
                "type": "JSON",
                "source": json.dumps(manifest, indent=2),
            }
        ]
        for name, source in files.items():
            payload_files.append({
                "name": name,
                "type": "SERVER_JS",
                "source": source,
            })

        self._svc.projects().updateContent(
            scriptId=script_id,
            body={"files": payload_files},
        ).execute()

    def create_version(self, script_id: str, description: str) -> int:
        """Cut an immutable version of current content. Returns versionNumber."""
        resp = (
            self._svc.projects()
            .versions()
            .create(
                scriptId=script_id,
                body={"description": description},
            )
            .execute()
        )
        return int(resp["versionNumber"])

    def deploy_webapp(
        self,
        script_id: str,
        version: int,
        *,
        description: str = "",
    ) -> WebAppDeployment:
        """Create a Web App deployment of an existing version.

        The web-app entry-point configuration (``executeAs``, ``access``)
        is declared in the project's ``appsscript.json`` manifest pushed
        via ``push_files``. The deployment body MUST NOT include
        ``entryPoints`` — Apps Script API rejects:

            HttpError 400: Invalid JSON payload received.
            Unknown name "entryPoints": Cannot find field.

        on ``projects.deployments.create`` when the body carries that
        field. This was the v1.0.x bug fixed in v1.1.1.

        The deployment response DOES contain ``entryPoints`` populated
        from the manifest — that's where we extract the live ``/exec``
        URL.
        """
        resp = (
            self._svc.projects()
            .deployments()
            .create(
                scriptId=script_id,
                body={
                    "versionNumber": version,
                    "manifestFileName": _MANIFEST_FILENAME,
                    "description": description,
                },
            )
            .execute()
        )
        deployment_id = resp["deploymentId"]
        # Pull the live /exec URL out of entryPoints. The response includes
        # it directly — no string concatenation, no second API call.
        url = ""
        for entry in resp.get("entryPoints", []):
            web = entry.get("webApp") or {}
            if web.get("url"):
                url = web["url"]
                break
        if not url:
            raise RuntimeError(
                "Apps Script API returned a deployment with no webApp.url "
                "in entryPoints — unexpected response shape. "
                f"Full response: {resp!r}"
            )
        return WebAppDeployment(
            script_id=script_id,
            deployment_id=deployment_id,
            version=version,
            url=url,
        )


def build_webapp_manifest(
    *,
    execute_as: str = "USER_DEPLOYING",
    access: str = "ANYONE_ANONYMOUS",
    time_zone: str = "Etc/GMT",
) -> dict[str, Any]:
    """Build an ``appsscript.json`` manifest declaring a Web App entry point.

    Pure (no I/O). The web-app entry-point config lives in the MANIFEST
    (``executeAs`` / ``access``), NOT in the deployment create body —
    ``AppsScriptClient.deploy_webapp`` documents why the Apps Script API
    rejects ``entryPoints`` on ``deployments.create``. Mirrors the shape
    the runtime installer uses (``setup_apps_script._BASE_MANIFEST``):
    V8 runtime + a timeZone + the ``webapp`` block.

    Args:
        execute_as: ``"USER_DEPLOYING"`` (default — the endpoint runs as
            the deploying user, with their data access) or
            ``"USER_ACCESSING"`` (runs as each invoking Google user).
        access: who may hit the ``/exec`` URL —
            ``"ANYONE_ANONYMOUS"`` (default; the webhook case — no Google
            sign-in, so Slack/Stripe/cron can POST), ``"ANYONE"``,
            ``"DOMAIN"``, or ``"MYSELF"``.
        time_zone: tz database name for the project (default ``Etc/GMT``).

    Returns:
        A manifest dict ready to hand to ``AppsScriptClient.push_files``.

    Raises:
        ValueError: ``execute_as`` / ``access`` outside the accepted sets
            (caught client-side rather than via a generic Apps Script
            400).
    """
    if execute_as not in _WEBAPP_EXECUTE_AS:
        raise ValueError(
            f"execute_as must be one of {sorted(_WEBAPP_EXECUTE_AS)}; "
            f"got {execute_as!r}."
        )
    if access not in _WEBAPP_ACCESS:
        raise ValueError(
            f"access must be one of {sorted(_WEBAPP_ACCESS)}; got {access!r}."
        )
    return {
        "timeZone": time_zone,
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "webapp": {"executeAs": execute_as, "access": access},
    }


def deploy_web_app_project(
    creds: Credentials,
    *,
    script_body: str,
    title: str,
    execute_as: str = "USER_DEPLOYING",
    access: str = "ANYONE_ANONYMOUS",
    file_name: str = "Code",
) -> WebAppDeployment:
    """Create a standalone Apps Script project from ``script_body`` and
    deploy it as a Web App, returning the live ``/exec`` URL.

    The full create → push → version → deploy flow for a NEW standalone
    project carrying a ``doGet`` / ``doPost`` handler — i.e. exposing the
    caller's automation as an inbound HTTP endpoint / webhook. Reuses the
    existing ``AppsScriptClient`` primitives end-to-end; the only new
    piece is composing them + ``build_webapp_manifest``.

    Args:
        creds: OAuth credentials carrying ``script.projects`` +
            ``script.deployments`` (both in the baseline scope set — no
            second consent).
        script_body: the Apps Script ``.gs`` source. MUST define
            ``doGet(e)`` and/or ``doPost(e)`` (the Web App entry points
            Apps Script invokes on GET / POST). Caller-authored.
        title: title for the new Apps Script project (also its Drive
            filename).
        execute_as / access: Web App entry-point config — see
            ``build_webapp_manifest``.
        file_name: the ``.gs`` file name (without extension) the body is
            pushed as (default ``"Code"``).

    Returns:
        A ``WebAppDeployment`` (``script_id``, ``deployment_id``,
        ``version``, ``url``) — ``url`` is the live ``/exec`` endpoint.

    Raises:
        ValueError: blank ``script_body`` / ``title``, a body missing both
            ``doGet`` and ``doPost``, or an invalid ``execute_as`` /
            ``access`` (from ``build_webapp_manifest``).
        HttpError: from the underlying Apps Script API on 4xx / 5xx —
            propagated to the tool-layer envelope.
    """
    if not script_body or not script_body.strip():
        raise ValueError(
            "script_body cannot be empty — it must define doGet(e) and/or "
            "doPost(e)."
        )
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")
    if "doGet" not in script_body and "doPost" not in script_body:
        raise ValueError(
            "script_body must define a Web App entry point — at least one "
            "of doGet(e) or doPost(e). A Web App with neither has no HTTP "
            "handler and Apps Script would serve an error on every request."
        )

    manifest = build_webapp_manifest(execute_as=execute_as, access=access)

    client = AppsScriptClient(creds)
    script_id = client.create_project(title.strip())
    client.push_files(
        script_id,
        manifest=manifest,
        files={file_name: script_body},
    )
    version = client.create_version(script_id, f"{title.strip()} — web app")
    return client.deploy_webapp(
        script_id,
        version,
        description=f"{title.strip()} — web app deploy",
    )
