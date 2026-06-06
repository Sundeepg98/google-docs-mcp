"""Tests for the base-tier slides→video frame-staging handoff.

Covers ``services/apps_script/_frames_staging`` (sign/verify/stage/list/
clear, path-traversal guards, v2.1 uid-binding + single-use nonce + upload
size/count caps) AND the ``/upload/frames/<batch>/<index>`` endpoint
handler (``http_server.routes.convert.upload_frame_endpoint``). This is the
mechanism that replaced the ``drive.readonly`` Drive round-trip: the bound
render script POSTs PNGs here, authed by an HMAC batch token;
``as_encode_video`` reads them off the volume.

v2.1 hardening mirrored from the docx convert path (``crypto.py``):
verify_frames_token now returns the bound ``user_id`` (str) on success or
``None`` on failure; the token is single-use (a captured token can't be
replayed across all indices for the TTL) and user-bound; uploads are
size/count-capped at staging time.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from appscriptly.services.apps_script import _frames_staging

_BATCH = "BATCHaaaaaaaaaaaaaaaa"
_BATCH2 = "BATCHbbbbbbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _staging_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-bearer-token-32-characters-x")
    # Fresh single-use nonce store per test so one test's redemptions
    # don't bleed into another (mirrors conftest's NonceStore reset).
    monkeypatch.setattr(
        _frames_staging, "_NONCE_STORE", _frames_staging._FrameBatchNonceStore()
    )


# ---------------------------------------------------------------------
# Sign / verify — returns bound user_id (str) on success, None on failure
# ---------------------------------------------------------------------


def test_sign_then_verify_returns_bound_uid():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="user-123")
    assert _frames_staging.verify_frames_token(_BATCH, token) == "user-123"


def test_sign_without_uid_binds_operator_sentinel():
    """stdio / no-auth path: current_user_id_or_none() is None → token is
    bound to the operator sentinel, not left uid-less (uniform verify)."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id=None)
    assert _frames_staging.verify_frames_token(_BATCH, token) == "operator"


def test_verify_rejects_tampered_signature():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    # Replace the trailing sig with garbage (4-part token: exp.nonce.uid.sig).
    head = token.rsplit(".", 1)[0]
    assert _frames_staging.verify_frames_token(_BATCH, f"{head}.deadbeef") is None


def test_verify_rejects_wrong_batch_id():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    assert _frames_staging.verify_frames_token(_BATCH2, token) is None


def test_verify_rejects_expired_token():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u", ttl_seconds=-1)
    assert _frames_staging.verify_frames_token(_BATCH, token) is None


def test_verify_rejects_malformed_token():
    assert _frames_staging.verify_frames_token(_BATCH, "garbage") is None
    assert _frames_staging.verify_frames_token(_BATCH, "") is None
    # Old 2-part format (exp.sig) is no longer accepted.
    assert _frames_staging.verify_frames_token(_BATCH, "9999999999.abc") is None


def test_verify_rejects_uid_tamper():
    """SECURITY (tenant binding): swapping the uid segment after signing
    must fail the HMAC (uid is in the canonical) — you can't repoint a
    captured token at a different tenant."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="alice")
    exp, nonce, _uid_b64, sig = token.split(".", 3)
    forged_uid = _frames_staging._encode_uid("bob")
    forged = f"{exp}.{nonce}.{forged_uid}.{sig}"
    assert _frames_staging.verify_frames_token(_BATCH, forged) is None


def test_new_batch_id_is_path_safe_and_passes_regex():
    bid = _frames_staging.new_batch_id()
    assert _frames_staging._BATCH_ID_RE.match(bid)
    assert "/" not in bid and "\\" not in bid and ".." not in bid


def test_sign_rejects_bad_batch_id():
    with pytest.raises(ValueError, match="invalid batch_id"):
        _frames_staging.sign_frames_batch("../etc/passwd", user_id="u")


# ---------------------------------------------------------------------
# Single-use (nonce) — replay protection mirroring the convert path
# ---------------------------------------------------------------------


def test_token_authorizes_whole_render_session_same_batch():
    """The renderer POSTs every frame under ONE token, so re-presenting the
    SAME token for the SAME batch must keep succeeding (single render
    session) — single-use binds the nonce to the batch, it doesn't kill
    the token after the first frame."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    assert _frames_staging.verify_frames_token(_BATCH, token) == "u"
    assert _frames_staging.verify_frames_token(_BATCH, token) == "u"
    assert _frames_staging.verify_frames_token(_BATCH, token) == "u"


