"""Google Slides REST wrapper (v2.3.2 — minimal start).

Only the outline-read + find/replace surface ships in this PR:

  * ``get_outline``      — ``presentations.get`` (structure + per-
                            slide text extraction)
  * ``replace_all_text`` — ``presentations.batchUpdate`` with the
                            single ``replaceAllText`` request type
  * ``create_presentation`` — ``presentations.create`` (creates an
                              empty deck so the read/write tools
                              have something to target in a single-
                              call workflow; matches the Sheets
                              ``create_spreadsheet`` shape)

The Slides ``batchUpdate`` tagged-union (~40 request types:
addSlide, replaceImage, updateTextStyle, updateShapeProperties,
etc.) is DELIBERATELY DEFERRED to a follow-up PR per the multi-
service feasibility audit ("clean bolt-on" — the audit explicitly
flagged this as scope creep beyond MVP). The single-request-type
carve-out for ``replaceAllText`` doesn't commit to the full tagged-
union design.

**Scope note.** Calls require
``https://www.googleapis.com/auth/presentations`` in the OAuth
consent. This scope was added to ``auth.SCOPES`` and
``oauth_google.GOOGLE_API_SCOPES`` in v2.3.2; existing user grants
pick it up automatically on next token refresh via Google's
``include_granted_scopes=true`` flow (same incremental-consent
pattern that handled the earlier ``drive.readonly`` + Apps Script +
``spreadsheets`` scope additions — most recently proven in PR #119).

**Text extraction helper.** ``_extract_slide_text`` flattens text
runs from a slide's ``pageElements`` into a single string. Slides'
text model is a nested ``shape.text.textElements[].textRun``
structure; the consumer just wants the readable content for
outline / search purposes. Tables, images, embedded charts, and
other element types are ignored — they don't carry slide-level
copy text.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


def _unique_object_id(prefix: str) -> str:
    """A fresh Slides objectId: ``<prefix>_<12 hex>``.

    Slides ``createSlide`` / ``createImage`` / ``createTable`` reject a
    duplicate objectId within one presentation, so a CONSTANT id 400s
    ('object ID already in use') on the second call against the same
    deck. Generating a unique id per call makes these create tools
    repeatable. Slides requires objectIds to match ``[a-zA-Z0-9_-]`` and
    be 5-50 chars; the longest prefix here (``appscriptly_title_ph``,
    20 chars) + ``_`` + 12 hex = 33 chars, comfortably within range, and
    every character is in the allowed set.
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_outline(creds: Credentials, presentation_id: str) -> dict:
    """Read a presentation's outline via ``presentations.get``.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope.
        presentation_id: The Slides file ID (the ID part of the
            sharing URL).

    Returns:
        ``{presentation_id, title, url, slides: [...]}``. Each entry
        in ``slides`` is ``{object_id, index, layout, text, elements,
        notes}``:

          * ``object_id``, Slides' stable per-slide identifier (the
            Slides equivalent of docs' tab IDs, per the multi-service
            audit).
          * ``index``, the slide's 0-based position in the deck (the
            same order Slides shows in the filmstrip). Stable handle
            for "the 3rd slide" without re-deriving it caller-side.
          * ``layout``, the layout's objectId (empty string when the
            slide has no explicit layout).
          * ``text``, the readable copy concatenated from all shapes
            on the slide. Empty when the slide has no text shapes
            (e.g. an image-only slide).
          * ``elements``, a list of ``{object_id, type}`` for every
            page element on the slide, classified as ``shape`` /
            ``table`` / ``image`` / ``line`` / ``video`` /
            ``word_art`` / ``sheets_chart`` / ``group`` / ``unknown``
            (see ``_classify_page_element``). Lets a caller inventory
            a slide's structure (count tables, find an image's
            objectId to delete, etc.) without a second round trip.
          * ``notes``, the speaker-notes text for the slide (read
            from ``slideProperties.notesPage``), or the empty string
            when the slide has no notes.

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx, let it
            propagate; the tool-layer envelope renders it.
    """
    slides = get_service("slides", "v1", credentials=creds)
    # PR-Δ3.5: gslides_get_outline is readonly=True, idempotent=True.
    presentation = execute_with_retry(
        lambda: slides.presentations().get(
            presentationId=presentation_id,
        ).execute(),
        idempotent=True,
        op_name="slides.presentations.get",
    )
    return {
        "presentation_id": presentation_id,
        "title": presentation.get("title", ""),
        "url": f"https://docs.google.com/presentation/d/{presentation_id}/edit",
        "slides": [
            {
                "object_id": slide.get("objectId", ""),
                "index": index,
                "layout": (
                    slide.get("slideProperties", {})
                    .get("layoutObjectId", "")
                ),
                "text": _extract_slide_text(slide),
                "elements": _list_slide_elements(slide),
                "notes": _extract_slide_notes(slide),
            }
            for index, slide in enumerate(presentation.get("slides", []))
        ],
    }


