"""Signed-URL frame staging for the base-tier slides→video handoff.

Replaces the ``drive.readonly``-dependent frame handoff so the free base
tier needs no restricted scope.

Why not "app-created folder + drive.file": ``drive.file`` is a PER-FILE
grant. A PNG the bound render script creates (running as the USER) is not
app-accessible just because its parent folder was app-created — folder
membership does NOT cascade ``drive.file`` access (verified against
Google's Drive API scope docs). So the only way for the encode step to
read the frames without ``drive.readonly`` is to NOT put them in Drive at
all.

New handoff (no Drive read scope): the server issues ONE HMAC-signed
batch token; the bound render script POSTs each rendered PNG straight to
the server's ``/upload/frames/<batch_id>/<index>?token=...`` endpoint via
``UrlFetchApp`` (the renderer already holds ``script.external_request``,
so no new scope). ``as_encode_video`` then reads the staged frames off
the server's own ``/data`` volume and runs ffmpeg. The final MP4 still
goes to the user's Drive via ``drive.file`` (the app creates that file,
so the app can access it).

Security mirrors the docx signed-upload path (``crypto.py`` v2.1):

  * the batch token is HMAC-signed over ``(batch_id, expiry, nonce,
    user_id)`` with the ``signed_url`` purpose key, so only the holder of
    a server-issued URL can write;
  * the token is **single-use** — a ``NonceStore`` redeems the nonce on
    first accepted upload, so a captured token can't be replayed across
    all 9999 indices for the TTL (the v2.1 nonce hardening, previously
    NOT mirrored here);
  * the token is **user-bound** — ``user_id`` is in the signed canonical
    (the v2.1 uid binding applied to the docx-convert URL, previously NOT
    mirrored here), closing the multi-tenant gap;
  * staged frames are TTL-bounded, live under ``/data`` (survive a restart
    mid-render), and are **size/count-capped at staging time** (per-frame
    bytes + per-batch frame-count + per-batch cumulative bytes) so a
    token holder can't disk-fill the box. ``BodySizeLimitMiddleware`` only
    caps a DECLARED Content-Length and is bypassed by a chunked /
    Content-Length-omitting POST, so the cap is enforced HERE (endpoint
    layer), not relied upon from the middleware;
  * the batch id and frame index are constrained to safe alphabets so a
    token holder can't escape the batch directory via path traversal.

**Single-use semantics.** The renderer POSTs ALL frames for a batch under
one token, so single-use cannot be "one POST then dead". The nonce is
redeemed once and then re-presentation of the SAME (token, batch) is
accepted for the life of the batch dir — i.e. the token authorizes one
*render session*, not one *frame*. Replay AFTER the batch is consumed /
expired / cleared is rejected because the batch dir is gone and a fresh
mint uses a fresh nonce. (See ``_FrameBatchNonceStore`` for the exact
contract; this is the per-batch analogue of the convert path's one-shot
nonce.)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from appscriptly import keys
from appscriptly.auth import default_data_dir

_log = logging.getLogger("appscriptly.services.apps_script._frames_staging")

_FRAMES_SUBDIR = "video_frames_staging"
# A batch id is server-generated (token_urlsafe) — restrict to the
# URL-safe alphabet so it can't contain path separators.
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# Frame index is a bounded integer string (1..9999; the encode half's
# _MAX_FRAMES cap is re-checked there too).
_FRAME_INDEX_RE = re.compile(r"^[0-9]{1,4}$")

# TTL for a frame-batch token. Longer than the docx 10-min default
# because the user must open the Slides editor and run the render
# manually, which is slower than a direct browser upload.
_DEFAULT_TTL_SECONDS = 1800  # 30 min

# Sentinel user_id for the single-tenant / stdio path (no auth context →
# current_user_id_or_none() is None). Binds the token to "the operator"
# rather than leaving uid empty, so the canonical is always uid-bound and
# the verify path is uniform (mirrors the convert path treating a
# None-uid request as the operator tenant).
_OPERATOR_UID = "operator"

# Upload size/count caps — enforced at staging time so a token holder
# can't disk-fill the box before the encode half's read-side caps run.
# Aligned with the encode half's resource bounds (encode_video.py:
# _MAX_FRAMES=200, _MAX_TOTAL_FRAME_BYTES=250 MiB) — staging refuses what
# encode would later reject anyway, but BEFORE the bytes hit disk.
_MAX_FRAME_BYTES = 10 * 1024 * 1024  # 10 MiB per PNG frame
_MAX_FRAMES_PER_BATCH = 200  # matches encode_video._MAX_FRAMES
_MAX_BATCH_TOTAL_BYTES = 250 * 1024 * 1024  # 250 MiB; encode parity


class FrameUploadTooLarge(Exception):
    """A frame upload exceeded a per-frame / per-batch size or count cap.

    The endpoint maps this to HTTP 413 (Payload Too Large). Carries a
    human-readable reason for the response body.
    """


class _FrameBatchNonceStore:
    """Single-use redemption for frame-batch tokens, scoped per (nonce).

    A frame-batch token authorizes ONE render session (the renderer POSTs
    every frame under the same token), so — unlike the convert path's
    strictly-one-request nonce — the FIRST upload redeems the nonce and
    binds it to its batch_id; subsequent uploads in the SAME batch under
    the SAME nonce are allowed, but the nonce can never be re-bound to a
    DIFFERENT batch_id (replaying a captured token against a fresh
    server-minted batch fails) and is forgotten on eviction so a replay
    after expiry is rejected. Time-bounded eviction like ``crypto.NonceStore``.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, int]] = {}  # nonce -> (batch_id, exp)
        self._lock = threading.Lock()

    def redeem(self, nonce: str, batch_id: str, exp: int) -> bool:
        """Bind ``nonce`` to ``batch_id`` (first use) or confirm the bind.

        Returns True if the nonce is unused (binds it now) OR already bound
        to THIS batch_id (same render session re-presenting the token for
        the next frame). Returns False if the nonce is already bound to a
        DIFFERENT batch_id (a replay/confusion attempt). Opportunistically
        evicts expired entries.
        """
        now = int(time.time())
        with self._lock:
            stale = [n for n, (_b, e) in self._seen.items() if e <= now]
            for n in stale:
                del self._seen[n]
            existing = self._seen.get(nonce)
            if existing is None:
                self._seen[nonce] = (batch_id, exp)
                return True
            return existing[0] == batch_id


