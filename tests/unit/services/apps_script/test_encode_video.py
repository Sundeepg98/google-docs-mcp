"""Tests for services/apps_script/encode_video.py (PR-Δ12).

``as_encode_video`` is the ENCODE half of the slides-to-video pipeline —
server-side ffmpeg compute (NOT a bound-script generator). Coverage:

  * **Pure ffmpeg command builder** (``_build_ffmpeg_cmd``) — fps, input
    pattern, libx264 / yuv420p / faststart / pad-to-even flags, output path.
  * **Manifest parsing** (``_parse_manifest``) — valid shape, and every
    malformed-shape ValueError (bad JSON, not-object, no frames, non-string
    frames).
  * **Tool happy path** — end-to-end at the ``@workspace_tool(creds=True,
    scopes=...)`` boundary via ``InMemoryGoogleAPIClient``, with
    ``subprocess.run`` MOCKED (we never invoke real ffmpeg in unit tests).
    Asserts: manifest order respected, frames downloaded + re-numbered, the
    correct ffmpeg argv built, the MP4 uploaded, the return envelope shape,
    and the temp dir cleaned up.
  * **Validation + error paths** — empty folder id, bad fps (range / type),
    missing manifest, manifest-listed frame absent from folder, frame-count
    cap, ffmpeg non-zero exit → RuntimeError → ToolError.

Fixture pattern mirrors test_video_deck.py: the tool DECLARES
``scopes=AS_ENCODE_VIDEO_SCOPES`` so the decorator takes the SCOPE-AWARE
credential path (``auth.load_credentials`` in stdio test mode) — patch THAT.
The Drive interactions go through ``InMemoryGoogleAPIClient``; ffmpeg is
patched at ``subprocess.run`` so no binary is needed in the test env.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from google_docs_mcp import decorators
from google_docs_mcp.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from google_docs_mcp.services.apps_script import encode_video
from google_docs_mcp.services.apps_script.encode_video import (
    _build_ffmpeg_cmd,
    _parse_manifest,
    as_encode_video,
)


# ---------------------------------------------------------------------
# Fixtures — scope-aware creds (same shape as test_video_deck.py)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """This tool declares scopes, so resolution flows through
    ``auth.load_credentials`` in stdio test mode — patch that, plus the
    no-scope fallback for belt-and-suspenders."""
    from google_docs_mcp import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_drive_stub(*, files: list[dict], media: dict[str, bytes]) -> MagicMock:
    """Drive v3 stub.

    ``files``: what files().list().execute() returns (the folder listing).
    ``media``: file_id → bytes, served by get_media (via the chunked
    MediaIoBaseDownload path the tool uses).
    Records created files on ``.created`` for upload assertions.
    """
    drive = MagicMock(name="drive-v3-stub")
    drive.files().list().execute.return_value = {"files": files}

    # get_media(fileId=...) → a request object MediaIoBaseDownload drives.
    # We bypass the real chunked download by making next_chunk write the
    # bytes in one shot. Simplest: patch MediaIoBaseDownload in the tool
    # module (done in the test that needs real bytes). For the stub here,
    # we attach the bytes map so that patch can resolve them.
    drive._media = media  # consumed by the patched downloader

    created_files: list[dict] = []

    def _create(**kwargs):
        body = kwargs.get("body", {})
        name = body.get("name", "out.mp4")
        new_id = f"VIDEO-{len(created_files) + 1}"
        created_files.append({"id": new_id, "name": name, "kwargs": kwargs})
        resp = MagicMock()
        resp.execute.return_value = {"id": new_id, "name": name}
        return resp

    drive.files().create.side_effect = _create
    drive.created = created_files
    return drive


@pytest.fixture
def patch_download(monkeypatch):
    """Patch the tool's MediaIoBaseDownload so get_media resolves bytes
    from the drive stub's ``_media`` map without a real HTTP chunk loop.

    The tool calls ``MediaIoBaseDownload(buf, request)`` then loops
    ``next_chunk()``. We replace it with a fake that writes the mapped
    bytes into ``buf`` once and reports done. The ``request`` carries the
    fileId via the stub's get_media mock call args; we thread the bytes
    through a module-level closure instead (simpler + deterministic)."""
    # The drive stub's get_media is a MagicMock; we make it return an
    # object that remembers which fileId was asked for.
    holder: dict[str, bytes] = {}

    class _FakeDownloader:
        def __init__(self, buf, request):
            self._buf = buf
            self._bytes = request._fetch_bytes()

        def next_chunk(self):
            self._buf.write(self._bytes)
            return (None, True)

    monkeypatch.setattr(encode_video, "MediaIoBaseDownload", _FakeDownloader)
    return holder


def _wire_get_media(drive: MagicMock) -> None:
    """Make drive.files().get_media(fileId=X) return a request whose
    ``_fetch_bytes()`` yields the stub's mapped bytes for X."""
    media = drive._media

    def _get_media(*, fileId):  # noqa: N803 — Google SDK kwarg name
        req = MagicMock(name=f"get_media({fileId})")
        req._fetch_bytes = lambda: media[fileId]
        return req

    drive.files().get_media.side_effect = _get_media


