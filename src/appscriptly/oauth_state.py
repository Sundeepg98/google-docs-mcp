"""HMAC-signed, single-use OAuth state parameter.

Encodes the calling user's ``sub`` claim into the OAuth state param so
the ``/oauth/google/api/callback`` handler can recover *who* came back
from Google without needing a server-side state store.

**The CSRF / replay threat we mitigate.** A naive design would put
``user_id`` in the state plaintext. An attacker could:

1. Initiate their OWN OAuth flow against our server.
2. Edit the state in transit to swap in a victim's ``user_id``.
3. Complete consent on Google.
4. Our callback stores the ATTACKER's Google creds under the VICTIM's
   key — now the victim's MCP tool calls run against the attacker's
   Google account.

HMAC over ``sub.nonce.exp[.enc]`` makes (2) impossible. The single-use
``NonceStore`` (shared with the signed-upload-URL plumbing) makes state
replay impossible: if an attacker observes a victim's state in browser
history / access logs and tries to redeem it later, ``consume()``
returns ``False`` and the callback rejects.

State on the wire is opaque-ish — embeds the ``sub`` (already a
public-ish identifier) but anyone tampering breaks the signature.

**Stateless encrypted PKCE (fix/…-login).** The PKCE ``code_verifier``
is carried INSIDE the state token as an AES-GCM ciphertext (the ``enc``
field), rather than in a server-side in-memory store. This is what lets
an in-flight login survive a deploy/restart or land its callback on a
different instance: there is no per-process verifier map to forget. See
``sign_state`` / ``verify_state`` for the token format and the
plaintext-rejection note the encryption answers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import NonceStore

# ---------------------------------------------------------------------
# PKCE code_verifier handling — encrypted-in-token, NOT a server store.
#
# The author originally rejected encoding the verifier into the state
# token because putting it in the URL *in plaintext* (which already
# carries the auth code on the callback) would defeat PKCE's whole
# purpose — the protection comes from the verifier being secret AND held
# only by the legitimate client. An observer of the URL who could read a
# plaintext verifier would hold both halves of PKCE.
#
# That objection is answered by ENCRYPTING the verifier. The ``enc``
# field is an AES-GCM ciphertext under ``keys.get_key("oauth_state_enc")``
# — a key only the server holds. An observer of the state URL sees opaque
# ciphertext, not the verifier; only the server (after HMAC verification)
# can decrypt it. The confidentiality PKCE needs is preserved, and the
# verifier now rides along with the state so a restart / cross-instance
# callback can still complete the token exchange. (Replaces the former
# in-memory ``_pending_verifiers`` dict, which was forgotten on restart —
# the narrow "deploy drops in-flight logins" failure this fix removes.)
# ---------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 600  # 10 minutes — long enough for browser dance,
                           # short enough that a leaked URL is mostly dead.
MAX_TTL_SECONDS = 3600

# AES-GCM standard 96-bit IV, prepended to the ciphertext blob.
_GCM_IV_BYTES = 12


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode("ascii") + b"=" * (-len(s) % 4))


def _encrypt_verifier(enc_key: bytes, verifier: str, aad: bytes) -> str:
    """AES-GCM-encrypt ``verifier``, returning a URL-safe token.

    ``aad`` (the state nonce) is bound as authenticated-but-unencrypted
    associated data so a ciphertext can't be transplanted onto a
    different state even in isolation from the outer HMAC. The 96-bit IV
    is random per call and prepended to the ciphertext.
    """
    iv = secrets.token_bytes(_GCM_IV_BYTES)
    ct = AESGCM(enc_key).encrypt(iv, verifier.encode("utf-8"), aad)
    return _b64url_encode(iv + ct)


def _decrypt_verifier(enc_key: bytes, enc: str, aad: bytes) -> str | None:
    """Reverse ``_encrypt_verifier``. Returns ``None`` on ANY failure.

    A garbage / tampered / wrong-key ``enc`` yields ``None`` rather than
    raising, so the callback path degrades to a clean auth failure with
    no traceback. ``InvalidTag`` covers auth-tag mismatch; ``ValueError``
    covers malformed base64 (``binascii.Error``) and a too-short blob;
    ``UnicodeDecodeError`` (a ``ValueError`` subclass) covers non-UTF-8
    plaintext.
    """
    try:
        blob = _b64url_decode(enc)
        if len(blob) <= _GCM_IV_BYTES:
            return None
        iv, ct = blob[:_GCM_IV_BYTES], blob[_GCM_IV_BYTES:]
        return AESGCM(enc_key).decrypt(iv, ct, aad).decode("utf-8")
    except (InvalidTag, ValueError):
        return None


def _canonical(sub_b64: str, nonce: str, exp: int, enc: str | None = None) -> str:
    """HMAC-signed canonical string.

    Without a PKCE verifier this is the historical ``sub.nonce.exp`` (the
    token stays 4-part ``sub.nonce.exp.sig``). With a verifier the ``enc``
    ciphertext is appended and authenticated too (the token is 5-part
    ``sub.nonce.exp.enc.sig``), so tampering the ciphertext breaks the
    signature before decryption is even attempted.
    """
    if enc is None:
        return f"{sub_b64}.{nonce}.{exp}"
    return f"{sub_b64}.{nonce}.{exp}.{enc}"


def sign_state(
    user_id: str,
    signing_key: bytes,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    code_verifier: str | None = None,
    enc_key: bytes | None = None,
) -> str:
    """Mint a fresh signed state token binding ``user_id`` to a TTL.

    Returns a ``.``-separated string. Without a PKCE ``code_verifier``
    it is ``sub_b64.nonce.exp.sig`` (4 parts, byte-format unchanged from
    pre-encrypted-PKCE). With one it is ``sub_b64.nonce.exp.enc.sig`` (5
    parts) where ``enc`` is the AES-GCM ciphertext of the verifier under
    ``enc_key``. Pass the result verbatim as the ``state`` query param in
    the Google auth URL.

    ``signing_key`` is bytes (matches ``keys.get_key("oauth_state")``'s
    return type). v2.0b strict-flip: HKDF returns raw 32-byte keys that
    aren't generally valid UTF-8 — passing bytes through directly works
    for all key sources (override / shim / HKDF).

    If ``code_verifier`` is provided (PKCE), ``enc_key`` is REQUIRED — the
    verifier is encrypted and embedded so ``verify_state`` can recover it
    on the callback without a server-side store (survives restarts /
    cross-instance callbacks). Omitting ``enc_key`` while passing a
    verifier is a misconfiguration and raises loudly rather than silently
    dropping the verifier (which would brick the token exchange).
    """
    if not user_id:
        raise ValueError("user_id is required")
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be in (0, {MAX_TTL_SECONDS}], got {ttl_seconds}"
        )
    if code_verifier and not enc_key:
        raise ValueError(
            "enc_key is required to embed a PKCE code_verifier in the "
            "state token (stateless encrypted PKCE)"
        )

    sub_b64 = base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")
    nonce = secrets.token_urlsafe(16)
    exp = int(time.time()) + ttl_seconds

    if code_verifier:
        assert enc_key is not None  # guarded above — narrows for type-checkers
        enc = _encrypt_verifier(enc_key, code_verifier, nonce.encode("ascii"))
        sig = _hmac(signing_key, _canonical(sub_b64, nonce, exp, enc))
        return f"{sub_b64}.{nonce}.{exp}.{enc}.{sig}"

    sig = _hmac(signing_key, _canonical(sub_b64, nonce, exp))
    return f"{sub_b64}.{nonce}.{exp}.{sig}"


def verify_state(
    state: str,
    signing_key: bytes,
    nonce_store: NonceStore,
    *,
    enc_key: bytes | None = None,
) -> tuple[bool, str | None, str | None, str | None]:
    """Validate + consume a state token.

    Returns ``(ok, user_id, error, code_verifier)``. On success the nonce
    is marked consumed atomically and any embedded PKCE verifier is
    decrypted (with ``enc_key``) and returned. A second call with the
    same state returns ``(False, None, "state already used", None)``.

    Accepts both token shapes: 4-part ``sub.nonce.exp.sig`` (no PKCE) and
    5-part ``sub.nonce.exp.enc.sig`` (encrypted PKCE). The 4th tuple
    element is the recovered verifier, or ``None`` when the token carried
    none — the caller checks before using.

    Decryption happens only AFTER the HMAC (which authenticates ``enc``)
    and the single-use nonce check pass, so a tampered token is rejected
    at the signature and a replay at the nonce before any decrypt runs.
    """
    if not state:
        return False, None, "state is empty", None

    parts = state.split(".")
    if len(parts) == 4:
        sub_b64, nonce, exp_str, sig = parts
        enc: str | None = None
    elif len(parts) == 5:
        sub_b64, nonce, exp_str, enc, sig = parts
    else:
        return False, None, "state malformed (expected sub.nonce.exp[.enc].sig)", None

    try:
        exp = int(exp_str)
    except ValueError:
        return False, None, "exp must be an integer", None

    if exp <= int(time.time()):
        return False, None, "state has expired", None

    expected = _hmac(signing_key, _canonical(sub_b64, nonce, exp, enc))
    if not hmac.compare_digest(expected, sig):
        return False, None, "signature mismatch", None

    if not nonce_store.consume(nonce, exp):
        return False, None, "state already used", None

    try:
        # Restore the padding stripped at sign time before decoding.
        padded = sub_b64 + "=" * (-len(sub_b64) % 4)
        user_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, None, "sub_b64 not decodable", None

    code_verifier: str | None = None
    if enc is not None:
        if not enc_key:
            return False, None, "encryption key required to decrypt verifier", None
        code_verifier = _decrypt_verifier(enc_key, enc, nonce.encode("ascii"))
        if code_verifier is None:
            return False, None, "verifier decryption failed", None

    return True, user_id, None, code_verifier


def _hmac(key: bytes, message: str) -> str:
    return hmac.new(
        key, message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
