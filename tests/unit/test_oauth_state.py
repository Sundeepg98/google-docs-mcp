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
    from appscriptly.crypto import NonceStore
    return NonceStore()


@pytest.fixture
def signing_key():
    # v2.0b: oauth_state.sign_state / verify_state take bytes (matches
    # keys.get_key("oauth_state") return type post-strict-flip).
    return b"test-signing-key-do-not-use-in-prod"


def test_sign_then_verify_roundtrip(signing_key, fresh_nonce_store):
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-abc", signing_key)
    ok, user_id, err, _verifier = verify_state(state, signing_key, fresh_nonce_store)

    assert ok is True
    assert user_id == "user-sub-abc"
    assert err is None


def test_state_is_single_use_replay_rejected(signing_key, fresh_nonce_store):
    """The killer guard against the CSRF attack described in the module
    docstring — a leaked state cannot be redeemed twice."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-def", signing_key)

    ok1, _, _, _ = verify_state(state, signing_key, fresh_nonce_store)
    ok2, _, err2, _ = verify_state(state, signing_key, fresh_nonce_store)

    assert ok1 is True
    assert ok2 is False
    assert err2 == "state already used"


def test_tampered_sig_rejected(signing_key, fresh_nonce_store):
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("victim-sub", signing_key)
    sub_b64, nonce, exp, _sig = state.split(".")
    tampered = f"{sub_b64}.{nonce}.{exp}.deadbeef" + "0" * 56

    ok, user_id, err, _ = verify_state(tampered, signing_key, fresh_nonce_store)
    assert ok is False
    assert user_id is None
    assert err == "signature mismatch"


def test_tampered_sub_rejected(signing_key, fresh_nonce_store):
    """If an attacker swaps the sub field hoping to redirect creds to a
    victim, the HMAC over (sub, nonce, exp) breaks."""
    from appscriptly.oauth_state import sign_state, verify_state
    import base64

    state = sign_state("attacker-sub", signing_key)
    _, nonce, exp, sig = state.split(".")
    victim_b64 = base64.urlsafe_b64encode(b"victim-sub").decode("ascii").rstrip("=")
    swapped = f"{victim_b64}.{nonce}.{exp}.{sig}"

    ok, user_id, err, _ = verify_state(swapped, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "signature mismatch"


def test_expired_state_rejected(signing_key, fresh_nonce_store):
    """An expired state cannot be redeemed even if signature is valid."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-ghi", signing_key, ttl_seconds=1)
    time.sleep(1.1)

    ok, _, err, _ = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "state has expired"


def test_wrong_signing_key_rejected(signing_key, fresh_nonce_store):
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-sub-jkl", signing_key)
    ok, _, err, _ = verify_state(state, b"different-key", fresh_nonce_store)
    assert ok is False
    assert err == "signature mismatch"


def test_malformed_state_rejected(signing_key, fresh_nonce_store):
    from appscriptly.oauth_state import verify_state

    for bad in ("", "abc", "a.b.c", "a.b.c.d.e", "a.b.notanint.sig"):
        ok, _, err, _ = verify_state(bad, signing_key, fresh_nonce_store)
        assert ok is False, f"expected rejection for {bad!r}"
        assert err is not None


def test_empty_user_id_rejected_at_sign_time():
    from appscriptly.oauth_state import sign_state
    with pytest.raises(ValueError, match="user_id is required"):
        sign_state("", "key")


