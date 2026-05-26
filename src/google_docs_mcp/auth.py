"""Google API OAuth flow handler.

Handles the one-time browser consent dance and caches the resulting
tokens so subsequent runs are silent.

Client config discovery order (first match wins):
  1. ``GOOGLE_DOCS_OAUTH_PATH`` environment variable
  2. ``<creds_dir>/credentials.json``
  3. ``~/.gmail-mcp/gcp-oauth.keys.json`` (reused from gmail-mcp)
"""
import json
import os
from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    # drive.readonly lets us read files uploaded by OTHER apps (e.g. cloud
    # chat's Drive connector). Required for the ``docx_drive_file_id``
    # input path on convert_docx_to_tabbed_doc.
    "https://www.googleapis.com/auth/drive.readonly",
    # v2.3.1 — Sheets read/write/create for the 2nd new service. The
    # full ``spreadsheets`` scope (not the narrower
    # ``spreadsheets.readonly``) is needed because gsheets_write_range
    # and gsheets_create_spreadsheet mutate the sheet. Existing users
    # pick this up automatically on next token refresh via the
    # ``include_granted_scopes=true`` incremental-consent flow (same
    # pattern that handled the earlier drive.readonly + Apps Script
    # scope additions); no forced re-consent.
    "https://www.googleapis.com/auth/spreadsheets",
]


def default_data_dir() -> Path:
    """User-scoped directory for OAuth tokens.

    Override with ``GOOGLE_DOCS_DATA_DIR`` env var. Default mirrors the
    gmail-mcp convention (``~/.google-docs-mcp/``) for predictability
    across pipx installs.
    """
    override = os.environ.get("GOOGLE_DOCS_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".google-docs-mcp"


def find_client_config(creds_dir: Path) -> Path:
    """Locate the OAuth client config (a.k.a. credentials.json / gcp-oauth.keys.json).

    Same Google Cloud project = same OAuth client, so reusing the
    gmail-mcp keys is fine. Token files stay separate per-app.
    """
    env_path = os.environ.get("GOOGLE_DOCS_OAUTH_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    local = creds_dir / "credentials.json"
    if local.exists():
        return local

    gmail_mcp = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
    if gmail_mcp.exists():
        return gmail_mcp

    raise FileNotFoundError(
        "No OAuth client config found. Tried:\n"
        f"  $GOOGLE_DOCS_OAUTH_PATH ({env_path or 'unset'})\n"
        f"  {local}\n"
        f"  {gmail_mcp}\n"
        "Either copy your existing gmail-mcp keys to one of these paths, "
        "set the env var, or download fresh ones from Google Cloud Console."
    )


def load_credentials(
    creds_dir: Path,
    extra_scopes: list[str] | None = None,
) -> Credentials:
    """Return valid Google OAuth credentials, running the consent flow if needed.

    Tokens are written to ``<creds_dir>/token.json`` regardless of where
    the client config came from — keeping app-specific scopes isolated.

    ``extra_scopes`` (optional) adds to the runtime ``SCOPES`` list — used
    by one-off privileged operations like ``setup-apps-script-auto``
    that need Apps Script management scopes which pure runtime callers
    don't need. If the cached token lacks any of these, it's deleted
    and a fresh consent flow runs.
    """
    required = SCOPES + (list(extra_scopes) if extra_scopes else [])

    creds_dir.mkdir(parents=True, exist_ok=True)
    token_file = creds_dir / "token.json"

    # Check the actual granted scopes in the token file BEFORE loading
    # via google-auth — ``from_authorized_user_file(file, SCOPES)`` echoes
    # the SCOPES arg back as ``creds.scopes``, masking missing grants
    # until the refresh attempt fails with ``invalid_scope``.
    if token_file.exists() and not _token_has_all_scopes(token_file, required):
        token_file.unlink()  # stale scope set — force fresh OAuth

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), required)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return creds

    client_config = find_client_config(creds_dir)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_config), required)
    # google_auth_oauthlib types run_local_server as returning the union
    # `external_account.Credentials | oauth2.Credentials`. In practice
    # an InstalledAppFlow always returns oauth2.Credentials (external-
    # account flows use a different Flow subclass). Cast to narrow the
    # return type to what this function actually returns.
    creds = cast(Credentials, flow.run_local_server(port=0))
    token_file.write_text(creds.to_json())
    return creds


def load_service_account_credentials(
    key_path: Path,
    impersonate_user: str,
    scopes: list[str],
):
    """Load Service Account credentials + impersonate a Workspace user via DWD.

    Used by the opt-in headless setup path (``setup-apps-script-auto
    --auth-mode=service-account``) for environments where no human can
    click OAuth consent — CI, server-side batch document processing,
    multi-user IT provisioning. For interactive desktop use, the
    regular OAuth flow (``load_credentials``) is the right call.

    Empirically confirmed against the Apps Script REST API (e.g.
    shiftavenue/gas-action does this in CI). Apps Script API rejects
    raw SA tokens but accepts SA-impersonating-user tokens because, to
    the API, the token looks like a user token for the subject email.

    Prerequisites (one-time, on the Workspace admin's side):
      1. Service Account created in GCP project, JSON key downloaded
      2. SA's numeric Client ID added to:
         Admin Console → Security → Access and data control →
         API controls → Manage Domain Wide Delegation → Add new
      3. Scopes authorized in that DWD entry (must include all of
         ``scopes`` here — typically GAS_DEPLOY_SCOPES)
      4. Up to 24h propagation (usually minutes)

    Args:
        key_path: path to the SA's JSON key file.
        impersonate_user: email of the Workspace user the SA acts as.
            The resulting Apps Script project will be owned by them.
        scopes: full scope list the SA needs (must match what the
            admin authorized in the DWD console).

    Personal @gmail.com accounts have no Admin Console and can NOT use
    this path — they must use the OAuth flow.
    """
    if not key_path.exists():
        raise FileNotFoundError(
            f"Service account key file not found: {key_path}. "
            "Download it from GCP Console → IAM & Admin → Service Accounts "
            "→ <your SA> → Keys → Add Key → JSON."
        )
    sa_creds = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=scopes,
    )
    return sa_creds.with_subject(impersonate_user)


def _token_has_all_scopes(token_file: Path, required: list[str]) -> bool:
    """Inspect the raw token JSON to see what scopes were actually granted.

    Unlike ``Credentials.scopes`` (which mirrors whatever was passed in to
    ``from_authorized_user_file``), the raw JSON's ``scopes`` field holds
    the actual set the user consented to. Used to detect when a version
    bump adds a new scope and the cached token needs replacing.
    """
    try:
        data = json.loads(token_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    granted = set(data.get("scopes") or [])
    return all(scope in granted for scope in required)
