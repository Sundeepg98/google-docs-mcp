"""Google OAuth flow tests — pure logic, no network.

State-handling and Flow construction are tested; the actual code
exchange with Google (Flow.fetch_token) is mocked because it's an
HTTP call to oauth2.googleapis.com that we don't want in CI.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.fixture
def client_config():
    """Minimal valid client_secrets.json shape for Flow.from_client_config."""
    return {
        "web": {
            "client_id": "TEST_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "TEST_CLIENT_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["https://example.fly.dev/oauth/google/api/callback"],
        },
    }


@pytest.fixture
def signing_key():
    return "test-signing-key-not-for-prod"


@pytest.fixture
def base_url():
    return "https://example.fly.dev"


@pytest.fixture
def fresh_nonce_store():
    from google_docs_mcp.crypto import NonceStore
    return NonceStore()


# ---------------------------------------------------------------
# load_client_config
# ---------------------------------------------------------------


def test_load_client_config_accepts_web_shape(tmp_path, client_config):
    from google_docs_mcp.oauth_google import load_client_config

    p = tmp_path / "client_secrets.json"
    p.write_text(json.dumps(client_config))
    loaded = load_client_config(p)
    assert loaded["web"]["client_id"] == "TEST_CLIENT_ID.apps.googleusercontent.com"


def test_load_client_config_accepts_installed_shape(tmp_path):
    from google_docs_mcp.oauth_google import load_client_config
    p = tmp_path / "client_secrets.json"
    p.write_text(json.dumps({"installed": {"client_id": "X", "client_secret": "Y"}}))
    loaded = load_client_config(p)
    assert "installed" in loaded


def test_load_client_config_rejects_garbage(tmp_path):
    from google_docs_mcp.oauth_google import load_client_config
    p = tmp_path / "garbage.json"
    p.write_text(json.dumps({"random": "junk"}))
    with pytest.raises(ValueError, match="doesn't look like"):
        load_client_config(p)


# ---------------------------------------------------------------
# build_authorization_url
# ---------------------------------------------------------------


def test_build_authorization_url_contains_signed_state(
    client_config, signing_key, base_url
):
    from google_docs_mcp.oauth_google import build_authorization_url

    url = build_authorization_url(
        "user-sub-1",
        base_url=base_url,
        client_config=client_config,
        signing_key=signing_key,
    )

    qs = parse_qs(urlparse(url).query)
    assert "state" in qs
    state = qs["state"][0]
    # Signed state shape: sub_b64.nonce.exp.sig — 4 dot-separated parts
    assert state.count(".") == 3


def test_build_authorization_url_uses_correct_redirect(
    client_config, signing_key, base_url
):
    from google_docs_mcp.oauth_google import (
        CALLBACK_PATH, build_authorization_url,
    )

    url = build_authorization_url(
        "user-1",
        base_url=base_url,
        client_config=client_config,
        signing_key=signing_key,
    )

    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"][0] == f"{base_url}{CALLBACK_PATH}"


def test_build_authorization_url_requests_offline_access_and_consent_prompt(
    client_config, signing_key, base_url
):
    """Without these flags, Google may omit refresh_token on re-auth —
    breaking our long-lived background refresh story."""
    from google_docs_mcp.oauth_google import build_authorization_url

    url = build_authorization_url(
        "user-1",
        base_url=base_url,
        client_config=client_config,
        signing_key=signing_key,
    )

    qs = parse_qs(urlparse(url).query)
    assert qs["access_type"][0] == "offline"
    assert qs["prompt"][0] == "consent"


def test_build_authorization_url_includes_all_default_scopes(
    client_config, signing_key, base_url
):
    from google_docs_mcp.oauth_google import (
        GOOGLE_API_SCOPES, build_authorization_url,
    )

    url = build_authorization_url(
        "u", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )

    scope_str = parse_qs(urlparse(url).query)["scope"][0]
    for scope in GOOGLE_API_SCOPES:
        assert scope in scope_str


def test_build_authorization_url_rejects_empty_user_id(
    client_config, signing_key, base_url
):
    from google_docs_mcp.oauth_google import build_authorization_url
    with pytest.raises(ValueError, match="user_id is required"):
        build_authorization_url(
            "", base_url=base_url, client_config=client_config,
            signing_key=signing_key,
        )


# ---------------------------------------------------------------
# exchange_code_for_credentials
# ---------------------------------------------------------------


def _mock_flow_with_creds(
    *,
    refresh_token: str | None = "REFRESH_TOKEN",
    access_token: str = "ACCESS_TOKEN",
) -> MagicMock:
    """Build a MagicMock standing in for google_auth_oauthlib's Flow."""
    flow = MagicMock()
    creds = MagicMock()
    creds.refresh_token = refresh_token
    creds.to_json.return_value = json.dumps(
        {"token": access_token, "refresh_token": refresh_token or ""}
    )
    flow.credentials = creds
    return flow


