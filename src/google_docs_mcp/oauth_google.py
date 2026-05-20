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
import os
from pathlib import Path

from google_auth_oauthlib.flow import Flow

from .crypto import NonceStore
from .oauth_state import sign_state, verify_state

# Default consent set requested at first authorization — mirrors what
# the upstream stdio mode requests (auth.py:SCOPES) for ordinary
# read/write Workspace operations. ``script.*`` scopes are NOT in this
# list anymore (v1.x scope reduction, Issue #17): they are requested
# incrementally only by ``gdocs_setup_apps_script``. The
# ``_check_scopes_or_raise`` path uses ``include_granted_scopes=true``
# (see ``build_authorization_url`` below), so consenting to the Apps
# Script scopes later does NOT reset the user's existing grants — it
# adds the missing ones. Pure-runtime users who never run the
# Apps-Script setup never see those scopes on their consent screen.
GOOGLE_API_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

CALLBACK_PATH = "/oauth/google/api/callback"
START_PATH = "/oauth/google/api/start"

# Identity-only scopes — what GoogleProvider's ``required_scopes``
# advertises so Claude.ai's connector OAuth completes with at least
# enough to identify the user. The full Workspace scope set goes into
# ``valid_scopes`` (and the post-init patches) so it gets requested
# during the same consent screen.
IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def configure_auth_for_http(mcp) -> None:
    """Wire FastMCP's GoogleProvider so HTTP requests are per-user-authed.

    Call ONCE at HTTP startup before ``mcp.run()`` / ``run_http(mcp)``.
    Stdio mode never calls this; ``mcp.auth`` stays None and FastMCP
    skips auth middleware entirely — preserving the v1.0 local trust
    model for Claude Desktop / Code.

    Implementation mirrors ``taylorwilsdon/google_workspace_mcp``'s
    ``configure_server_for_http`` (``core/server.py:494-539``):

    1. ``required_scopes=[openid, email]`` — minimum for identity.
       ``email`` must be REQUIRED (not just valid) or
       ``get_access_token().claims["email"]`` returns None and our
       per-user lookup breaks.
    2. ``valid_scopes=[full union]`` — advertises Workspace scopes
       at the well-known endpoint.
    3. **Post-init patches**: writing ``valid_scopes`` in the
       constructor only updates discovery metadata. To make
       Claude.ai's DCR client actually REQUEST the Workspace scopes,
       we must also mutate:
       - ``provider.client_registration_options.default_scopes``
       - ``provider._default_scope_str`` (private — fragile across
         FastMCP versions; documented in v1.1 release notes)
       Without these, Claude.ai gets identity-only tokens and every
       Workspace tool call silently fails with insufficient scope.

    Sets ``OAUTHLIB_RELAX_TOKEN_SCOPE=1`` so partial-grant consents
    (user unchecked Apps Script on the Google consent screen) don't
    raise from ``Flow.fetch_token``.

    Idempotent — if ``mcp.auth`` is already set, returns without
    re-wiring.
    """
    if mcp.auth is not None:
        return

    # Tolerate partial-grant consents. Without this, if a user unchecks
    # "manage Apps Script" on the consent screen, the entire OAuth
    # callback raises and re-auth becomes a dead-end. With it, the
    # creds reflect what was actually granted and our per-tool scope
    # check later triggers a targeted re-elicit.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    # NEVER set OAUTHLIB_INSECURE_TRANSPORT on Fly — we're on HTTPS.
    # Documented warning in the agent's research; surface loudly if
    # someone foot-guns themselves.
    if os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") == "1":
        raise RuntimeError(
            "OAUTHLIB_INSECURE_TRANSPORT=1 detected. This is for local "
            "http://localhost dev ONLY and must NEVER be set on Fly "
            "(which uses HTTPS). Remove it from your fly.toml / secrets."
        )

    cfg = resolve_runtime_oauth_config()
    client_block = cfg["client_config"].get("web") or cfg["client_config"].get("installed") or {}
    client_id = client_block.get("client_id")
    client_secret = client_block.get("client_secret")
    if not (client_id and client_secret):
        raise RuntimeError(
            "GoogleProvider requires client_id and client_secret in "
            "GOOGLE_OAUTH_CLIENT_SECRETS_JSON. Check your OAuth client "
            "config has the 'web' or 'installed' block populated."
        )

    # Import GoogleProvider lazily so stdio users without fastmcp's
    # extras installed don't trip on a missing dep at module-load time.
    from fastmcp.server.auth.providers.google import GoogleProvider

    full_scope_union = sorted(set(IDENTITY_SCOPES) | set(GOOGLE_API_SCOPES))

    provider = GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=cfg["base_url"],
        required_scopes=IDENTITY_SCOPES,
        valid_scopes=full_scope_union,
    )

    # Post-init scope bundling — taylorwilsdon's core/server.py:524-537.
    # These are what actually make Claude.ai request the Workspace
    # scopes during connector consent (vs just openid+email).
    if getattr(provider, "client_registration_options", None) is not None:
        provider.client_registration_options.default_scopes = full_scope_union
    # Private attr — load-bearing per the agent's source dive. If a
    # future FastMCP renames this, every Workspace tool call silently
    # 401s; CI live tests will catch it (re-consent is needed anyway).
    provider._default_scope_str = " ".join(full_scope_union)
    # CIMD path (Claude Code uses CIMD; Claude.ai uses DCR — set both
    # so we work across surfaces).
    cimd_mgr = getattr(provider, "_cimd_manager", None)
    if cimd_mgr is not None:
        try:
            cimd_mgr.default_scope = " ".join(full_scope_union)
        except AttributeError:
            pass  # older FastMCP without _cimd_manager.default_scope

    mcp.auth = provider


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

    from . import keys as _keys

    # v2.6 (#48): purpose-routed via keys.get_key("oauth_state") so the
    # v2.0b strict-flip activates HKDF-derivation for OAuth state HMACs
    # without further edits here. _master() inside get_key raises with
    # the existing operator-config message if MCP_BEARER_TOKEN is unset.
    try:
        signing_key = _keys.get_key("oauth_state").decode("utf-8")
    except RuntimeError as e:
        # Preserve the historical message wording so callers /
        # integration tests that match on it keep working.
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var is required for OAuth state signing"
        ) from e

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

    # Proper PKCE: generate our own code_verifier so we control storage,
    # then pass it to sign_state for server-side persistence keyed by
    # the state's nonce. verify_state retrieves it on callback so the
    # token exchange can complete. This makes PKCE behavior
    # deterministic across all auth URLs (every URL has code_challenge;
    # the matching verifier is always recoverable on callback).
    import secrets as _secrets
    code_verifier = _secrets.token_urlsafe(48)  # 64 chars, within RFC 7636 limits
    flow.code_verifier = code_verifier

    state = sign_state(
        user_id, signing_key, ttl_seconds=ttl_seconds,
        code_verifier=code_verifier,
    )
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
    ok, user_id, err, code_verifier = verify_state(state, signing_key, nonce_store)
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
    # Restore the PKCE verifier from server-side store so fetch_token
    # can pass it to Google. If verifier is None here, either PKCE
    # wasn't used at sign time OR the server restarted between sign
    # and verify — fetch_token will fail loudly with "invalid_grant:
    # Missing code verifier" in that case, which is the right
    # behavior (user just retries the auth flow).
    if code_verifier:
        flow.code_verifier = code_verifier

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
