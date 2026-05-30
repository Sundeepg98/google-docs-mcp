"""Google Slides MCP tool registrations (v2.3.2 — 3rd new service).

Mirrors the layout established by ``services/sheets/tools.py`` (v2.3.1,
PR #119): ``@workspace_tool``-decorated functions registered with the
live ``mcp`` instance via ``server.py``'s side-effect import.

**Tools registered here** (3 slides-service tools — minimal start):

1. ``gslides_get_outline``         — read structure + per-slide text
2. ``gslides_replace_all_text``    — find/replace across all slides
3. ``gslides_create_presentation`` — create an empty new deck

The minimal trio enables a complete 3-call workflow:
``create_presentation`` → ``replace_all_text`` (against a template
the user created) → ``get_outline`` (to verify).

**Deferred to a follow-up PR** (per multi-service feasibility audit):
the ``batchUpdate`` tagged-union surface (addSlide, replaceImage,
updateTextStyle, updateShapeProperties, etc. — ~40 request types).
This PR proves Slides slots into the per-service template just as
cleanly as Sheets did; the batchUpdate abstraction can be designed
when 3+ real consumers exist (M3 Phase C's extraction-trigger rule
applied to API shapes).

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
    create_presentation as _create_presentation,
    get_outline as _get_outline,
    replace_all_text as _replace_all_text,
)
from appscriptly.tool_schemas import (
    GSLIDES_CREATE_PRESENTATION_OUTPUT_SCHEMA,
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
