"""Google Calendar MCP tool registrations (v2.4.0 — 4th new service).

Mirrors the layout established by ``services/sheets/tools.py`` and
``services/slides/tools.py``: ``@workspace_tool``-decorated functions
that register with the live ``mcp`` instance when this module is
imported. Post auto-discovery refactor, ``server.py`` walks
``services/`` and imports each non-private leaf module, so this module's
decorations register as a side effect of that walk (no hand-maintained
``from .services.calendar import tools`` line).

**Tools registered here** (7 calendar-service tools):

1. ``gcal_list_events``    — list events in a time window (events.list)
2. ``gcal_get_event``      — fetch one event by id (events.get)
3. ``gcal_create_event``   — create an event (events.insert)
4. ``gcal_update_event``   — partial-update an event (events.patch)
5. ``gcal_delete_event``   — delete an event (events.delete)
6. ``gcal_list_calendars`` — list the user's calendars (calendarList.list)
7. ``gcal_freebusy``       — busy-interval availability query (freebusy.query)

(Authoritative declaration: ``services/calendar/_expected_tools.py``.)

**Scope (SENSITIVE, no CASA).** Every tool declares
``scopes=[CALENDAR_SCOPE]`` where ``CALENDAR_SCOPE`` is the full
``https://www.googleapis.com/auth/calendar`` read/write scope — a Google
**SENSITIVE** scope, NOT restricted, so it does not trigger CASA. The
scope is already in the baseline consent set (added once to
``auth.WORKSPACE_SCOPES``), so the per-tool ``scopes=`` declaration is
documentary + observability (it rides on ``ToolAnnotations.scopes`` and
the wrapper's ``_check_scopes_or_raise`` passes because the scope is
baseline-granted) — the same posture
``services/gas_deploy/tools.py`` uses with its
``required_scopes=GAS_DEPLOY_SCOPES``.

**Import discipline.** Same as ``services/sheets/tools.py``:

- ``_get_credentials`` + ``_format_http_error`` imported directly from
  ``_tool_helpers`` (the M3 Phase C extraction) for parity; the standard
  ``@workspace_tool(creds=True)`` envelope handles creds injection +
  ``HttpError`` translation, so tool bodies stay thin pass-throughs.
- ``@workspace_tool(service="calendar", ...)`` carries the ``service=``
  literal that drives the partition test + telemetry.
"""
from __future__ import annotations

from appscriptly.decorators import workspace_tool
from appscriptly.services.calendar.api import (
    DEFAULT_CALENDAR_ID,
    DEFAULT_MAX_RESULTS,
    create_event as _create_event,
    delete_event as _delete_event,
    freebusy as _freebusy,
    get_event as _get_event,
    list_calendars as _list_calendars,
    list_events as _list_events,
    update_event as _update_event,
)
from appscriptly.tool_schemas import (
    GCAL_CREATE_EVENT_OUTPUT_SCHEMA,
    GCAL_DELETE_EVENT_OUTPUT_SCHEMA,
    GCAL_FREEBUSY_OUTPUT_SCHEMA,
    GCAL_GET_EVENT_OUTPUT_SCHEMA,
    GCAL_LIST_CALENDARS_OUTPUT_SCHEMA,
    GCAL_LIST_EVENTS_OUTPUT_SCHEMA,
    GCAL_UPDATE_EVENT_OUTPUT_SCHEMA,
)

# Imported for parity with services/sheets/tools.py; the standard
# decorator envelope handles creds + HttpError so the tool bodies don't
# reference these directly. Kept as a top-level import so a future tool
# that needs custom shaping doesn't trigger a separate import statement.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# The full read/write Calendar scope. Declared on every tool via
# ``scopes=[CALENDAR_SCOPE]``. SENSITIVE (not restricted) → no CASA. It is
# also in the baseline ``auth.WORKSPACE_SCOPES`` single source, so this
# per-tool declaration is the SRP-aligned annotation half (the resolution
# is a no-op because the scope is baseline-granted) — same pattern as
# ``gas_deploy``'s ``required_scopes=GAS_DEPLOY_SCOPES``.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


