"""Tests for the base-tier slides→video frame-staging handoff.

Covers ``services/apps_script/_frames_staging`` (sign/verify/stage/list/
clear, path-traversal guards) AND the ``/upload/frames/<batch>/<index>``
endpoint handler (``http_server.routes.convert.upload_frame_endpoint``).
This is the mechanism that replaced the ``drive.readonly`` Drive
round-trip: the bound render script POSTs PNGs here, authed by an
HMAC batch token; ``as_encode_video`` reads them off the volume.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from appscriptly.services.apps_script import _frames_staging

_BATCH = "BATCHaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _staging_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-bearer-token-32-characters-x")


# ---------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------


def test_sign_then_verify_roundtrip():
    token = _frames_staging.sign_frames_batch(_BATCH)
    assert _frames_staging.verify_frames_token(_BATCH, token) is True


def test_verify_rejects_tampered_signature():
    token = _frames_staging.sign_frames_batch(_BATCH)
    expiry = token.split(".", 1)[0]
    assert _frames_staging.verify_frames_token(_BATCH, f"{expiry}.deadbeef") is False


def test_verify_rejects_wrong_batch_id():
    token = _frames_staging.sign_frames_batch(_BATCH)
    assert _frames_staging.verify_frames_token("BATCHbbbbbbbbbbbbbbbb", token) is False


def test_verify_rejects_expired_token():
    token = _frames_staging.sign_frames_batch(_BATCH, ttl_seconds=-1)
    assert _frames_staging.verify_frames_token(_BATCH, token) is False


def test_verify_rejects_malformed_token():
    assert _frames_staging.verify_frames_token(_BATCH, "garbage") is False
    assert _frames_staging.verify_frames_token(_BATCH, "") is False


def test_new_batch_id_is_path_safe_and_passes_regex():
    bid = _frames_staging.new_batch_id()
    assert _frames_staging._BATCH_ID_RE.match(bid)
    assert "/" not in bid and "\\" not in bid and ".." not in bid


def test_sign_rejects_bad_batch_id():
    with pytest.raises(ValueError, match="invalid batch_id"):
        _frames_staging.sign_frames_batch("../etc/passwd")


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


def _req(batch_id: str, index: str, token: str, body: bytes) -> MagicMock:
    req = MagicMock()
    req.path_params = {"batch_id": batch_id, "index": index}
    req.query_params = {"token": token}

    async def _body():
        return body

    req.body = _body
    return req


def _call(req):
    from appscriptly.http_server.routes.convert import upload_frame_endpoint
    return asyncio.run(upload_frame_endpoint(req))


def test_endpoint_accepts_valid_token_and_stages():
    token = _frames_staging.sign_frames_batch(_BATCH)
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


def test_endpoint_rejects_empty_body_400():
    token = _frames_staging.sign_frames_batch(_BATCH)
    resp = _call(_req(_BATCH, "1", token, b""))
    assert resp.status_code == 400


def test_endpoint_rejects_traversal_index_400():
    # A valid token for the batch, but a malformed index → 400 (the staging
    # layer's ValueError), NOT a write outside the batch dir.
    token = _frames_staging.sign_frames_batch(_BATCH)
    resp = _call(_req(_BATCH, "../evil", token, b"x"))
    assert resp.status_code == 400