def test_ttl_validation():
    from appscriptly.oauth_state import sign_state
    with pytest.raises(ValueError, match="ttl_seconds"):
        sign_state("user", "key", ttl_seconds=0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        sign_state("user", "key", ttl_seconds=10_000)  # > MAX_TTL_SECONDS


def test_different_users_get_different_states(signing_key):
    """Same TTL, different users → different state tokens."""
    from appscriptly.oauth_state import sign_state

    a = sign_state("alice", signing_key)
    b = sign_state("bob", signing_key)
    assert a != b


def test_same_user_repeated_calls_get_different_states(signing_key):
    """Each sign call generates a fresh nonce — replay-protection requires
    each state to be globally unique even for the same user."""
    from appscriptly.oauth_state import sign_state

    s1 = sign_state("user", signing_key)
    s2 = sign_state("user", signing_key)
    assert s1 != s2


def test_unicode_user_id_roundtrips(signing_key, fresh_nonce_store):
    """sub claims are typically ASCII digits, but be safe — base64 the bytes."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-ünıcödé-123", signing_key)
    ok, user_id, _, _ = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is True
    assert user_id == "user-ünıcödé-123"


# ---------------------------------------------------------------
# PKCE code_verifier store lifecycle (R2 audit Gap #7)
#
# sign_state(code_verifier=...) stashes the PKCE verifier in the
# module-level _pending_verifiers dict keyed by nonce; verify_state
# pops it atomically and returns it as the 4th tuple element so the
# OAuth callback can pass it to Flow.fetch_token. Every existing
# oauth_state test passes NO code_verifier (they all discard the 4th
# element), so the ENTIRE PKCE round-trip — store on sign, return on
# verify, the _evict_expired_verifiers cleanup, and eviction-after-TTL
# — is unverified.
#
# PKCE is the protection against authorization-code interception. A
# regression that failed to return the stored verifier, returned the
# wrong one, or evicted it prematurely would break the OAuth callback
# (Flow.fetch_token fails) or, worse, silently complete WITHOUT PKCE.
#
# NOTE: _pending_verifiers is a module-level dict that the shared
# conftest isolated_db fixture does NOT reset (it only knows about the
# user_store/credentials/keys/_tool_helpers state). These tests reset
# it explicitly so a stored verifier can't leak between tests and so a
# parallel run (-n auto) can't race on it.
# ---------------------------------------------------------------


@pytest.fixture
def clean_pkce_store():
    """Reset the module-level PKCE verifier store pre- and post-test."""
    from appscriptly import oauth_state

    with oauth_state._pending_lock:
        oauth_state._pending_verifiers.clear()
    yield oauth_state
    with oauth_state._pending_lock:
        oauth_state._pending_verifiers.clear()


def test_sign_with_verifier_returns_it_on_verify(
    signing_key, fresh_nonce_store, clean_pkce_store
):
    """sign_state(code_verifier=V) -> verify_state returns V as the 4th
    tuple element. This is the PKCE round-trip the OAuth callback relies
    on to call Flow.fetch_token(code_verifier=...)."""
    sign_state = clean_pkce_store.sign_state
    verify_state = clean_pkce_store.verify_state

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    state = sign_state("user-pkce", signing_key, code_verifier=verifier)

    ok, user_id, err, code_verifier = verify_state(
        state, signing_key, fresh_nonce_store
    )
    assert ok is True
    assert user_id == "user-pkce"
    assert err is None
    assert code_verifier == verifier, (
        "verify_state must return the exact code_verifier stored at "
        "sign time; PKCE token exchange fails otherwise."
    )


def test_verifier_is_popped_after_first_verify(
    signing_key, fresh_nonce_store, clean_pkce_store
):
    """The verifier is single-use: stored under the nonce on sign,
    POPPED on the first verify. A second verify (already rejected by the
    nonce-store as replay) must also return code_verifier=None — the
    entry is gone from _pending_verifiers, not lingering for reuse."""
    sign_state = clean_pkce_store.sign_state
    verify_state = clean_pkce_store.verify_state

    state = sign_state("user-pop", signing_key, code_verifier="verifier-xyz")

    ok1, _, _, v1 = verify_state(state, signing_key, fresh_nonce_store)
    ok2, _, err2, v2 = verify_state(state, signing_key, fresh_nonce_store)

    assert ok1 is True
    assert v1 == "verifier-xyz"
    # Second redemption: replay-rejected AND no verifier handed back.
    assert ok2 is False
    assert err2 == "state already used"
    assert v2 is None
    # The store no longer holds the entry.
    assert clean_pkce_store._pending_verifiers == {}


def test_sign_without_verifier_yields_none_on_verify(
    signing_key, fresh_nonce_store, clean_pkce_store
):
    """The no-PKCE path: sign WITHOUT a verifier -> verify returns None
    as the 4th element and the store stays empty (we never stash a
    None)."""
    sign_state = clean_pkce_store.sign_state
    verify_state = clean_pkce_store.verify_state

    state = sign_state("user-nopkce", signing_key)
    assert clean_pkce_store._pending_verifiers == {}, (
        "signing without a code_verifier must not create a store entry."
    )

    ok, _, _, code_verifier = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is True
    assert code_verifier is None


def test_expired_verifier_entry_is_evicted_on_next_sign(
    signing_key, fresh_nonce_store, clean_pkce_store
):
    """_evict_expired_verifiers (called at the start of every
    sign_state that carries a verifier) must drop entries whose exp has
    passed. We mint a verifier-bearing state with a 1s TTL, let it
    expire, then mint a SECOND verifier-bearing state — the second
    sign's eviction sweep must remove the first (expired) nonce, so the
    store ends up holding ONLY the live second entry. Without eviction
    the store would grow unboundedly with dead verifiers (a slow leak of
    secret material)."""
    import time

    sign_state = clean_pkce_store.sign_state

    expired_state = sign_state(
        "user-expire", signing_key, ttl_seconds=1, code_verifier="dead-verifier"
    )
    expired_nonce = expired_state.split(".")[1]
    assert expired_nonce in clean_pkce_store._pending_verifiers

    time.sleep(1.1)  # let the first entry's exp pass

    # A second verifier-bearing sign triggers _evict_expired_verifiers.
    live_state = sign_state(
        "user-live", signing_key, ttl_seconds=600, code_verifier="live-verifier"
    )
    live_nonce = live_state.split(".")[1]

    store = clean_pkce_store._pending_verifiers
    assert expired_nonce not in store, (
        "expired verifier entry was not evicted on the next sign — "
        "_pending_verifiers leaks dead PKCE secrets over time."
    )
    assert live_nonce in store
    assert store[live_nonce][0] == "live-verifier"


def test_expired_state_does_not_return_verifier(
    signing_key, fresh_nonce_store, clean_pkce_store
):
    """An expired state is rejected at the expiry check, BEFORE the
    verifier pop — so verify returns code_verifier=None even though an
    entry was stored at sign time. (Confirms the verifier is never
    leaked via an expired-but-otherwise-parsable state.)"""
    import time

    sign_state = clean_pkce_store.sign_state
    verify_state = clean_pkce_store.verify_state

    state = sign_state(
        "user-exp-pkce", signing_key, ttl_seconds=1, code_verifier="v-exp"
    )
    time.sleep(1.1)

    ok, _, err, code_verifier = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "state has expired"
    assert code_verifier is None