# ---------------------------------------------------------------------
# 1. gcal_list_events — events.list (time-range read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="List Google Calendar events in a time range",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_LIST_EVENTS_OUTPUT_SCHEMA,
)
def gcal_list_events(
    creds,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    single_events: bool = True,
    order_by: str = "startTime",
) -> dict:
    """List events on a calendar within an optional time window.

    USE WHEN: the agent needs to see what's scheduled — "what's on my
    calendar this week", "find my 1:1 with Sam", a conflict check before
    proposing a meeting time.

    Uses Calendar v3 ``events.list``. By default expands recurring events
    into individual instances (``single_events=True``) ordered by start
    time — the right shape for "what is on my calendar". Pass
    ``single_events=False`` to get recurring series as single objects.

    Args:
        calendar_id: Which calendar to read. Defaults to ``"primary"``
            (your main calendar). Get other ids from
            ``gcal_list_calendars``.
        time_min: RFC 3339 lower bound on event start, inclusive (e.g.
            ``"2026-06-20T00:00:00Z"``). Omit for no lower bound.
        time_max: RFC 3339 upper bound on event start, exclusive. Omit
            for no upper bound.
        query: Free-text search over summary / description / location /
            attendees. Omit for no text filter.
        max_results: Page size, 1..2500 (default 250).
        single_events: ``True`` (default) expands recurring events into
            instances; ``False`` returns the series object.
        order_by: ``"startTime"`` (default; needs ``single_events=True``)
            or ``"updated"``.

    Returns:
        ``{calendar_id, events, next_page_token}`` — ``events`` is the raw
        v3 Event list; ``next_page_token`` is ``None`` when there are no
        more pages (pass it back to page — wire a follow-up call when
        present).

    Choreography: pair with ``gcal_list_calendars`` to target a
    non-primary calendar, or with ``gcal_freebusy`` for a pure
    availability (busy-gaps) view rather than full event detail.
    """
    return _list_events(
        creds,
        calendar_id=calendar_id,
        time_min=time_min,
        time_max=time_max,
        query=query,
        max_results=max_results,
        single_events=single_events,
        order_by=order_by,
    )


# ---------------------------------------------------------------------
# 2. gcal_get_event — events.get (one event by id)
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="Get a single Google Calendar event by id",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_GET_EVENT_OUTPUT_SCHEMA,
)
def gcal_get_event(
    creds,
    event_id: str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
) -> dict:
    """Fetch the full detail of one calendar event by its id.

    USE WHEN: the agent has an ``event_id`` (from ``gcal_list_events`` or
    ``gcal_create_event``) and needs the complete event resource —
    attendees, description, conference data, reminders, etc. — before
    summarizing or updating it.

    Uses Calendar v3 ``events.get``.

    Args:
        event_id: The event's id.
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.

    Returns:
        ``{calendar_id, event}`` — ``event`` is the raw v3 Event resource.

    Choreography: get the ``event_id`` from ``gcal_list_events``; follow
    with ``gcal_update_event`` / ``gcal_delete_event`` to act on it.
    """
    return _get_event(creds, event_id, calendar_id=calendar_id)


