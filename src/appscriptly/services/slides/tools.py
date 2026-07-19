"""Google Slides MCP tool registrations (v2.3.2 — 3rd new service).

Mirrors the layout established by ``services/sheets/tools.py`` (v2.3.1,
PR #119): ``@workspace_tool``-decorated functions registered with the
live ``mcp`` instance via ``server.py``'s side-effect import.

**Tools registered here** (12 slides-service tools):

1. ``gslides_get_outline``         — read structure + per-slide text/elements/notes
2. ``gslides_replace_all_text``    — find/replace across all slides
3. ``gslides_create_presentation`` — create an empty new deck
4. ``gslides_add_slide``           — append a slide (+ title/body text)
5. ``gslides_create_image``        — insert an image (by URL) on a slide
6. ``gslides_create_table``        — insert an empty table on a slide
7. ``gslides_create_shape``        — insert a shape (rect/ellipse/…) on a slide
8. ``gslides_create_line``         — draw a line (start→end) on a slide
9. ``gslides_set_speaker_notes``   — set (replace) a slide's speaker notes
10. ``gslides_delete_object``      (delete a page element or a whole slide)
11. ``gslides_duplicate_object``   (duplicate an element or slide; returns the new id)
12. ``gslides_update_element_transform`` (move / resize an element via its EMU transform)

The first three were the minimal trio; ``gslides_add_slide`` closed
the slide-population gap; ``gslides_create_image`` /
``gslides_create_table`` add visual + tabular content; and
``gslides_create_shape`` / ``gslides_create_line`` complete the #155
slide-geometry trio (table + shape + line) so a deck can be authored
end-to-end — including diagrams: ``create_presentation`` →
``add_slide`` (×N) → ``create_image`` / ``create_table`` /
``create_shape`` / ``create_line`` → ``get_outline`` to verify.

**Still deferred to a follow-up PR**: the rest of the Slides
``batchUpdate`` tagged-union (updateTextStyle, updateShapeProperties,
updateTableCellProperties, etc.). The write tools here are targeted
carve-outs from that surface; the broader tagged-union abstraction
can be designed when more request types have real consumers.

**Import discipline.** Same as ``services/sheets/tools.py``:

- ``_get_credentials`` + ``_format_http_error`` imported directly
  from ``_tool_helpers`` (M3 Phase C extraction).
- The api module is the standard ``from ... import api`` pattern.
- ``@workspace_tool(service="slides", ...)`` carries the service=
  literal that drives the partition test + future telemetry.
"""
from __future__ import annotations

from appscriptly.decorators import workspace_tool
from appscriptly.services.slides.api import (
    add_slide as _add_slide,
    create_image as _create_image,
    create_line as _create_line,
    create_presentation as _create_presentation,
    create_shape as _create_shape,
    create_table as _create_table,
    delete_object as _delete_object,
    duplicate_object as _duplicate_object,
    get_outline as _get_outline,
    replace_all_text as _replace_all_text,
    set_speaker_notes as _set_speaker_notes,
    update_element_transform as _update_element_transform,
)
from appscriptly.tool_schemas import (
    GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_LINE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
    GSLIDES_CREATE_SHAPE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA,
    GSLIDES_DELETE_OBJECT_OUTPUT_SCHEMA,
    GSLIDES_DUPLICATE_OBJECT_OUTPUT_SCHEMA,
    GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA,
    GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
    GSLIDES_SET_SPEAKER_NOTES_OUTPUT_SCHEMA,
    GSLIDES_UPDATE_ELEMENT_TRANSFORM_OUTPUT_SCHEMA,
)

# Imported for parity with services/sheets/tools.py; not currently
# used by the minimal trio (none of these need _format_http_error —
# HttpError propagates to the standard decorator envelope). Kept as
# a top-level import so adding a 4th tool that DOES need it doesn't
# trigger a separate import statement.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)