def test_token_nonce_cannot_be_rebound_to_a_different_batch():
    """SECURITY (replay): a captured token's nonce, once bound to its
    batch, cannot be reused under a DIFFERENT batch_id. (The HMAC also
    wouldn't match for another batch — this is the nonce-store defence in
    depth.) We bind the nonce to _BATCH, then forge a token that reuses the
    SAME nonce but targets _BATCH2 with a VALID sig for _BATCH2; the nonce
    store still rejects it because the nonce is already bound to _BATCH."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    _exp, nonce, _uidb, _sig = token.split(".", 3)
    # First use binds the nonce to _BATCH.
    assert _frames_staging.verify_frames_token(_BATCH, token) == "u"

    # Build a token for _BATCH2 that REUSES the same nonce, with a sig that
    # is valid for _BATCH2 (so it would pass HMAC) — only the nonce store
    # stops the cross-batch replay.
    import time
    exp2 = int(time.time()) + 600
    uid_b64 = _frames_staging._encode_uid("u")
    sig2 = _frames_staging._sig(_BATCH2, exp2, nonce, "u")
    forged = f"{exp2}.{nonce}.{uid_b64}.{sig2}"
    assert _frames_staging.verify_frames_token(_BATCH2, forged) is None


# ---------------------------------------------------------------------
# Upload size/count caps (staging layer) — disk-fill DoS protection
# ---------------------------------------------------------------------


def test_stage_rejects_oversize_single_frame():
    big = b"x" * (_frames_staging._MAX_FRAME_BYTES + 1)
    with pytest.raises(_frames_staging.FrameUploadTooLarge, match="per-frame cap"):
        _frames_staging.stage_frame_bytes(_BATCH, "1", big)


def test_stage_rejects_over_frame_count(monkeypatch):
    """Per-batch frame-count cap — a token holder can't stage 9999 frames."""
    monkeypatch.setattr(_frames_staging, "_MAX_FRAMES_PER_BATCH", 3)
    for i in range(1, 4):
        _frames_staging.stage_frame_bytes(_BATCH, str(i), b"x")
    with pytest.raises(_frames_staging.FrameUploadTooLarge, match="per-batch cap"):
        _frames_staging.stage_frame_bytes(_BATCH, "4", b"x")


def test_stage_rejects_over_cumulative_bytes(monkeypatch):
    """Per-batch cumulative-byte cap — many small frames can't disk-fill."""
    monkeypatch.setattr(_frames_staging, "_MAX_BATCH_TOTAL_BYTES", 10)
    _frames_staging.stage_frame_bytes(_BATCH, "1", b"12345")  # 5 bytes
    _frames_staging.stage_frame_bytes(_BATCH, "2", b"12345")  # 10 total — ok
    with pytest.raises(_frames_staging.FrameUploadTooLarge, match="cumulative cap"):
        _frames_staging.stage_frame_bytes(_BATCH, "3", b"1")  # would be 11


# ---------------------------------------------------------------------
# Stage / list / clear + traversal guards
# ---------------------------------------------------------------------


def test_stage_list_clear_roundtrip():
    _frames_staging.stage_frame_bytes(_BATCH, "1", b"png1")
    _frames_staging.stage_frame_bytes(_BATCH, "2", b"png2")
    staged = _frames_staging.list_staged_frames(_BATCH)
    assert [p.name for p in staged] == ["frame_0001.png", "frame_0002.png"]
    assert staged[0].read_bytes() == b"png1"
    _frames_staging.clear_batch(_BATCH)
    assert _frames_staging.list_staged_frames(_BATCH) == []


def test_stage_names_frames_zero_padded_for_ffmpeg_order():
    # index 10 must sort after index 2 (zero-pad, not lexical "10" < "2").
    for i in (1, 2, 10):
        _frames_staging.stage_frame_bytes(_BATCH, str(i), b"x")
    names = [p.name for p in _frames_staging.list_staged_frames(_BATCH)]
    assert names == ["frame_0001.png", "frame_0002.png", "frame_0010.png"]


