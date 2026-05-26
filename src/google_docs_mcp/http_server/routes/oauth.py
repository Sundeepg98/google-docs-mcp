"""``GET /oauth/google/api/callback`` — per-user Google OAuth callback handler."""
from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import Response

from ... import keys, user_store
from ...oauth_google import (
    OAuthCallbackError,
    exchange_code_for_credentials,
)
from .. import _state  # late-bound access to _state._NONCE_STORE so test
                       # reassignments propagate (tests reset between cases)
from .._helpers import _resolve_base_url, _resolve_client_config
from .._pages import _error_page, _success_page

log = logging.getLogger("google_docs_mcp.http")


async def oauth_google_api_callback(request: Request) -> Response:
    """``GET /oauth/google/api/callback?code=...&state=...``

    Final leg of the per-user Google OAuth dance. Verifies the
    HMAC-signed state, exchanges the auth code for tokens, persists
    them to ``user_store`` keyed by Google ``sub``. Returns a simple
    HTML page the user sees in their browser.
    """
    qp = request.query_params

    # Google sends ?error=access_denied if the user clicked Cancel on
    # the consent screen. Surface that cleanly instead of trying to
    # exchange a nonexistent code.
    if "error" in qp:
        log.info("oauth: user cancelled consent (%s)", qp["error"])
        return _error_page(
            f"You declined the authorization (Google said: {qp['error']}). "
            "Re-run the tool in your chat to try again.",
            status_code=400,
        )

    if "code" not in qp or "state" not in qp:
        return _error_page(
            "Missing 'code' or 'state' in callback URL. This usually "
            "means Google did not complete the authorization.",
            status_code=400,
        )

    # v2.6 (#48): purpose-routed via keys.get_key("oauth_state") so the
    # v2.0b strict-flip activates HKDF-derivation for OAuth state HMACs
    # without further edits here. get_key raises RuntimeError on missing
    # MCP_BEARER_TOKEN; preserve the prior fail-closed behavior.
    # v2.0b: keys.get_key() returns bytes — pass through to verify_state
    # directly; the prior .decode("utf-8") round-trip crashed on HKDF
    # output (~99.96% of 32 random bytes aren't valid UTF-8).
    try:
        signing_key = keys.get_key("oauth_state")
    except RuntimeError:
        log.error("oauth: MCP_BEARER_TOKEN unset; cannot verify state")
        return _error_page(
            "Server configuration error. Contact the operator.",
            status_code=500,
        )

    try:
        client_config = _resolve_client_config()
    except (RuntimeError, ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        log.error("oauth: client_config load failed: %s", e)
        return _error_page(
            "Server configuration error (OAuth client not configured). "
            "Contact the operator.",
            status_code=500,
        )

    base_url = _resolve_base_url(request)

    # Fly terminates TLS at the edge; inside the container the proxied
    # request shows scheme=http even though the public URL is HTTPS.
    # oauthlib's Flow.fetch_token validates the authorization_response
    # URL and rejects any http://, raising InsecureTransportError. Since
    # we KNOW we're behind Fly's HTTPS edge (base_url begins with
    # https://), rewrite the scheme on the URL we hand to oauthlib. Do
    # NOT set OAUTHLIB_INSECURE_TRANSPORT=1 — that disables transport
    # security checks globally; we only want to lie about THIS one URL.
    authorization_response_url = str(request.url)
    if base_url.startswith("https://") and authorization_response_url.startswith("http://"):
        authorization_response_url = "https://" + authorization_response_url[len("http://"):]

    try:
        user_id, creds_json = exchange_code_for_credentials(
            state=qp["state"],
            authorization_response_url=authorization_response_url,
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=_state._NONCE_STORE,
        )
    except OAuthCallbackError as e:
        log.warning("oauth: callback rejected: %s", e)
        return _error_page(str(e), status_code=e.status_code)

    try:
        # MUST be save_credentials_json (not save_state) — the wrapper
        # strips the operator's OAuth client_id + client_secret from the
        # Credentials.to_json() output before persisting. Calling
        # save_state directly here would leak those operator secrets
        # into every per-user row in user_state.db. The matching
        # regression guard is
        # test_oauth_callback_endpoint_strips_operator_secrets_in_production
        # in tests/integration/test_fresh_user_flow.py.
        user_store.save_credentials_json(user_id, creds_json)
    except Exception as e:  # noqa: BLE001 — last line of defence
        log.exception("oauth: user_store.save_credentials_json failed for %s", user_id)
        return _error_page(
            f"Failed to persist credentials: {e}. Contact the operator.",
            status_code=500,
        )

    log.info("oauth: persisted Google API creds for user %s", user_id)
    return _success_page()