# Process-wide single-use store for frame-batch tokens. Module-level so
# every frame POST in a batch shares it; tests rebind via
# ``_frames_staging._NONCE_STORE = _FrameBatchNonceStore()``.
_NONCE_STORE = _FrameBatchNonceStore()


def new_batch_id() -> str:
    """Generate a fresh, path-safe batch id (32 url-safe chars)."""
    return secrets.token_urlsafe(24)


def _encode_uid(user_id: str) -> str:
    """URL-safe, dot-free encoding of user_id for the ``.``-delimited token."""
    return base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")


def sign_frames_batch(
    batch_id: str,
    *,
    user_id: str | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """Return an HMAC token ``<expiry>.<nonce>.<uid_b64>.<sig>`` for the batch.

    Signed with the ``signed_url`` purpose key (same key family as the
    docx upload URLs) so the bound Apps Script can authenticate frame
    POSTs without the MCP bearer credential. The token is:

    - **user-bound**: ``user_id`` (the OAuth ``sub`` from
      ``current_user_id_or_none()``, or the ``operator`` sentinel in
      stdio mode) is in the signed canonical — mirrors the convert path's
      v2.1 uid binding.
    - **single-use**: carries a fresh ``nonce`` redeemed on first upload.
    """
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"invalid batch_id {batch_id!r}")
    uid = user_id or _OPERATOR_UID
    expiry = int(time.time()) + int(ttl_seconds)
    nonce = secrets.token_urlsafe(16)
    uid_b64 = _encode_uid(uid)
    return f"{expiry}.{nonce}.{uid_b64}.{_sig(batch_id, expiry, nonce, uid)}"


