"""``as_generate_video_deck`` — render a Slides deck to PNG frames (PR-Δ11).

The RENDER half of the slides-to-video pipeline. A *use-case* tool layered
on the PR-Δ7 bound-script primitive (``services/apps_script/api.py``):
given a Google Slides presentation ID, it deploys a *bound* Apps Script
whose ``renderFrames()`` function exports each slide to a PNG frame in a
dated Drive folder and writes a ``manifest.json`` listing the frames in
order.

**This is HALF a pipeline — the PNG→MP4 encode is a SEPARATE follow-up
(PR-Δ12) and is deliberately NOT here.** No ffmpeg, no Docker change, no
encode tool. This tool's contract ends at "frames + manifest in a Drive
folder"; the encode step is isolated into its own PR for the Docker-change
deploy risk.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py``:

  * ``auto_detect_container_kind`` — to VALIDATE the container is a Slides
    presentation (reject Docs / Sheets / Forms with a clear error before
    any project is created).
  * ``build_manifest`` — to derive the manifest's ``oauthScopes`` from the
    scopes this script actually exercises (Slides read + Drive file write
    + ``UrlFetchApp`` for the thumbnail ``contentUrl``).
  * ``create_bound_project`` → ``set_project_content`` →
    ``create_deployment`` — the same create/push/deploy sequence
    ``as_generate_bound_script`` orchestrates.

This module's OWN contribution is the ``.gs`` *script-body synthesis* —
the ``renderFrames()`` loop (advanced Slides
``Slides.Presentations.Pages.getThumbnail`` at ``LARGE`` size →
``UrlFetchApp.fetch(contentUrl)`` → ``folder.createFile(blob)`` per slide
→ ``manifest.json``) plus an ``onOpen()`` "Video" menu for one-click run.

**How frame export works (verified against the Slides REST reference).**
``presentations.pages.getThumbnail`` returns a ``Thumbnail`` with a
``contentUrl`` (a short-lived URL to the rendered PNG); ``thumbnailSize:
'LARGE'`` is 1600px wide; the default ``mimeType`` is PNG. The advanced
Slides service exposes this as
``Slides.Presentations.Pages.getThumbnail(presentationId, pageObjectId,
{thumbnailProperties: {...}})``. The script then fetches the PNG bytes
from ``contentUrl`` with ``UrlFetchApp.fetch`` and saves the blob to
Drive. (Server-side thumbnail rendering is why the deck read needs the
Slides scope, the byte fetch needs ``script.external_request``, and the
folder/file writes need ``drive.file``.)

**The activation caveat — read this (mirrors #140's trigger caveat).**
``renderFrames`` only produces frames when it actually RUNS. Deploying the
script does NOT run it (the Apps Script API's deploy step publishes code;
it doesn't execute functions), and there is no REST endpoint to invoke an
arbitrary bound function. So this tool deploys the renderer + wires a
one-click "Video → Render frames" menu, but the frames don't exist yet on
return. The return payload is HONEST about this: ``activation_note``
spells out the single step (open the deck → Video menu → Render frames, or
run ``renderFrames`` once in the editor). We do NOT claim frames exist
until the function has run.

**Execution-limit caveat.** ``getThumbnail`` is an expensive,
server-rendered read, and Apps Script caps a single execution at ~6
minutes. For the MVP, decks up to ~50 slides render comfortably in one
``renderFrames`` pass; very large decks may exceed the cap and would need
chunking (a future enhancement). The docstring + ``activation_note`` are
explicit about the ~50-slide guidance.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from google_docs_mcp.decorators import workspace_tool
from google_docs_mcp.services.apps_script.api import (
    auto_detect_container_kind as _auto_detect_container_kind,
    build_manifest as _build_manifest,
    create_bound_project as _create_bound_project,
    create_deployment as _create_deployment,
    set_project_content as _set_project_content,
)
from google_docs_mcp.services.apps_script.scopes import GAS_BOUND_SCOPES
from google_docs_mcp.tool_schemas import AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA

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

# This tool only ever targets a Google Slides presentation — a video
# deck IS a Slides deck. We still auto-detect (via the #138 primitive's
# Drive lookup) to VALIDATE the container is Slides and reject Docs /
# Sheets / Forms before creating any project; "slides" is the only kind
# that makes sense here.
_SLIDES_KIND = "slides"

# Scopes the generated renderFrames() body exercises, declared in the
# manifest so the in-editor run is authorized:
#   * presentations  — read the deck + getThumbnail (an "expensive read").
#   * drive.file     — create the output folder + the PNG / manifest files.
#   * script.external_request — UrlFetchApp.fetch(contentUrl) to pull the
#     rendered PNG bytes from the thumbnail's short-lived content URL.
# All three are in the baseline auth.SCOPES grant (presentations +
# drive.file are baseline; script.external_request rides the Apps Script
# deploy authorization) so the deploy itself needs no second consent; the
# manifest declares them so the in-editor renderFrames run is authorized.
_PRESENTATIONS_SCOPE = "https://www.googleapis.com/auth/presentations"
_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_URLFETCH_SCOPE = "https://www.googleapis.com/auth/script.external_request"

_RENDER_SCOPES = [_PRESENTATIONS_SCOPE, _DRIVE_FILE_SCOPE, _URLFETCH_SCOPE]

# A conservative single-pass slide-count guidance surfaced in the return
# payload + docstring. Not enforced (the script renders whatever the deck
# has); it documents where the ~6-min execution cap starts to bite.
_SINGLE_PASS_SLIDE_GUIDANCE = 50

# A valid Drive folder-name character guard for the OPTIONAL caller
# override. We don't allow path separators or control chars in a name
# we embed as a JS string literal forming a Drive folder name.
_FOLDER_NAME_BAD_CHARS_RE = re.compile(r"[\x00-\x1f/\\]")

# A frame-prefix must be a safe filename stem (it's embedded into
# `<prefix>_001.png`). Letters / digits / _ / - only — no spaces, dots,
# or separators that could confuse the downstream encode step's glob.
_FRAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _js_string(value: str) -> str:
    """Render a Python str as a safe JS string literal.

    Uses ``json.dumps`` — JSON string syntax is a subset of JS string
    syntax, so quotes / backslashes / newlines / control chars are
    escaped and can't break out of the literal or inject code. Same
    helper shape as ``doc_menu._js_string``; kept local so the feature
    files stay independent (no cross-feature import coupling).
    """
    return json.dumps(value)


def build_video_deck_script(
    presentation_id: str,
    output_folder_name: str,
    frame_prefix: str,
) -> str:
    """Generate the ``.gs`` source for the Slides→frames renderer (PURE).

    Deterministic: same inputs → byte-identical output (no I/O; easy to
    test). Produces:

      1. ``onOpen()`` — adds a "Video" menu with a single "Render frames"
         item (``SlidesApp.getUi()``) so the user can run the export with
         one click after opening the deck.
      2. ``renderFrames()`` — the export loop:
         * opens the bound deck (``SlidesApp.getActivePresentation()``)
           to enumerate its slides + their object IDs in order;
         * creates the dated output folder under Drive root
           (``DriveApp.createFolder``);
         * for each slide, calls the advanced Slides service
           ``Slides.Presentations.Pages.getThumbnail(...)`` at
           ``thumbnailSize: 'LARGE'`` (PNG), fetches the rendered bytes
           from the returned ``contentUrl`` via ``UrlFetchApp.fetch``,
           and saves them as ``<frame_prefix>_001.png`` (1-based,
           zero-padded to 3 digits) via ``folder.createFile(blob)``;
         * writes ``manifest.json`` to the same folder listing the frames
           in order (so the downstream PNG→MP4 encode — a separate PR —
           has a deterministic, ordered file list).

    Args:
        presentation_id: the Slides deck ID (embedded as a JS literal +
            also resolvable at runtime via getActivePresentation; we use
            the literal so the advanced-service getThumbnail call has an
            explicit presentationId).
        output_folder_name: the Drive folder name to create + write frames
            into. Embedded as a JS string literal.
        frame_prefix: the per-frame filename stem (``<prefix>_001.png``).

    Returns:
        The complete ``.gs`` source as a string, trailing newline
        included.
    """
    pid = _js_string(presentation_id)
    folder = _js_string(output_folder_name)
    prefix = _js_string(frame_prefix)

    return f"""\
