"""Google API OAuth flow handler.

Handles the one-time browser consent dance and caches the resulting
tokens so subsequent runs are silent.

Client config discovery order (first match wins):
  1. ``GOOGLE_DOCS_OAUTH_PATH`` environment variable
  2. ``<creds_dir>/credentials.json``
  3. ``~/.gmail-mcp/gcp-oauth.keys.json`` (reused from gmail-mcp)
"""
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
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


def load_credentials(creds_dir: Path) -> Credentials:
    """Return valid Google OAuth credentials, running the consent flow if needed.

    Tokens are written to ``<creds_dir>/token.json`` regardless of where
    the client config came from — keeping app-specific scopes isolated.
    """
    creds_dir.mkdir(parents=True, exist_ok=True)
    token_file = creds_dir / "token.json"

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return creds

    client_config = find_client_config(creds_dir)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_config), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json())
    return creds
