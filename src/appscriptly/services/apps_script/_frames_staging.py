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

Security mirrors the docx signed-upload path: the batch token is an
HMAC-signed ``(batch_id, expiry)`` (the ``signed_url`` purpose key), so
only the holder of a server-issued URL can write; staged frames are
TTL-bounded, live under ``/data`` (survive a restart mid-render), and are
size-capped by the BodySizeLimitMiddleware. The batch id and frame index
are constrained to safe alphabets so a token holder can't escape the
batch directory via path traversal.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import shutil
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


def new_batch_id() -> str:
    """Generate a fresh, path-safe batch id (32 url-safe chars)."""
    return secrets.token_urlsafe(24)


def sign_frames_batch(batch_id: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> str:
    """Return an HMAC token ``<expiry>.<sig>`` binding the batch.

    Signed with the ``signed_url`` purpose key (same key family as the
    docx upload URLs) so the bound Apps Script can authenticate frame
    POSTs without the MCP bearer credential.
    """
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"invalid batch_id {batch_id!r}")
    expiry = int(time.time()) + int(ttl_seconds)
    return f"{expiry}.{_sig(batch_id, expiry)}"


def verify_frames_token(batch_id: str, token: str) -> bool:
    """Constant-time verify of a ``sign_frames_batch`` token."""
    if not _BATCH_ID_RE.match(batch_id):
        return False
    try:
        expiry_s, sig = token.split(".", 1)
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return False
    if time.time() > expiry:
        return False
    return hmac.compare_digest(sig, _sig(batch_id, expiry))


def stage_frame_bytes(batch_id: str, index: str, data: bytes) -> Path:
    """Persist one PNG frame for ``batch_id`` under ``index``.

    ``index`` is the 1-based frame number as a string (validated). Files
    are named ``frame_0001.png`` … so a lexical sort yields render order.
    Returns the written path.
    """
    if not _BATCH_ID_RE.match(batch_id):
        raise ValueError(f"invalid batch_id {batch_id!r}")
    if not _FRAME_INDEX_RE.match(index):
        raise ValueError(f"invalid frame index {index!r}")
    d = _batch_dir(batch_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"frame_{int(index):04d}.png"
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


def _sig(batch_id: str, expiry: int) -> str:
    key = keys.get_key("signed_url")
    msg = f"frames:{batch_id}:{expiry}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _batch_dir(batch_id: str) -> Path:
    return default_data_dir() / _FRAMES_SUBDIR / batch_id
