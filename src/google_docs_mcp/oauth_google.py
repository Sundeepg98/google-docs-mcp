"""Google OAuth auth-code flow for downstream API access (v1.1+).

This is the SECOND OAuth dance — separate from FastMCP's
``GoogleProvider`` which handles the claude.ai connector auth and
whose upstream Google tokens are not exposed to tool code (private
``OAuthProxy._upstream_token_store``, no public getter).

Architecture (Shape C):

    claude.ai connector consent  ──── GoogleProvider ────►  identity
    (FastMCP issues JWT;                                    claims (sub, email)
     upstream Google tokens                                 → visible in tools
     trapped in private store)

    tool detects no Workspace creds                         user_store[sub]
    ────► returns Markdown URL ─────────────────────────►   gets the
                                                            *accessible*
    user clicks ─► /oauth/google/api/start                  Workspace
    ────► redirect to Google with state=signed_sub          tokens

    Google consent ─────────────────────────────────►       v
    user returns ─► /oauth/google/api/callback?code+state   stored, keyed
    ────► exchange code, persist creds to user_store        by ``sub``

Same OAuth client_id MUST be used for both flows (GoogleProvider's
and this one) — if they differ, ``sub`` claims won't match across
flows and the per-user key breaks. See README setup.
"""
from __future__ import annotations

import json
from pathlib import Path

from google_auth_oauthlib.flow import Flow

from .crypto import NonceStore
from .oauth_state import sign_state, verify_state

# Full scope set we need to operate on a user's Workspace. Mirrors what
# the upstream stdio mode requests (auth.py:SCOPES) plus Apps Script
# management scopes for the gdocs_setup_apps_script tool.
GOOGLE_API_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
]

CALLBACK_PATH = "/oauth/google/api/callback"
START_PATH = "/oauth/google/api/start"


def resolve_runtime_oauth_config() -> dict:
    """Bundle the env-derived OAuth config for tool code.

    Returns ``{client_config, signing_key, base_url}`` — the kwargs
    ``credentials.get_credentials_for_user`` needs. Read from env
    only (no request context), so it's safe to call from MCP tool
    code that doesn't have access to the HTTP request.

    Raises ``RuntimeError`` if any required env var is missing —
    that's an operator-config issue, not a user-facing one, and
    we want it to surface loudly at first call.
    """
    import json as _json
    import os as _os

    signing_key = _os.environ.get("MCP_BEARER_TOKEN")
    if not signing_key:
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var is required for OAuth state signing"
        )

    base_url = _os.environ.get("GOOGLE_OAUTH_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "GOOGLE_OAUTH_BASE_URL env var is required when resolving "
            "OAuth config without a request context (set it to the "
            "publicly-reachable URL of this server, e.g. "
            "https://my-app.fly.dev)"
        )

    inline = _os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_JSON")
    if inline:
        client_config = _json.loads(inline)
    else:
        path = _os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_PATH")
        if not path:
            raise RuntimeError(
                "One of GOOGLE_OAUTH_CLIENT_SECRETS_JSON or "
                "GOOGLE_OAUTH_CLIENT_SECRETS_PATH env vars is required"
            )
        from pathlib import Path as _Path
        client_config = load_client_config(_Path(path))

    return {
        "client_config": client_config,
        "signing_key": signing_key,
        "base_url": base_url.rstrip("/"),
    }


class OAuthCallbackError(Exception):
    """Raised when the OAuth callback can't complete (bad state, bad code, etc.).

    Carries an HTTP status code so the route handler can map it back
    to a clean response.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def load_client_config(client_secrets_path: Path) -> dict:
    """Load and validate a Google OAuth client_secrets.json.

    The file shape is ``{"web": {...}}`` or ``{"installed": {...}}``;
    ``Flow.from_client_config`` accepts either. We just sanity-check
    the structure to fail loudly on a malformed file rather than
    deep inside Flow's parsing.
    """
    data = json.loads(client_secrets_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not any(k in data for k in ("web", "installed")):
        raise ValueError(
            f"{client_secrets_path} doesn't look like a Google OAuth "
            "client_secrets file (expected top-level 'web' or 'installed' key)"
        )
    return data


def build_authorization_url(
    user_id: str,
    *,
    base_url: str,
    client_config: dict,
    signing_key: str,
    scopes: list[str] | None = None,
    ttl_seconds: int = 600,
) -> str:
    """Construct the Google consent URL the user clicks to authorize us.

    The returned URL contains a signed ``state`` param binding the
    response to ``user_id``. The matching ``/oauth/google/api/callback``
    handler verifies that state before persisting any tokens.
    """
    if not user_id:
        raise ValueError("user_id is required")

    flow = Flow.from_client_config(client_config, scopes=scopes or GOOGLE_API_SCOPES)
    flow.redirect_uri = f"{base_url.rstrip('/')}{CALLBACK_PATH}"

    state = sign_state(user_id, signing_key, ttl_seconds=ttl_seconds)
    # access_type=offline + prompt=consent: the only combination that
    # *reliably* returns a refresh_token. Without prompt=consent Google
    # may skip the consent screen on a re-auth and omit refresh_token,
    # which breaks our long-lived background refresh story.
    auth_url, _returned_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return auth_url


def exchange_code_for_credentials(
    *,
    state: str,
    authorization_response_url: str,
    base_url: str,
    client_config: dict,
    signing_key: str,
    nonce_store: NonceStore,
    scopes: list[str] | None = None,
) -> tuple[str, str]:
    """Verify state, exchange the auth code, return ``(user_id, creds_json)``.

    ``authorization_response_url`` is the full URL Google redirected
    to (i.e. ``request.url`` in the Starlette handler). ``Flow.fetch_token``
    parses the ``code`` and re-verifies the ``state`` from the URL.

    The ``creds_json`` is ``Credentials.to_json()`` output — store it
    raw in user_store and reconstruct with ``Credentials(...)`` at
    consumption time.
    """
    ok, user_id, err = verify_state(state, signing_key, nonce_store)
    if not ok:
        # Don't leak which validation failed publicly — could help an
        # attacker probe HMAC vs replay vs expiry. The server log
        # captures the specific reason for our debugging.
        raise OAuthCallbackError(
            "OAuth state could not be validated. Restart the authorization "
            "flow from your MCP tool.",
            status_code=400,
        )

    flow = Flow.from_client_config(
        client_config, scopes=scopes or GOOGLE_API_SCOPES, state=state,
    )
    flow.redirect_uri = f"{base_url.rstrip('/')}{CALLBACK_PATH}"

    try:
        flow.fetch_token(authorization_response=authorization_response_url)
    except Exception as e:  # noqa: BLE001 — google-auth-oauthlib raises many types
        raise OAuthCallbackError(
            f"Failed to exchange auth code with Google: {e}",
            status_code=502,
        ) from e

    creds = flow.credentials
    if not creds.refresh_token:
        # Without a refresh_token we can never silently extend the
        # session — first hour works, then user has to re-consent. Fail
        # loudly so the deployer knows their prompt/access_type config
        # is wrong.
        raise OAuthCallbackError(
            "Google returned credentials without a refresh_token. "
            "The OAuth client may be misconfigured (need access_type=offline, "
            "prompt=consent).",
            status_code=502,
        )

    assert user_id is not None  # narrowing — verify_state's contract on ok=True
    return user_id, creds.to_json()