def verify_frames_token(batch_id: str, token: str) -> str | None:
    """Verify + REDEEM a frames token; return the bound user_id or None.

    Constant-time HMAC compare over ``(batch_id, expiry, nonce, user_id)``,
    expiry check, and single-use redemption via ``_NONCE_STORE``. Returns
    the token's bound ``user_id`` on success (so the caller can enforce
    tenant match), or ``None`` if the token is invalid / expired / a
    replay against a different batch.

    NOTE: returns a (truthy) ``str`` on success and ``None`` on failure —
    callers must check ``is not None`` (the operator sentinel ``"operator"``
    is truthy, but be explicit). This is a behavior change from the prior
    ``bool`` return; the sole caller (the upload endpoint) is updated.
    """
    if not _BATCH_ID_RE.match(batch_id):
        return None
    try:
        expiry_s, nonce, uid_b64, sig = token.split(".", 3)
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return None
    if not nonce or not uid_b64:
        return None
    if time.time() > expiry:
        return None
    try:
        pad = "=" * (-len(uid_b64) % 4)
        uid = base64.urlsafe_b64decode(uid_b64 + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(sig, _sig(batch_id, expiry, nonce, uid)):
        return None
    # Single-use: redeem the nonce, bound to this batch_id. A replay of a
    # captured token against a different batch_id is rejected here even
    # though the HMAC would (for that other batch) not match anyway —
    # defence in depth + the canonical replay guard for the SAME batch.
    if not _NONCE_STORE.redeem(nonce, batch_id, expiry):
        return None
    return uid


def stage_frame_bytes(batch_id: str, index: str, data: bytes) -> Path:
    """Persist one PNG frame for ``batch_id`` under ``index``.

    ``index`` is the 1-based frame number as a string (validated). Files
    are named ``frame_0001.png`` … so a lexical sort yields render order.
    Returns the written path.

    Enforces upload caps BEFORE writing (so a pathological batch can't
    disk-fill the box): per-frame bytes, per-batch frame count, and
    per-batch cumulative bytes. Over-cap raises ``FrameUploadTooLarge``
    (the endpoint maps it to HTTP 413).
    """
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"invalid batch_id {batch_id!r}")
    if not _FRAME_INDEX_RE.match(index):
        raise ValueError(f"invalid frame index {index!r}")
    if len(data) > _MAX_FRAME_BYTES:
        raise FrameUploadTooLarge(
            f"frame is {len(data)} bytes, over the {_MAX_FRAME_BYTES}-byte "
            f"per-frame cap"
        )
    d = _batch_dir(batch_id)
    d.mkdir(parents=True, exist_ok=True)

    # Per-batch count + cumulative-byte caps. Re-derive an existing frame
    # set under the lock-free dir scan; a concurrent racer could in
    # principle slip one extra frame past the boundary, but the encode
    # half re-enforces both caps on read, so the disk-fill ceiling holds.
    out = d / f"frame_{int(index):04d}.png"
    existing = [p for p in d.glob("frame_*.png") if p != out]
    if len(existing) >= _MAX_FRAMES_PER_BATCH:
        raise FrameUploadTooLarge(
            f"batch already has {len(existing)} frames, at the "
            f"{_MAX_FRAMES_PER_BATCH}-frame per-batch cap"
        )
    existing_bytes = sum(p.stat().st_size for p in existing)
    if existing_bytes + len(data) > _MAX_BATCH_TOTAL_BYTES:
        cap_mb = _MAX_BATCH_TOTAL_BYTES // (1024 * 1024)
        raise FrameUploadTooLarge(
            f"batch would exceed the {cap_mb} MiB cumulative cap "
            f"({existing_bytes + len(data)} bytes)"
        )

    out.write_bytes(data)
    return out


def list_staged_frames(batch_id: str) -> list[Path]:
    """Return the staged frame paths for ``batch_id``, sorted by name."""
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"invalid batch_id {batch_id!r}")
    d = _batch_dir(batch_id)
    if not d.is_dir():
        return []
    return sorted(d.glob("frame_*.png"))


def clear_batch(batch_id: str) -> None:
    """Delete all staged frames for ``batch_id`` (one-shot consume).

    Called by ``as_encode_video`` after a successful read so frames don't
    linger on the volume. Best-effort.
    """
    if not _BATCH_ID_RE.match(batch_id):
        return
    shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)


def _sig(batch_id: str, expiry: int, nonce: str, user_id: str) -> str:
    """HMAC over the v2.1-style canonical: batch + expiry + nonce + uid.

    Adding ``nonce`` (single-use) and ``user_id`` (tenant binding) to the
    canonical mirrors ``crypto._canonical`` for the docx-convert URL — a
    tampered uid or a swapped nonce fails the signature before any disk
    write or nonce redemption.
    """
    key = keys.get_key("signed_url")
    msg = f"frames:{batch_id}:{expiry}:{nonce}:{user_id}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _batch_dir(batch_id: str) -> Path:
    return default_data_dir() / _FRAMES_SUBDIR / batch_id
