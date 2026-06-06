"""Google Slides MCP tool registrations (v2.3.2 — 3rd new service).

Mirrors the layout established by ``services/sheets/tools.py`` (v2.3.1,
PR #119): ``@workspace_tool``-decorated functions registered with the
live ``mcp`` instance via ``server.py``'s side-effect import.

**Tools registered here** (6 slides-service tools):

1. ``gslides_get_outline``         — read structure + per-slide text
2. ``gslides_replace_all_text``    — find/replace across all slides
3. ``gslides_create_presentation`` — create an empty new deck
4. ``gslides_add_slide``           — append a slide (+ title/body text)
5. ``gslides_create_image``        — insert an image (by URL) on a slide
6. ``gslides_create_table``        — insert an empty table on a slide

The first three were the minimal trio; ``gslides_add_slide`` closed
the slide-population gap, and ``gslides_create_image`` /
``gslides_create_table`` add visual + tabular content so a deck can
be authored end-to-end: ``create_presentation`` → ``add_slide`` (×N)
→ ``create_image`` / ``create_table`` → ``get_outline`` to verify.

**Still deferred to a follow-up PR**: the rest of the Slides
``batchUpdate`` tagged-union (createShape, createLine,
updateTextStyle, updateShapeProperties, element transform/delete,
speaker notes, etc.). The four write tools here are
``createSlide``/``insertText``/``createImage``/``createTable``
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
    create_presentation as _create_presentation,
    create_table as _create_table,
    get_outline as _get_outline,
    replace_all_text as _replace_all_text,
)
from appscriptly.tool_schemas import (
    GSLIDES_ADD_SLIDE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_IMAGE_OUTPUT_SCHEMA,
    GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
    GSLIDES_CREATE_TABLE_OUTPUT_SCHEMA,
    GSLIDES_GET_OUTLINE_OUTPUT_SCHEMA,
    GSLIDES_REPLACE_ALL_TEXT_OUTPUT_SCHEMA,
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

    Uses Slides' ``presentations.get`` REST endpoint. Returns each
    slide's stable ``object_id`` (the Slides equivalent of docs'
    tab IDs — usable later as a target for batchUpdate write
    operations when those ship), its layout objectId, and the
    flattened readable text from all text shapes on the slide.

    Args:
        presentation_id: The presentation ID (the ID part of the
            sharing URL).

    Returns:
        ``{presentation_id, title, url, slides: [{object_id, layout,
        text}, ...]}``. ``slides`` is empty when the deck has no
        slides (rare; Slides auto-creates a default slide on
        ``create_presentation``). Per-slide ``text`` is the empty
        string for image-only slides.

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