# ---------------------------------------------------------------------
# 1. gslides_get_outline — presentations.get (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Read outline of a Google Slides presentation",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA,
)
def gslides_get_outline(creds, presentation_id: str) -> dict:
    """Read a Slides presentation's structure + per-slide text.

    USE WHEN: you need to inspect a deck — summarize its contents,
    find a specific slide by its text, or verify the result of a
    prior ``replace_all_text`` call.

    Uses Slides' ``presentations.get`` REST endpoint. For each slide it
    returns the stable ``object_id`` (the Slides equivalent of docs' tab
    IDs, a valid target for batchUpdate write operations), the 0-based
    ``index`` (filmstrip position), the layout objectId, the flattened
    readable ``text`` from all text shapes, an ``elements`` inventory
    (one ``{object_id, type}`` per page element, classified shape /
    table / image / line / video / word_art / sheets_chart / group /
    unknown), and the slide's speaker ``notes`` text.

    Args:
        presentation_id: The presentation ID (the ID part of the
            sharing URL).

    Returns:
        ``{presentation_id, title, url, slides: [{object_id, index,
        layout, text, elements, notes}, ...]}``. ``slides`` is empty
        when the deck has no slides (rare; Slides auto-creates a default
        slide on ``create_presentation``). Per-slide ``text`` / ``notes``
        are the empty string when absent (e.g. an image-only slide with
        no notes); ``elements`` is empty for a truly blank slide.

    Choreography: ``presentation_id`` typically from
    ``gdocs_find_doc_by_title`` (slides files have
    mimeType=``application/vnd.google-apps.presentation``), from a
    prior ``gslides_create_presentation`` call, or from the user.
    """
    return _get_outline(creds, presentation_id)


# ---------------------------------------------------------------------
# 2. gslides_replace_all_text — batchUpdate (single request type)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Find and replace text across all slides in a presentation",
    # Replacing text in place is not "destructive" in our sense
    # (slides still exist; text can be re-replaced to recover);
    # matches the convention used by gdocs_replace_all_text +
    # gsheets_write_range.
    readonly=False,
    destructive=False,
    # Same input → same Slides state. Running replace_all_text twice
    # with the same args is a no-op the second time (find_text won't
    # match anymore because the first call replaced it).
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
)
def gslides_replace_all_text(
    creds,
    presentation_id: str,
    find_text: str,
    replace_text: str,
    match_case: bool = True,
) -> dict:
    """Replace all occurrences of ``find_text`` across every slide.

    USE WHEN: updating a templated deck (e.g. swapping {{ClientName}}
    for "Acme Corp" across a slides template), correcting a typo
    that appears multiple places, or doing any cross-slide text
    substitution.

    Uses Slides' ``presentations.batchUpdate`` REST endpoint with a
    single ``replaceAllText`` request — the most common write use
    case carved out from the full batchUpdate tagged-union surface
    (which is deferred to a follow-up PR).

    Args:
        presentation_id: The presentation ID.
        find_text: Literal text to search for. Empty rejected
            client-side (Slides would 400 anyway, plus an empty
            search would match everywhere).
        replace_text: What to replace matches with. May be empty
            (effectively deletes matched text).
        match_case: When True (default), case-sensitive match
            (``"Foo"`` matches ``"Foo"`` but not ``"FOO"``).
            False does case-insensitive matching.

    Returns:
        ``{presentation_id, occurrences_changed}``. ``occurrences_changed``
        is 0 when nothing matched (not an error — common when
        running on a deck that's already been replaced).

    Choreography: typically follows a templated
    ``gslides_create_presentation`` (where you set up placeholders
    like ``{{Name}}`` then replace them per recipient) or
    ``gdocs_find_doc_by_title`` to locate the deck. Verify with
    ``gslides_get_outline`` afterward.

    NOTE: replaces ALL occurrences. There's no "first match only"
    mode — that would need the deferred batchUpdate tagged-union
    (specifically ``updateTextStyle`` targeting a specific
    objectId + textRange).
    """
    return _replace_all_text(
        creds,
        presentation_id=presentation_id,
        find_text=find_text,
        replace_text=replace_text,
        match_case=match_case,
    )


