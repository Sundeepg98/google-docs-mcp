"""``as_generate_video_deck`` — render a Slides deck to PNG frames (PR-Δ11).

The RENDER half of the slides-to-video pipeline. A *use-case* tool layered
on the PR-Δ7 bound-script primitive (``services/apps_script/api.py``):
given a Google Slides presentation ID, it deploys a *bound* Apps Script
whose ``renderFrames()`` function renders each slide to a PNG and POSTs
the bytes to the appscriptly server's signed frame-staging endpoint.

**Base-tier handoff (no ``drive.readonly``).** The frames used to land in
a user-owned Drive folder + a ``manifest.json`` that the encode half then
re-read with ``drive.readonly`` (``drive.file`` is a per-file grant that
can't see files the bound script creates inside an app-created folder).
The base-tier redesign POSTs each frame straight to the server instead, so
the encode reads them off the server's own volume — zero Drive read scope.
This is what let the free base tier drop ``drive.readonly``.
``as_encode_video`` is the server-side ffmpeg half; this tool hands it the
frames via a ``frames_batch_id``.

**Composition, not reimplementation.** The deploy machinery is reused
verbatim from the #138 primitive's ``api.py``:

  * ``auto_detect_container_kind`` — to VALIDATE the container is a Slides
    presentation (reject Docs / Sheets / Forms with a clear error before
    any project is created).
  * ``build_manifest`` — to derive the manifest's ``oauthScopes`` from the
    scopes this script actually exercises (Slides read + ``UrlFetchApp``
    for the thumbnail fetch AND the frame POST).
  * ``create_bound_project`` → ``set_project_content`` →
    ``create_deployment`` — the same create/push/deploy sequence
    ``as_generate_bound_script`` orchestrates.

This module's OWN contribution is the ``.gs`` *script-body synthesis* —
the ``renderFrames()`` loop (advanced Slides
``Slides.Presentations.Pages.getThumbnail`` at ``LARGE`` size →
``UrlFetchApp.fetch(contentUrl)`` → ``UrlFetchApp.fetch(POST)`` the bytes
to the server per slide) plus an ``onOpen()`` "Video" menu for one-click
run.

**How frame export works (verified against the Slides REST reference).**
``presentations.pages.getThumbnail`` returns a ``Thumbnail`` with a
``contentUrl`` (a short-lived URL to the rendered PNG); ``thumbnailSize:
'LARGE'`` is 1600px wide; the default ``mimeType`` is PNG. The advanced
Slides service exposes this as
``Slides.Presentations.Pages.getThumbnail(presentationId, pageObjectId,
{thumbnailProperties: {...}})``. The script then fetches the PNG bytes
from ``contentUrl`` with ``UrlFetchApp.fetch`` and POSTs them to the
server's signed frame-staging endpoint. (Server-side thumbnail rendering
is why the deck read needs the Slides scope and the fetch+POST need
``script.external_request``; the renderer no longer touches Drive.)

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
from typing import TYPE_CHECKING

from appscriptly.activation import build_activation_fields
from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._observability import (
    reporter_helper_source as _reporter_helper_source,
    wrap_generated_body as _wrap_generated_body,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    render as _render,
)
from appscriptly.services.apps_script.api import (
    auto_detect_container_kind as _auto_detect_container_kind,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA

# Imported for parity with the other apps_script feature files; not used
# on the happy path (the @workspace_tool(creds=True) envelope injects
# creds and maps HttpError → ToolError). Kept top-level so a future
# error-path addition doesn't need a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
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
#   * script.external_request — UrlFetchApp.fetch: pull the rendered PNG
#     bytes from the thumbnail's short-lived contentUrl, AND POST those
#     bytes to the appscriptly server's signed frame-staging endpoint.
# Base-tier redesign: the renderer NO LONGER writes to Drive — it POSTs
# each frame to the server instead of creating a Drive folder + files —
# so ``drive.file`` is NOT in the renderer's manifest scope set. Both
# remaining scopes are in the baseline auth.SCOPES grant (presentations
# is baseline; script.external_request rides the Apps Script deploy
# authorization) so the deploy needs no second consent; the manifest
# declares them so the in-editor renderFrames run is authorized.
_PRESENTATIONS_SCOPE = "https://www.googleapis.com/auth/presentations"
_URLFETCH_SCOPE = "https://www.googleapis.com/auth/script.external_request"

_RENDER_SCOPES = [_PRESENTATIONS_SCOPE, _URLFETCH_SCOPE]

# A conservative single-pass slide-count guidance surfaced in the return
# payload + docstring. Not enforced (the script renders whatever the deck
# has); it documents where the ~6-min execution cap starts to bite.
_SINGLE_PASS_SLIDE_GUIDANCE = 50


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
    upload_base_url: str,
    upload_token: str,
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
         * for each slide, calls the advanced Slides service
           ``Slides.Presentations.Pages.getThumbnail(...)`` at
           ``thumbnailSize: 'LARGE'`` (PNG), fetches the rendered bytes
           from the returned ``contentUrl`` via ``UrlFetchApp.fetch``,
           and **POSTs the PNG bytes straight to the appscriptly server's
           signed frame-staging endpoint** (``UPLOAD_BASE/<index>?token=
           UPLOAD_TOKEN``) — NOT to Drive.

    **Base-tier handoff (no ``drive.readonly``).** The slide RENDER is
    unchanged — slides are still rasterized via the Slides thumbnail
    endpoint as the user. What changed is the HANDOFF: the old version
    wrote PNGs + a ``manifest.json`` to a user-owned Drive folder, which
    only ``drive.readonly`` could re-read (``drive.file`` is a per-file
    grant that doesn't cover files the bound script creates). Now the
    bytes go to the server, which reads them off its own volume — zero
    Drive read scope. ``script.external_request`` (already in the render
    scope set) covers the ``UrlFetchApp`` POST.

    Args:
        presentation_id: the Slides deck ID (embedded as a JS literal for
            the explicit advanced-service getThumbnail ``presentationId``).
        upload_base_url: the server's per-batch frame-staging base, i.e.
            ``<server>/upload/frames/<batch_id>``. Each frame is POSTed to
            ``<base>/<1-based-index>?token=<upload_token>``.
        upload_token: the matching HMAC batch token authorizing the POSTs.

    Returns:
        The complete ``.gs`` source as a string, trailing newline
        included.
    """
    pid = _js_string(presentation_id)
    base = _js_string(upload_base_url)
    token = _js_string(upload_token)

    # The renderFrames body. Wrapped (below) in the appscriptly failure
    # reporter so an unattended render error is emailed to the owner instead
    # of only landing in the execution log; the wrapper rethrows so the run
    # is still recorded as failed (gap #5).
    render_inner = f"""\
  var presentation = SlidesApp.getActivePresentation();
  var slides = presentation.getSlides();
  var uploaded = 0;

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
    var frameNumber = i + 1;  // 1-based; server names files frame_%04d.png
    // POST the PNG bytes straight to the server's frame-staging endpoint.
    var postUrl = UPLOAD_BASE + '/' + frameNumber
      + '?token=' + encodeURIComponent(UPLOAD_TOKEN);
    var up = UrlFetchApp.fetch(postUrl, {{
      method: 'post',
      contentType: 'image/png',
      payload: response.getBlob().getBytes(),
      muteHttpExceptions: true
    }});
    var code = up.getResponseCode();
    if (code < 200 || code >= 300) {{
      throw new Error('Frame ' + frameNumber + ' upload failed (HTTP '
        + code + '): ' + up.getContentText().slice(0, 300));
    }}
    uploaded++;
  }}

  return uploaded;"""

    return f"""\
// Auto-generated by appscriptly as_generate_video_deck (PR-Δ11).
// Renders each slide of this Google Slides deck to a PNG frame and POSTs
// the bytes to the appscriptly server's signed frame-staging endpoint
// (base-tier handoff — no Drive read scope). This is the RENDER half of
// a slides-to-video pipeline; the PNG->MP4 encode runs server-side.
// Runs on Google's infrastructure.

var PRESENTATION_ID = {pid};
var UPLOAD_BASE = {base};    // <server>/upload/frames/<batch_id>
var UPLOAD_TOKEN = {token};  // HMAC batch token (authorizes the POSTs)
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
 * Renders every slide to a PNG frame and uploads it to the appscriptly
 * server. Run this ONCE (Video > Render frames, or the editor Run
 * button) to produce the frames — deploying the script does not run it.
 * A failure is emailed to you (best-effort) then rethrown.
 *
 * Returns the number of frames uploaded (handy when run from the editor).
 */
function renderFrames() {{
{_wrap_generated_body("renderFrames", render_inner)}}}

{_reporter_helper_source().rstrip()}
"""