def test_stage_rejects_batch_id_traversal():
    with pytest.raises(ValueError, match="invalid batch_id"):
        _frames_staging.stage_frame_bytes("../../evil", "1", b"x")


def test_stage_rejects_non_integer_index():
    with pytest.raises(ValueError, match="invalid frame index"):
        _frames_staging.stage_frame_bytes(_BATCH, "1/../../x", b"x")
    with pytest.raises(ValueError, match="invalid frame index"):
        _frames_staging.stage_frame_bytes(_BATCH, "abc", b"x")


# ---------------------------------------------------------------------
# The /upload/frames/<batch>/<index> endpoint handler
# ---------------------------------------------------------------------


def _req(
    batch_id: str,
    index: str,
    token: str,
    body: bytes,
    *,
    headers: dict | None = None,
) -> MagicMock:
    """A minimal Request stub. The endpoint reads body via request.stream()
    (chunk iterator) and consults request.headers for Content-Length."""
    req = MagicMock()
    req.path_params = {"batch_id": batch_id, "index": index}
    req.query_params = {"token": token}
    req.headers = headers if headers is not None else {}

    async def _stream():
        # Yield the body as a single chunk (the endpoint sums chunk lengths).
        yield body

    req.stream = _stream
    return req


def _call(req):
    from appscriptly.http_server.routes.convert import upload_frame_endpoint
    return asyncio.run(upload_frame_endpoint(req))


def test_endpoint_accepts_valid_token_and_stages():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    resp = _call(_req(_BATCH, "1", token, b"\x89PNGframebytes"))
    assert resp.status_code == 200
    staged = _frames_staging.list_staged_frames(_BATCH)
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"\x89PNGframebytes"


def test_endpoint_rejects_bad_token_403():
    resp = _call(_req(_BATCH, "1", "0.deadbeef", b"x"))
    assert resp.status_code == 403
    # Nothing staged on a rejected upload.
    assert _frames_staging.list_staged_frames(_BATCH) == []


def test_endpoint_rejects_replayed_cross_batch_token_403():
    """SECURITY (replay): a token bound to one batch, replayed against a
    different server-minted batch, is rejected at the endpoint (403) and
    stages nothing — the disk-fill-across-batches vector is closed."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    # First, a legit upload binds the nonce to _BATCH.
    assert _call(_req(_BATCH, "1", token, b"ok")).status_code == 200
    # Now forge a same-nonce token valid for _BATCH2 and replay it.
    import time
    _exp, nonce, _uidb, _sig = token.split(".", 3)
    exp2 = int(time.time()) + 600
    forged = (
        f"{exp2}.{nonce}.{_frames_staging._encode_uid('u')}."
        f"{_frames_staging._sig(_BATCH2, exp2, nonce, 'u')}"
    )
    resp = _call(_req(_BATCH2, "1", forged, b"evil"))
    assert resp.status_code == 403
    assert _frames_staging.list_staged_frames(_BATCH2) == []


def test_endpoint_rejects_oversize_declared_content_length_413():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    over = str(_frames_staging._MAX_FRAME_BYTES + 1)
    resp = _call(_req(_BATCH, "1", token, b"x", headers={"content-length": over}))
    assert resp.status_code == 413
    assert _frames_staging.list_staged_frames(_BATCH) == []


def test_endpoint_rejects_oversize_chunked_body_413():
    """The chunked / Content-Length-omitting case the body-size middleware
    lets fall through — the endpoint's streaming cap must still reject it."""
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    big = b"x" * (_frames_staging._MAX_FRAME_BYTES + 1)
    # No content-length header → middleware would pass it; endpoint caps.
    resp = _call(_req(_BATCH, "1", token, big, headers={}))
    assert resp.status_code == 413
    assert _frames_staging.list_staged_frames(_BATCH) == []


def test_endpoint_rejects_empty_body_400():
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    resp = _call(_req(_BATCH, "1", token, b""))
    assert resp.status_code == 400


def test_endpoint_rejects_traversal_index_400():
    # A valid token for the batch, but a malformed index → 400 (the staging
    # layer's ValueError), NOT a write outside the batch dir.
    token = _frames_staging.sign_frames_batch(_BATCH, user_id="u")
    resp = _call(_req(_BATCH, "../evil", token, b"x"))
    assert resp.status_code == 400