# ---------------------------------------------------------------------
# 3. gcal_create_event — events.insert
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="Create a Google Calendar event",
    # Creating a fresh event isn't a mutation of existing state. Matches
    # gsheets_create_spreadsheet / gslides_add_slide.
    readonly=False,
    destructive=False,
    # NOT idempotent: re-running creates ANOTHER event. Same convention as
    # the other create/append tools.
    idempotent=False,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_CREATE_EVENT_OUTPUT_SCHEMA,
)
def gcal_create_event(
    creds,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    time_zone: str | None = None,
    send_updates: str = "none",
) -> dict:
    """Create a calendar event (timed or all-day).

    USE WHEN: the agent should schedule something — "book a 30-min sync
    Friday 2pm", "add a reminder for the launch on the 20th", "invite
    sam@ and lee@ to a design review".

    Uses Calendar v3 ``events.insert``. ``start`` / ``end`` accept either
    RFC 3339 timestamps (``"2026-06-20T14:00:00-07:00"`` / ``"...Z"``) for
    a TIMED event, or a bare ``"YYYY-MM-DD"`` for an ALL-DAY event — both
    ends must be the same kind.

    Args:
        summary: The event title (required).
        start: RFC 3339 start instant, or ``"YYYY-MM-DD"`` for all-day.
        end: RFC 3339 end instant, or ``"YYYY-MM-DD"`` for all-day. Must
            match ``start``'s kind.
        calendar_id: Which calendar to add to. Defaults to ``"primary"``.
        description: Optional event body text.
        location: Optional location string.
        attendees: Optional list of attendee emails.
        time_zone: IANA tz (e.g. ``"America/Los_Angeles"``) for timed
            events; ignored for all-day. Omit to use the calendar default.
        send_updates: Whether attendees get an email invite —
            ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``: create silently).

    Returns:
        ``{calendar_id, event_id, html_link, summary}`` — ``event_id`` is
        the id Calendar assigned (feed it to ``gcal_get_event`` /
        ``gcal_update_event`` / ``gcal_delete_event``); ``html_link`` is
        the event's web URL.

    Choreography: the natural starter for any scheduling workflow. To put
    it on a non-primary calendar, get the id from ``gcal_list_calendars``
    first. Re-running creates ANOTHER event (not idempotent) — create each
    distinct event once.
    """
    return _create_event(
        creds,
        summary=summary,
        start=start,
        end=end,
        calendar_id=calendar_id,
        description=description,
        location=location,
        attendees=attendees,
        time_zone=time_zone,
        send_updates=send_updates,
    )


# ---------------------------------------------------------------------
# 4. gcal_update_event — events.patch (partial update)
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="Update (patch) a Google Calendar event",
    # A partial in-place edit — not destructive (the event persists; only
    # the passed fields change). Matches gsheets_rename_sheet.
    readonly=False,
    destructive=False,
    # Re-applying the SAME patch yields the same event state — safe to
    # retry. (The api layer dispatches it idempotent=True.)
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_UPDATE_EVENT_OUTPUT_SCHEMA,
)
def gcal_update_event(
    creds,
    event_id: str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    time_zone: str | None = None,
    send_updates: str = "none",
) -> dict:
    """Partially update an event — change only the fields you pass.

    USE WHEN: an event needs a tweak — reschedule the time, change the
    title, update the location, swap the attendee list. Only the fields
    you supply change; everything else is left as-is (``events.patch``, a
    true partial merge — not a full overwrite).

    Uses Calendar v3 ``events.patch``.

    Args:
        event_id: The event to modify (required).
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.
        summary / description / location: New values; omit to leave
            unchanged.
        start / end: New RFC 3339 timestamps (or ``"YYYY-MM-DD"`` all-day);
            omit to leave the time unchanged. Pass BOTH when moving an
            event so start/end stay consistent.
        attendees: New COMPLETE attendee email list — REPLACES the current
            list (does not merge). Omit to leave attendees unchanged.
        time_zone: IANA tz applied to any timed start/end you pass.
        send_updates: ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``).

    Returns:
        ``{calendar_id, event_id, html_link, summary}`` — echoes the
        patched event's id + web link + (possibly updated) summary.

    Choreography: get the ``event_id`` from ``gcal_list_events`` /
    ``gcal_create_event``. Pass at least one updatable field — an empty
    patch raises a ValueError rather than issuing a no-op round-trip.
    """
    return _update_event(
        creds,
        event_id,
        calendar_id=calendar_id,
        summary=summary,
        start=start,
        end=end,
        description=description,
        location=location,
        attendees=attendees,
        time_zone=time_zone,
        send_updates=send_updates,
    )


