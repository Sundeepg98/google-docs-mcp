"""Apps Script project + deployment management via Google's REST API.

Generic plumbing: knows nothing about google-docs-mcp's domain. Takes
a Credentials object and lets the caller create a project, push files,
cut a version, and deploy as a Web App — returning the live ``/exec``
URL.

For .gs file content authoring + which scripts to deploy, see callers
(e.g. ``google_docs_mcp.setup_apps_script``).

API reference:
  https://developers.google.com/apps-script/api/reference/rest
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.oauth2.credentials import Credentials
from google_docs_mcp.google_clients import get_service
from googleapiclient.errors import HttpError

# Apps Script's manifest file is conventionally named "appsscript" with
# type JSON. Every project requires exactly one.
_MANIFEST_FILENAME = "appsscript"


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
