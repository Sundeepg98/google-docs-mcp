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

from typing import TYPE_CHECKING

from google_docs_mcp.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


def get_outline(creds: Credentials, presentation_id: str) -> dict:
    """Read a presentation's outline via ``presentations.get``.

    Args:
        creds: OAuth credentials carrying the ``presentations`` scope.
        presentation_id: The Slides file ID (the ID part of the
            sharing URL).

    Returns:
        ``{presentation_id, title, url, slides: [...]}``. Each entry
        in ``slides`` is ``{object_id, layout, text}`` — the
        ``object_id`` is Slides' stable per-slide identifier (the
        Slides equivalent of docs' tab IDs, per the multi-service
        audit), the ``layout`` is the layout's objectId (empty
        string when the slide has no explicit layout), and ``text``
        is the readable copy concatenated from all shapes on the
        slide. Empty when the slide has no text shapes (e.g. an
        image-only slide).

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx — let it
            propagate; the tool-layer envelope renders it.
    """
    slides = get_service("slides", "v1", credentials=creds)
    presentation = slides.presentations().get(
        presentationId=presentation_id,
    ).execute()
    return {
        "presentation_id": presentation_id,
        "title": presentation.get("title", ""),
        "url": f"https://docs.google.com/presentation/d/{presentation_id}/edit",
        "slides": [
            {
                "object_id": slide.get("objectId", ""),
                "layout": (
                    slide.get("slideProperties", {})
                    .get("layoutObjectId", "")
                ),
                "text": _extract_slide_text(slide),
            }
            for slide in presentation.get("slides", [])
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
    resp = slides.presentations().batchUpdate(
        presentationId=presentation_id,
        body=body,
    ).execute()
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