def replace_all_text(
    creds: Credentials,
    presentation_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = True,
) -> dict:
    """Replace all occurrences of ``find_text`` across every slide.

    Uses ``presentations.batchUpdate`` with a SINGLE ``replaceAllText``
    request — the most common find/replace use case carved out from
    the full batchUpdate tagged-union surface (which is deferred).

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope.
        presentation_id: The Slides file ID.
        find_text: The literal text to search for. Empty rejected
            client-side (Slides would 400 anyway).
        replace_text: What to replace it with. May be empty
            (effectively deletes the matched text).
        match_case: When True (default), the match is case-sensitive
            — ``"Foo"`` matches ``"Foo"`` but not ``"FOO"``. False
            does case-insensitive matching.

    Returns:
        ``{presentation_id, occurrences_changed}`` — flat envelope
        echoing the ID back plus the total count of replacements
        Slides actually performed. The count may be 0 (no matches);
        that's a valid outcome, not an error.

    Raises:
        ValueError: ``find_text`` empty.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not find_text:
        raise ValueError(
            "find_text cannot be empty — pass at least one character "
            "to search for. (Empty find_text would match everywhere "
            "and Slides rejects it with HTTP 400.)"
        )

    slides = get_service("slides", "v1", credentials=creds)
    body = {
        "requests": [
            {
                "replaceAllText": {
                    "containsText": {
                        "text": find_text,
                        "matchCase": match_case,
                    },
                    "replaceText": replace_text,
                },
            },
        ],
    }
    # PR-Δ3.5: gslides_replace_all_text is annotated idempotent=True —
    # replacing the same text twice is a no-op once the first call
    # already replaced everything; the second call's
    # ``occurrencesChanged`` is 0.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body=body,
        ).execute(),
        idempotent=True,
        op_name="slides.presentations.batchUpdate.replaceAllText",
    )
    occurrences = sum(
        r.get("replaceAllText", {}).get("occurrencesChanged", 0)
        for r in resp.get("replies", [])
    )
    return {
        "presentation_id": presentation_id,
        "occurrences_changed": occurrences,
    }


def create_presentation(creds: Credentials, title: str) -> dict:
    """Create an empty Google Slides presentation via ``presentations.create``.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope.
        title: The title for the new presentation. Becomes the Drive
            filename AND the presentation's display title.

    Returns:
        ``{presentation_id, url, title}`` — same flat envelope as
        ``gsheets_create_spreadsheet`` (PR #119) and
        ``gdocs_make_tabbed_doc``. Callers can immediately pipe
        ``presentation_id`` into ``get_outline`` / ``replace_all_text``.

    Raises:
        ValueError: empty / whitespace ``title``.
        HttpError: from the underlying SDK — propagated.

    Note:
        The created presentation is owned by the OAuth user and lands
        in Drive root by default. Move it via ``gdocs_move_to_folder``.
        Slides auto-adds a default title slide; subsequent
        ``batchUpdate`` calls (when the tagged-union ships) can
        replace / append slides as needed.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")

    slides = get_service("slides", "v1", credentials=creds)
    resp = slides.presentations().create(
        body={"title": title.strip()},
    ).execute()
    pid = resp["presentationId"]
    return {
        "presentation_id": pid,
        "url": f"https://docs.google.com/presentation/d/{pid}/edit",
        "title": resp.get("title", title.strip()),
    }


# Predefined layouts that expose a TITLE placeholder (for the optional
# ``title`` insert). ``TITLE_AND_BODY`` additionally exposes a BODY
# placeholder (for the optional ``body`` insert). Restricting to this
# pair keeps placeholder-targeting deterministic; the full predefined-
# layout enum (~12 values) is a follow-up if a real consumer needs it.
_LAYOUTS_WITH_TITLE = frozenset({"TITLE_AND_BODY", "TITLE_ONLY"})
_LAYOUTS_WITH_BODY = frozenset({"TITLE_AND_BODY"})


def add_slide(
    creds: Credentials,
    presentation_id: str,
    title: str | None = None,
    body: str | None = None,
    layout: str = "TITLE_AND_BODY",
) -> dict:
    """Append a slide (optionally with title + body text) to a deck.

    Uses ``presentations.batchUpdate`` with a ``createSlide`` request
    (carrying a ``predefinedLayout`` + deterministic
    ``placeholderIdMappings``) followed by ``insertText`` requests
    targeting those placeholders — all in a SINGLE batchUpdate round
    trip, so the new slide and its text commit atomically.

    This is the first ``createSlide``/``insertText`` carve-out from the
    deferred Slides batchUpdate tagged-union. Pairs with
    ``create_presentation`` to produce a NON-empty deck (the gap the
    minimal trio left: create made an empty deck, replace_all_text
    could only swap text that already existed).

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (already in ``auth.SCOPES`` baseline — no extra grant).
        presentation_id: The Slides file ID to append the slide to.
        title: Optional title-placeholder text. Inserted only when the
            chosen ``layout`` exposes a TITLE placeholder and the
            string is non-empty.
        body: Optional body-placeholder text. Inserted only when the
            chosen ``layout`` exposes a BODY placeholder (i.e.
            ``TITLE_AND_BODY``) and the string is non-empty.
        layout: A Slides ``predefinedLayout`` enum value. Supported
            here: ``"TITLE_AND_BODY"`` (default), ``"TITLE_ONLY"``,
            ``"BLANK"``. Other values rejected client-side.

    Returns:
        ``{presentation_id, slide_object_id, url}`` — flat envelope.
        ``slide_object_id`` is the new slide's stable ID (usable as a
        later batchUpdate target / matches ``get_outline``'s
        ``object_id``). ``url`` deep-links to the slide.

    Raises:
        ValueError: ``layout`` unsupported, or ``body`` supplied for a
            layout without a BODY placeholder.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated;
            the tool-layer envelope renders it.
    """
    supported = {"TITLE_AND_BODY", "TITLE_ONLY", "BLANK"}
    if layout not in supported:
        raise ValueError(
            f"layout must be one of {sorted(supported)} — got {layout!r}."
        )
    if body and layout not in _LAYOUTS_WITH_BODY:
        raise ValueError(
            f"body text requires a layout with a BODY placeholder "
            f"(TITLE_AND_BODY) — layout {layout!r} has none. Pass "
            f"body=None or use layout='TITLE_AND_BODY'."
        )

    slides = get_service("slides", "v1", credentials=creds)

    # UNIQUE objectIds (per call) so the follow-up insertText requests can
    # target the placeholders created in the SAME batch (Slides assigns
    # random IDs otherwise, which we couldn't reference until a second
    # round trip) WITHOUT colliding on a second add_slide against the same
    # deck — a constant id 400s 'object ID already in use'.
    slide_id = _unique_object_id("appscriptly_slide")
    title_ph_id = _unique_object_id("appscriptly_title_ph")
    body_ph_id = _unique_object_id("appscriptly_body_ph")

    placeholder_mappings: list[dict] = []
    want_title = bool(title) and layout in _LAYOUTS_WITH_TITLE
    want_body = bool(body) and layout in _LAYOUTS_WITH_BODY
    if want_title:
        placeholder_mappings.append({
            "objectId": title_ph_id,
            "layoutPlaceholder": {"type": "TITLE", "index": 0},
        })
    if want_body:
        placeholder_mappings.append({
            "objectId": body_ph_id,
            "layoutPlaceholder": {"type": "BODY", "index": 0},
        })

    create_slide: dict = {
        "objectId": slide_id,
        "slideLayoutReference": {"predefinedLayout": layout},
    }
    if placeholder_mappings:
        create_slide["placeholderIdMappings"] = placeholder_mappings

    requests: list[dict] = [{"createSlide": create_slide}]
    if want_title:
        requests.append({
            "insertText": {"objectId": title_ph_id, "text": title or ""},
        })
    if want_body:
        requests.append({
            "insertText": {"objectId": body_ph_id, "text": body or ""},
        })

    # NOT idempotent: each call appends ANOTHER slide. Same convention
    # as create_presentation. The objectIds are unique per call (see
    # _unique_object_id) so repeated calls against the same deck don't
    # collide on 'object ID already in use'.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.createSlide",
    )

    # The createSlide reply echoes the slide's objectId. Fall back to
    # the deterministic id we requested if the reply omits it.
    created_id = slide_id
    for reply in resp.get("replies", []) or []:
        cs = reply.get("createSlide")
        if cs and cs.get("objectId"):
            created_id = cs["objectId"]
            break

    return {
        "presentation_id": presentation_id,
        "slide_object_id": created_id,
        "url": (
            f"https://docs.google.com/presentation/d/{presentation_id}"
            f"/edit#slide=id.{created_id}"
        ),
    }


