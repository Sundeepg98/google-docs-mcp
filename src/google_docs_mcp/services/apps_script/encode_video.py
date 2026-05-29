"""``as_encode_video`` — stitch rendered slide PNG frames into an MP4 (PR-Δ12).

The ENCODE half of the slides-to-video pipeline. Where ``as_generate_video_deck``
(PR-Δ11) deploys a bound Apps Script that renders each slide to a PNG frame in
a Drive folder + writes ``manifest.json``, THIS tool does the server-side
compute the render half deliberately left out:

  1. **Read** the ``manifest.json`` + the ordered PNG frames from the Drive
     folder the render step produced.
  2. **Encode** them into an H.264 MP4 with ffmpeg, running on OUR server.
  3. **Upload** the resulting MP4 back to the user's Drive + return its file
     ID + link.

**This is NOT a bound-script generator.** Every other tool in
``services/apps_script/`` deploys an Apps Script that runs on Google's
infrastructure. This one is the odd sibling: it's server-side compute (ffmpeg
on our Fly machine). It lives in the apps_script package anyway because it's
the second half of the apps_script-owned slides-to-video pipeline — keeping
the two halves co-located (``video_deck.py`` + ``encode_video.py``) is clearer
than scattering a 2-tool feature across two services. The
``@workspace_tool(service="apps_script")`` annotation reflects that grouping.

**The access path — why drive.readonly is load-bearing.** The frames live in
the user's Drive, but they were created by the BOUND render-script (running as
the user), NOT by our app. Our app's ``drive.file`` scope only sees files our
app created, so it cannot read those frames. ``drive.readonly`` (in the
baseline ``auth.SCOPES`` grant since PR #125) is what lets us read the
manifest + PNGs. The MP4 we then create IS our app's file, so ``drive.file``
covers the upload. This is the concrete, load-bearing reason ``drive.readonly``
was kept in the baseline scope set.

**Manifest-driven frame order.** We do NOT trust filename sort. The render
step writes ``manifest.json`` with a ``frames`` array in slide order; we honor
that order exactly. (A deck whose frames sort differently from slide order —
e.g. a custom ``frame_prefix`` that interleaves — would otherwise produce an
out-of-order video.) ffmpeg's ``-i frame_%03d.png`` pattern needs the temp
files numbered in playback order, so we re-number the downloaded frames
``0000.png``, ``0001.png``, … in manifest order before invoking ffmpeg, fully
decoupling the on-Drive filenames from the encode input pattern.

**Temp-file hygiene.** Each request gets a fresh ``tempfile.mkdtemp()`` dir
(under ``/tmp`` — world-writable 1777, so the non-root ``app`` uid 10001 from
PR #127/#137 can write there; NOT a root-owned path). The frames + the MP4 are
transient on our server: the whole dir is removed in a ``finally`` block,
success or failure, so a crash mid-encode doesn't leak disk.

**ffmpeg invocation.** ``ffmpeg -y -framerate {fps} -i {dir}/%04d.png
-c:v libx264 -pix_fmt yuv420p -movflags +faststart {dir}/out.mp4``. Notes:
  * ``-pix_fmt yuv420p`` is required for broad player compatibility (QuickTime
    / Safari refuse 4:4:4 H.264); slide PNGs are RGB so the conversion is
    lossless-enough for static frames.
  * ``-movflags +faststart`` moves the moov atom to the front so the MP4
    streams / previews without a full download (Drive's web preview wants it).
  * ``-framerate`` BEFORE ``-i`` sets the INPUT rate (how long each PNG is
    shown); for a slideshow that's the knob the caller wants (``fps=2`` →
    each slide on screen 0.5s).
  * Slide PNGs can have odd pixel dimensions (getThumbnail LARGE = 1600px
    wide, height varies by aspect). libx264 + yuv420p needs even dimensions;
    we pass ``-vf "pad=ceil(iw/2)*2:ceil(ih/2)*2"`` to round up to even
    without distorting (pad, not scale).
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from google_docs_mcp.decorators import workspace_tool
from google_docs_mcp.google_clients import get_service
from google_docs_mcp.tool_schemas import AS_ENCODE_VIDEO_OUTPUT_SCHEMA

# Imported for parity with the other apps_script feature files; not used
# on the happy path (the @workspace_tool(creds=True) envelope injects
# creds and maps HttpError → ToolError). Kept top-level so a future
# error-path addition doesn't need a separate import.
from google_docs_mcp._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

_log = logging.getLogger("google_docs_mcp.encode_video")

# Scopes this tool exercises:
#   * drive.readonly — read the manifest.json + PNG frames the bound
#     render-script created (our app's drive.file can't see them).
#   * drive.file     — upload the MP4 we create (our file → drive.file).
# Both are in the baseline auth.SCOPES grant (PR #125), so declaring them
# is a no-op for consent; it keeps the per-tool scope annotation honest
# (readable via tool.annotations.scopes). NOTE the deliberate divergence
# from the other apps_script tools' GAS_BOUND_SCOPES: this tool does NOT
# deploy a script, so it needs neither script.projects nor
# script.deployments — it needs Drive read + Drive write.
_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AS_ENCODE_VIDEO_SCOPES = [_DRIVE_READONLY_SCOPE, _DRIVE_FILE_SCOPE]

_MANIFEST_NAME = "manifest.json"
_PNG_MIME = "image/png"
_MP4_MIME = "video/mp4"

# Guardrails on fps so a caller can't request an absurd encode.
_MIN_FPS = 1
_MAX_FPS = 60

# Defensive cap on frame count — a single Apps Script render pass tops
# out around ~50 slides (the render half's documented guidance), so a
# manifest claiming hundreds of frames is almost certainly malformed /
# a wrong-folder pointer. Reject rather than spend minutes downloading.
# v2.x hardening (#4a): lowered 1000 → 200. 200 is ~4x the documented
# single-pass slide ceiling — generous headroom for legitimate decks
# while still refusing an obviously-wrong manifest before the download
# loop spends time + memory + disk on it.
_MAX_FRAMES = 200

# Cumulative byte budget across ALL frame downloads (#4a). The prod
# machine is shared-cpu-1x / 512mb; each frame is read fully into RAM
# (BytesIO) before being written to the temp dir, and ffmpeg then needs
# headroom on top. getThumbnail LARGE frames are ~0.3-2 MB each, so a
# legitimate ~50-slide deck is well under this; the cap exists to stop a
# pathological / malformed manifest (huge frames, or a wrong-folder
# pointer at a folder full of large images) from OOM-ing the box.
#
# 250 MB leaves ~260 MB for the Python process + the ffmpeg child + OS
# on a 512 MB machine — deliberately conservative. The budget is
# enforced INCREMENTALLY inside the download loop: we abort the moment
# the running total would exceed the cap, so we never complete the full
# in-RAM/disk load of an over-budget set.
_MAX_TOTAL_FRAME_BYTES = 250 * 1024 * 1024  # 250 MiB

# ffmpeg's exit is trusted, but bound the wall-clock so a pathological
# encode can't pin the machine. 50 frames at libx264 ultrafast is
# sub-second; 5 minutes is generous headroom that still fails fast.
_FFMPEG_TIMEOUT_SEC = 300

# How much to lower the ffmpeg child's scheduling priority on POSIX
# (#4b). The prod container has ONE shared vCPU; a multi-second encode
# at default priority competes head-to-head with the HTTP server's
# event loop and can delay the /health probe. `os.nice(10)` keeps the
# encode CPU-bound work below request handling so a big encode degrades
# its OWN latency, not the server's liveness. (No-op on non-POSIX dev.)
_FFMPEG_NICE_INCREMENT = 10


def _drive_list_folder(drive, folder_id: str) -> list[dict]:
    """List the (non-trashed) files directly in a Drive folder.

    Returns ``[{id, name, mimeType}, ...]``. Used to locate the
    manifest + the PNG frames. ``drive.readonly`` covers this read
    even for a folder our app didn't create.
    """
    resp = drive.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id,name,mimeType)",
        pageSize=1000,
    ).execute()
    return resp.get("files", [])


def _drive_download_bytes(drive, file_id: str) -> bytes:
    """Download a Drive file's raw bytes via ``files.get_media``.

    Streamed through ``MediaIoBaseDownload`` (chunked) so a large PNG
    doesn't balloon a single response. Returns the full byte string.
    """
    buf = io.BytesIO()
    request = drive.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def _parse_manifest(raw: bytes) -> list[str]:
    """Parse the render step's ``manifest.json`` → ordered frame names.

    The render half writes ``{presentationId, framePrefix, frameCount,
    frames: [...]}``. We only need ``frames`` (the slide-ordered name
    list). Raises ``ValueError`` with a clear message on any shape
    problem so the tool surfaces "this folder's manifest is malformed"
    rather than a bare KeyError / JSONDecodeError.
    """
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(
            f"{_MANIFEST_NAME} is not valid UTF-8 JSON ({e}). The folder "
            f"may not be an as_generate_video_deck output folder, or the "
            f"render step wrote a corrupt manifest."
        ) from e
    if not isinstance(manifest, dict):
        raise ValueError(
            f"{_MANIFEST_NAME} is not a JSON object (got "
            f"{type(manifest).__name__}). Expected the "
            f"as_generate_video_deck manifest shape."
        )
    frames = manifest.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError(
            f"{_MANIFEST_NAME} has no non-empty 'frames' array. The render "
            f"step (renderFrames) may not have run yet — open the deck and "
            f"click 'Video > Render frames' first, then retry the encode."
        )
    if not all(isinstance(f, str) and f for f in frames):
        raise ValueError(
            f"{_MANIFEST_NAME} 'frames' must be a list of non-empty "
            f"filename strings; got {frames!r}."
        )
    return frames


def _close_media_stream(media: object) -> None:
    """Best-effort close of a ``MediaFileUpload``'s underlying file handle.

    ``MediaFileUpload(resumable=False)`` opens the file and keeps the
    handle until GC. On Windows an open handle blocks ``rmtree`` of the
    containing dir, so we close it explicitly after the upload. The SDK
    exposes the handle via ``.stream()``; guarded so a future SDK shape
    change degrades to a no-op rather than an error (the OS reclaims the
    fd on process exit regardless).
    """
    try:
        stream = media.stream()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — no stream() / not open: nothing to close
        return
    try:
        stream.close()
    except Exception:  # noqa: BLE001 — already closed / not closeable
        pass


def _build_ffmpeg_cmd(work_dir: Path, fps: int, output_path: Path) -> list[str]:
    """Build the ffmpeg argv (PURE — no I/O; trivially testable).

    Input pattern ``%04d.png`` matches the re-numbered frames we write
    (0000.png, 0001.png, …) in manifest order. See module docstring for
    the flag rationale (pix_fmt / faststart / pad-to-even / input
    framerate).
    """
    return [
        "ffmpeg",
        "-y",  # overwrite output (the temp file is ours, fresh dir)
        "-framerate", str(fps),  # INPUT rate: how long each PNG shows
        "-i", str(work_dir / "%04d.png"),
        "-c:v", "libx264",
        # #4b: bound CPU on the single shared vCPU. `-preset veryfast`
        # trades a little compression efficiency for a much shorter,
        # lighter encode (slide frames are static — the size cost is
        # negligible); `-threads 1` stops libx264 from spawning a thread
        # per core and saturating the box. Together with the os.nice()
        # de-prioritization on the subprocess, a big encode degrades its
        # OWN latency rather than starving the HTTP server's /health probe.
        "-preset", "veryfast",
        "-threads", "1",
        "-pix_fmt", "yuv420p",  # broad-player compat (QuickTime/Safari)
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",  # even dims for yuv420p
        "-movflags", "+faststart",  # moov atom up front for streaming
        str(output_path),
    ]


def _ffmpeg_preexec():
    """Return a ``preexec_fn`` that lowers the ffmpeg child's priority,
    or ``None`` when ``os.nice`` is unavailable (non-POSIX / dev).

    #4b: ``os.nice`` only exists on POSIX. The prod container is Linux,
    so this fires in production; on Windows dev it returns ``None`` and
    ``subprocess.run`` simply runs the child at normal priority (no
    crash). ``preexec_fn`` runs in the forked child BEFORE exec, so the
    nice increment applies to ffmpeg only — never the parent server.
    """
    nice_fn = getattr(os, "nice", None)
    if nice_fn is None:
        return None

    def _apply_nice() -> None:  # pragma: no cover — runs in forked child
        nice_fn(_FFMPEG_NICE_INCREMENT)

    return _apply_nice


@workspace_tool(
    title="Encode rendered slide frames into an MP4 video",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW MP4 file in Drive — re-running produces a
    # SECOND video file (Drive allows duplicate names). NOT idempotent;
    # matches the as_generate_video_deck / gslides_create_presentation
    # convention. (Also: server-side compute with a fresh temp dir per
    # call — never safe to silently retry a half-finished encode.)
    idempotent=False,
    external=True,
    creds=True,
    scopes=AS_ENCODE_VIDEO_SCOPES,
    output_schema=AS_ENCODE_VIDEO_OUTPUT_SCHEMA,
)
def as_encode_video(
    creds: Credentials,
    frames_folder_id: str,
    fps: int = 2,
    output_name: str | None = None,
    name: str | None = None,
) -> dict:
    """Stitch the PNG frames produced by as_generate_video_deck into an MP4.

    This is the SECOND HALF of the slides-to-video pipeline. Given the
    Drive folder that ``as_generate_video_deck``'s ``renderFrames`` run
    populated (ordered PNG frames + a ``manifest.json``), this tool reads
    the frames, encodes them into an H.264 MP4 with ffmpeg ON OUR SERVER,
    uploads the MP4 back to the user's Drive, and returns its file ID +
    link.

    USE WHEN: the user has already run the render step (frames exist in a
    Drive folder) and wants the actual video file — "encode my slide
    frames into a video", "make the MP4 from the rendered frames". If the
    frames don't exist yet, run ``as_generate_video_deck`` first and have
    the user click 'Video > Render frames'.

    HOW THE ACCESS WORKS: the frames were created by the bound render
    script (running as the user), not by this app, so this app's
    ``drive.file`` scope can't see them — the read uses ``drive.readonly``
    (in the baseline grant). The MP4 this tool creates IS this app's file,
    so ``drive.file`` covers the upload. No second OAuth consent: both
    scopes are already granted.

    FRAME ORDER: the ``manifest.json`` the render step wrote lists the
    frames in slide order; this tool honors that order exactly (it does
    NOT sort by filename). Frames are re-numbered ``0000.png`` … in
    manifest order before encoding, so the video plays in slide order
    regardless of the on-Drive frame filenames.

    Args:
        frames_folder_id: Drive ID of the folder ``as_generate_video_deck``
            rendered into (the folder containing the PNG frames +
            ``manifest.json``). The ID part of the folder's URL.
        fps: frames per second — how many slides play per second. Default
            ``2`` (each slide on screen 0.5s). Clamped to the range
            1..60. A slideshow usually wants a LOW value (1-3); high fps
            only makes sense if the frames are an animation.
        output_name: OPTIONAL filename for the MP4 in Drive. Defaults to a
            generated ``appscriptly video.mp4`` name. ``.mp4`` is appended
            if you don't include it.
        name: OPTIONAL alias for ``output_name`` (accepted for
            cross-tool ergonomic parity; ``output_name`` wins if both
            are given).

    Returns:
        ``{video_file_id, video_url, frame_count, duration_sec, fps,
        output_name}``. ``frame_count`` is the number of frames encoded
        (from the manifest); ``duration_sec`` is ``frame_count / fps``
        (the video's wall-clock length); ``video_url`` deep-links to the
        MP4 in Drive.

    Raises:
        ValueError: empty ``frames_folder_id``, ``fps`` out of range, the
            folder has no ``manifest.json`` (render step hasn't run), the
            manifest is malformed, a manifest-listed frame is missing from
            the folder, the frame count exceeds the safety cap
            (``_MAX_FRAMES``), or the cumulative frame bytes exceed the
            server-side size limit (``_MAX_TOTAL_FRAME_BYTES``, enforced
            incrementally during download to protect the 512 MB machine)
            — all rejected with a clear, actionable message rather than
            crashing.
        ToolError: ffmpeg failed, or any Drive API error — the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError``; an ffmpeg failure surfaces as
            a RuntimeError → ToolError with the ffmpeg stderr tail.

    Choreography: run AFTER ``as_generate_video_deck`` + the user's
    'Video > Render frames' click. Get ``frames_folder_id`` from the
    folder the render step created (its name is in that tool's
    ``output_folder_name`` / ``activation_note``), or from
    ``gdocs_find_doc_by_title`` if the user only knows the folder name.
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call.
    if not frames_folder_id or not frames_folder_id.strip():
        raise ValueError(
            "frames_folder_id cannot be empty — pass the Drive ID of the "
            "folder as_generate_video_deck rendered the PNG frames into."
        )
    if not isinstance(fps, int) or isinstance(fps, bool):
        raise ValueError(
            f"fps must be an integer, got {type(fps).__name__}."
        )
    if fps < _MIN_FPS or fps > _MAX_FPS:
        raise ValueError(
            f"fps must be between {_MIN_FPS} and {_MAX_FPS} (got {fps}). "
            f"A slideshow usually wants 1-3 fps (each slide on screen "
            f"1s / 0.5s / 0.33s)."
        )

    # Resolve the output filename (output_name wins over the name alias).
    chosen = output_name or name or "appscriptly video"
    chosen = chosen.strip() or "appscriptly video"
    if not chosen.lower().endswith(".mp4"):
        chosen = f"{chosen}.mp4"

    drive = get_service("drive", "v3", credentials=creds)

    # 2. List the folder; locate the manifest + index PNGs by name.
    files = _drive_list_folder(drive, frames_folder_id)
    by_name: dict[str, dict] = {f["name"]: f for f in files}

    manifest_entry = by_name.get(_MANIFEST_NAME)
    if manifest_entry is None:
        raise ValueError(
            f"No {_MANIFEST_NAME} found in folder {frames_folder_id!r}. "
            f"Either the folder ID is wrong, or as_generate_video_deck's "
            f"renderFrames step hasn't run yet (open the deck → "
            f"'Video > Render frames', then retry)."
        )

    # 3. Read + parse the manifest → ordered frame names.
    manifest_bytes = _drive_download_bytes(drive, manifest_entry["id"])
    frame_names = _parse_manifest(manifest_bytes)

    if len(frame_names) > _MAX_FRAMES:
        raise ValueError(
            f"{_MANIFEST_NAME} lists {len(frame_names)} frames, exceeding "
            f"the {_MAX_FRAMES}-frame safety cap. A single render pass tops "
            f"out near ~50 slides; this manifest is likely malformed or "
            f"points at the wrong folder."
        )

    # 4. Encode in a per-request temp dir under /tmp (world-writable, so
    #    the non-root app user can write there). Cleaned up in finally.
    work_dir = Path(tempfile.mkdtemp(prefix="appscriptly-encode-"))
    try:
        # Download each manifest-listed frame, re-numbering to a
        # zero-padded sequential name in MANIFEST ORDER so ffmpeg's
        # %04d.png pattern plays them in slide order (decoupled from
        # the on-Drive filenames).
        #
        # #4a: enforce the cumulative byte budget INCREMENTALLY. We add
        # each frame's size to a running total and abort the moment the
        # total exceeds _MAX_TOTAL_FRAME_BYTES — BEFORE downloading the
        # rest of the set. This bounds peak memory + temp-disk to roughly
        # the cap plus one frame, instead of loading an arbitrarily large
        # set into RAM/disk and OOM-ing the 512 MB machine. The refusal
        # is a clean, actionable ValueError, not a crash.
        total_bytes = 0
        for idx, fname in enumerate(frame_names):
            entry = by_name.get(fname)
            if entry is None:
                raise ValueError(
                    f"Frame {fname!r} listed in {_MANIFEST_NAME} is missing "
                    f"from folder {frames_folder_id!r}. The render step may "
                    f"have been interrupted; re-run 'Video > Render frames' "
                    f"and retry."
                )
            frame_bytes = _drive_download_bytes(drive, entry["id"])
            total_bytes += len(frame_bytes)
            if total_bytes > _MAX_TOTAL_FRAME_BYTES:
                cap_mb = _MAX_TOTAL_FRAME_BYTES // (1024 * 1024)
                raise ValueError(
                    f"Frame data exceeds the {cap_mb} MB total-size limit "
                    f"(reached {total_bytes / 1024 / 1024:.0f} MB at frame "
                    f"{idx + 1} of {len(frame_names)}). This deck's frames "
                    f"are too large to encode on the server. Re-render with "
                    f"fewer / smaller slides, or split the deck into parts "
                    f"and encode each separately."
                )
            (work_dir / f"{idx:04d}.png").write_bytes(frame_bytes)

        output_path = work_dir / "out.mp4"
        cmd = _build_ffmpeg_cmd(work_dir, fps, output_path)

        _log.info(
            "encode_video start folder=%s frames=%d fps=%d",
            frames_folder_id, len(frame_names), fps,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT_SEC,
            # #4b: lower the encode's scheduling priority on POSIX so it
            # can't starve the HTTP server on the single shared vCPU.
            # No-op (None) on non-POSIX dev — see _ffmpeg_preexec.
            preexec_fn=_ffmpeg_preexec(),
        )
        if result.returncode != 0:
            # Surface a trimmed stderr tail — ffmpeg's last lines carry
            # the actual error. RuntimeError → ToolError via the envelope.
            stderr_tail = (result.stderr or "").strip().splitlines()[-8:]
            raise RuntimeError(
                "ffmpeg failed to encode the frames (exit "
                f"{result.returncode}). Last output:\n"
                + "\n".join(stderr_tail)
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(
                "ffmpeg reported success but produced no output file. "
                "The frames may be unreadable as PNGs."
            )

        # 5. Upload the MP4 to the user's Drive (our file → drive.file).
        # MediaFileUpload opens the file and holds the handle. On Windows
        # an open handle blocks the finally-block rmtree of work_dir
        # (POSIX doesn't care, but we develop/test on Windows too), so we
        # explicitly close the stream after the upload completes. The
        # _close_media_stream helper is defensive — it no-ops if the SDK
        # shape ever changes.
        media = MediaFileUpload(
            str(output_path), mimetype=_MP4_MIME, resumable=False
        )
        try:
            created = drive.files().create(
                body={"name": chosen, "mimeType": _MP4_MIME},
                media_body=media,
                fields="id,name",
            ).execute()
        finally:
            _close_media_stream(media)
        video_id = created["id"]

        frame_count = len(frame_names)
        return {
            "video_file_id": video_id,
            "video_url": f"https://drive.google.com/file/d/{video_id}/view",
            "frame_count": frame_count,
            "duration_sec": round(frame_count / fps, 3),
            "fps": fps,
            "output_name": created.get("name", chosen),
        }
    finally:
        # Transient frames + MP4 — never persist on our server. Remove
        # the whole dir, success or failure. ignore_errors so a cleanup
        # hiccup never masks the real result / error.
        shutil.rmtree(work_dir, ignore_errors=True)
