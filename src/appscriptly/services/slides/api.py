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

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

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

    # Deterministic objectIds so the follow-up insertText requests can
    # target the placeholders created in the SAME batch (Slides assigns
    # random IDs otherwise, which we couldn't reference until a second
    # round trip).
    slide_id = "appscriptly_slide"
    title_ph_id = "appscriptly_title_ph"
    body_ph_id = "appscriptly_body_ph"

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
    # as create_presentation. (We pass a fixed objectId for terseness;
    # Slides rejects a duplicate objectId within one presentation, so a
    # naive re-run would 400 rather than silently double-insert — but
    # the agent-facing contract is "appends a slide", annotated
    # idempotent=False, so retries are the caller's concern.)
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