def _manifest_bytes(frames: list[str]) -> bytes:
    return json.dumps(
        {
            "presentationId": "DECK1",
            "framePrefix": "frame",
            "frameCount": len(frames),
            "frames": frames,
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------
# Pure: _build_ffmpeg_cmd
# ---------------------------------------------------------------------


def test_ffmpeg_cmd_has_input_framerate_and_pattern():
    cmd = _build_ffmpeg_cmd(Path("/tmp/work"), fps=2, output_path=Path("/tmp/work/out.mp4"))
    # -framerate must precede -i (input rate, not output rate).
    fr_idx = cmd.index("-framerate")
    i_idx = cmd.index("-i")
    assert fr_idx < i_idx
    assert cmd[fr_idx + 1] == "2"
    # input pattern is the zero-padded sequence we re-number frames to.
    assert cmd[i_idx + 1].endswith("%04d.png")


def test_ffmpeg_cmd_has_compat_flags():
    out = Path("/w/out.mp4")
    cmd = _build_ffmpeg_cmd(Path("/w"), fps=5, output_path=out)
    assert "libx264" in cmd
    assert "yuv420p" in cmd  # broad-player pixel format
    assert "+faststart" in cmd  # streamable moov atom
    # pad-to-even filter (libx264 + yuv420p needs even dims).
    assert any("pad=ceil(iw/2)*2:ceil(ih/2)*2" in part for part in cmd)
    # Output path is the OS-native string of the Path (\\ on Windows).
    assert cmd[-1] == str(out)
    assert cmd[0] == "ffmpeg"


def test_ffmpeg_cmd_fps_threaded():
    cmd = _build_ffmpeg_cmd(Path("/w"), fps=30, output_path=Path("/w/o.mp4"))
    assert cmd[cmd.index("-framerate") + 1] == "30"


# ---------------------------------------------------------------------
# Pure: _parse_manifest
# ---------------------------------------------------------------------


def test_parse_manifest_returns_frames_in_order():
    raw = _manifest_bytes(["frame_001.png", "frame_002.png", "frame_003.png"])
    assert _parse_manifest(raw) == ["frame_001.png", "frame_002.png", "frame_003.png"]


def test_parse_manifest_rejects_bad_json():
    with pytest.raises(ValueError, match="not valid UTF-8 JSON"):
        _parse_manifest(b"{not json")


def test_parse_manifest_rejects_non_object():
    with pytest.raises(ValueError, match="not a JSON object"):
        _parse_manifest(b"[1, 2, 3]")


def test_parse_manifest_rejects_missing_frames():
    with pytest.raises(ValueError, match="no non-empty 'frames'"):
        _parse_manifest(json.dumps({"frameCount": 0}).encode("utf-8"))


def test_parse_manifest_rejects_empty_frames():
    with pytest.raises(ValueError, match="no non-empty 'frames'"):
        _parse_manifest(json.dumps({"frames": []}).encode("utf-8"))


def test_parse_manifest_rejects_non_string_frames():
    with pytest.raises(ValueError, match="non-empty filename strings"):
        _parse_manifest(json.dumps({"frames": ["ok.png", 42]}).encode("utf-8"))


# ---------------------------------------------------------------------
# Validation (no API call)
# ---------------------------------------------------------------------


# NOTE on exception types: the @workspace_tool(creds=True) envelope
# translates ONLY HttpError → ToolError (see decorators.py). Client-side
# ValueError validation + the ffmpeg RuntimeError propagate as their own
# types — exactly the convention the sibling video_deck tests assert
# (ValueError for validation; ToolError only for the HttpError path).


def test_empty_folder_id_raises():
    with pytest.raises(ValueError, match="frames_folder_id cannot be empty"):
        as_encode_video(frames_folder_id="   ")


@pytest.mark.parametrize("bad_fps", [0, -1, 61, 1000])
def test_fps_out_of_range_raises(bad_fps):
    with pytest.raises(ValueError, match="fps must be between"):
        as_encode_video(frames_folder_id="FOLDER1", fps=bad_fps)


def test_fps_bool_rejected():
    # bool is an int subclass — guard explicitly (True would pass a naive
    # range check as 1).
    with pytest.raises(ValueError, match="fps must be an integer"):
        as_encode_video(frames_folder_id="FOLDER1", fps=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Tool happy path — ffmpeg subprocess MOCKED
# ---------------------------------------------------------------------


@pytest.fixture
def mock_ffmpeg_success(monkeypatch):
    """Patch subprocess.run to simulate a successful ffmpeg + write a
    non-empty out.mp4 so the post-encode existence check passes.

    Records the argv it was called with on ``.calls`` for assertions."""
    calls: list[list[str]] = []

    def _run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls.append(cmd)
        # cmd[-1] is the output path; create a non-empty file there so
        # the tool's "ffmpeg produced output" check passes.
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes")
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    monkeypatch.setattr(encode_video.subprocess, "run", _run)
    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def test_happy_path_encodes_and_uploads(mock_ffmpeg_success, patch_download):
    """End-to-end: list folder → read manifest → download frames →
    ffmpeg (mocked) → upload MP4 → return envelope."""
    frames = ["frame_001.png", "frame_002.png", "frame_003.png"]
    files = [
        {"id": "MANIFEST", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
        {"id": "F2", "name": "frame_002.png", "mimeType": "image/png"},
        {"id": "F3", "name": "frame_003.png", "mimeType": "image/png"},
    ]
    media = {
        "MANIFEST": _manifest_bytes(frames),
        "F1": b"png-1-bytes",
        "F2": b"png-2-bytes",
        "F3": b"png-3-bytes",
    }
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = as_encode_video(frames_folder_id="FOLDER1", fps=2)

    # Envelope shape.
    assert result["video_file_id"] == "VIDEO-1"
    assert result["video_url"] == "https://drive.google.com/file/d/VIDEO-1/view"
    assert result["frame_count"] == 3
    assert result["duration_sec"] == 1.5  # 3 frames / 2 fps
    assert result["fps"] == 2
    assert result["output_name"] == "appscriptly video.mp4"

    # ffmpeg was invoked once with our argv (fps + libx264).
    assert len(mock_ffmpeg_success.calls) == 1
    argv = mock_ffmpeg_success.calls[0]
    assert argv[0] == "ffmpeg"
    assert "libx264" in argv
    assert argv[argv.index("-framerate") + 1] == "2"

    # The MP4 was uploaded with the right mimeType.
    assert len(drive.created) == 1
    assert drive.created[0]["kwargs"]["body"]["mimeType"] == "video/mp4"


def test_happy_path_respects_manifest_order_not_filename_sort(
    mock_ffmpeg_success, patch_download, monkeypatch
):
    """Frames are re-numbered 0000.png… in MANIFEST order, decoupled from
    on-Drive names. Verify by capturing what bytes land in which numbered
    temp file: a manifest that lists frames in REVERSE filename order must
    produce 0000.png = the manifest's first entry's bytes."""
    # Manifest lists c, a, b (NOT sorted) — encode must follow this order.
    frames = ["c.png", "a.png", "b.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "A", "name": "a.png", "mimeType": "image/png"},
        {"id": "B", "name": "b.png", "mimeType": "image/png"},
        {"id": "C", "name": "c.png", "mimeType": "image/png"},
    ]
    media = {
        "M": _manifest_bytes(frames),
        "A": b"AAAA",
        "B": b"BBBB",
        "C": b"CCCC",
    }
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    # Capture the numbered files written into the work dir.
    written: dict[str, bytes] = {}
    real_write_bytes = Path.write_bytes

    def _capture_write(self, data):  # noqa: ANN001
        if self.name.endswith(".png") and self.name[0].isdigit():
            written[self.name] = data
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _capture_write)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        as_encode_video(frames_folder_id="FOLDER1", fps=1)

    # 0000 = manifest[0] = c.png = CCCC; 0001 = a.png = AAAA; 0002 = b = BBBB.
    assert written["0000.png"] == b"CCCC"
    assert written["0001.png"] == b"AAAA"
    assert written["0002.png"] == b"BBBB"


def test_output_name_override_appends_mp4(mock_ffmpeg_success, patch_download):
    frames = ["frame_001.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
    ]
    media = {"M": _manifest_bytes(frames), "F1": b"x"}
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        result = as_encode_video(
            frames_folder_id="FOLDER1", fps=1, output_name="my deck"
        )
    assert result["output_name"] == "my deck.mp4"


def test_temp_dir_cleaned_up_after_success(mock_ffmpeg_success, patch_download, monkeypatch):
    """The per-request temp dir must be removed after a successful encode."""
    created_dirs: list[str] = []
    real_mkdtemp = encode_video.tempfile.mkdtemp

    def _track_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(encode_video.tempfile, "mkdtemp", _track_mkdtemp)

    frames = ["frame_001.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
    ]
    media = {"M": _manifest_bytes(frames), "F1": b"x"}
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        as_encode_video(frames_folder_id="FOLDER1", fps=1)

    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "temp dir leaked after success"


# ---------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------


def test_missing_manifest_raises(patch_download):
    """Folder with frames but no manifest.json → clear ValueError."""
    files = [{"id": "F1", "name": "frame_001.png", "mimeType": "image/png"}]
    drive = _make_drive_stub(files=files, media={"F1": b"x"})
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(ValueError, match="No manifest.json found"):
            as_encode_video(frames_folder_id="FOLDER1", fps=2)


def test_manifest_frame_missing_from_folder_raises(mock_ffmpeg_success, patch_download):
    """Manifest names a frame that isn't in the folder → ValueError."""
    frames = ["frame_001.png", "frame_002.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
        # frame_002.png deliberately ABSENT.
    ]
    media = {"M": _manifest_bytes(frames), "F1": b"x"}
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(ValueError, match="frame_002.png.*is missing"):
            as_encode_video(frames_folder_id="FOLDER1", fps=2)


def test_frame_count_cap_raises(patch_download):
    """A manifest claiming > _MAX_FRAMES frames is rejected pre-download."""
    too_many = [f"f_{i}.png" for i in range(encode_video._MAX_FRAMES + 1)]
    files = [{"id": "M", "name": "manifest.json", "mimeType": "application/json"}]
    drive = _make_drive_stub(files=files, media={"M": _manifest_bytes(too_many)})
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(ValueError, match="exceeding the .* safety cap"):
            as_encode_video(frames_folder_id="FOLDER1", fps=2)


def test_ffmpeg_nonzero_exit_raises_toolerror(patch_download, monkeypatch):
    """ffmpeg exit != 0 → RuntimeError → ToolError (with stderr tail)."""
    def _run_fail(cmd, capture_output, text, timeout):  # noqa: ANN001
        result = MagicMock()
        result.returncode = 1
        result.stderr = "line1\nline2\nx264: bad frame\n"
        return result

    monkeypatch.setattr(encode_video.subprocess, "run", _run_fail)

    frames = ["frame_001.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
    ]
    media = {"M": _manifest_bytes(frames), "F1": b"x"}
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            as_encode_video(frames_folder_id="FOLDER1", fps=2)


def test_ffmpeg_failure_still_cleans_temp_dir(patch_download, monkeypatch):
    """Even on ffmpeg failure, the temp dir is removed (finally block)."""
    created_dirs: list[str] = []
    real_mkdtemp = encode_video.tempfile.mkdtemp

    def _track_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(encode_video.tempfile, "mkdtemp", _track_mkdtemp)
    monkeypatch.setattr(
        encode_video.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=1, stderr="boom"),
    )

    frames = ["frame_001.png"]
    files = [
        {"id": "M", "name": "manifest.json", "mimeType": "application/json"},
        {"id": "F1", "name": "frame_001.png", "mimeType": "image/png"},
    ]
    media = {"M": _manifest_bytes(frames), "F1": b"x"}
    drive = _make_drive_stub(files=files, media=media)
    _wire_get_media(drive)

    with with_google_api_client(InMemoryGoogleAPIClient({("drive", "v3"): drive})):
        with pytest.raises(RuntimeError):
            as_encode_video(frames_folder_id="FOLDER1", fps=2)

    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "temp dir leaked after ffmpeg failure"