# ---------------------------------------------------------------------
# 5. gcal_delete_event — events.delete
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="Delete a Google Calendar event",
    readonly=False,
    # Removing an event deletes it — genuinely destructive (the event +
    # its data are gone). Matches gsheets_delete_sheet / gdocs_delete_tab.
    destructive=True,
    # Deleting an already-deleted event 410s rather than double-deleting,
    # so the OUTCOME is idempotent in intent; annotated True to match
    # gsheets_delete_sheet. (The api layer still dispatches non-retried to
    # honor the destructive-op safety floor.)
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_DELETE_EVENT_OUTPUT_SCHEMA,
)
def gcal_delete_event(
    creds,
    event_id: str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    send_updates: str = "none",
) -> dict:
    """Delete a calendar event — removes it (and optionally notifies attendees).

    USE WHEN: an event should be cancelled / removed — "cancel my 3pm",
    "delete that duplicate hold". DESTRUCTIVE: the event is removed.

    Uses Calendar v3 ``events.delete``.

    Args:
        event_id: The event to remove (required).
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.
        send_updates: Whether to email attendees about the cancellation —
            ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``: delete silently).

    Returns:
        ``{calendar_id, deleted_event_id}`` — ``deleted_event_id`` echoes
        the id that was removed.

    Choreography: get the ``event_id`` from ``gcal_list_events``. To merely
    RESCHEDULE rather than cancel, use ``gcal_update_event`` instead.
    """
    return _delete_event(
        creds, event_id, calendar_id=calendar_id, send_updates=send_updates,
    )


# ---------------------------------------------------------------------
# 6. gcal_list_calendars — calendarList.list
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="List the user's Google Calendars",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_LIST_CALENDARS_OUTPUT_SCHEMA,
)
def gcal_list_calendars(
    creds,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """List the calendars the user can see (to discover their ids).

    USE WHEN: the agent needs to target a calendar OTHER than the primary
    one — a shared team calendar, a secondary "Personal" calendar, a
    subscribed calendar. Every event tool defaults to ``"primary"``; this
    is how you find the id for the rest.

    Uses Calendar v3 ``calendarList.list``.

    Args:
        max_results: Page size, 1..250 (default 250).

    Returns:
        ``{calendars, next_page_token}`` — ``calendars`` is a list of
        ``{id, summary, primary, access_role}`` (``id`` is what the event
        tools take as ``calendar_id``; ``access_role`` tells you whether
        you can write — ``"owner"`` / ``"writer"`` vs ``"reader"``).
        ``next_page_token`` is ``None`` when there are no more pages.

    Choreography: run this first when a request names a specific (non-
    primary) calendar; pass the returned ``id`` as ``calendar_id`` to
    ``gcal_list_events`` / ``gcal_create_event`` / etc.
    """
    return _list_calendars(creds, max_results=max_results)


# ---------------------------------------------------------------------
# 7. gcal_freebusy — freebusy.query (availability)
# ---------------------------------------------------------------------


@workspace_tool(
    service="calendar",
    title="Query free/busy availability across Google Calendars",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=[CALENDAR_SCOPE],
    output_schema=GCAL_FREEBUSY_OUTPUT_SCHEMA,
)
def gcal_freebusy(
    creds,
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
) -> dict:
    """Find busy time ranges across one or more calendars (availability).

    USE WHEN: the agent needs to find a free slot or check availability
    BEFORE proposing/booking a time — "when am I free Thursday", "is the
    team calendar busy 2-4pm", scheduling across several people's
    calendars. Returns BUSY intervals; the gaps between them are the free
    time.

    Uses Calendar v3 ``freebusy.query``. Lighter than ``gcal_list_events``
    when you only care about busy-vs-free (no event detail).

    Args:
        time_min: RFC 3339 start of the window to check (required).
        time_max: RFC 3339 end of the window to check (required).
        calendar_ids: Calendars to include. Omit to check just
            ``"primary"``; pass ids from ``gcal_list_calendars`` for
            others (and for cross-person scheduling, their calendar ids if
            shared with you).

    Returns:
        ``{time_min, time_max, calendars}`` — ``calendars`` maps each
        calendar id to ``{"busy": [{start, end}, ...]}`` (the busy ranges
        within the window). An empty ``busy`` list means fully free in
        that window.

    Choreography: pair with ``gcal_list_calendars`` to assemble the
    ``calendar_ids`` set, then ``gcal_create_event`` once a free slot is
    chosen.
    """
    return _freebusy(
        creds, time_min=time_min, time_max=time_max, calendar_ids=calendar_ids,
    )