# ---------------------------------------------------------------------
# 3. gslides_create_presentation — presentations.create
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Create an empty new Google Slides presentation",
    # Creating a fresh resource isn't a mutation of existing state.
    # Matches gsheets_create_spreadsheet + gdocs_make_tabbed_doc.
    readonly=False,
    destructive=False,
    # Re-running creates ANOTHER deck — NOT idempotent. Same
    # convention as gsheets_create_spreadsheet.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
)
def gslides_create_presentation(creds, title: str) -> dict:
    """Create an empty Google Slides presentation (lands in Drive root).

    USE WHEN: starting a new deck — typically the FIRST call in a
    create → batchUpdate (when shipped) → get_outline workflow.

    Uses Slides' ``presentations.create`` REST endpoint. The created
    presentation is owned by the OAuth user, lands in Drive root,
    and ships with a single default title slide. Move it elsewhere
    via ``gdocs_move_to_folder`` (works because slides are Drive
    files under the hood).

    Args:
        title: Title for the new presentation. Becomes both the
            Drive filename and the presentation's display title.

    Returns:
        ``{presentation_id, url, title}`` — same flat envelope
        as ``gsheets_create_spreadsheet`` (v2.3.1) and
        ``gdocs_make_tabbed_doc``. Callers can immediately pipe
        ``presentation_id`` into ``gslides_get_outline`` /
        ``gslides_replace_all_text``.

    Choreography: pair with ``gdocs_move_to_folder`` to file it
    into a project folder, ``gdocs_share_file`` to grant
    collaborators access, and ``gslides_replace_all_text`` to
    swap in templated content.
    """
    return _create_presentation(creds, title)


# ---------------------------------------------------------------------
# 4. gslides_add_slide — batchUpdate (createSlide + insertText)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Append a slide (optional title + body) to a presentation",
    # Appends a new slide — not a mutation of existing slides, and
    # text can be edited afterward. Matches gslides_create_presentation
    # / gsheets_write_range (writes, but not "destructive").
    readonly=False,
    destructive=False,
    # Re-running appends ANOTHER slide — NOT idempotent. Same
    # convention as gslides_create_presentation.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA,
)
def gslides_add_slide(
    creds,
    presentation_id: str,
    title: str | None = None,
    body: str | None = None,
    layout: str = "TITLE_AND_BODY",
) -> dict:
    """Append a slide to a deck, optionally with title + body text.

    USE WHEN: building out a deck's CONTENT — this is what turns a
    freshly-created (empty) presentation into a real one. Pairs with
    ``gslides_create_presentation``: create the deck, then call this
    once per slide to populate it.

    Uses Slides' ``presentations.batchUpdate`` with a ``createSlide``
    request (predefined layout + deterministic placeholder IDs)
    followed by ``insertText`` into those placeholders — one atomic
    round trip, so the slide and its text commit together. This is
    the first ``createSlide``/``insertText`` carve-out from the
    larger Slides batchUpdate surface.

    Args:
        presentation_id: The presentation to append to (from
            ``gslides_create_presentation`` or
            ``gdocs_find_doc_by_title``).
        title: Optional title text. Inserted only when ``layout`` has
            a TITLE placeholder (``TITLE_AND_BODY`` / ``TITLE_ONLY``)
            and the text is non-empty.
        body: Optional body text. Inserted only with
            ``layout="TITLE_AND_BODY"`` (the only supported layout
            with a BODY placeholder). Passing ``body`` with another
            layout is rejected.
        layout: Slides ``predefinedLayout`` — one of
            ``"TITLE_AND_BODY"`` (default), ``"TITLE_ONLY"``,
            ``"BLANK"``.

    Returns:
        ``{presentation_id, slide_object_id, url}``. ``slide_object_id``
        is the new slide's stable ID (matches the ``object_id`` you'll
        later see in ``gslides_get_outline``, and is a valid target
        for future batchUpdate writes). ``url`` deep-links to the
        slide.

    Choreography: ``gslides_create_presentation`` → ``gslides_add_slide``
    (×N) → ``gslides_get_outline`` to verify, or
    ``gslides_replace_all_text`` to swap templated tokens. File the
    deck with ``gdocs_move_to_folder`` / share via ``gdocs_share_file``.
    """
    return _add_slide(
        creds,
        presentation_id=presentation_id,
        title=title,
        body=body,
        layout=layout,
    )


