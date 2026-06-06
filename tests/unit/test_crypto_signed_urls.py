"""Signed-URL HMAC tests (v2.1 — user_id binding for multi-tenant /api/convert).

Guards the v2.1 design intent (per R10/R13/R23):

- Canonical string includes user_id; a URL signed for user A cannot be
  re-purposed against user B (HMAC mismatch).
- Mint requires non-empty user_id (validation on the sign side too).
- Verify rejects pre-v2.1 URLs (no ``uid`` query param) — strict cutoff.
- Verify returns the user_id on success so the request handler knows
  whose Google credentials to use.
- Nonce single-use semantics preserved (no regression vs v2.0).
- Expiry semantics preserved.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest


# ---------------------------------------------------------------------
# sign_upload_url — minting
# ---------------------------------------------------------------------


def test_sign_url_requires_non_empty_user_id():
    from appscriptly.crypto import sign_upload_url

    with pytest.raises(ValueError, match="user_id"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="",
        )

    with pytest.raises(ValueError, match="user_id"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id=None,  # type: ignore[arg-type]
        )


def test_sign_url_returns_user_id_in_payload():
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"k",
        user_id="user-A",
    )
    assert minted["user_id"] == "user-A"


def test_sign_url_query_string_contains_uid():
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"k",
        user_id="user-A",
    )
    qs = parse_qs(urlparse(minted["url"]).query)
    assert qs["uid"] == ["user-A"]
    # And all the legacy params are still present.
    for k in ("exp", "nonce", "max", "sig"):
        assert k in qs, f"missing {k} in {qs.keys()}"


# ---------------------------------------------------------------------
# verify_signed_params — happy path
# ---------------------------------------------------------------------


def _verify_minted(minted: dict, *, signing_key: bytes, override_uid: str | None = "use-minted"):
    """Round-trip helper: extract params from minted URL and call verify."""
    from appscriptly.crypto import NonceStore, verify_signed_params

    qs = parse_qs(urlparse(minted["url"]).query)
    uid = qs["uid"][0] if override_uid == "use-minted" else override_uid
    return verify_signed_params(
        signing_key=signing_key,
        exp=qs["exp"][0],
        nonce=qs["nonce"][0],
        max_bytes=qs["max"][0],
        sig=qs["sig"][0],
        user_id=uid,
        nonce_store=NonceStore(),
    )


def test_verify_happy_path_returns_user_id():
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    ok, err, max_i, uid = _verify_minted(minted, signing_key=b"kkk")
    assert ok is True, err
    assert max_i == minted["max_bytes"]
    assert uid == "user-A"


# ---------------------------------------------------------------------
# v2.1 strict cutoff — pre-v2.1 URLs (no uid) rejected
# ---------------------------------------------------------------------


def test_verify_rejects_missing_uid():
    """Pre-v2.1 URLs lack the uid query param; verify must refuse them."""
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    # Caller (middleware) passes uid=None when query string lacks it.
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key=b"kkk", override_uid=None,
    )
    assert ok is False
    assert err is not None
    assert "uid" in err.lower()


def test_verify_rejects_empty_uid():
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key=b"kkk", override_uid="",
    )
    assert ok is False
    assert "uid" in (err or "").lower()


# ---------------------------------------------------------------------
# Cross-tenant attack — URL signed for A cannot be replayed for B
# ---------------------------------------------------------------------


def test_verify_rejects_swapped_uid_signed_for_other_user():
    """Cross-tenant exploit prevention: A's URL with B substituted as uid
    must fail HMAC compare — the canonical no longer matches the sig."""
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key=b"kkk", override_uid="user-B",
    )
    assert ok is False
    assert err == "signature mismatch"


def test_verify_rejects_tampered_exp():
    """Tamper-evidence: changing exp must break HMAC."""
    from appscriptly.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    qs = parse_qs(urlparse(minted["url"]).query)
    ok, err, _max, _uid = verify_signed_params(
        signing_key=b"kkk",
        exp=str(int(qs["exp"][0]) + 9999),  # tampered
        nonce=qs["nonce"][0],
        max_bytes=qs["max"][0],
        sig=qs["sig"][0],
        user_id=qs["uid"][0],
        nonce_store=NonceStore(),
    )
    assert ok is False
    assert err == "signature mismatch"


# ---------------------------------------------------------------------
# Preserved semantics: expiry, single-use nonce
# ---------------------------------------------------------------------


def test_verify_rejects_expired():
    from appscriptly.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
        ttl_seconds=1,
    )
    time.sleep(1.1)
    qs = parse_qs(urlparse(minted["url"]).query)
    ok, err, _max, _uid = verify_signed_params(
        signing_key=b"kkk",
        exp=qs["exp"][0],
        nonce=qs["nonce"][0],
        max_bytes=qs["max"][0],
        sig=qs["sig"][0],
        user_id=qs["uid"][0],
        nonce_store=NonceStore(),
    )
    assert ok is False
    assert err is not None
    assert "expired" in err.lower()


def test_verify_nonce_is_single_use():
    from appscriptly.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"kkk",
        user_id="user-A",
    )
    qs = parse_qs(urlparse(minted["url"]).query)
    store = NonceStore()

    ok1, _err1, _max1, _uid1 = verify_signed_params(
        signing_key=b"kkk",
        exp=qs["exp"][0], nonce=qs["nonce"][0], max_bytes=qs["max"][0],
        sig=qs["sig"][0], user_id=qs["uid"][0], nonce_store=store,
    )
    assert ok1 is True

    ok2, err2, _max2, _uid2 = verify_signed_params(
        signing_key=b"kkk",
        exp=qs["exp"][0], nonce=qs["nonce"][0], max_bytes=qs["max"][0],
        sig=qs["sig"][0], user_id=qs["uid"][0], nonce_store=store,
    )
    assert ok2 is False
    assert err2 is not None
    assert "used" in err2.lower()


def test_verify_rejects_wrong_signing_key():
    from appscriptly.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=b"key-A",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(minted, signing_key=b"key-B")
    assert ok is False
    assert err == "signature mismatch"


# ---------------------------------------------------------------------
# max_bytes floor/ceiling validation (dd-apps-maxbytes-enforce)
#
# The signed ``max`` is no longer a dead advisory value — the convert
# endpoint enforces it (over-cap → 413). So a nonsensical (≤0) or
# unbounded cap must be rejected at BOTH the mint and verify boundaries.
# ---------------------------------------------------------------------


def test_sign_url_rejects_zero_max_bytes():
    from appscriptly.crypto import sign_upload_url

    with pytest.raises(ValueError, match="max_bytes"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="user-A",
            max_bytes=0,
        )


def test_sign_url_rejects_negative_max_bytes():
    from appscriptly.crypto import sign_upload_url

    with pytest.raises(ValueError, match="max_bytes"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="user-A",
            max_bytes=-1,
        )


def test_sign_url_rejects_over_ceiling_max_bytes():
    from appscriptly.crypto import MAX_MAX_BYTES, sign_upload_url

    with pytest.raises(ValueError, match="max_bytes"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="user-A",
            max_bytes=MAX_MAX_BYTES + 1,
        )


def test_sign_url_rejects_bool_max_bytes():
    """bool is an int subclass in Python; True/False must not slip past
    the int check and become a 1/0-byte cap."""
    from appscriptly.crypto import sign_upload_url

    with pytest.raises(ValueError, match="max_bytes"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="user-A",
            max_bytes=True,  # type: ignore[arg-type]
        )


def test_sign_url_accepts_boundary_max_bytes():
    """The floor and ceiling themselves are valid (inclusive bounds)."""
    from appscriptly.crypto import (
        MAX_MAX_BYTES,
        MIN_MAX_BYTES,
        sign_upload_url,
    )

    for cap in (MIN_MAX_BYTES, MAX_MAX_BYTES):
        minted = sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key=b"k",
            user_id="user-A",
            max_bytes=cap,
        )
        assert minted["max_bytes"] == cap


def test_default_max_bytes_is_within_bounds():
    """The default cap must itself satisfy the bounds — otherwise the
    no-arg mint path would raise."""
    from appscriptly.crypto import (
        DEFAULT_MAX_BYTES,
        MAX_MAX_BYTES,
        MIN_MAX_BYTES,
    )

    assert MIN_MAX_BYTES <= DEFAULT_MAX_BYTES <= MAX_MAX_BYTES


def test_verify_rejects_out_of_bounds_max_even_when_signed():
    """Defense-in-depth: a URL whose ``max`` is out of policy must be
    rejected by verify EVEN if the HMAC is valid (e.g. signed by older
    code before bounds existed, or after the ceiling was tightened).

    We forge a *correctly-signed* over-ceiling URL by signing the
    canonical directly with the same key — bypassing sign_upload_url's
    mint-time guard — to prove verify is an independent gate.
    """
    import hashlib
    import hmac
    import time
    from urllib.parse import parse_qs, urlparse

    from appscriptly.crypto import (
        MAX_MAX_BYTES,
        NonceStore,
        _canonical,
        sign_upload_url,
        verify_signed_params,
    )

    key = b"kkk"
    # Start from a valid mint to get a fresh exp/nonce, then swap in an
    # out-of-bounds max and RE-SIGN so the HMAC genuinely validates.
    seed = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=key,
        user_id="user-A",
    )
    qs = parse_qs(urlparse(seed["url"]).query)
    exp_i = int(qs["exp"][0])
    nonce = qs["nonce"][0]
    bad_max = MAX_MAX_BYTES + 5_000_000
    canonical = _canonical(exp_i, nonce, bad_max, "user-A")
    forged_sig = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    assert exp_i > int(time.time())  # sanity: not expired
    ok, err, max_out, uid = verify_signed_params(
        signing_key=key,
        exp=str(exp_i),
        nonce=nonce,
        max_bytes=str(bad_max),
        sig=forged_sig,
        user_id="user-A",
        nonce_store=NonceStore(),
    )
    assert ok is False
    assert max_out is None
    assert uid is None
    assert "bounds" in (err or "").lower()


def test_verify_out_of_bounds_does_not_consume_nonce():
    """An out-of-policy (but HMAC-valid) URL must NOT burn its nonce —
    the bounds check is placed before nonce consumption so a fixed
    re-mint isn't blocked by a spent nonce."""
    import hashlib
    import hmac
    from urllib.parse import parse_qs, urlparse

    from appscriptly.crypto import (
        MAX_MAX_BYTES,
        NonceStore,
        _canonical,
        sign_upload_url,
        verify_signed_params,
    )

    key = b"kkk"
    seed = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key=key,
        user_id="user-A",
    )
    qs = parse_qs(urlparse(seed["url"]).query)
    exp_i = int(qs["exp"][0])
    nonce = qs["nonce"][0]
    bad_max = MAX_MAX_BYTES + 1
    canonical = _canonical(exp_i, nonce, bad_max, "user-A")
    forged_sig = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    store = NonceStore()
    ok1, _e1, _m1, _u1 = verify_signed_params(
        signing_key=key, exp=str(exp_i), nonce=nonce, max_bytes=str(bad_max),
        sig=forged_sig, user_id="user-A", nonce_store=store,
    )
    assert ok1 is False
    # The nonce is still unconsumed: consuming it now succeeds (would
    # return False if the rejected attempt had burned it).
    assert store.consume(nonce, exp_i) is True