// Auto-generated by appscriptly as_generate_video_deck (PR-Δ11).
// Renders each slide of this Google Slides deck to a PNG frame in a
// Drive folder + writes a manifest.json listing the frames in order.
// This is the RENDER half of a slides-to-video pipeline; the PNG->MP4
// encode is a separate step. Runs on Google's infrastructure.

var PRESENTATION_ID = {pid};
var OUTPUT_FOLDER_NAME = {folder};
var FRAME_PREFIX = {prefix};
var THUMBNAIL_SIZE = 'LARGE';  // 1600px wide; PNG by default.

/**
 * Adds a "Video" menu to the deck with a one-click "Render frames" item.
 * Runs automatically when the presentation is opened.
 */
function onOpen() {{
  SlidesApp.getUi()
    .createMenu('Video')
    .addItem('Render frames', 'renderFrames')
    .addToUi();
}}

/**
 * Renders every slide to a PNG frame in OUTPUT_FOLDER_NAME and writes a
 * manifest.json. Run this ONCE (Video > Render frames, or the editor Run
 * button) to produce the frames — deploying the script does not run it.
 *
 * Returns the created folder's ID (handy when run from the editor).
 */
function renderFrames() {{
  var presentation = SlidesApp.getActivePresentation();
  var slides = presentation.getSlides();
  var folder = DriveApp.createFolder(OUTPUT_FOLDER_NAME);
  var frames = [];

  for (var i = 0; i < slides.length; i++) {{
    var pageObjectId = slides[i].getObjectId();
    // Advanced Slides service: getThumbnail returns {{contentUrl, width,
    // height}}. contentUrl is a short-lived URL to the rendered PNG.
    var thumbnail = Slides.Presentations.Pages.getThumbnail(
      PRESENTATION_ID,
      pageObjectId,
      {{ 'thumbnailProperties.thumbnailSize': THUMBNAIL_SIZE }}
    );
    // Fetch the rendered PNG bytes from the content URL.
    var response = UrlFetchApp.fetch(thumbnail.contentUrl);
    var frameNumber = i + 1;
    var padded = ('000' + frameNumber).slice(-3);  // 1 -> "001"
    var fileName = FRAME_PREFIX + '_' + padded + '.png';
    var blob = response.getBlob().setName(fileName);
    folder.createFile(blob);
    frames.push(fileName);
  }}

  // Write an ordered manifest so the downstream encode step has a
  // deterministic frame list (frame_count + names in slide order).
  var manifest = {{
    presentationId: PRESENTATION_ID,
    framePrefix: FRAME_PREFIX,
    frameCount: frames.length,
    frames: frames
  }};
  folder.createFile(
    'manifest.json',
    JSON.stringify(manifest, null, 2),
    'application/json'
  );

  return folder.getId();
}}
"""


def _slugify_folder_name(name: str) -> str:
    """Best-effort sanitize an OPTIONAL caller folder name (PURE).

    Strips control chars + path separators (so the name is safe both as a
    Drive folder name and as a JS string literal). Empty/whitespace input
    is rejected by the caller; here we only clean a provided value.
    """
    return _FOLDER_NAME_BAD_CHARS_RE.sub("", name).strip()


@workspace_tool(
    title="Render a Slides deck to video frames",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the deck —
    # re-running installs a SECOND renderer bound to the same deck. NOT
    # idempotent (same convention as as_generate_bound_script /
    # as_install_sheet_dashboard / gsheets_create_spreadsheet).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
)
def as_generate_video_deck(
    creds: Credentials,
    presentation_id: str,
    output_folder_name: str | None = None,
    frame_prefix: str = "frame",
    name: str | None = None,
) -> dict:
    """Render each slide of a Google Slides deck to a PNG frame in Drive.

    This is the FIRST HALF of a slides-to-video pipeline: it deploys a
    bound Apps Script whose ``renderFrames()`` function exports every
    slide to a numbered PNG (``<frame_prefix>_001.png`` …) in a Drive
    folder and writes a ``manifest.json`` listing the frames in order. The
    PNG→MP4 ENCODE is a SEPARATE step (a follow-up tool) — this tool's job
    ends at "ordered frames + manifest in a Drive folder".

    USE WHEN: the user wants to turn a Slides deck into video frames /
    eventually a video — "render my deck to frames for a video", "export
    each slide as an image for a slideshow video". For just viewing or
    editing the deck, use the ``gslides_*`` tools (no script needed).

    Composition over ``as_generate_bound_script``: this writes the
    ``renderFrames`` + ``onOpen`` boilerplate for you and deploys it as a
    bound Apps Script using the same create→push→deploy machinery the
    generic primitive uses. The renderer uses the advanced Slides service
    ``getThumbnail`` at LARGE size (1600px PNG) per slide, fetches the
    rendered bytes, and saves them to a dated Drive folder.

    IMPORTANT — the frames do NOT exist yet on return. Deploying a script
    does NOT run it, and there's no API to invoke a bound function
    remotely. So this tool deploys the renderer + wires a one-click
    "Video → Render frames" menu, but you must run it once to produce the
    frames: open the deck and click Video → Render frames (or open the
    returned ``project_url`` and run ``renderFrames`` from the editor,
    approving the authorization prompt). The return payload says so —
    ``activation_note`` is the literal step. Do NOT tell the user the
    frames are ready until ``renderFrames`` has run.

    LIMITS: ``getThumbnail`` is an expensive server-rendered read and Apps
    Script caps a single execution at ~6 minutes. Decks up to ~50 slides
    render comfortably in one pass; much larger decks may hit the cap and
    would need chunking (a future enhancement).

    Args:
        presentation_id: Drive ID of the Google Slides presentation to
            render (the ID part of the deck's URL). The renderer is bound
            to THIS deck. Must be a Slides file — a Doc / Sheet / Form ID
            is rejected with a clear error.
        output_folder_name: OPTIONAL name for the Drive folder the frames
            land in. Defaults to a generated ``appscriptly video frames``
            name. Path separators / control chars are stripped.
        frame_prefix: the per-frame filename stem — frames are named
            ``<frame_prefix>_001.png``, ``_002.png``, … (1-based,
            zero-padded to 3 digits). Default ``"frame"``. Must be a safe
            filename stem (letters / digits / ``_`` / ``-`` only).
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated video-deck name.

    Returns:
        ``{script_id, deployment_id, presentation_id, output_folder_name,
        frames_expected, render_function, activation_note, project_url}``.
        ``frames_expected`` is the slide count the renderer will export —
        BUT note the frames only exist after ``renderFrames`` runs (see
        the activation note). ``render_function`` is ``"renderFrames"``.
        ``project_url`` deep-links to the script editor.

    Raises:
        ValueError: empty ``presentation_id`` / invalid ``frame_prefix`` /
            blank ``output_folder_name`` override — rejected client-side
            before any API call.
        ToolError: the container is not a Slides deck, or any Apps Script
            / Drive / Slides API error — the standard
            ``@workspace_tool(creds=True)`` envelope renders ``HttpError``
            as a user-facing ``ToolError`` (and the not-Slides ``ValueError``
            from container validation surfaces directly).

    Choreography: get ``presentation_id`` from the user's URL, from a
    prior ``gslides_create_presentation`` call, or from
    ``gdocs_find_doc_by_title``. After this returns, point the user at the
    deck's "Video → Render frames" menu (or ``project_url``) to run the
    render once. (The Apps Script scopes are in the baseline grant, so the
    deploy itself needs no second OAuth consent; the in-editor
    ``renderFrames`` run has its own one-time authorization prompt for the
    Slides / Drive / UrlFetch scopes the renderer uses.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call.
    if not presentation_id or not presentation_id.strip():
        raise ValueError(
            "presentation_id cannot be empty — pass the Drive ID of the "
            "Google Slides deck to render (the ID part of the deck's URL)."
        )
    if not frame_prefix or not _FRAME_PREFIX_RE.match(frame_prefix):
        raise ValueError(
            f"frame_prefix {frame_prefix!r} is not a valid filename stem "
            f"— use letters, digits, '_', or '-' only (frames are named "
            f"'<prefix>_001.png'). No spaces, dots, or path separators."
        )

    # Resolve the output folder name (sanitize an override; default
    # otherwise). A provided-but-empty-after-sanitize name is rejected.
    if output_folder_name is not None:
        folder_name = _slugify_folder_name(output_folder_name)
        if not folder_name:
            raise ValueError(
                "output_folder_name became empty after stripping path "
                "separators / control characters — pass a plain folder "
                "name (no slashes)."
            )
    else:
        folder_name = "appscriptly video frames"

    # 2. Validate the container is actually a Slides deck. auto_detect
    #    reads the Drive mimeType + raises ValueError for non-Doc/Sheet/
    #    Slides; we additionally require it be SLIDES (a Doc/Sheet can't be
    #    rendered to video frames). This surfaces a clear error before any
    #    project is created.
    kind = _auto_detect_container_kind(creds, presentation_id)
    if kind != _SLIDES_KIND:
        raise ValueError(
            f"as_generate_video_deck requires a Google Slides presentation, "
            f"but {presentation_id!r} is a {kind!r} container. Pass a Slides "
            f"deck ID. (To add automation to a Doc or Sheet, use "
            f"as_install_doc_menu / as_install_sheet_dashboard / "
            f"as_generate_bound_script instead.)"
        )

    # 3. Generate the .gs renderer body (onOpen menu + renderFrames loop).
    script_body = build_video_deck_script(
        presentation_id, folder_name, frame_prefix
    )

    # 4. Build the manifest — reuse build_manifest with oauth_scopes so the
    #    Slides-read / drive.file / UrlFetch scopes are declared (the
    #    renderer's getThumbnail + UrlFetchApp + folder writes need them).
    #    A menu is code, not a manifest field (the #138 manifest-reality
    #    finding); the onOpen menu needs no extra manifest scope beyond
    #    what we declare here.
    manifest_dict = _build_manifest({"oauth_scopes": _RENDER_SCOPES})

    # 5. Default the project name when not supplied.
    project_name = name or "appscriptly video deck renderer"

    # 6. Deploy via the SAME machinery as as_generate_bound_script: create
    #    the bound project (parentId=presentation_id), push the body +
    #    manifest, cut a version + deploy.
    project = _create_bound_project(creds, presentation_id, project_name)
    script_id = project["scriptId"]

    _set_project_content(creds, script_id, script_body, manifest_dict)

    deployment = _create_deployment(
        creds, script_id, description=f"{project_name} — initial deploy"
    )
    deployment_id = deployment["deploymentId"]

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "presentation_id": presentation_id,
        "output_folder_name": folder_name,
        # frames_expected is intentionally None here: the slide count is
        # only known when renderFrames runs (we don't read the deck from
        # this tool — that would be an extra API round-trip for a number
        # the user gets from the rendered manifest.json anyway). The
        # schema permits null; the activation_note explains.
        "frames_expected": None,
        "render_function": "renderFrames",
        "activation_note": (
            f"Frames are NOT generated yet. Open the deck and click "
            f"'Video > Render frames' (the menu this tool installed), OR "
            f"open the script editor at the project_url and run "
            f"'renderFrames' once (approve the authorization prompt). That "
            f"exports each slide as a PNG into a Drive folder named "
            f"'{folder_name}' and writes a manifest.json listing the frames "
            f"in order. Decks up to ~{_SINGLE_PASS_SLIDE_GUIDANCE} slides "
            f"render in a single pass (Apps Script's ~6-minute execution "
            f"cap); much larger decks may need chunking. The PNG-to-MP4 "
            f"encode is a separate step."
        ),
        "project_url": f"https://script.google.com/d/{script_id}/edit",
    }