# ---------------------------------------------------------------------
# 5. gslides_create_image — batchUpdate (createImage)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Insert an image (by URL) onto a slide",
    # Adds an image element to a slide — a write, but not destructive
    # (existing content untouched; the image can be deleted later).
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER image — NOT idempotent. Matches
    # gslides_add_slide / gslides_create_presentation.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA,
)
def gslides_create_image(
    creds,
    presentation_id: str,
    slide_object_id: str,
    image_url: str,
    width_inches: float = 4.0,
    height_inches: float = 3.0,
    x_inches: float = 1.0,
    y_inches: float = 1.0,
) -> dict:
    """Insert an image onto a slide from a public URL.

    USE WHEN: adding a logo, chart export, screenshot, or any image to
    a slide. Pairs with ``gslides_add_slide`` (add a slide, then place
    images/tables on it).

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``createImage`` request. Slides fetches the bytes from
    ``image_url`` at insert time and copies them into the deck — so
    the URL must be publicly reachable when this runs, but need not
    stay live afterward. Slides constraints: image ≤ 50 MB, ≤ 25
    megapixels, PNG/JPEG/GIF.

    Args:
        presentation_id: The presentation to add the image to.
        slide_object_id: The slide to place it on (an ``object_id``
            from ``gslides_add_slide`` / ``gslides_get_outline``).
        image_url: Publicly-accessible https image URL. Unreachable /
            oversized / unsupported URLs are rejected by Slides (400).
        width_inches: Image width in inches (default 4.0).
        height_inches: Image height in inches (default 3.0).
        x_inches: Left inset from the slide's top-left (default 1.0).
        y_inches: Top inset from the slide's top-left (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, image_object_id, url}``.
        ``image_object_id`` is the created image element's stable ID
        (a valid target for a future transform / delete tool); ``url``
        deep-links to the slide.

    Choreography: ``gslides_create_presentation`` →
    ``gslides_add_slide`` → ``gslides_create_image`` →
    ``gslides_get_outline`` to verify.
    """
    return _create_image(
        creds,
        presentation_id=presentation_id,
        slide_object_id=slide_object_id,
        image_url=image_url,
        width_inches=width_inches,
        height_inches=height_inches,
        x_inches=x_inches,
        y_inches=y_inches,
    )