def _resolve_speaker_notes_object_id(
    slides: object,
    presentation_id: str,
    slide_object_id: str,
) -> str:
    """Resolve a slide's speaker-notes shape objectId.

    Speaker notes are text inside a dedicated shape on the slide's
    notesPage. Its objectId is NOT something the caller knows up front -
    Slides assigns it, so we read the presentation and pull
    ``slideProperties.notesPage.notesProperties.speakerNotesObjectId``
    for the requested slide.

    Args:
        slides: A Slides v1 service Resource.
        presentation_id: The Slides file ID.
        slide_object_id: The target slide's objectId (from
            ``get_outline`` / ``add_slide``).

    Returns:
        The speaker-notes shape objectId.

    Raises:
        ValueError: the slide isn't found in the deck, or it has no
            resolvable speaker-notes shape (no notesPage /
            speakerNotesObjectId).
    """
    presentation = execute_with_retry(
        lambda: slides.presentations().get(  # type: ignore[attr-defined]
            presentationId=presentation_id,
        ).execute(),
        idempotent=True,
        op_name="slides.presentations.get.notes_id",
    )
    for slide in presentation.get("slides", []) or []:
        if slide.get("objectId") != slide_object_id:
            continue
        notes_id = (
            slide.get("slideProperties", {})
            .get("notesPage", {})
            .get("notesProperties", {})
            .get("speakerNotesObjectId")
        )
        if not notes_id:
            raise ValueError(
                f"slide {slide_object_id!r} has no resolvable speaker-notes "
                "shape (no notesPage.notesProperties.speakerNotesObjectId). "
                "This is unusual, every slide normally has a notes shape."
            )
        return notes_id
    raise ValueError(
        f"slide {slide_object_id!r} not found in presentation "
        f"{presentation_id!r}. Pass a slide objectId from "
        "gslides_get_outline (the per-slide object_id)."
    )


def set_speaker_notes(
    creds: Credentials,
    presentation_id: str,
    slide_object_id: str,
    notes_text: str,
) -> dict:
    """Set (replace) the speaker notes of a single slide.

    Resolves the slide's speaker-notes shape objectId (via
    ``_resolve_speaker_notes_object_id``), then issues ONE
    ``presentations.batchUpdate`` that deletes any existing notes text
    and inserts ``notes_text``, so the call is a full REPLACE (the
    slide ends with exactly ``notes_text`` as its notes), committed
    atomically.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (already in ``auth.SCOPES`` baseline, no extra grant).
        presentation_id: The Slides file ID.
        slide_object_id: The target slide's objectId (from
            ``get_outline``'s per-slide ``object_id`` or ``add_slide``).
        notes_text: The notes text to set. May be empty, an empty
            string CLEARS the slide's notes (the deleteText runs; no
            insertText is emitted).

    Returns:
        ``{presentation_id, slide_object_id, speaker_notes_object_id,
        notes_text}``, echoes the request plus the resolved notes
        shape objectId.

    Raises:
        ValueError: the slide isn't found / has no notes shape (from
            ``_resolve_speaker_notes_object_id``).
        HttpError: from the underlying SDK on 4xx / 5xx, propagated.
    """
    slides = get_service("slides", "v1", credentials=creds)
    notes_object_id = _resolve_speaker_notes_object_id(
        slides, presentation_id, slide_object_id
    )

    # deleteText with an ALL range clears whatever notes exist (a no-op
    # on already-empty notes, Slides accepts it), then insertText sets
    # the new copy. insertText rejects an empty string, so for an empty
    # notes_text we emit ONLY the delete (clearing the notes).
    requests: list[dict] = [
        {
            "deleteText": {
                "objectId": notes_object_id,
                "textRange": {"type": "ALL"},
            }
        }
    ]
    if notes_text:
        requests.append({
            "insertText": {
                "objectId": notes_object_id,
                "insertionIndex": 0,
                "text": notes_text,
            }
        })

    # idempotent=True: setting the same notes twice yields the same
    # state (delete-all then insert the same text is deterministic).
    execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=True,
        op_name="slides.presentations.batchUpdate.setSpeakerNotes",
    )
    return {
        "presentation_id": presentation_id,
        "slide_object_id": slide_object_id,
        "speaker_notes_object_id": notes_object_id,
        "notes_text": notes_text,
    }


