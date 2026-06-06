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

  POST a frame  ->  the handler accepts + persists it
                ->  as_encode_video reads it off the volume + encodes.

Isolation note: the staging directory comes from ``default_data_dir()``,
which reads ``GOOGLE_DOCS_DATA_DIR`` from the PROCESS env at call time.
Other integration test files mutate that env in their own fixtures, so a
post-hoc ``list_staged_frames`` read from test code is order-dependent
across the full suite. We therefore assert on the HTTP RESPONSE (which
the handler returns only AFTER writing the frame, inside the request,
while this fixture's env is active) — deterministic regardless of suite
order. The end-to-end test proves the read-back path by having
``as_encode_video`` consume exactly the POSTed frames within one env
scope.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from appscriptly.http_server.app import build_app
from appscriptly.services.apps_script import _frames_staging

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"


@pytest.fixture
def batch() -> str:
    """A fresh, unique, regex-passing batch id per test."""
    return _frames_staging.new_batch_id()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """The REAL Starlette app (build_app) with a throwaway MCP + env.

    Host allowlist: ``derive_trusted_hosts()`` reads ``TRUSTED_HOSTS``
    (priority 1) else derives from ``FLY_APP_NAME``. CI sets
    ``FLY_APP_NAME=ci-test-app``, which would build a restricted allowlist
    and make TrustedHostMiddleware reject TestClient's default
    ``Host: testserver`` with 400 BEFORE the request reaches our route
    (masking the real route assertion). So we pin ``TRUSTED_HOSTS`` to the
    TestClient host AND clear ``FLY_APP_NAME`` — deterministic regardless
    of the CI/local environment.
    """
    monkeypatch.setenv("MCP_BEARER_TOKEN", "x" * 40)
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://t.fly.dev")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS_JSON",
        '{"web": {"client_id": "x", "client_secret": "y"}}',
    )
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    # TestClient's default base URL is http://testserver -> Host: testserver.
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_REGION", raising=False)
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
    """Leg 1: a POSTed frame is accepted + persisted by the handler.

    Asserts on the HTTP response (the handler returns the staged byte
    count only after ``stage_frame_bytes`` has written the file inside the
    request) rather than re-reading the global staging dir — see the
    module docstring's isolation note.
    """
    token = _frames_staging.sign_frames_batch(batch)
    resp = client.post(
        f"/upload/frames/{batch}/1?token={token}",
        content=_PNG,
        headers={"content-type": "image/png"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"] == batch
    assert body["index"] == "1"
    assert body["bytes"] == len(_PNG)


def test_bad_token_rejected_403_on_real_route(client, batch):
    """The real route enforces the HMAC token: 403, specifically NOT 404
    (which would mean the route isn't registered and the catch-all
    answered) and NOT 200 (which would mean a bad token was accepted)."""
    resp = client.post(
        f"/upload/frames/{batch}/1?token=0.deadbeef",
        content=_PNG,
        headers={"content-type": "image/png"},
    )
    assert resp.status_code == 403, resp.text


def test_full_handoff_post_then_encode_reads_the_frames(client, batch, monkeypatch):
    """END-TO-END: POST frames over the real route -> as_encode_video reads
    them off the volume + encodes (ffmpeg + Drive mocked).

    This is the whole point of the base-tier redesign — the encode half
    consumes exactly what the renderFrames() POSTs deliver, with NO Drive
    read. If the route weren't wired, leg 1 would 404 and there'd be
    nothing for the encode to read; ``frame_count == 3`` proves the
    POST->stage->read chain end to end within one env scope.
    """
    from pathlib import Path

    from appscriptly.services.apps_script import encode_video

    # Leg 1: POST 3 frames over the REAL route. Each 200 proves the route
    # accepted + staged the frame (the response is returned post-write).
    token = _frames_staging.sign_frames_batch(batch)
    for i in (1, 2, 3):
        r = client.post(
            f"/upload/frames/{batch}/{i}?token={token}",
            content=_PNG + bytes([i]),
            headers={"content-type": "image/png"},
        )
        assert r.status_code == 200, r.text

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

    # Call the UNDECORATED function: as_encode_video is a
    # @workspace_tool(creds=True) whose envelope runs the real credential
    # resolver (needs OAuth client config CI doesn't have) BEFORE the body,
    # ignoring our creds= arg. We test the staged-frame read + encode here,
    # not cred resolution (covered elsewhere), so reach through __wrapped__.
    raw_encode = getattr(
        encode_video.as_encode_video, "__wrapped__", encode_video.as_encode_video
    )
    result = raw_encode(creds=MagicMock(), frames_batch_id=batch, fps=2)

    # The encode consumed exactly the 3 POSTed frames -> proves the handoff
    # (POST over the real route -> staged -> read back by the encode half).
    assert result["frame_count"] == 3
    assert result["video_file_id"] == "VIDEO-1"
    # NO Drive read anywhere (the drive.readonly drop's whole premise).
    drive.files.return_value.list.assert_not_called()
    drive.files.return_value.get_media.assert_not_called()