# ---------------------------------------------------------------------
# 6. gslides_create_table — batchUpdate (createTable)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Insert an empty table onto a slide",
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER table — NOT idempotent.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA,
)
def gslides_create_table(
    creds,
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

    USE WHEN: laying out tabular content on a slide (comparison grids,
    schedules, specs). The table is created EMPTY; populate cells
    afterward by seeding template tokens and calling
    ``gslides_replace_all_text``, or via a future cell-level text tool.

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``createTable`` request.

    Args:
        presentation_id: The presentation to add the table to.
        slide_object_id: The slide to place it on (an ``object_id``
            from ``gslides_add_slide`` / ``gslides_get_outline``).
        rows: Number of rows (>= 1).
        columns: Number of columns (>= 1).
        width_inches: Table width in inches (default 6.0).
        height_inches: Table height in inches (default 3.0).
        x_inches: Left inset from the slide's top-left (default 1.0).
        y_inches: Top inset from the slide's top-left (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, table_object_id, rows,
        columns, url}``. ``table_object_id`` is the created table's
        stable ID; ``url`` deep-links to the slide.

    Choreography: ``gslides_add_slide`` → ``gslides_create_table`` →
    seed tokens + ``gslides_replace_all_text`` to fill cells →
    ``gslides_get_outline`` to verify.
    """
    return _create_table(
        creds,
        presentation_id=presentation_id,
        slide_object_id=slide_object_id,
        rows=rows,
        columns=columns,
        width_inches=width_inches,
        height_inches=height_inches,
        x_inches=x_inches,
        y_inches=y_inches,
    )


# ---------------------------------------------------------------------
# 7. gslides_create_shape — batchUpdate (createShape)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Insert a shape (rectangle / ellipse / text box / …) onto a slide",
    # Adds a shape element to a slide — a write, but not destructive
    # (existing content untouched; the shape can be deleted later).
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER shape — NOT idempotent. Matches
    # gslides_create_table / gslides_create_image.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_CREATE_SHAPE_OUTPUT_SCHEMA,
)
def gslides_create_shape(
    creds,
    presentation_id: str,
    slide_object_id: str,
    shape_type: str = "RECTANGLE",
    width_inches: float = 2.0,
    height_inches: float = 2.0,
    x_inches: float = 1.0,
    y_inches: float = 1.0,
) -> dict:
    """Insert a shape onto a slide (rectangle, ellipse, text box, …).

    USE WHEN: drawing a box, ellipse, callout, arrow, or empty text box
    on a slide — diagram primitives, highlight boxes, flow-chart nodes.
    Pairs with ``gslides_create_line`` (shapes + connectors = a diagram)
    and completes the slide-geometry trio alongside
    ``gslides_create_table``.

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``createShape`` request — the same positioning envelope (EMU size +
    transform) as ``gslides_create_image`` / ``gslides_create_table``,
    discriminated by ``shape_type``. The shape is created EMPTY (no
    text); seed a token + ``gslides_replace_all_text`` to add copy.

    Args:
        presentation_id: The presentation to add the shape to.
        slide_object_id: The slide to place it on (an ``object_id``
            from ``gslides_add_slide`` / ``gslides_get_outline``).
        shape_type: Slides ``shapeType`` enum — e.g. ``"RECTANGLE"``
            (default), ``"ELLIPSE"``, ``"TEXT_BOX"``, ``"ROUND_RECTANGLE"``,
            ``"DIAMOND"``, ``"RIGHT_ARROW"``, ``"CLOUD"``,
            ``"WEDGE_RECTANGLE_CALLOUT"``. Unsupported values rejected.
        width_inches: Shape width in inches (default 2.0).
        height_inches: Shape height in inches (default 2.0).
        x_inches: Left inset from the slide's top-left (default 1.0).
        y_inches: Top inset from the slide's top-left (default 1.0).

    Returns:
        ``{presentation_id, slide_object_id, shape_object_id,
        shape_type, url}``. ``shape_object_id`` is the created shape's
        stable ID (a valid target for a future transform / text / delete
        tool); ``url`` deep-links to the slide.

    Choreography: ``gslides_add_slide`` → ``gslides_create_shape`` /
    ``gslides_create_line`` → ``gslides_get_outline`` to verify.
    """
    return _create_shape(
        creds,
        presentation_id=presentation_id,
        slide_object_id=slide_object_id,
        shape_type=shape_type,
        width_inches=width_inches,
        height_inches=height_inches,
        x_inches=x_inches,
        y_inches=y_inches,
    )


# ---------------------------------------------------------------------
# 8. gslides_create_line — batchUpdate (createLine)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Draw a line (start → end) on a slide",
    # Adds a line element to a slide — a write, but not destructive.
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER line — NOT idempotent.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_CREATE_LINE_OUTPUT_SCHEMA,
)
def gslides_create_line(
    creds,
    presentation_id: str,
    slide_object_id: str,
    start_x_inches: float = 1.0,
    start_y_inches: float = 1.0,
    end_x_inches: float = 4.0,
    end_y_inches: float = 3.0,
    line_category: str = "STRAIGHT",
) -> dict:
    """Draw a line on a slide from a start point to an end point.

    USE WHEN: connecting two shapes, drawing a divider / underline, or
    adding any straight/bent/curved line to a slide. Pairs with
    ``gslides_create_shape`` to build diagrams (shapes = nodes, lines =
    connectors) and completes the #155 slide-geometry trio.

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``createLine`` request. Slides positions a line via its bounding box
    (the line runs along the box's diagonal); this tool converts the
    intuitive start/end inch coordinates into that size + transform for
    you, so you don't construct an affine transform by hand.

    Args:
        presentation_id: The presentation to draw the line on.
        slide_object_id: The slide to draw it on (an ``object_id`` from
            ``gslides_add_slide`` / ``gslides_get_outline``).
        start_x_inches: Start X (inches from the slide's left edge).
        start_y_inches: Start Y (inches from the slide's top edge).
        end_x_inches: End X (inches from the slide's left edge).
        end_y_inches: End Y (inches from the slide's top edge).
        line_category: Slides ``lineCategory`` — ``"STRAIGHT"``
            (default), ``"BENT"``, or ``"CURVED"``. Other values rejected.

    Returns:
        ``{presentation_id, slide_object_id, line_object_id,
        line_category, url}``. ``line_object_id`` is the created line's
        stable ID (a valid target for a future style / delete tool);
        ``url`` deep-links to the slide.

    Choreography: ``gslides_add_slide`` → ``gslides_create_shape``
    (×N nodes) → ``gslides_create_line`` (×N connectors) →
    ``gslides_get_outline`` to verify.
    """
    return _create_line(
        creds,
        presentation_id=presentation_id,
        slide_object_id=slide_object_id,
        start_x_inches=start_x_inches,
        start_y_inches=start_y_inches,
        end_x_inches=end_x_inches,
        end_y_inches=end_y_inches,
        line_category=line_category,
    )


# ---------------------------------------------------------------------
# 9. gslides_set_speaker_notes (batchUpdate: deleteText + insertText)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Set the speaker notes of a slide",
    # Replaces the slide's notes text in place (a write, not
    # destructive: the slide and its visible content are untouched; the
    # notes can be re-set). Matches gslides_replace_all_text's posture.
    readonly=False,
    destructive=False,
    # Setting the same notes twice yields the same state (delete-all then
    # insert the same text is deterministic), so idempotent. The api layer
    # dispatches idempotent=True.
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSLIDES_SET_SPEAKER_NOTES_OUTPUT_SCHEMA,
)
def gslides_set_speaker_notes(
    creds,
    presentation_id: str,
    slide_object_id: str,
    notes_text: str,
) -> dict:
    """Set (replace) the speaker notes of a single slide.

    USE WHEN: adding presenter notes to a deck (talking points for a
    slide, a script for a slides-to-video render, or context for
    collaborators). This REPLACES the slide's existing notes with
    ``notes_text`` (pass an empty string to CLEAR the notes).

    Uses Slides' ``presentations.batchUpdate``. It first resolves the
    slide's speaker-notes shape (reading the presentation to find the
    notesPage's ``speakerNotesObjectId``), then deletes any existing
    notes text and inserts the new text in a single batch, so the change
    commits atomically.

    Args:
        presentation_id: The presentation ID.
        slide_object_id: The target slide's ``object_id`` (from
            ``gslides_get_outline`` / ``gslides_add_slide``).
        notes_text: The notes text to set. Empty string CLEARS the
            slide's notes.

    Returns:
        ``{presentation_id, slide_object_id, speaker_notes_object_id,
        notes_text}`` (echoes the request plus the resolved notes-shape
        objectId).

    Choreography: ``gslides_get_outline`` (to pick the slide's
    ``object_id`` and read its current ``notes``) then
    ``gslides_set_speaker_notes``. Verify with another
    ``gslides_get_outline`` (the per-slide ``notes`` field reflects the
    change).
    """
    return _set_speaker_notes(
        creds,
        presentation_id=presentation_id,
        slide_object_id=slide_object_id,
        notes_text=notes_text,
    )


# ---------------------------------------------------------------------
# 10. gslides_delete_object - batchUpdate (deleteObject)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Delete a page element or slide by its objectId",
    # Removes an element (or a whole slide) and its content, so this is
    # destructive. Matches gsheets_delete_sheet's posture.
    readonly=False,
    destructive=True,
    # Deleting an already-deleted objectId 400s rather than double
    # deleting, so semantically idempotent (same convention as
    # gsheets_delete_sheet). The api layer dispatches single-shot.
    idempotent=True,
    external=True,
    creds=True,
    output_schema=GSLIDES_DELETE_OBJECT_OUTPUT_SCHEMA,
)
def gslides_delete_object(
    creds,
    presentation_id: str,
    object_id: str,
) -> dict:
    """Delete a page element, or an entire slide, by its objectId.

    USE WHEN: removing a shape / image / table / line you (or a prior
    step) placed on a slide, or deleting a whole slide from a deck. The
    ``object_id`` is any objectId from ``gslides_get_outline``: a
    slide's ``object_id`` deletes that slide (and everything on it), or
    an entry from a slide's ``elements[].object_id`` deletes just that
    element.

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``deleteObject`` request. The deletion is permanent (there is no
    undo through the API); re-create the content if you need it back.

    Args:
        presentation_id: The presentation to delete from.
        object_id: The objectId of the element or slide to remove
            (from ``gslides_get_outline``).

    Returns:
        ``{presentation_id, deleted_object_id}``. ``deleted_object_id``
        echoes the objectId that was removed, so the caller can confirm
        which object it acted on.

    Choreography: ``gslides_get_outline`` (to pick the objectId) then
    ``gslides_delete_object``. Verify with another
    ``gslides_get_outline`` (the object is absent from the deck).
    """
    return _delete_object(
        creds,
        presentation_id=presentation_id,
        object_id=object_id,
    )


# ---------------------------------------------------------------------
# 11. gslides_duplicate_object - batchUpdate (duplicateObject)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Duplicate a page element or slide",
    # Adds a copy alongside the original; existing content untouched, and
    # the copy can be deleted later. Not destructive. Matches the
    # create_* posture.
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER copy - NOT idempotent. Same convention as
    # gslides_create_shape / gslides_add_slide.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_DUPLICATE_OBJECT_OUTPUT_SCHEMA,
)
def gslides_duplicate_object(
    creds,
    presentation_id: str,
    object_id: str,
) -> dict:
    """Duplicate a page element (or slide), returning the new objectId.

    USE WHEN: cloning an element you have already styled or positioned
    (a shape, image, table, line) so you can reuse it, or duplicating a
    whole slide as a template for the next one. Pairs with
    ``gslides_update_element_transform`` to reposition the copy after
    duplicating.

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``duplicateObject`` request. Slides copies the object (and, for a
    table / group / slide, its child objects) with fresh objectIds and
    returns the new top-level objectId.

    Args:
        presentation_id: The presentation to duplicate within.
        object_id: The objectId of the element or slide to duplicate
            (from ``gslides_get_outline``).

    Returns:
        ``{presentation_id, source_object_id, new_object_id, id_map}``.
        ``new_object_id`` is the duplicate's stable objectId (a valid
        target for a later ``gslides_update_element_transform`` /
        ``gslides_delete_object``); ``id_map`` is the
        ``{source_object_id: new_object_id}`` mapping Slides returned.

    Choreography: ``gslides_get_outline`` (pick the source objectId) then
    ``gslides_duplicate_object``, then usually
    ``gslides_update_element_transform`` to move the copy off the
    original. Verify with ``gslides_get_outline``.
    """
    return _duplicate_object(
        creds,
        presentation_id=presentation_id,
        object_id=object_id,
    )


# ---------------------------------------------------------------------
# 12. gslides_update_element_transform - batchUpdate
#     (updatePageElementTransform)
# ---------------------------------------------------------------------


@workspace_tool(
    service="slides",
    title="Move or resize a page element via its affine transform",
    # Repositions / rescales an element in place; existing content and
    # other elements untouched, and the move is reversible. Not
    # destructive. Matches gslides_replace_all_text's posture.
    readonly=False,
    destructive=False,
    # The default RELATIVE apply COMPOSES onto the current transform, so
    # re-running keeps moving the element - NOT idempotent.
    idempotent=False,
    external=True,
    creds=True,
    output_schema=GSLIDES_UPDATE_ELEMENT_TRANSFORM_OUTPUT_SCHEMA,
)
def gslides_update_element_transform(
    creds,
    presentation_id: str,
    object_id: str,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    translate_x_emu: float = 0.0,
    translate_y_emu: float = 0.0,
    apply_mode: str = "RELATIVE",
) -> dict:
    """Move or resize a page element by setting its affine transform.

    USE WHEN: repositioning or rescaling a shape / image / table / line
    already on a slide, or nudging a freshly duplicated copy off its
    original. Pairs with ``gslides_duplicate_object`` (duplicate, then
    move the copy) and ``gslides_create_*`` (place, then fine-tune).

    Uses Slides' ``presentations.batchUpdate`` with a single
    ``updatePageElementTransform`` request. ``apply_mode`` chooses how
    the given matrix combines with the element's CURRENT transform:

      * ``"RELATIVE"`` (default, the safe one) COMPOSES onto the existing
        transform. ``scale_x`` / ``scale_y`` of 1 keep the size;
        ``translate_x_emu`` / ``translate_y_emu`` nudge the element by
        that many EMU. A bare call with all defaults is a no-op, so an
        under-specified call cannot collapse or teleport the element.
      * ``"ABSOLUTE"`` REPLACES the transform outright: the element moves
        to exactly (``translate_x_emu``, ``translate_y_emu``) at scale
        (``scale_x``, ``scale_y``) regardless of where it was. Set
        ``scale_x`` / ``scale_y`` explicitly (the 1.0 defaults mean unit
        scale) or the element resets to unit size.

    Units: translate is EMU (914400 EMU per inch, the same unit the
    ``gslides_create_*`` tools use internally); scale is a dimensionless
    multiplier (2.0 doubles the size, -1.0 mirrors).

    Args:
        presentation_id: The presentation the element lives in.
        object_id: The element's objectId (from ``gslides_get_outline``'s
            ``elements[].object_id``).
        scale_x: X-axis scale multiplier (default 1.0). Non-zero.
        scale_y: Y-axis scale multiplier (default 1.0). Non-zero.
        translate_x_emu: X translation in EMU (default 0).
        translate_y_emu: Y translation in EMU (default 0).
        apply_mode: ``"RELATIVE"`` (default) or ``"ABSOLUTE"``.

    Returns:
        ``{presentation_id, object_id, apply_mode, transform}``.
        ``transform`` echoes the exact matrix sent
        (``{scaleX, scaleY, translateX, translateY, unit}``), and
        ``object_id`` names the element that was moved.

    Choreography: ``gslides_get_outline`` (pick the element objectId)
    then ``gslides_update_element_transform``. Verify by re-reading with
    ``gslides_get_outline``.
    """
    return _update_element_transform(
        creds,
        presentation_id=presentation_id,
        object_id=object_id,
        scale_x=scale_x,
        scale_y=scale_y,
        translate_x_emu=translate_x_emu,
        translate_y_emu=translate_y_emu,
        apply_mode=apply_mode,
    )
