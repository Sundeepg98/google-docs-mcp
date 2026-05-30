"""Integration test for the base-tier slides->video frame handoff over the
REAL HTTP route table.

WHY THIS EXISTS (regression guard): the bound ``renderFrames()`` script
POSTs each rendered PNG to ``POST /upload/frames/<batch_id>/<index>``.
``test_frames_staging.py`` exercises the *handler function*
(``upload_frame_endpoint``) directly with a mock request — which passes
even if the route is NOT registered in ``http_server/app.py``'s route
table. That exact gap shipped once: the handler + staging module landed
but the ``Route(...)`` registration did not, so the real POST 404'd
through the FastMCP ``Mount("/")`` catch-all and the whole handoff (the
mechanism that justified dropping ``drive.readonly``) was broken in prod
while CI stayed green.

These tests drive the REAL Starlette app built by ``build_app`` via a
``TestClient`` — the same route table prod serves — so a missing/mis-
ordered route fails LOUD here:

  POST a frame  ->  it lands in the signed staging area
                ->  as_encode_video reads it off the volume + encodes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from google_docs_mcp.http_server.app import build_app
from google_docs_mcp.services.apps_script import _frames_staging

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"


@pytest.fixture
def batch() -> str:
    """A fresh, unique, regex-passing batch id per test.

    Per-test-unique so the staging area can't cross-contaminate between
    tests regardless of how ``default_data_dir()`` resolves/caches the
    GOOGLE_DOCS_DATA_DIR path (the negative '403 stages nothing'
    assertion must not be fooled by a frame another test left behind).
    """
    return _frames_staging.new_batch_id()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """The REAL Starlette app (build_app) with a throwaway MCP + env.

    GOOGLE_DOCS_DATA_DIR points the frame-staging area at tmp_path; the
    MCP_BEARER_TOKEN seeds the signed_url HMAC key the route + the token
    minter both use.
    """
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 40)
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://t.fly.dev")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS_JSON",
        '{"web": {"client_id": "x", "client_secret": "y"}}',
    )
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_TRUSTED_HOSTS", "*")
    mcp = FastMCP("test")
    app = build_app(mcp)
    return TestClient(app, raise_server_exceptions=True)


def test_frame_upload_route_is_registered_and_accepts_a_signed_post(client, batch):
    """The REAL route accepts a token-authed PNG POST with 200 — NOT a 404.

    A 404 here is the precise regression: the route falling through to the
    FastMCP Mount('/') catch-all because it wasn't registered.
    """
    token = _frames_staging.sign_frames_batch(batch)
    resp = client.post(
        f"/upload/frames/{batch}/1?token={token}",
        content=_PNG,
        headers={"content-type": "image/png"},
    )
    assert resp.status_code != 404, (
        "POST /upload/frames/... 404'd — the route is NOT registered in the "
        "real app route table (the shipped regression). The bound "
        "renderFrames() script POSTs here; a 404 breaks the whole "
        "slides->video handoff."
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"] == batch
    assert body["bytes"] == len(_PNG)


def test_posted_frame_lands_in_staging(client, batch):
    """End of leg 1: a POSTed frame is persisted in the signed staging area
    (so the encode half can later read it)."""
    token = _frames_staging.sign_frames_batch(batch)
    client.post(
        f"/upload/frames/{batch}/1?token={token}",
        content=_PNG,
        headers={"content-type": "image/png"},
    )
    staged = _frames_staging.list_staged_frames(batch)
    assert len(staged) == 1
    assert staged[0].read_bytes() == _PNG


def test_bad_token_rejected_403_on_real_route(client, batch):
    """The real route enforces the HMAC token (403, not 404, not 200)."""
    resp = client.post(
        f"/upload/frames/{batch}/1?token=0.deadbeef",
        content=_PNG,
        headers={"content-type": "image/png"},
    )
    # 403 (token rejected by the handler), specifically NOT 404 (which
    # would mean the route isn't registered and the catch-all answered).
    assert resp.status_code == 403, resp.text
    # This batch (unique to this test) staged nothing — the reject worked.
    assert _frames_staging.list_staged_frames(batch) == []


def test_full_handoff_post_then_encode_reads_the_frames(client, batch, monkeypatch):
    """END-TO-END: POST frames over the real route -> as_encode_video reads
    them off the volume + encodes (ffmpeg + Drive mocked).

    This is the whole point of the base-tier redesign — the encode half
    consumes exactly what the renderFrames() POSTs deliver, with NO Drive
    read. If the route weren't wired, leg 1 would 404 and there'd be
    nothing for the encode to read.
    """
    from pathlib import Path

    from google_docs_mcp.services.apps_script import encode_video

    # Leg 1: POST 3 frames over the REAL route.
    token = _frames_staging.sign_frames_batch(batch)
    for i in (1, 2, 3):
        r = client.post(
            f"/upload/frames/{batch}/{i}?token={token}",
            content=_PNG + bytes([i]),
            headers={"content-type": "image/png"},
        )
        assert r.status_code == 200, r.text
    assert len(_frames_staging.list_staged_frames(batch)) == 3

    # Leg 2: as_encode_video reads the staged frames + encodes (mock ffmpeg
    # so no binary is needed; mock Drive so the MP4 "upload" succeeds).
    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        res = MagicMock()
        res.returncode = 0
        res.stderr = ""
        return res

    monkeypatch.setattr(encode_video.subprocess, "run", _fake_run)

    drive = MagicMock(name="drive-v3")
    created: list[dict] = []

    def _create(**kw):
        new_id = f"VIDEO-{len(created) + 1}"
        created.append(kw)
        resp = MagicMock()
        resp.execute.return_value = {"id": new_id, "name": kw["body"]["name"]}
        return resp

    drive.files().create.side_effect = _create
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)

    result = encode_video.as_encode_video(
        creds=MagicMock(), frames_batch_id=batch, fps=2
    )

    # The encode consumed exactly the 3 POSTed frames -> proves the handoff.
    assert result["frame_count"] == 3
    assert result["video_file_id"] == "VIDEO-1"
    # NO Drive read anywhere (the drive.readonly drop's whole premise).
    drive.files.return_value.list.assert_not_called()
    drive.files.return_value.get_media.assert_not_called()
    # Frames consumed after a successful encode.
    assert _frames_staging.list_staged_frames(batch) == []