# EMU (English Metric Units), Slides' geometry unit. 914400 EMU = 1
# inch. A default slide is 10in × 5.63in (16:9). These defaults place a
# created element at a comfortable inset with a readable size; callers
# can override. Kept module-level so create_image / create_table share
# one source of truth.
_EMU_PER_INCH = 914400
_DEFAULT_X_EMU = _EMU_PER_INCH * 1          # 1in from the left
_DEFAULT_Y_EMU = _EMU_PER_INCH * 1          # 1in from the top
_DEFAULT_W_EMU = _EMU_PER_INCH * 4          # 4in wide
_DEFAULT_H_EMU = _EMU_PER_INCH * 3          # 3in tall


def _element_properties(
    slide_object_id: str,
    width_emu: int,
    height_emu: int,
    x_emu: int,
    y_emu: int,
) -> dict:
    """Build the ``elementProperties`` block shared by create requests.

    Pins the element to ``slide_object_id`` with an explicit size (EMU)
    and an affine ``transform`` placing its top-left at (x, y). This is
    the standard Slides positioning envelope used by createImage,
    createTable, createShape, etc.
    """
    return {
        "pageObjectId": slide_object_id,
        "size": {
            "width": {"magnitude": width_emu, "unit": "EMU"},
            "height": {"magnitude": height_emu, "unit": "EMU"},
        },
        "transform": {
            "scaleX": 1,
            "scaleY": 1,
            "translateX": x_emu,
            "translateY": y_emu,
            "unit": "EMU",
        },
    }


def create_image(
    creds: Credentials,
    presentation_id: str,
    slide_object_id: str,
    image_url: str,
    width_inches: float = 4.0,
    height_inches: float = 3.0,
    x_inches: float = 1.0,
    y_inches: float = 1.0,
) -> dict:
    """Insert an image (by public URL) onto a slide via ``createImage``.

    Uses ``presentations.batchUpdate`` with a single ``createImage``
    request. Slides fetches the image from ``image_url`` at insert time
    (the URL must be publicly reachable AND ≤ 50 MB / supported format
    per Slides' constraints — Google copies the bytes into the deck, so
    the URL doesn't need to stay live afterward).

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline — no extra grant).
        presentation_id: The Slides file ID.
        slide_object_id: The slide to place the image on (an
            ``object_id`` from ``gslides_add_slide`` /
            ``gslides_get_outline``).
        image_url: Publicly-accessible image URL (https). Slides
            rejects unreachable / oversized / unsupported-format URLs
            with HTTP 400.
        width_inches: Image width in inches (default 4.0).
        height_inches: Image height in inches (default 3.0).
        x_inches: Left inset in inches (default 1.0).
        y_inches: Top inset in inches (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, image_object_id, url}`` —
        ``image_object_id`` is the created image element's stable ID
        (a valid target for later transform / delete batchUpdates);
        ``url`` deep-links to the slide.

    Raises:
        ValueError: empty ``image_url`` / ``slide_object_id``, or a
            non-positive dimension.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not image_url or not image_url.strip():
        raise ValueError("image_url cannot be empty.")
    if not slide_object_id or not slide_object_id.strip():
        raise ValueError("slide_object_id cannot be empty.")
    if width_inches <= 0 or height_inches <= 0:
        raise ValueError("width_inches and height_inches must be positive.")

    slides = get_service("slides", "v1", credentials=creds)
    # Unique per call — a constant objectId 400s on the second create_image
    # against the same deck ('object ID already in use').
    image_id = _unique_object_id("appscriptly_image")
    requests = [
        {
            "createImage": {
                "objectId": image_id,
                "url": image_url.strip(),
                "elementProperties": _element_properties(
                    slide_object_id,
                    int(width_inches * _EMU_PER_INCH),
                    int(height_inches * _EMU_PER_INCH),
                    int(x_inches * _EMU_PER_INCH),
                    int(y_inches * _EMU_PER_INCH),
                ),
            },
        },
    ]
    # NOT idempotent: each call adds ANOTHER image. Same convention as
    # add_slide / create_presentation.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.createImage",
    )
    created_id = image_id
    for reply in resp.get("replies", []) or []:
        ci = reply.get("createImage")
        if ci and ci.get("objectId"):
            created_id = ci["objectId"]
            break
    return {
        "presentation_id": presentation_id,
        "slide_object_id": slide_object_id,
        "image_object_id": created_id,
        "url": (
            f"https://docs.google.com/presentation/d/{presentation_id}"
            f"/edit#slide=id.{slide_object_id}"
        ),
    }


def create_table(
    creds: Credentials,
    presentation_id: str,
    slide_object_id: str,
    rows: int,
    columns: int,
    width_inches: float = 6.0,
    height_inches: float = 3.0,
    x_inches: float = 1.0,
    y_inches: float = 1.0,
) -> dict:
    """Insert an empty ``rows`` × ``columns`` table onto a slide.

    Uses ``presentations.batchUpdate`` with a single ``createTable``
    request. The table is created empty; populate cells afterward with
    ``gslides_replace_all_text`` (template tokens) or a future
    cell-level insertText tool.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline — no extra grant).
        presentation_id: The Slides file ID.
        slide_object_id: The slide to place the table on.
        rows: Number of rows (≥ 1).
        columns: Number of columns (≥ 1).
        width_inches: Table width in inches (default 6.0).
        height_inches: Table height in inches (default 3.0).
        x_inches: Left inset in inches (default 1.0).
        y_inches: Top inset in inches (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, table_object_id, rows,
        columns, url}``. ``table_object_id`` is the created table's
        stable ID; ``url`` deep-links to the slide.

    Raises:
        ValueError: empty ``slide_object_id``, or ``rows`` / ``columns``
            < 1, or a non-positive dimension.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not slide_object_id or not slide_object_id.strip():
        raise ValueError("slide_object_id cannot be empty.")
    if rows < 1 or columns < 1:
        raise ValueError("rows and columns must each be >= 1.")
    if width_inches <= 0 or height_inches <= 0:
        raise ValueError("width_inches and height_inches must be positive.")

    slides = get_service("slides", "v1", credentials=creds)
    # Unique per call — a constant objectId 400s on the second create_table
    # against the same deck ('object ID already in use').
    table_id = _unique_object_id("appscriptly_table")
    requests = [
        {
            "createTable": {
                "objectId": table_id,
                "elementProperties": _element_properties(
                    slide_object_id,
                    int(width_inches * _EMU_PER_INCH),
                    int(height_inches * _EMU_PER_INCH),
                    int(x_inches * _EMU_PER_INCH),
                    int(y_inches * _EMU_PER_INCH),
                ),
                "rows": rows,
                "columns": columns,
            },
        },
    ]
    # NOT idempotent: each call adds ANOTHER table.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.createTable",
    )
    created_id = table_id
    for reply in resp.get("replies", []) or []:
        ct = reply.get("createTable")
        if ct and ct.get("objectId"):
            created_id = ct["objectId"]
            break
    return {
        "presentation_id": presentation_id,
        "slide_object_id": slide_object_id,
        "table_object_id": created_id,
        "rows": rows,
        "columns": columns,
        "url": (
            f"https://docs.google.com/presentation/d/{presentation_id}"
            f"/edit#slide=id.{slide_object_id}"
        ),
    }


