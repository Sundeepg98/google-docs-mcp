"""Tests for services/apps_script/encode_video.py (PR-Δ12 + base-tier).

``as_encode_video`` is the ENCODE half of the slides-to-video pipeline —
server-side ffmpeg compute (NOT a bound-script generator).

Base-tier redesign: frames come from the server's signed staging area
(the bound render script POSTed them), keyed by ``frames_batch_id`` — NOT
from a Drive folder. The encode reads them off the volume (no
drive.readonly). The only Drive op is the MP4 upload, which the app
creates → drive.file. Staged frames are consumed after a successful encode.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from google_docs_mcp import decorators
from google_docs_mcp.services.apps_script import _frames_staging, encode_video
from google_docs_mcp.services.apps_script.encode_video import (
    _build_ffmpeg_cmd,
    _collect_staged_frames,
    _ffmpeg_preexec,
    as_encode_video,
)

_BATCH = "BATCHaaaaaaaaaaaaaaaa"


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds_and_staging(stub_creds, monkeypatch, tmp_path):
    """Scope-aware creds patch + point the frame-staging dir at a tmp /data."""
    from google_docs_mcp import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setenv("GOOGLE_DOCS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-bearer-token-32-characters-x")


def _stage(batch_id: str, n: int, size_each: int = 1000) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * max(0, size_each - 8)
    for i in range(1, n + 1):
        _frames_staging.stage_frame_bytes(batch_id, str(i), payload)


def _make_drive_stub() -> MagicMock:
    drive = MagicMock(name="drive-v3-stub")
    created: list[dict] = []

    def _create(**kwargs):
        new_id = f"VIDEO-{len(created) + 1}"
        created.append({"id": new_id, "kwargs": kwargs})
        resp = MagicMock()
        resp.execute.return_value = {"id": new_id, "name": kwargs["body"]["name"]}
        return resp

    drive.files().create.side_effect = _create
    drive.created = created
    return drive


@pytest.fixture
def mock_ffmpeg_success(monkeypatch):
    calls: list[list[str]] = []
    kwargs_seen: list[dict] = []

    def _run(cmd, **kwargs):  # noqa: ANN001
        calls.append(cmd)
        kwargs_seen.append(kwargs)
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(encode_video.subprocess, "run", _run)
    _run.calls = calls  # type: ignore[attr-defined]
    _run.kwargs = kwargs_seen  # type: ignore[attr-defined]
    return _run


# ---------------------------------------------------------------------
# Pure: _build_ffmpeg_cmd + _ffmpeg_preexec (unchanged by the redesign)
# ---------------------------------------------------------------------


def test_ffmpeg_cmd_has_input_framerate_and_pattern():
    cmd = _build_ffmpeg_cmd(Path("/tmp/work"), fps=2, output_path=Path("/tmp/work/out.mp4"))
    assert cmd.index("-framerate") < cmd.index("-i")
    assert cmd[cmd.index("-framerate") + 1] == "2"
    assert cmd[cmd.index("-i") + 1].endswith("%04d.png")


def test_ffmpeg_cmd_has_compat_flags():
    out = Path("/w/out.mp4")
    cmd = _build_ffmpeg_cmd(Path("/w"), fps=5, output_path=out)
    assert "libx264" in cmd
    assert "yuv420p" in cmd
    assert "+faststart" in cmd
    assert any("pad=ceil(iw/2)*2:ceil(ih/2)*2" in part for part in cmd)
    assert cmd[-1] == str(out)
    assert cmd[0] == "ffmpeg"


def test_ffmpeg_cmd_bounds_cpu_with_preset_and_threads():
    cmd = _build_ffmpeg_cmd(Path("/w"), fps=2, output_path=Path("/w/out.mp4"))
    assert cmd[cmd.index("-preset") + 1] == "veryfast"
    assert cmd[cmd.index("-threads") + 1] == "1"


def test_ffmpeg_preexec_returns_callable_or_none():
    result = _ffmpeg_preexec()
    if hasattr(encode_video.os, "nice"):
        assert callable(result)
    else:
        assert result is None


def test_ffmpeg_preexec_none_when_os_nice_absent(monkeypatch):
    monkeypatch.delattr(encode_video.os, "nice", raising=False)
    assert _ffmpeg_preexec() is None


# ---------------------------------------------------------------------
# _collect_staged_frames — reads staged frames, enforces bounds
# ---------------------------------------------------------------------


def test_collect_staged_frames_copies_in_render_order(tmp_path):
    _stage(_BATCH, 3)
    work = tmp_path / "work"
    work.mkdir()
    out = _collect_staged_frames(_BATCH, work)
    assert [p.name for p in out] == ["0000.png", "0001.png", "0002.png"]
    assert all(p.exists() for p in out)


def test_collect_staged_frames_raises_on_empty_batch(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="No staged frames"):
        _collect_staged_frames("BATCHemptyemptyempty", work)


def test_collect_staged_frames_enforces_max_frame_count(tmp_path):
    _stage(_BATCH, encode_video._MAX_FRAMES + 1)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="Too many frames"):
        _collect_staged_frames(_BATCH, work)


def test_collect_staged_frames_enforces_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(encode_video, "_MAX_TOTAL_FRAME_BYTES", 10)
    _stage(_BATCH, 5, size_each=8)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ValueError, match="total-size limit"):
        _collect_staged_frames(_BATCH, work)


# ---------------------------------------------------------------------
# Validation (no encode)
# ---------------------------------------------------------------------


def test_empty_batch_id_raises():
    with pytest.raises(ValueError, match="frames_batch_id cannot be empty"):
        as_encode_video(frames_batch_id="   ")


@pytest.mark.parametrize("bad_fps", [0, -1, 61, 1000])
def test_fps_out_of_range_raises(bad_fps):
    with pytest.raises(ValueError, match="fps must be between"):
        as_encode_video(frames_batch_id=_BATCH, fps=bad_fps)


def test_fps_bool_rejected():
    with pytest.raises(ValueError, match="fps must be an integer"):
        as_encode_video(frames_batch_id=_BATCH, fps=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Tool happy path — ffmpeg subprocess MOCKED
# ---------------------------------------------------------------------


def test_happy_path_encodes_and_uploads(mock_ffmpeg_success, monkeypatch):
    _stage(_BATCH, 3)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)

    result = as_encode_video(frames_batch_id=_BATCH, fps=2)

    assert result["video_file_id"] == "VIDEO-1"
    assert result["video_url"] == "https://drive.google.com/file/d/VIDEO-1/view"
    assert result["frame_count"] == 3
    assert result["duration_sec"] == 1.5
    assert result["fps"] == 2
    assert result["output_name"] == "appscriptly video.mp4"

    assert len(mock_ffmpeg_success.calls) == 1
    argv = mock_ffmpeg_success.calls[0]
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-framerate") + 1] == "2"
    assert argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-threads") + 1] == "1"
    assert "preexec_fn" in mock_ffmpeg_success.kwargs[0]


def test_mp4_uploaded_via_drive_file_with_no_parent_and_no_read(
    mock_ffmpeg_success, monkeypatch
):
    """MP4 upload uses files().create with NO 'parents' (app-created, lands
    in My Drive root → drive.file suffices), and makes NO Drive read —
    files().list / get_media (which needed drive.readonly) must NOT fire."""
    _stage(_BATCH, 2)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)

    as_encode_video(frames_batch_id=_BATCH, fps=1)

    drive.files.return_value.create.assert_called_once()
    drive.files.return_value.list.assert_not_called()
    drive.files.return_value.get_media.assert_not_called()
    body = drive.created[0]["kwargs"]["body"]
    assert "parents" not in body
    assert body["mimeType"] == "video/mp4"


def test_staged_frames_consumed_after_success(mock_ffmpeg_success, monkeypatch):
    _stage(_BATCH, 2)
    assert len(_frames_staging.list_staged_frames(_BATCH)) == 2
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    as_encode_video(frames_batch_id=_BATCH, fps=1)
    assert _frames_staging.list_staged_frames(_BATCH) == []


def test_output_name_override_appends_mp4(mock_ffmpeg_success, monkeypatch):
    _stage(_BATCH, 1)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    result = as_encode_video(frames_batch_id=_BATCH, fps=1, output_name="my deck")
    assert result["output_name"] == "my deck.mp4"


def test_temp_dir_cleaned_up_after_success(mock_ffmpeg_success, monkeypatch):
    _stage(_BATCH, 1)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    created_dirs: list[str] = []
    real_mkdtemp = encode_video.tempfile.mkdtemp

    def _track(*a, **k):
        d = real_mkdtemp(*a, **k)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(encode_video.tempfile, "mkdtemp", _track)
    as_encode_video(frames_batch_id=_BATCH, fps=1)
    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "temp dir leaked after success"


# ---------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------


def test_no_staged_frames_raises(monkeypatch):
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    with pytest.raises(ValueError, match="No staged frames"):
        as_encode_video(frames_batch_id="BATCHneverrenderedxx", fps=2)


def test_within_byte_budget_encodes_normally(mock_ffmpeg_success, monkeypatch):
    _stage(_BATCH, 2, size_each=1000)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    result = as_encode_video(frames_batch_id=_BATCH, fps=2)
    assert result["frame_count"] == 2
    assert result["video_file_id"] == "VIDEO-1"


def test_new_max_frames_is_lowered_to_200():
    assert encode_video._MAX_FRAMES == 200


def test_ffmpeg_nonzero_exit_raises(monkeypatch):
    _stage(_BATCH, 1)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)

    def _run_fail(cmd, **kwargs):  # noqa: ANN001
        result = MagicMock()
        result.returncode = 1
        result.stderr = "line1\nx264: bad frame\n"
        return result

    monkeypatch.setattr(encode_video.subprocess, "run", _run_fail)
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        as_encode_video(frames_batch_id=_BATCH, fps=2)


def test_ffmpeg_failure_still_cleans_temp_dir(monkeypatch):
    _stage(_BATCH, 1)
    drive = _make_drive_stub()
    monkeypatch.setattr(encode_video, "get_service", lambda *a, **k: drive)
    created_dirs: list[str] = []
    real_mkdtemp = encode_video.tempfile.mkdtemp

    def _track(*a, **k):
        d = real_mkdtemp(*a, **k)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(encode_video.tempfile, "mkdtemp", _track)
    monkeypatch.setattr(
        encode_video.subprocess, "run",
        lambda *a, **k: MagicMock(returncode=1, stderr="boom"),
    )
    with pytest.raises(RuntimeError):
        as_encode_video(frames_batch_id=_BATCH, fps=2)
    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "temp dir leaked after ffmpeg failure"
