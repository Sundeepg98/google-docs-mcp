"""Per-request credential resolver tests.

Critical regression guards for the v1.1 multi-tenant story:
- Missing creds → NeedsReauthError with usable auth_url (not bare error)
- Valid creds → returned as-is, no Google API call wasted
- Expired creds → refresh, persist, return
- Refresh raises invalid_grant → clear stored creds, raise NeedsReauthError
- Refresh raises other errors → propagate (don't swallow network blips)
- Concurrent refresh for same user → serialized; only ONE refresh
- Concurrent refresh for different users → parallel; no false serialization
- Missing required_scopes → NeedsReauthError with expanded-scope URL
- Operator client_secret NOT readable from stored JSON
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# isolated_db fixture is auto-applied from tests/conftest.py (R23 B3
# consolidation, v2.0.5). The canonical version now resets
# _per_user_locks both pre- and post-yield — same guarantee the
# previously-local reset_lock_registry fixture provided, plus the
# additional _initialized_paths / _shim_hit_counter / _creds_cache
# resets the other ex-locals contributed.


@pytest.fixture
def client_config():
    return {
        "web": {
            "client_id": "OPERATOR_CLIENT_ID.apps.googleusercontent.com",
            "client_secret": "OPERATOR_SECRET",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["https://example.fly.dev/oauth/google/api/callback"],
        },
    }


@pytest.fixture
def runtime_oauth(client_config):
    """Bundle the env-derived OAuth config the resolver needs."""
    return {
        "client_config": client_config,
        # v2.0b: get_credentials_for_user takes signing_key as bytes
        # (matches keys.get_key("oauth_state") return type).
        "signing_key": b"test-signing-key",
        "base_url": "https://example.fly.dev",
    }


def _seed_creds(user_id: str, *, scopes=None, expired=False, with_refresh=True) -> None:
    """Drop a synthetic creds row into user_store. Tests use this to
    simulate 'user previously authorized'."""
    from appscriptly import user_store

    if expired:
        expiry = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    else:
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    payload = {
        "token": "STORED_ACCESS_TOKEN",
        "refresh_token": "STORED_REFRESH_TOKEN" if with_refresh else None,
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes or [
            "openid",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
        ],
        "expiry": expiry,
    }
    user_store.save_state(user_id, {"google_creds_json": json.dumps(payload)})


# ---------------------------------------------------------------
# Missing creds path
# ---------------------------------------------------------------


def test_no_creds_raises_NeedsReauthError_with_auth_url(runtime_oauth):
    from appscriptly.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )

    with pytest.raises(NeedsReauthError) as exc:
        get_credentials_for_user("user-new", **runtime_oauth)

    assert exc.value.user_id == "user-new"
    assert exc.value.auth_url.startswith("https://accounts.google.com/")
    assert "not yet authorized" in exc.value.reason


# ---------------------------------------------------------------
# Valid creds path
# ---------------------------------------------------------------


def test_valid_creds_returned_without_refresh(runtime_oauth):
    """Don't waste a refresh call when the cached token is still good."""
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds("user-valid", expired=False)

    with patch(
        "appscriptly.credentials.Credentials.refresh"
    ) as refresh_mock:
        creds = get_credentials_for_user("user-valid", **runtime_oauth)

    refresh_mock.assert_not_called()
    assert creds.token == "STORED_ACCESS_TOKEN"


# ---------------------------------------------------------------
# Refresh path
# ---------------------------------------------------------------


def test_expired_creds_refreshed_and_persisted(runtime_oauth):
    """Refresh updates user_store with the new token + expiry."""
    from appscriptly import user_store
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds("user-exp", expired=True)

    def fake_refresh(self, _request):
        self.token = "REFRESHED_TOKEN"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    with patch.object(
        __import__("google.oauth2.credentials", fromlist=["Credentials"]).Credentials,
        "refresh", fake_refresh,
    ):
        creds = get_credentials_for_user("user-exp", **runtime_oauth)

    assert creds.token == "REFRESHED_TOKEN"
    # Persisted change visible to next reader.
    stored = json.loads(user_store.get_state("user-exp")["google_creds_json"])
    assert stored["token"] == "REFRESHED_TOKEN"


def test_expired_with_no_refresh_token_raises_NeedsReauthError(runtime_oauth):
    from appscriptly.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )

    _seed_creds("user-stale", expired=True, with_refresh=False)

    with pytest.raises(NeedsReauthError, match="no refresh token"):
        get_credentials_for_user("user-stale", **runtime_oauth)


# ---------------------------------------------------------------
# Revocation handling
# ---------------------------------------------------------------


def test_invalid_grant_clears_state_and_raises_NeedsReauth(runtime_oauth):
    """User revoked access (or refresh_token rotated out-of-band) —
    drop the bad creds and tell the tool to re-elicit auth."""
    from google.auth.exceptions import RefreshError

    from appscriptly import user_store
    from appscriptly.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )

    _seed_creds("user-revoked", expired=True)

    with patch.object(
        __import__("google.oauth2.credentials", fromlist=["Credentials"]).Credentials,
        "refresh",
        side_effect=RefreshError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant'})"
        ),
    ), pytest.raises(NeedsReauthError, match="revoked or rotated"):
        get_credentials_for_user("user-revoked", **runtime_oauth)

    # Stored creds were cleared so the next call lands in the
    # "first-time user" branch, not stuck in the revoked branch.
    assert user_store.get_state("user-revoked") == {}