# Slides ``createShape`` ``shapeType`` enum values exposed here. The full
# enum is ~140 values (every autoshape); this curated subset covers the
# shapes a deck-authoring agent actually reaches for (rectangles, ellipse,
# text box, the common callout/flow shapes). Restricting it keeps the
# surface deterministic + gives a helpful client-side error instead of a
# Slides 400 on a typo'd enum; widen if a real consumer needs more.
_SHAPE_TYPES = frozenset({
    "TEXT_BOX",
    "RECTANGLE",
    "ROUND_RECTANGLE",
    "ELLIPSE",
    "DIAMOND",
    "TRIANGLE",
    "RIGHT_TRIANGLE",
    "PARALLELOGRAM",
    "TRAPEZOID",
    "PENTAGON",
    "HEXAGON",
    "OCTAGON",
    "STAR_5",
    "RIGHT_ARROW",
    "LEFT_ARROW",
    "UP_ARROW",
    "DOWN_ARROW",
    "CLOUD",
    "SMILEY_FACE",
    "HEART",
    "WEDGE_RECTANGLE_CALLOUT",
    "WEDGE_ELLIPSE_CALLOUT",
})

# Slides ``createLine`` ``lineCategory`` enum. Only three connector
# categories exist (plus the legacy ``STRAIGHT`` alias is the simplest);
# ``STRAIGHT`` draws a plain segment, ``BENT`` / ``CURVED`` route around
# elements. Default STRAIGHT — the common "draw a line between A and B".
_LINE_CATEGORIES = frozenset({"STRAIGHT", "BENT", "CURVED"})


