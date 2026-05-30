"""``as_encode_video`` — stitch staged slide PNG frames into an MP4 (PR-Δ12).

The ENCODE half of the slides-to-video pipeline. Where
``as_generate_video_deck`` (PR-Δ11) deploys a bound Apps Script whose
``renderFrames()`` renders each slide to a PNG and POSTs the bytes to the
appscriptly server's signed frame-staging endpoint, THIS tool does the
server-side compute the render half left out:

  1. **Read** the staged PNG frames for the batch off the server's own
     ``/data`` volume (NO Drive — see the access-path note below).
  2. **Encode** them into an H.264 MP4 with ffmpeg, running on OUR server.
  3. **Upload** the resulting MP4 to the user's Drive + return its file
     ID + link, then consume (delete) the staged frames.

**This is NOT a bound-script generator.** Every other tool in
``services/apps_script/`` deploys an Apps Script that runs on Google's
infrastructure. This one is the odd sibling: it's server-side compute
(ffmpeg on our Fly machine). It lives in the apps_script package anyway
because it's the second half of the apps_script-owned slides-to-video
pipeline — keeping the two halves co-located (``video_deck.py`` +
``encode_video.py``) is clearer than scattering a 2-tool feature across
two services. The ``@workspace_tool(service="apps_script")`` annotation
reflects that grouping.

**The access path — base-tier, no drive.readonly.** The bound render
script POSTs each rendered PNG straight to the server's signed
frame-staging area (see ``_frames_staging``), so the frames live on the
server's own volume and this tool reads them LOCALLY — NO Drive read
scope. The final MP4 is uploaded to the user's Drive via ``drive.file``
(the app creates that file, so the app can write it). This is what let
the free base tier drop ``drive.readonly``.

(History: frames used to land in a user-owned Drive folder + a
``manifest.json`` that this tool re-read with ``drive.readonly`` —
because ``drive.file`` is a per-file grant that does NOT cover files a
different identity created inside an app-created folder. The signed-upload
handoff replaced that Drive round-trip.)

**Frame order.** The staged frames are named ``frame_0001.png`` … in
render (slide) order, so a name sort is render order. We re-number them
``0000.png``, ``0001.png``, … into the encode temp dir so ffmpeg's
``-i %04d.png`` pattern plays them in slide order, decoupled from the
staged names.

**Temp-file hygiene.** Each request gets a fresh ``tempfile.mkdtemp()``
dir (under ``/tmp`` — world-writable 1777, so the non-root ``app`` uid
10001 from PR #127/#137 can write there; NOT a root-owned path). The
frames + the MP4 are transient on our server: the whole dir is removed
in a ``finally`` block, success or failure, so a crash mid-encode
doesn't leak disk.

**ffmpeg invocation.** ``ffmpeg -y -framerate {fps} -i {dir}/%04d.png
-c:v libx264 -preset veryfast -threads 1 -pix_fmt yuv420p
-vf pad=... -movflags +faststart {dir}/out.mp4``. Notes:
  * ``-pix_fmt yuv420p`` is required for broad player compatibility
    (QuickTime / Safari refuse 4:4:4 H.264); slide PNGs are RGB so the
    conversion is lossless-enough for static frames.
  * ``-movflags +faststart`` moves the moov atom to the front so the MP4
    streams / previews without a full download (Drive's web preview).
  * ``-framerate`` BEFORE ``-i`` sets the INPUT rate (how long each PNG
    is shown); for a slideshow that's the knob the caller wants.
  * Slide PNGs can have odd pixel dimensions (getThumbnail LARGE =
    1600px wide, height varies by aspect). libx264 + yuv420p needs even
    dimensions; we pass ``-vf "pad=ceil(iw/2)*2:ceil(ih/2)*2"`` to round
    up to even without distorting (pad, not scale).
  * #4b: ``-preset veryfast -threads 1`` + an ``os.nice`` preexec keep a
    big encode from saturating the single shared vCPU / starving the
    HTTP server's /health probe.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from googleapiclient.http import MediaFileUpload

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

# Scopes this tool exercises (base-tier — NO drive.readonly):
#   * drive.file — upload the MP4 we create (our file → drive.file).
# The frames come from the server's own signed staging area (the bound
# render script POSTed them there), so there is NO Drive READ — that's
# what let the free base tier drop the restricted Drive-read scope.
# drive.file is in the baseline grant, so declaring it is a no-op for
# consent; it keeps the per-tool scope annotation honest (readable via
# tool.annotations.scopes). NOTE the deliberate divergence from the
# other apps_script tools' GAS_BOUND_SCOPES: this tool does NOT deploy a
# script, so it needs neither script.projects nor script.deployments —
# only Drive write.
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AS_ENCODE_VIDEO_SCOPES = [_DRIVE_FILE_SCOPE]

_MP4_MIME = "video/mp4"

# Guardrails on fps so a caller can't request an absurd encode.
_MIN_FPS = 1
_MAX_FPS = 60

# Defensive cap on frame count — a single Apps Script render pass tops
# out around ~50 slides (the render half's documented guidance), so a
# batch with hundreds of frames is almost certainly malformed / wrong.
# v2.x hardening (#4a): 200 is ~4x the documented single-pass ceiling —
# generous headroom for legitimate decks while still refusing an
# obviously-wrong batch before the encode spends time + disk on it.
_MAX_FRAMES = 200

# Cumulative byte budget across ALL frames (#4a). The prod machine is
# shared-cpu-1x / 512mb; ffmpeg needs headroom. getThumbnail LARGE frames
# are ~0.3-2 MB each, so a legitimate ~50-slide deck is well under this;
# the cap stops a pathological batch (huge frames) from filling temp disk
# / OOM-ing the box. Enforced INCREMENTALLY while copying staged frames:
# we abort the moment the running total would exceed the cap.
_MAX_TOTAL_FRAME_BYTES = 250 * 1024 * 1024  # 250 MiB

# ffmpeg's exit is trusted, but bound the wall-clock so a pathological
# encode can't pin the machine. 50 frames at libx264 veryfast is
# sub-second; 5 minutes is generous headroom that still fails fast.
_FFMPEG_TIMEOUT_SEC = 300

# How much to lower the ffmpeg child's scheduling priority on POSIX
# (#4b). The prod container has ONE shared vCPU; a multi-second encode at
# default priority competes head-to-head with the HTTP server's event
# loop and can delay the /health probe. `os.nice(10)` keeps the encode
# CPU-bound work below request handling. (No-op on non-POSIX dev.)
_FFMPEG_NICE_INCREMENT = 10


def _collect_staged_frames(frames_batch_id: str, work_dir: Path) -> list[Path]:
    """Copy a batch's staged PNG frames into ``work_dir``, re-numbered in order.

    Base-tier: the frames were POSTed to the server's signed staging area
    by the bound render script (NO Drive, no drive.readonly). They are
    already named ``frame_0001.png`` … in render order, so a name sort is
    render order. We copy them into the encode work dir as
    ``0000.png``, ``0001.png``, … so ffmpeg's ``%04d.png`` input pattern
    plays them in order (decoupled from the staged names).

    Preserves the PR-Δ12 resource bounds: ``_MAX_FRAMES`` (count) and
    ``_MAX_TOTAL_FRAME_BYTES`` (cumulative size), the latter enforced
    INCREMENTALLY so a pathological batch can't blow up temp-disk on the
    512 MB box. Returns the sorted local frame paths.
    """
    from ._frames_staging import list_staged_frames

    staged = list_staged_frames(frames_batch_id)
    if not staged:
        raise ValueError(
            f"No staged frames found for batch {frames_batch_id!r}. The "
            f"render step may not have run (or finished) yet — open the deck "
            f"and click 'Video > Render frames', then retry as_encode_video. "
            f"(Frames expire ~30 min after as_generate_video_deck, and are "
            f"deleted once encoded.)"
        )
    if len(staged) > _MAX_FRAMES:
        raise ValueError(
            f"Too many frames ({len(staged)}), exceeding the {_MAX_FRAMES}-"
            f"frame safety cap. A single render pass tops out near ~50 "
            f"slides; re-render with fewer slides or split the deck."
        )
    total_bytes = 0
    out_paths: list[Path] = []
    for idx, src in enumerate(staged):
        total_bytes += src.stat().st_size
        if total_bytes > _MAX_TOTAL_FRAME_BYTES:
            cap_mb = _MAX_TOTAL_FRAME_BYTES // (1024 * 1024)
            raise ValueError(
                f"Frame data exceeds the {cap_mb} MB total-size limit "
                f"(reached {total_bytes / 1024 / 1024:.0f} MB at frame "
                f"{idx + 1} of {len(staged)}). This deck's frames are too "
                f"large to encode on the server. Re-render with fewer / "
                f"smaller slides, or split the deck into parts."
            )
        local = work_dir / f"{idx:04d}.png"
        local.write_bytes(src.read_bytes())
        out_paths.append(local)
    return out_paths


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
    (0000.png, 0001.png, …) in render order. See module docstring for the
    flag rationale (pix_fmt / faststart / pad-to-even / input framerate /
    CPU bounds).
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
        # per core and saturating the box.
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
    frames_batch_id: str,
    fps: int = 2,
    output_name: str | None = None,
    name: str | None = None,
) -> dict:
    """Stitch the PNG frames produced by as_generate_video_deck into an MP4.

    This is the SECOND HALF of the slides-to-video pipeline. The bound
    render script (``as_generate_video_deck``'s ``renderFrames`` run) POSTs
    each rendered slide PNG to the appscriptly server's signed staging
    area, keyed by a ``frames_batch_id``. This tool reads those staged
    frames OFF THE SERVER, encodes them into an H.264 MP4 with ffmpeg ON
    OUR SERVER, uploads the MP4 to the user's Drive, and returns its file
    ID + link.

    USE WHEN: the user has already run the render step (you have the
    ``frames_batch_id`` from as_generate_video_deck and the user has run
    'Video > Render frames') and wants the actual video file — "encode my
    slide frames into a video", "make the MP4". If the render hasn't run
    yet, run ``as_generate_video_deck`` first and have the user click
    'Video > Render frames'.

    HOW THE ACCESS WORKS (base-tier — no ``drive.readonly``): the frames
    live on the server (the bound script POSTed them), so reading them
    needs NO Drive scope at all. The MP4 this tool creates IS this app's
    file, so ``drive.file`` covers the upload. That's the whole reason the
    free base tier could drop the restricted Drive-read scope.

    FRAME ORDER: the staged frames are named in render (slide) order; this
    tool re-numbers them ``0000.png`` … in that order before encoding, so
    the video plays in slide order. The staged frames are CONSUMED
    (deleted) after a successful encode.

    Args:
        frames_batch_id: the batch handle ``as_generate_video_deck``
            returned (its ``frames_batch_id`` field). Ties this encode to
            the frames the render step uploaded.
        fps: frames per second — how many slides play per second. Default
            ``2`` (each slide on screen 0.5s). Clamped to the range
            1..60. A slideshow usually wants a LOW value (1-3); high fps
            only makes sense if the frames are an animation.
        output_name: OPTIONAL filename for the MP4 in Drive. Defaults to a
            generated ``appscriptly video.mp4`` name. ``.mp4`` is appended
            if you don't include it.
        name: OPTIONAL alias for ``output_name`` (accepted for cross-tool
            ergonomic parity; ``output_name`` wins if both are given).

    Returns:
        ``{video_file_id, video_url, frame_count, duration_sec, fps,
        output_name}``. ``frame_count`` is the number of frames encoded;
        ``duration_sec`` is ``frame_count / fps`` (the video's wall-clock
        length); ``video_url`` deep-links to the MP4 in Drive.

    Raises:
        ValueError: empty ``frames_batch_id``, ``fps`` out of range, no
            staged frames found for the batch (render step hasn't run /
            finished, or the batch expired), the frame count exceeds the
            safety cap (``_MAX_FRAMES``), or the cumulative frame bytes
            exceed the server-side size limit (``_MAX_TOTAL_FRAME_BYTES``,
            enforced incrementally to protect the 512 MB machine) — all
            rejected with a clear, actionable message rather than crashing.
        ToolError: ffmpeg failed, or any Drive API error on the MP4 upload
            — the standard ``@workspace_tool(creds=True)`` envelope renders
            ``HttpError`` as a user-facing ``ToolError``; an ffmpeg failure
            surfaces as a RuntimeError → ToolError with the stderr tail.

    Choreography: run AFTER ``as_generate_video_deck`` + the user's
    'Video > Render frames' click. Use the ``frames_batch_id`` from the
    as_generate_video_deck result.
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any work.
    if not frames_batch_id or not frames_batch_id.strip():
        raise ValueError(
            "frames_batch_id cannot be empty — pass the frames_batch_id "
            "that as_generate_video_deck returned (after you ran the deck's "
            "'Video > Render frames')."
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

    # 2. Encode in a per-request temp dir under /tmp (world-writable, so
    #    the non-root app user can write there). Cleaned up in finally.
    work_dir = Path(tempfile.mkdtemp(prefix="appscriptly-encode-"))
    try:
        # 3. Collect the staged frames the bound render script POSTed to
        #    the server (base-tier — NO Drive read). _collect_staged_frames
        #    re-numbers them 0000.png … in render order for ffmpeg's
        #    %04d.png pattern, and enforces the _MAX_FRAMES + incremental
        #    _MAX_TOTAL_FRAME_BYTES resource bounds.
        frames = _collect_staged_frames(frames_batch_id, work_dir)
        frame_count = len(frames)

        output_path = work_dir / "out.mp4"
        cmd = _build_ffmpeg_cmd(work_dir, fps, output_path)

        _log.info(
            "encode_video start batch=%s frames=%d fps=%d",
            frames_batch_id, frame_count, fps,
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

        # 4. Upload the MP4 to the user's Drive (our file → drive.file).
        #    No 'parents' — the app creates the file in My Drive root, and
        #    because the app created it, drive.file grants the write. This
        #    is the ONLY Drive op (no drive.readonly anywhere).
        #    MediaFileUpload opens the file + holds the handle; on Windows
        #    that blocks the finally rmtree, so close it explicitly.
        drive = get_service("drive", "v3", credentials=creds)
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

        # 5. One-shot consume: drop the staged frames now they're encoded.
        from ._frames_staging import clear_batch
        clear_batch(frames_batch_id)

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