@workspace_tool(
    title="Render a Slides deck to video frames",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment for the deck —
    # re-running installs a SECOND renderer bound to the same deck. NOT
    # idempotent (same convention as as_generate_bound_script /
    # as_install_sheet_dashboard / gsheets_create_presentation).
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_GENERATE_VIDEO_DECK_OUTPUT_SCHEMA,
)
def as_generate_video_deck(
    creds: Credentials,
    presentation_id: str,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Render each slide of a Google Slides deck to a PNG frame.

    This is the FIRST HALF of a slides-to-video pipeline: it deploys a
    bound Apps Script whose ``renderFrames()`` function exports every
    slide to a PNG and uploads it to the appscriptly server's signed
    frame-staging area. The PNG→MP4 ENCODE is the SECOND half
    (``as_encode_video``) — this tool's job ends at "frames uploaded";
    you pass the returned ``frames_batch_id`` to ``as_encode_video``.

    USE WHEN: the user wants to turn a Slides deck into video frames /
    eventually a video — "render my deck to frames for a video", "export
    each slide as an image for a slideshow video". For just viewing or
    editing the deck, use the ``gslides_*`` tools (no script needed).

    Composition over ``as_generate_bound_script``: this writes the
    ``renderFrames`` + ``onOpen`` boilerplate for you and deploys it as a
    bound Apps Script using the same create→push→deploy machinery the
    generic primitive uses. The renderer uses the advanced Slides service
    ``getThumbnail`` at LARGE size (1600px PNG) per slide, fetches the
    rendered bytes, and POSTs them to the server.

    BASE-TIER HANDOFF (no ``drive.readonly``): the frames used to land in
    a user-owned Drive folder, which the encode half re-read with
    ``drive.readonly`` (``drive.file`` is a per-file grant that can't see
    files the bound script creates). Now the renderer POSTs each frame
    straight to the server, so the encode reads them off the server's own
    volume — zero Drive read scope. This is what let the free base tier
    drop ``drive.readonly``.

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
    would need chunking (a future enhancement). The upload token expires
    ~30 minutes after this call, so run the render promptly.

    Args:
        presentation_id: Drive ID of the Google Slides presentation to
            render (the ID part of the deck's URL). The renderer is bound
            to THIS deck. Must be a Slides file — a Doc / Sheet / Form ID
            is rejected with a clear error.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated video-deck name.
        on_conflict: what to do when a video-deck renderer from THIS tool
            already exists on this presentation. "new" (the default) always
            installs a fresh one (which can leave duplicate menus);
            "replace" uninstalls the prior install(s) on this presentation
            first (no duplicate, no orphan); "skip" returns the existing
            install unchanged instead of adding a duplicate. Keyed by (this
            tool, this container) via appscriptly's automation ledger; the
            response adds ``reused_existing`` / ``replaced_count``.

    Returns:
        ``{script_id, deployment_id, presentation_id, frames_batch_id,
        frames_expected, render_function, activation_note, project_url}``.
        ``frames_batch_id`` is the handle you pass to ``as_encode_video``
        after ``renderFrames`` runs. ``frames_expected`` is null until the
        render runs. ``render_function`` is ``"renderFrames"``;
        ``project_url`` deep-links to the script editor.

    Raises:
        ValueError: empty ``presentation_id`` — rejected client-side
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
    render once, then call ``as_encode_video`` with ``frames_batch_id``.
    (The Apps Script scopes are in the baseline grant, so the deploy
    itself needs no second OAuth consent; the in-editor ``renderFrames``
    run has its own one-time authorization prompt for the Slides + UrlFetch
    scopes the renderer uses.)
    """
    # 1. Validate inputs cheaply, client-side, BEFORE any API call.
    if not presentation_id or not presentation_id.strip():
        raise ValueError(
            "presentation_id cannot be empty — pass the Drive ID of the "
            "Google Slides deck to render (the ID part of the deck's URL)."
        )

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

    # 3. Codegen via the recipe registry (_recipes.py) — the SINGLE source
    #    for this tool's .gs body + manifest. video_deck is the one recipe
    #    with a pre_mint hook: it mints the impure inputs the renderer embeds
    #    (a server-side frames batch id + a user-bound single-use HMAC upload
    #    token + the server base URL — base-tier handoff, no Drive folder, no
    #    drive.readonly), injecting them into params BEFORE the pure render()
    #    runs build_video_deck_script + threads the render manifest plan
    #    (presentations + script.external_request + add_mail_scope). The
    #    byte-identity pin drives BOTH this tool and pre_mint through the same
    #    stubbed sources, proving the output is unchanged. batch_id is pulled
    #    back out for the frames_batch_id / activation payload.
    spec = _RECIPES["as_generate_video_deck"]
    # Invariant (pinned by test_only_video_deck_carries_a_pre_mint_hook):
    # video_deck is the sole recipe carrying a pre_mint hook.
    assert spec.pre_mint is not None
    params = spec.pre_mint({"presentation_id": presentation_id, "name": name})
    rendered = _render(spec, params)
    batch_id = params["batch_id"]

    # 4. Deploy via the SAME machinery as as_generate_bound_script: create
    #    the bound project (parentId=presentation_id), push the body +
    #    manifest, cut a version + deploy.
    result = _mint_bound_automation(
        creds,
        tool=spec.name,
        container_id=presentation_id,
        container_kind=spec.container_kind,
        project_name=spec.project_name(params),
        script_body=rendered.script_body,
        manifest_dict=rendered.manifest,
        on_conflict=on_conflict,
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "presentation_id": presentation_id,
        # The batch id ties the render to the encode. Pass it to
        # as_encode_video once renderFrames has run.
        "frames_batch_id": batch_id,
        # frames_expected is intentionally None here: the slide count is
        # only known when renderFrames runs. The schema permits null; the
        # activation_note explains.
        "frames_expected": None,
        "render_function": "renderFrames",
        "activation_note": (
            f"Frames are NOT generated yet. Open the deck and click "
            f"'Video > Render frames' (the menu this tool installed), OR "
            f"open the script editor at the project_url and run "
            f"'renderFrames' once (approve the authorization prompt). That "
            f"renders each slide and uploads it to the appscriptly server "
            f"(no Drive folder, no extra permission). When it finishes, "
            f"call as_encode_video with frames_batch_id='{batch_id}' to get "
            f"the MP4. Decks up to ~{_SINGLE_PASS_SLIDE_GUIDANCE} slides "
            f"render in a single pass (Apps Script's ~6-minute execution "
            f"cap); much larger decks may need chunking. The upload token "
            f"expires in ~30 minutes — run the render promptly."
        ),
        # Unified activation contract (Stream 3): activation_note is the
        # legacy alias (it carries the extra batch/encode/token detail);
        # these carry the canonical shape. Activation = running renderFrames
        # once.
        **build_activation_fields(
            script_id,
            "renderFrames",
            (
                f"Frames are NOT generated yet. Open the deck and click "
                f"'Video > Render frames' (the menu this tool installed), or "
                f"open the script editor at the activation_url, select "
                f"'renderFrames' in the function dropdown and click Run once, "
                f"then approve the authorization prompt. That renders each "
                f"slide and uploads it to the appscriptly server. When it "
                f"finishes, call as_encode_video with "
                f"frames_batch_id='{batch_id}' to get the MP4. The upload "
                f"token expires in about 30 minutes, so run the render "
                f"promptly."
            ),
        ),
        "project_url": f"https://script.google.com/d/{script_id}/edit",
    }