def create_shape(
    creds: Credentials,
    presentation_id: str,
    slide_object_id: str,
    shape_type: str = "RECTANGLE",
    width_inches: float = 2.0,
    height_inches: float = 2.0,
    x_inches: float = 1.0,
    y_inches: float = 1.0,
) -> dict:
    """Insert a shape (rectangle / ellipse / text box / …) onto a slide.

    Uses ``presentations.batchUpdate`` with a single ``createShape``
    request — the geometry sibling of ``createImage`` / ``createTable``:
    same ``elementProperties`` (size + transform) positioning envelope,
    discriminated by ``shapeType`` instead of carrying a URL / row-col
    count. The shape is created empty (no text); add copy afterward with
    ``gslides_replace_all_text`` (seed a token) or a future cell/shape
    text tool.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline — no extra grant).
        presentation_id: The Slides file ID.
        slide_object_id: The slide to place the shape on (an
            ``object_id`` from ``gslides_add_slide`` /
            ``gslides_get_outline``).
        shape_type: A Slides ``shapeType`` enum value. Supported subset
            in ``_SHAPE_TYPES`` (``RECTANGLE`` default, ``ELLIPSE``,
            ``TEXT_BOX``, common callout/flow shapes). Other values
            rejected client-side.
        width_inches: Shape width in inches (default 2.0).
        height_inches: Shape height in inches (default 2.0).
        x_inches: Left inset in inches (default 1.0).
        y_inches: Top inset in inches (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, shape_object_id,
        shape_type, url}``. ``shape_object_id`` is the created shape's
        stable ID (a valid target for later transform / text / delete
        batchUpdates); ``url`` deep-links to the slide.

    Raises:
        ValueError: empty ``slide_object_id``, unsupported ``shape_type``,
            or a non-positive dimension.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not slide_object_id or not slide_object_id.strip():
        raise ValueError("slide_object_id cannot be empty.")
    if shape_type not in _SHAPE_TYPES:
        raise ValueError(
            f"shape_type must be one of {sorted(_SHAPE_TYPES)} — got "
            f"{shape_type!r}."
        )
    if width_inches <= 0 or height_inches <= 0:
        raise ValueError("width_inches and height_inches must be positive.")

    slides = get_service("slides", "v1", credentials=creds)
    # Unique per call — a constant objectId 400s on the second create_shape
    # against the same deck ('object ID already in use').
    shape_id = _unique_object_id("appscriptly_shape")
    requests = [
        {
            "createShape": {
                "objectId": shape_id,
                "shapeType": shape_type,
                "elementProperties": _element_properties(
                    slide_object_id,
                    int(width_inches * _EMU_PER_INCH),
                    int(height_inches * _EMU_PER_INCH),
                    int(x_inches * _EMU_PER_INCH),
                    int(y_inches * _EMU_PER_INCH),
                ),
            },
        },
    ]
    # NOT idempotent: each call adds ANOTHER shape.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.createShape",
    )
    created_id = shape_id
    for reply in resp.get("replies", []) or []:
        cs = reply.get("createShape")
        if cs and cs.get("objectId"):
            created_id = cs["objectId"]
            break
    return {
        "presentation_id": presentation_id,
        "slide_object_id": slide_object_id,
        "shape_object_id": created_id,
        "shape_type": shape_type,
        "url": (
            f"https://docs.google.com/presentation/d/{presentation_id}"
            f"/edit#slide=id.{slide_object_id}"
        ),
    }


def create_line(
    creds: Credentials,
    presentation_id: str,
    slide_object_id: str,
    start_x_inches: float = 1.0,
    start_y_inches: float = 1.0,
    end_x_inches: float = 4.0,
    end_y_inches: float = 3.0,
    line_category: str = "STRAIGHT",
) -> dict:
    """Draw a line on a slide from a start point to an end point.

    Uses ``presentations.batchUpdate`` with a single ``createLine``
    request. Slides positions a line via the SAME ``elementProperties``
    (size + transform) envelope as every other page element: the line
    runs along the diagonal of its bounding box, so a start → end point
    pair maps to a top-left translate + a width/height equal to the
    point delta. This wrapper does that math so callers pass intuitive
    start/end coordinates rather than constructing a transform by hand.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline — no extra grant).
        presentation_id: The Slides file ID.
        slide_object_id: The slide to draw the line on (an ``object_id``
            from ``gslides_add_slide`` / ``gslides_get_outline``).
        start_x_inches: Start point X (inches from the slide's left edge).
        start_y_inches: Start point Y (inches from the slide's top edge).
        end_x_inches: End point X (inches from the slide's left edge).
        end_y_inches: End point Y (inches from the slide's top edge).
        line_category: A Slides ``lineCategory`` enum — ``"STRAIGHT"``
            (default), ``"BENT"``, or ``"CURVED"``. Other values
            rejected client-side.

    Returns:
        ``{presentation_id, slide_object_id, line_object_id,
        line_category, url}``. ``line_object_id`` is the created line's
        stable ID (a valid target for later style / delete
        batchUpdates); ``url`` deep-links to the slide.

    Raises:
        ValueError: empty ``slide_object_id``, unsupported
            ``line_category``, or a zero-length line (start == end —
            Slides would 400 on a degenerate transform).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not slide_object_id or not slide_object_id.strip():
        raise ValueError("slide_object_id cannot be empty.")
    if line_category not in _LINE_CATEGORIES:
        raise ValueError(
            f"line_category must be one of {sorted(_LINE_CATEGORIES)} — "
            f"got {line_category!r}."
        )
    if start_x_inches == end_x_inches and start_y_inches == end_y_inches:
        raise ValueError(
            "start and end points are identical — a zero-length line has "
            "no direction and Slides rejects the degenerate transform. "
            "Pass distinct start/end coordinates."
        )

    # A line's bounding box: top-left at min(start, end), size = |delta|.
    # The transform's scaleX/scaleY sign encodes direction so the line
    # runs the right way along the diagonal (Slides draws from the box's
    # top-left toward bottom-right scaled by the signed transform).
    left_emu = int(min(start_x_inches, end_x_inches) * _EMU_PER_INCH)
    top_emu = int(min(start_y_inches, end_y_inches) * _EMU_PER_INCH)
    width_emu = int(abs(end_x_inches - start_x_inches) * _EMU_PER_INCH)
    height_emu = int(abs(end_y_inches - start_y_inches) * _EMU_PER_INCH)
    # Slides requires a positive size magnitude; a perfectly horizontal or
    # vertical line has a zero delta on one axis. Floor that axis to 1 EMU
    # so the size stays valid while the line still reads as straight.
    width_emu = max(width_emu, 1)
    height_emu = max(height_emu, 1)

    slides = get_service("slides", "v1", credentials=creds)
    # Unique per call — a constant objectId 400s on the second create_line
    # against the same deck ('object ID already in use').
    line_id = _unique_object_id("appscriptly_line")
    requests = [
        {
            "createLine": {
                "objectId": line_id,
                "lineCategory": line_category,
                "elementProperties": _element_properties(
                    slide_object_id,
                    width_emu,
                    height_emu,
                    left_emu,
                    top_emu,
                ),
            },
        },
    ]
    # NOT idempotent: each call adds ANOTHER line.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.createLine",
    )
    created_id = line_id
    for reply in resp.get("replies", []) or []:
        cl = reply.get("createLine")
        if cl and cl.get("objectId"):
            created_id = cl["objectId"]
            break
    return {
        "presentation_id": presentation_id,
        "slide_object_id": slide_object_id,
        "line_object_id": created_id,
        "line_category": line_category,
        "url": (
            f"https://docs.google.com/presentation/d/{presentation_id}"
            f"/edit#slide=id.{slide_object_id}"
        ),
    }


# Accepted ``updatePageElementTransform`` apply modes. RELATIVE composes
# the given matrix onto the element's existing transform; ABSOLUTE
# replaces it outright. (Slides' APPLY_MODE_UNSPECIFIED is deliberately
# not exposed.)
_APPLY_MODES = frozenset({"RELATIVE", "ABSOLUTE"})


def delete_object(
    creds: Credentials,
    presentation_id: str,
    object_id: str,
) -> dict:
    """Delete a page element (or an entire slide) by its objectId.

    Uses ``presentations.batchUpdate`` with a single ``deleteObject``
    request. The ``object_id`` may be a page element's objectId (a
    shape / image / table / line taken from ``gslides_get_outline``'s
    per-slide ``elements``) OR a slide's ``object_id``. Slides'
    ``deleteObject`` removes either: deleting a slide removes the slide
    and everything on it; deleting an element removes just that element.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline, no extra grant).
        presentation_id: The Slides file ID.
        object_id: The objectId of the page element or slide to delete
            (from ``get_outline``: a slide's ``object_id``, or an entry
            in a slide's ``elements[].object_id``).

    Returns:
        ``{presentation_id, deleted_object_id}``. ``deleted_object_id``
        echoes the objectId that was removed.

    Raises:
        ValueError: empty ``object_id``.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated. A
            bogus or already-deleted objectId surfaces Slides' 400.
    """
    if not object_id or not object_id.strip():
        raise ValueError("object_id cannot be empty.")

    slides = get_service("slides", "v1", credentials=creds)
    requests = [{"deleteObject": {"objectId": object_id}}]
    # Single-shot (idempotent=False): deleting an already-deleted objectId
    # 400s, so retrying after a lost success would surface a spurious error
    # instead of a clean no-op. Same non-retried posture the sheets
    # delete_sheet uses for its destructive floor.
    execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.deleteObject",
    )
    return {
        "presentation_id": presentation_id,
        "deleted_object_id": object_id,
    }


def duplicate_object(
    creds: Credentials,
    presentation_id: str,
    object_id: str,
) -> dict:
    """Duplicate a page element (or slide), surfacing the new-id mapping.

    Uses ``presentations.batchUpdate`` with a single ``duplicateObject``
    request. Slides copies the object (and, for a table / group / slide,
    all of its child objects) and assigns fresh objectIds. The reply
    carries the new top-level objectId; this wrapper surfaces it as
    ``new_object_id`` (the headline copy, a valid target for a later
    transform / delete) plus an ``id_map`` of the ``{source: new}``
    mapping Slides returned.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline, no extra grant).
        presentation_id: The Slides file ID.
        object_id: The objectId of the page element or slide to
            duplicate (from ``get_outline``).

    Returns:
        ``{presentation_id, source_object_id, new_object_id, id_map}``.
        ``new_object_id`` is the duplicate's stable objectId; ``id_map``
        is ``{source_object_id: new_object_id}`` (one entry, the
        top-level object). Slides does not enumerate copied child ids in
        the reply, so ``id_map`` mirrors exactly what it returned.

    Raises:
        ValueError: empty ``object_id``.
        HttpError: from the underlying SDK on 4xx / 5xx, propagated. A
            bogus ``object_id`` surfaces Slides' 400.
    """
    if not object_id or not object_id.strip():
        raise ValueError("object_id cannot be empty.")

    slides = get_service("slides", "v1", credentials=creds)
    requests = [{"duplicateObject": {"objectId": object_id}}]
    # NOT idempotent: each call adds ANOTHER copy. Same single-shot
    # convention as the create_* ops.
    resp = execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.duplicateObject",
    )
    new_object_id = ""
    for reply in resp.get("replies", []) or []:
        do = reply.get("duplicateObject")
        if do and do.get("objectId"):
            new_object_id = do["objectId"]
            break
    return {
        "presentation_id": presentation_id,
        "source_object_id": object_id,
        "new_object_id": new_object_id,
        "id_map": {object_id: new_object_id} if new_object_id else {},
    }


def update_element_transform(
    creds: Credentials,
    presentation_id: str,
    object_id: str,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    translate_x_emu: float = 0.0,
    translate_y_emu: float = 0.0,
    apply_mode: str = "RELATIVE",
) -> dict:
    """Reposition / resize a page element via its affine transform.

    Uses ``presentations.batchUpdate`` with a single
    ``updatePageElementTransform`` request. Slides positions every page
    element with an affine matrix (scale + translate); this sets or
    composes that matrix.

    ``apply_mode`` (default ``"RELATIVE"``) chooses how the given matrix
    combines with the element's CURRENT transform:

      * ``"RELATIVE"`` (the safe default) COMPOSES the given matrix onto
        the existing one. ``scale_x`` / ``scale_y`` of 1 leave the
        element's size unchanged; ``translate_x_emu`` / ``translate_y_emu``
        NUDGE it by that many EMU. A bare call (all defaults) is a no-op,
        so an under-specified call can never collapse or teleport the
        element.
      * ``"ABSOLUTE"`` REPLACES the element's transform outright with the
        given matrix: the element is placed at exactly
        (``translate_x_emu``, ``translate_y_emu``) with scale
        (``scale_x``, ``scale_y``), regardless of where it was. Pass
        ``scale_x`` / ``scale_y`` explicitly (the 1.0 defaults mean unit
        scale) or the element resets to unit size.

    Units: translate is in EMU (914400 EMU per inch, the same unit the
    ``create_*`` tools convert their inch arguments into); scale is a
    dimensionless multiplier (2.0 doubles the size, -1.0 mirrors).

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope
            (baseline, no extra grant).
        presentation_id: The Slides file ID.
        object_id: The page element's objectId (from
            ``get_outline``'s ``elements[].object_id``).
        scale_x: X-axis scale multiplier (default 1.0). Must be non-zero.
        scale_y: Y-axis scale multiplier (default 1.0). Must be non-zero.
        translate_x_emu: X translation in EMU (default 0).
        translate_y_emu: Y translation in EMU (default 0).
        apply_mode: ``"RELATIVE"`` (default) or ``"ABSOLUTE"`` (see
            above). Other values rejected client-side.

    Returns:
        ``{presentation_id, object_id, apply_mode, transform}``.
        ``transform`` echoes the exact matrix sent to Slides
        (``{scaleX, scaleY, translateX, translateY, unit}``).

    Raises:
        ValueError: empty ``object_id``, unsupported ``apply_mode``, or a
            zero ``scale_x`` / ``scale_y`` (a zero scale collapses the
            element to nothing).
        HttpError: from the underlying SDK on 4xx / 5xx, propagated. A
            bogus ``object_id`` surfaces Slides' 400.
    """
    if not object_id or not object_id.strip():
        raise ValueError("object_id cannot be empty.")
    if apply_mode not in _APPLY_MODES:
        raise ValueError(
            f"apply_mode must be one of {sorted(_APPLY_MODES)} (got "
            f"{apply_mode!r})."
        )
    if scale_x == 0 or scale_y == 0:
        raise ValueError(
            "scale_x and scale_y must be non-zero (a zero scale collapses "
            "the element to nothing)."
        )

    slides = get_service("slides", "v1", credentials=creds)
    transform = {
        "scaleX": scale_x,
        "scaleY": scale_y,
        "translateX": translate_x_emu,
        "translateY": translate_y_emu,
        "unit": "EMU",
    }
    requests = [
        {
            "updatePageElementTransform": {
                "objectId": object_id,
                "transform": transform,
                "applyMode": apply_mode,
            },
        },
    ]
    # Single-shot (idempotent=False): a RELATIVE apply COMPOSES, so a
    # retry after a lost success would double-apply the matrix. (ABSOLUTE
    # alone would be retry-safe, but the default is RELATIVE; keep one
    # posture matching the create_* single-shot convention.)
    execute_with_retry(
        lambda: slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute(),
        idempotent=False,
        op_name="slides.presentations.batchUpdate.updatePageElementTransform",
    )
    return {
        "presentation_id": presentation_id,
        "object_id": object_id,
        "apply_mode": apply_mode,
        "transform": transform,
    }


# ---------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------


def _extract_slide_text(slide: dict) -> str:
    """Flatten readable text from a slide's ``pageElements``.

    Slides' text model:

        slide.pageElements[].shape.text.textElements[].textRun.content

    Iterates every element with a ``shape``, extracts each
    ``textRun.content``, joins them, and strips trailing whitespace.
    Elements without a ``shape`` (images, embedded charts, line
    drawings) carry no slide-level text and are skipped.

    Returns the empty string for slides with no text shapes (e.g.
    an image-only slide). Stable identity for the consumer: empty
    string, not ``None``, so JSON consumers can iterate safely.
    """
    parts: list[str] = []
    for element in slide.get("pageElements", []) or []:
        shape = element.get("shape")
        if not shape:
            continue
        text = shape.get("text", {})
        for te in text.get("textElements", []) or []:
            text_run = te.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts).strip()