def test_unknown_refresh_error_propagates(runtime_oauth):
    """Don't swallow network blips / 5xx as 'needs re-auth' —
    that'd retrain users to spam the re-auth flow."""
    from google.auth.exceptions import RefreshError

    from appscriptly import user_store
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds("user-blip", expired=True)

    with patch.object(
        __import__("google.oauth2.credentials", fromlist=["Credentials"]).Credentials,
        "refresh",
        side_effect=RefreshError("connection reset by peer", None),
    ), pytest.raises(RefreshError, match="connection reset"):
        get_credentials_for_user("user-blip", **runtime_oauth)

    # Stored creds are NOT cleared — this isn't a revocation.
    assert "google_creds_json" in user_store.get_state("user-blip")


# ---------------------------------------------------------------
# Scope check
# ---------------------------------------------------------------


def test_missing_required_scope_raises_NeedsReauth(runtime_oauth):
    from appscriptly.credentials import (
        NeedsReauthError, get_credentials_for_user,
    )

    _seed_creds(
        "user-narrow",
        expired=False,
        scopes=["openid", "https://www.googleapis.com/auth/documents"],
    )

    with pytest.raises(NeedsReauthError, match="Missing required scopes"):
        get_credentials_for_user(
            "user-narrow",
            required_scopes=["https://www.googleapis.com/auth/script.projects"],
            **runtime_oauth,
        )


def test_all_required_scopes_present_returns_creds(runtime_oauth):
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds(
        "user-broad",
        expired=False,
        scopes=[
            "openid",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/script.projects",
        ],
    )

    creds = get_credentials_for_user(
        "user-broad",
        required_scopes=["https://www.googleapis.com/auth/script.projects"],
        **runtime_oauth,
    )
    assert creds.token == "STORED_ACCESS_TOKEN"


# ---------------------------------------------------------------
# Concurrent refresh — the headline race
# ---------------------------------------------------------------


def test_concurrent_refresh_same_user_serialized(runtime_oauth):
    """The killer race: N parallel tool calls for one user must trigger
    exactly ONE Google refresh, not N. Without the per-user lock,
    Google rotates refresh_tokens between calls and one parallel
    branch ends up with an invalidated refresh_token."""
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds("user-concurrent", expired=True)

    refresh_call_count = [0]

    def slow_refresh(self, _request):
        # Hold long enough to overlap with the other threads.
        time.sleep(0.05)
        refresh_call_count[0] += 1
        self.token = f"REFRESHED_{refresh_call_count[0]}"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    threads = []
    results: list = []

    def worker():
        with patch.object(
            __import__("google.oauth2.credentials", fromlist=["Credentials"]).Credentials,
            "refresh", slow_refresh,
        ):
            results.append(get_credentials_for_user("user-concurrent", **runtime_oauth))

    for _ in range(5):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert refresh_call_count[0] == 1, (
        f"expected 1 refresh, got {refresh_call_count[0]} — "
        "per-user lock is not serializing"
    )
    # All callers see the same refreshed token.
    assert all(c.token == "REFRESHED_1" for c in results)


def test_concurrent_refresh_different_users_parallel(runtime_oauth):
    """Lock per user, not global — A's refresh must not block B."""
    from appscriptly.credentials import get_credentials_for_user

    _seed_creds("user-A", expired=True)
    _seed_creds("user-B", expired=True)

    started_at: dict[str, float] = {}
    finished_at: dict[str, float] = {}
    inside_refresh = threading.Event()

    def slow_refresh(self, _request):
        # We can't easily tell which user this is from inside refresh,
        # so just sleep and record per-thread.
        inside_refresh.set()
        time.sleep(0.1)
        self.token = "REFRESHED"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    def worker(uid: str):
        started_at[uid] = time.monotonic()
        with patch.object(
            __import__("google.oauth2.credentials", fromlist=["Credentials"]).Credentials,
            "refresh", slow_refresh,
        ):
            get_credentials_for_user(uid, **runtime_oauth)
        finished_at[uid] = time.monotonic()

    t_a = threading.Thread(target=worker, args=("user-A",))
    t_b = threading.Thread(target=worker, args=("user-B",))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    # If locks were global, the second worker's start would be delayed
    # past the first's finish. With per-user locks, they overlap.
    a_dur = finished_at["user-A"] - started_at["user-A"]
    b_dur = finished_at["user-B"] - started_at["user-B"]
    total_wall = max(finished_at.values()) - min(started_at.values())
    assert total_wall < a_dur + b_dur - 0.05, (
        f"different-user refreshes ran serially "
        f"(wall={total_wall:.2f}s, sum={a_dur + b_dur:.2f}s) — "
        "lock is global, not per-user"
    )


# ---------------------------------------------------------------
# Operator secret confidentiality
# ---------------------------------------------------------------


def test_operator_client_secret_not_persisted_via_save_credentials_json(client_config):
    """save_credentials_json must strip client_id + client_secret before
    save, so a user_state.db leak doesn't hand the operator OAuth secret
    to an attacker."""
    from appscriptly import user_store
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token="ACCESS",
        refresh_token="REFRESH",
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_config["web"]["client_id"],
        client_secret=client_config["web"]["client_secret"],
        scopes=["openid"],
    )
    user_store.save_credentials_json("user-X", creds.to_json())

    stored_raw = json.loads(user_store.get_state("user-X")["google_creds_json"])
    assert "client_id" not in stored_raw, "client_id leaked into per-user storage"
    assert "client_secret" not in stored_raw, "client_secret leaked into per-user storage"
    assert stored_raw["token"] == "ACCESS"
    assert stored_raw["refresh_token"] == "REFRESH"
