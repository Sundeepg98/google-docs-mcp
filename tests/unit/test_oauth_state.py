"""OAuth state-param signing / verification tests.

Guards the CSRF + replay mitigations documented in oauth_state.py:
- tamper detection (attacker swapping sub → signature breaks)
- expiry enforcement (old state rejected)
- single-use semantics (replay rejected via NonceStore)
- malformed input handling (no crashes, clean error returns)
- stateless encrypted PKCE (verifier rides inside the token, encrypted)
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


@pytest.fixture
def enc_key():
    # AES-GCM 256-bit key (matches keys.get_key("oauth_state_enc") length).
    # Encrypts the PKCE code_verifier carried inside the state token.
    return bytes(range(32))


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
# Stateless encrypted PKCE (fix/…-login)
#
# The PKCE code_verifier is AES-GCM-encrypted under enc_key and carried
# INSIDE the state token (5-part sub.nonce.exp.enc.sig), replacing the
# former in-memory _pending_verifiers dict. sign_state embeds it;
# verify_state decrypts it (after HMAC + nonce) and returns it as the
# 4th tuple element so the OAuth callback can pass it to
# Flow.fetch_token. The whole point is that the verifier rides in the
# token, so a callback landing after a restart / on another instance
# still recovers it — the "deploy drops in-flight logins" fix.
#
# The classic HMAC/replay/expiry tests above all sign WITHOUT a
# verifier, so their tokens stay 4-part and byte-format-unchanged — the
# discriminating tests below cover the 5-part encrypted path.
# ---------------------------------------------------------------


def test_sign_with_verifier_roundtrips_encrypted(
    signing_key, fresh_nonce_store, enc_key
):
    """sign_state(code_verifier=V, enc_key=K) -> verify_state returns V.
    The PKCE round-trip the OAuth callback relies on to call
    Flow.fetch_token(code_verifier=...)."""
    from appscriptly.oauth_state import sign_state, verify_state

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    state = sign_state(
        "user-pkce", signing_key, code_verifier=verifier, enc_key=enc_key,
    )
    ok, user_id, err, recovered = verify_state(
        state, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    assert ok is True
    assert user_id == "user-pkce"
    assert err is None
    assert recovered == verifier, (
        "verify_state must return the exact code_verifier encrypted at "
        "sign time; PKCE token exchange fails otherwise."
    )


def test_pkce_token_is_five_part_no_pkce_is_four_part(signing_key, enc_key):
    """With a verifier the token is 5-part (sub.nonce.exp.enc.sig); without
    one it stays 4-part (sub.nonce.exp.sig) — the byte-format the classic
    HMAC tests depend on."""
    from appscriptly.oauth_state import sign_state

    with_pkce = sign_state(
        "u", signing_key, code_verifier="v-abc", enc_key=enc_key,
    )
    without = sign_state("u", signing_key)
    assert with_pkce.count(".") == 4
    assert without.count(".") == 3


def test_verifier_is_encrypted_not_plaintext_in_token(signing_key, enc_key):
    """The plaintext verifier must NOT appear anywhere in the state — this
    is exactly the objection to plaintext-in-URL the encryption answers.
    An observer of the state sees opaque ciphertext, not the verifier."""
    from appscriptly.oauth_state import sign_state

    verifier = "super-secret-pkce-verifier-value-xyz"
    state = sign_state(
        "u", signing_key, code_verifier=verifier, enc_key=enc_key,
    )
    assert verifier not in state


def test_sign_verifier_without_enc_key_raises(signing_key):
    """A verifier with no enc_key is a misconfiguration — sign_state fails
    loudly rather than silently dropping the verifier (which would brick
    the token exchange)."""
    from appscriptly.oauth_state import sign_state

    with pytest.raises(ValueError, match="enc_key is required"):
        sign_state("u", signing_key, code_verifier="v")


def test_verifier_survives_simulated_restart(signing_key, enc_key):
    """DISCRIMINATING (a): the verifier rides INSIDE the token, so a
    callback that lands after a restart / on a different instance — with a
    brand-new nonce store and NO shared in-memory verifier map — still
    recovers it. On main this FAILS: the verifier lived in the
    process-global ``_pending_verifiers`` dict, which a restart forgets."""
    from appscriptly import oauth_state
    from appscriptly.crypto import NonceStore

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    state = oauth_state.sign_state(
        "user-restart", signing_key, code_verifier=verifier, enc_key=enc_key,
    )
    # The old in-memory verifier store is gone for good in this design.
    assert not hasattr(oauth_state, "_pending_verifiers")

    # "Restart": a fresh process has a fresh (empty) nonce store and no
    # verifier map. Everything needed rides in the token.
    fresh_store = NonceStore()
    ok, user_id, err, recovered = oauth_state.verify_state(
        state, signing_key, fresh_store, enc_key=enc_key,
    )
    assert ok is True
    assert user_id == "user-restart"
    assert err is None
    assert recovered == verifier


def test_encrypted_verifier_single_use_replay(
    signing_key, fresh_nonce_store, enc_key
):
    """The 5-part token is single-use like the 4-part one: first verify
    returns the verifier; a replay is nonce-rejected and hands back no
    verifier."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state(
        "user-pop", signing_key, code_verifier="verifier-xyz", enc_key=enc_key,
    )
    ok1, _, _, v1 = verify_state(
        state, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    ok2, _, err2, v2 = verify_state(
        state, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    assert ok1 is True and v1 == "verifier-xyz"
    assert ok2 is False
    assert err2 == "state already used"
    assert v2 is None


def test_sign_without_verifier_yields_none_on_verify(
    signing_key, fresh_nonce_store
):
    """The no-PKCE path: sign WITHOUT a verifier -> 4-part token -> verify
    returns None as the 4th element."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state("user-nopkce", signing_key)
    ok, _, _, code_verifier = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is True
    assert code_verifier is None


def test_expired_encrypted_state_does_not_decrypt(
    signing_key, fresh_nonce_store, enc_key
):
    """An expired 5-part state is rejected at the expiry check, BEFORE the
    decrypt — so verify returns code_verifier=None and never touches the
    ciphertext."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state(
        "user-exp-pkce", signing_key, ttl_seconds=1,
        code_verifier="v-exp", enc_key=enc_key,
    )
    time.sleep(1.1)

    ok, _, err, code_verifier = verify_state(
        state, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    assert ok is False
    assert err == "state has expired"
    assert code_verifier is None


def test_garbage_ciphertext_clean_auth_failure(
    signing_key, fresh_nonce_store, enc_key
):
    """DISCRIMINATING (b): a garbage ciphertext (with a VALID HMAC, so it
    survives the signature check) decrypts to nothing -> a clean auth
    failure, no traceback. Exercises the AES-GCM InvalidTag path
    specifically (not the HMAC path)."""
    import base64

    from appscriptly.oauth_state import _canonical, _hmac, sign_state, verify_state

    # Start from a real token, then swap in a garbage ciphertext and
    # RE-SIGN the HMAC over it so the signature check passes and we reach
    # decryption.
    state = sign_state(
        "u", signing_key, code_verifier="v", enc_key=enc_key,
    )
    sub_b64, nonce, exp, _enc, _sig = state.split(".")
    garbage = base64.urlsafe_b64encode(b"\x00" * 40).decode("ascii").rstrip("=")
    new_sig = _hmac(signing_key, _canonical(sub_b64, nonce, int(exp), garbage))
    forged = f"{sub_b64}.{nonce}.{exp}.{garbage}.{new_sig}"

    ok, user_id, err, verifier = verify_state(
        forged, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    assert ok is False
    assert user_id is None
    assert err == "verifier decryption failed"
    assert verifier is None


def test_tampered_ciphertext_breaks_hmac(
    signing_key, fresh_nonce_store, enc_key
):
    """The ``enc`` field is authenticated by the outer HMAC too: flipping
    a ciphertext byte WITHOUT re-signing is caught at the signature check
    before any decrypt is attempted."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state(
        "u", signing_key, code_verifier="v", enc_key=enc_key,
    )
    sub_b64, nonce, exp, enc, sig = state.split(".")
    flipped = ("A" if enc[0] != "A" else "B") + enc[1:]
    tampered = f"{sub_b64}.{nonce}.{exp}.{flipped}.{sig}"

    ok, _, err, _ = verify_state(
        tampered, signing_key, fresh_nonce_store, enc_key=enc_key,
    )
    assert ok is False
    assert err == "signature mismatch"


def test_wrong_enc_key_fails_cleanly(signing_key, fresh_nonce_store, enc_key):
    """Decrypting a valid token with the WRONG enc_key fails the AES-GCM
    auth tag -> clean auth failure (no crash). Guards against a silently
    mismatched key rotation."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state(
        "u", signing_key, code_verifier="v", enc_key=enc_key,
    )
    wrong_key = bytes(range(1, 33))
    ok, _, err, verifier = verify_state(
        state, signing_key, fresh_nonce_store, enc_key=wrong_key,
    )
    assert ok is False
    assert err == "verifier decryption failed"
    assert verifier is None


def test_five_part_token_requires_enc_key_to_verify(
    signing_key, fresh_nonce_store, enc_key
):
    """A 5-part token can't be verified without an enc_key to decrypt the
    embedded verifier — surfaced loudly rather than silently returning a
    None verifier that would brick the token exchange later."""
    from appscriptly.oauth_state import sign_state, verify_state

    state = sign_state(
        "u", signing_key, code_verifier="v", enc_key=enc_key,
    )
    ok, _, err, _ = verify_state(state, signing_key, fresh_nonce_store)
    assert ok is False
    assert err == "encryption key required to decrypt verifier"