# Map a pageElement's discriminating key to a stable, human-readable
# ``type`` label. Slides models each page element as exactly one of
# these keyed sub-objects (a tagged union); the key that is present is
# the element's kind. Kept as an ordered tuple (not a dict) so the
# classification is deterministic even in the (spec-illegal) event that
# an element ever carried two keys — first match wins.
_PAGE_ELEMENT_KINDS: tuple[tuple[str, str], ...] = (
    ("shape", "shape"),
    ("table", "table"),
    ("image", "image"),
    ("line", "line"),
    ("video", "video"),
    ("wordArt", "word_art"),
    ("sheetsChart", "sheets_chart"),
    ("elementGroup", "group"),
)


def _classify_page_element(element: dict) -> str:
    """Classify a Slides ``pageElement`` into a stable type label.

    Slides represents a page element as a tagged union, the element
    carries exactly one of ``shape`` / ``table`` / ``image`` / ``line``
    / ``video`` / ``wordArt`` / ``sheetsChart`` / ``elementGroup``. This
    returns the snake_case label for whichever discriminator is present,
    or ``"unknown"`` for an element that carries none of them (forward
    compatibility, Slides may add element kinds we don't yet map; an
    unknown kind is reported rather than dropped).
    """
    for key, label in _PAGE_ELEMENT_KINDS:
        if key in element:
            return label
    return "unknown"


