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
    from google_docs_mcp.crypto import sign_upload_url

    with pytest.raises(ValueError, match="user_id"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key="k",
            user_id="",
        )

    with pytest.raises(ValueError, match="user_id"):
        sign_upload_url(
            base_url="https://x.example/api/convert",
            signing_key="k",
            user_id=None,  # type: ignore[arg-type]
        )


def test_sign_url_returns_user_id_in_payload():
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="k",
        user_id="user-A",
    )
    assert minted["user_id"] == "user-A"


def test_sign_url_query_string_contains_uid():
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="k",
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


def _verify_minted(minted: dict, *, signing_key: str, override_uid: str | None = "use-minted"):
    """Round-trip helper: extract params from minted URL and call verify."""
    from google_docs_mcp.crypto import NonceStore, verify_signed_params

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
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    ok, err, max_i, uid = _verify_minted(minted, signing_key="kkk")
    assert ok is True, err
    assert max_i == minted["max_bytes"]
    assert uid == "user-A"


# ---------------------------------------------------------------------
# v2.1 strict cutoff — pre-v2.1 URLs (no uid) rejected
# ---------------------------------------------------------------------


def test_verify_rejects_missing_uid():
    """Pre-v2.1 URLs lack the uid query param; verify must refuse them."""
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    # Caller (middleware) passes uid=None when query string lacks it.
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key="kkk", override_uid=None,
    )
    assert ok is False
    assert err is not None
    assert "uid" in err.lower()


def test_verify_rejects_empty_uid():
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key="kkk", override_uid="",
    )
    assert ok is False
    assert "uid" in (err or "").lower()


# ---------------------------------------------------------------------
# Cross-tenant attack — URL signed for A cannot be replayed for B
# ---------------------------------------------------------------------


def test_verify_rejects_swapped_uid_signed_for_other_user():
    """Cross-tenant exploit prevention: A's URL with B substituted as uid
    must fail HMAC compare — the canonical no longer matches the sig."""
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(
        minted, signing_key="kkk", override_uid="user-B",
    )
    assert ok is False
    assert err == "signature mismatch"


def test_verify_rejects_tampered_exp():
    """Tamper-evidence: changing exp must break HMAC."""
    from google_docs_mcp.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    qs = parse_qs(urlparse(minted["url"]).query)
    ok, err, _max, _uid = verify_signed_params(
        signing_key="kkk",
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
    from google_docs_mcp.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
        ttl_seconds=1,
    )
    time.sleep(1.1)
    qs = parse_qs(urlparse(minted["url"]).query)
    ok, err, _max, _uid = verify_signed_params(
        signing_key="kkk",
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
    from google_docs_mcp.crypto import NonceStore, sign_upload_url, verify_signed_params

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="kkk",
        user_id="user-A",
    )
    qs = parse_qs(urlparse(minted["url"]).query)
    store = NonceStore()

    ok1, _err1, _max1, _uid1 = verify_signed_params(
        signing_key="kkk",
        exp=qs["exp"][0], nonce=qs["nonce"][0], max_bytes=qs["max"][0],
        sig=qs["sig"][0], user_id=qs["uid"][0], nonce_store=store,
    )
    assert ok1 is True

    ok2, err2, _max2, _uid2 = verify_signed_params(
        signing_key="kkk",
        exp=qs["exp"][0], nonce=qs["nonce"][0], max_bytes=qs["max"][0],
        sig=qs["sig"][0], user_id=qs["uid"][0], nonce_store=store,
    )
    assert ok2 is False
    assert err2 is not None
    assert "used" in err2.lower()


def test_verify_rejects_wrong_signing_key():
    from google_docs_mcp.crypto import sign_upload_url

    minted = sign_upload_url(
        base_url="https://x.example/api/convert",
        signing_key="key-A",
        user_id="user-A",
    )
    ok, err, _max, _uid = _verify_minted(minted, signing_key="key-B")
    assert ok is False
    assert err == "signature mismatch"