def test_exchange_code_returns_user_id_and_creds_json(
    client_config, signing_key, base_url, fresh_nonce_store
):
    from google_docs_mcp.oauth_google import (
        build_authorization_url, exchange_code_for_credentials,
    )

    auth_url = build_authorization_url(
        "user-sub-xyz", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_with_creds()
        user_id, creds_json = exchange_code_for_credentials(
            state=state,
            authorization_response_url=f"{base_url}/oauth/google/api/callback"
                f"?state={state}&code=FAKE_CODE",
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=fresh_nonce_store,
        )

    assert user_id == "user-sub-xyz"
    assert json.loads(creds_json)["token"] == "ACCESS_TOKEN"


def test_exchange_code_rejects_bad_state(
    client_config, signing_key, base_url, fresh_nonce_store
):
    from google_docs_mcp.oauth_google import (
        OAuthCallbackError, exchange_code_for_credentials,
    )

    with pytest.raises(OAuthCallbackError, match="state could not be validated"):
        exchange_code_for_credentials(
            state="bogus.state.token.sig",
            authorization_response_url=f"{base_url}/cb?code=X",
            base_url=base_url,
            client_config=client_config,
            signing_key=signing_key,
            nonce_store=fresh_nonce_store,
        )


def test_exchange_code_rejects_replayed_state(
    client_config, signing_key, base_url, fresh_nonce_store
):
    """A state token consumed once must not work a second time."""
    from google_docs_mcp.oauth_google import (
        OAuthCallbackError, build_authorization_url,
        exchange_code_for_credentials,
    )

    auth_url = build_authorization_url(
        "user-1", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_with_creds()
        # First redemption: succeeds.
        exchange_code_for_credentials(
            state=state,
            authorization_response_url=f"{base_url}/cb?state={state}&code=X",
            base_url=base_url, client_config=client_config,
            signing_key=signing_key, nonce_store=fresh_nonce_store,
        )
        # Second redemption: must fail.
        with pytest.raises(OAuthCallbackError, match="state could not be validated"):
            exchange_code_for_credentials(
                state=state,
                authorization_response_url=f"{base_url}/cb?state={state}&code=X",
                base_url=base_url, client_config=client_config,
                signing_key=signing_key, nonce_store=fresh_nonce_store,
            )


def test_exchange_code_rejects_creds_without_refresh_token(
    client_config, signing_key, base_url, fresh_nonce_store
):
    """If Google returns access_token but no refresh_token, fail loudly
    rather than silently saving short-lived creds that'll break in 1h."""
    from google_docs_mcp.oauth_google import (
        OAuthCallbackError, build_authorization_url,
        exchange_code_for_credentials,
    )

    auth_url = build_authorization_url(
        "user-1", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        mk_flow.return_value = _mock_flow_with_creds(refresh_token=None)
        with pytest.raises(OAuthCallbackError, match="without a refresh_token"):
            exchange_code_for_credentials(
                state=state,
                authorization_response_url=f"{base_url}/cb?state={state}&code=X",
                base_url=base_url, client_config=client_config,
                signing_key=signing_key, nonce_store=fresh_nonce_store,
            )


def test_exchange_code_wraps_fetch_token_errors_with_502(
    client_config, signing_key, base_url, fresh_nonce_store
):
    """If google-auth-oauthlib raises (network blip, invalid code, etc.)
    we want a clean OAuthCallbackError, not a 500 leaking internals."""
    from google_docs_mcp.oauth_google import (
        OAuthCallbackError, build_authorization_url,
        exchange_code_for_credentials,
    )

    auth_url = build_authorization_url(
        "user-1", base_url=base_url, client_config=client_config,
        signing_key=signing_key,
    )
    state = parse_qs(urlparse(auth_url).query)["state"][0]

    with patch(
        "google_docs_mcp.oauth_google.Flow.from_client_config"
    ) as mk_flow:
        flow_mock = MagicMock()
        flow_mock.fetch_token.side_effect = RuntimeError("network blip")
        mk_flow.return_value = flow_mock
        with pytest.raises(OAuthCallbackError, match="Failed to exchange"):
            exchange_code_for_credentials(
                state=state,
                authorization_response_url=f"{base_url}/cb?state={state}&code=X",
                base_url=base_url, client_config=client_config,
                signing_key=signing_key, nonce_store=fresh_nonce_store,
            )