def _list_slide_elements(slide: dict) -> list[dict]:
    """List ``{object_id, type}`` for every page element on a slide.

    Walks ``slide.pageElements`` in document order, classifying each via
    ``_classify_page_element``. The ``object_id`` is the element's stable
    Slides objectId (usable as a later batchUpdate target, e.g. to
    delete an image or restyle a shape). Returns an empty list for a
    slide with no page elements (a truly blank slide).
    """
    return [
        {
            "object_id": element.get("objectId", ""),
            "type": _classify_page_element(element),
        }
        for element in slide.get("pageElements", []) or []
    ]


def _extract_slide_notes(slide: dict) -> str:
    """Extract the speaker-notes text for a slide.

    Speaker notes live on the slide's ``slideProperties.notesPage``, a
    page whose own ``pageElements`` contain the notes body shape. The
    notes shape is the one whose ``shape.placeholder.type`` is
    ``BODY``; reusing ``_extract_slide_text`` on the notesPage would
    also pick up the slide-number placeholder, so this targets the BODY
    placeholder specifically.

    Returns the readable notes text (trailing whitespace stripped), or
    the empty string when the slide has no notesPage or an empty notes
    body. Stable identity for the consumer: empty string, not ``None``.
    """
    notes_page = (
        slide.get("slideProperties", {}).get("notesPage", {})
    )
    parts: list[str] = []
    for element in notes_page.get("pageElements", []) or []:
        shape = element.get("shape")
        if not shape:
            continue
        # The notes body is the BODY placeholder; skip the slide-number
        # (and any other) placeholder so we return only the notes copy.
        placeholder_type = shape.get("placeholder", {}).get("type")
        if placeholder_type != "BODY":
            continue
        text = shape.get("text", {})
        for te in text.get("textElements", []) or []:
            text_run = te.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts).strip()
