"""OAuth state-param signing / verification tests.

Guards the CSRF + replay mitigations documented in oauth_state.py:
- tamper detection (attacker swapping sub → signature breaks)
- expiry enforcement (old state rejected)
- single-use semantics (replay rejected via NonceStore)
- malformed input handling (no crashes, clean error returns)
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_nonce_store():
    from google_docs_mcp.crypto import NonceStore
    return NonceStore()


@pytest.fixture
def signing_key():
    return "test-signing-key-do-not-use-in-prod"


def test_sign_then_verify_roundtrip(signing_key, fresh_nonce_store):
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-abc", signing_key)
    ok, user_id, err = verify_state(state, signing_key, fresh_nonce_store)

    assert ok is True
    assert user_id == "user-sub-abc"
    assert err is None


def test_state_is_single_use_replay_rejected(signing_key, fresh_nonce_store):
    """The killer guard against the CSRF attack described in the module
    docstring — a leaked state cannot be redeemed twice."""
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-def", signing_key)

    ok1, _, _ = verify_state(state, signing_key, fresh_nonce_store)
    ok2, _, err2 = verify_state(state, signing_key, fresh_nonce_store)

    assert ok1 is True
    assert ok2 is False
    assert err2 == "state already used"


def test_tampered_sig_rejected(signing_key, fresh_nonce_store):
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("victim-sub", signing_key)
    sub_b64, nonce, exp, _sig = state.split(".")
    tampered = f"{sub_b64}.{nonce}.{exp}.deadbeef" + "0" * 56

    ok, user_id, err = verify_state(tampered, signing_key, fresh_nonce_store)
    assert ok is False
    assert user_id is None
    assert err == "signature mismatch"


def test_tampered_sub_rejected(signing_key, fresh_nonce_store):
    """If an attacker swaps the sub field hoping to redirect creds to a
    victim, the HMAC over (sub, nonce, exp) breaks."""
    from google_docs_mcp.oauth_state import sign_state, verify_state
    import base64

    state = sign_state("attacker-sub", signing_key)
    _, nonce, exp, sig = state.split(".")
    victim_b64 = base64.urlsafe_b64encode(b"victim-sub").decode("ascii").rstrip("=")
    swapped = f"{victim_b64}.{nonce}.{exp}.{sig}"

    ok, user_id, err = verify_state(swapped, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "signature mismatch"


def test_expired_state_rejected(signing_key, fresh_nonce_store):
    """An expired state cannot be redeemed even if signature is valid."""
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-ghi", signing_key, ttl_seconds=1)
    time.sleep(1.1)

    ok, _, err = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "state has expired"


def test_wrong_signing_key_rejected(signing_key, fresh_nonce_store):
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-jkl", signing_key)
    ok, _, err = verify_state(state, "different-key", fresh_nonce_store)
    assert ok is False
    assert err == "signature mismatch"


def test_malformed_state_rejected(signing_key, fresh_nonce_store):
    from google_docs_mcp.oauth_state import verify_state

    for bad in ("", "abc", "a.b.c", "a.b.c.d.e", "a.b.notanint.sig"):
        ok, _, err = verify_state(bad, signing_key, fresh_nonce_store)
        assert ok is False, f"expected rejection for {bad!r}"
        assert err is not None


def test_empty_user_id_rejected_at_sign_time():
    from google_docs_mcp.oauth_state import sign_state
    with pytest.raises(ValueError, match="user_id is required"):
        sign_state("", "key")


def test_ttl_validation():
    from google_docs_mcp.oauth_state import sign_state
    with pytest.raises(ValueError, match="ttl_seconds"):
        sign_state("user", "key", ttl_seconds=0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        sign_state("user", "key", ttl_seconds=10_000)  # > MAX_TTL_SECONDS


def test_different_users_get_different_states(signing_key):
    """Same TTL, different users → different state tokens."""
    from google_docs_mcp.oauth_state import sign_state

    a = sign_state("alice", signing_key)
    b = sign_state("bob", signing_key)
    assert a != b


def test_same_user_repeated_calls_get_different_states(signing_key):
    """Each sign call generates a fresh nonce — replay-protection requires
    each state to be globally unique even for the same user."""
    from google_docs_mcp.oauth_state import sign_state

    s1 = sign_state("user", signing_key)
    s2 = sign_state("user", signing_key)
    assert s1 != s2


def test_unicode_user_id_roundtrips(signing_key, fresh_nonce_store):
    """sub claims are typically ASCII digits, but be safe — base64 the bytes."""
    from google_docs_mcp.oauth_state import sign_state, verify_state

    state = sign_state("user-ünıcödé-123", signing_key)
    ok, user_id, _ = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is True
    assert user_id == "user-ünıcödé-123"
