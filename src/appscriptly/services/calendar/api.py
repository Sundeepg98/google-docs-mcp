"""Google Calendar REST wrapper (Calendar API v3).

The event surface (``events`` collection):

  * ``list_events``   — ``events.list``   (time-range window over a calendar)
  * ``get_event``     — ``events.get``    (one event by id)
  * ``create_event``  — ``events.insert`` (new event)
  * ``update_event``  — ``events.patch``  (partial update, field-mask semantics)
  * ``delete_event``  — ``events.delete`` (remove an event)

The calendar + availability surface:

  * ``list_calendars`` — ``calendarList.list`` (the calendars the user can see)
  * ``freebusy``       — ``freebusy.query``    (busy intervals across calendars)

**Scope note.** Calls require ``https://www.googleapis.com/auth/calendar``
(read/write events + calendar metadata) in the OAuth consent. This is a
Google **SENSITIVE** scope (NOT restricted) — it does not trigger CASA.
It was added to the single-source ``auth.WORKSPACE_SCOPES`` in v2.4.0, so
it flows into both ``auth.SCOPES`` and ``oauth_google.GOOGLE_API_SCOPES``
with no twin-list drift; existing user grants get it automatically on
next token refresh via the ``include_granted_scopes=true`` incremental-
consent flow (same pattern that handled the Sheets / Slides scope
additions). No forced re-consent.

**Time model.** Calendar API v3 represents an event's start/end as an
``EventDateTime`` — either a timed instant (``{"dateTime": "...RFC3339",
"timeZone": "..."}``) or an all-day date (``{"date": "YYYY-MM-DD"}``).
These wrappers accept RFC 3339 strings (e.g.
``"2026-06-20T09:00:00-07:00"`` or ``"2026-06-20T16:00:00Z"``) and a
``timed`` body by default; passing a bare ``"YYYY-MM-DD"`` to
``create_event`` produces an all-day event. ``list_events`` /
``freebusy`` take RFC 3339 ``time_min`` / ``time_max`` bounds.

**Default calendar.** Every event/freebusy op takes ``calendar_id`` and
defaults it to ``"primary"`` — the authenticated user's main calendar —
so a single-call workflow ("add this to my calendar") needs no id
plumbing. ``list_calendars`` surfaces the ids of secondary / shared
calendars for the cases that target them.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# The default calendar every event/freebusy op targets when the caller
# doesn't name one. ``"primary"`` is the Calendar API's reserved alias
# for the authenticated user's main calendar — resolving it server-side
# means a "put this on my calendar" workflow needs no id lookup.
DEFAULT_CALENDAR_ID = "primary"

# Default page size for list_events. Calendar caps events.list at 2500
# results/page; 250 is a sane default window that keeps a single call
# cheap while covering most "what's on my calendar this week" reads.
# Callers wanting more page explicitly via max_results.
DEFAULT_MAX_RESULTS = 250
_MAX_RESULTS_CAP = 2500

# An all-day date is exactly ``YYYY-MM-DD`` (no time component). A timed
# instant carries a ``T`` separator (RFC 3339). We branch the EventDateTime
# shape on this so a bare date becomes an all-day event and a full
# timestamp becomes a timed one — matching how the Calendar UI treats them.
_ALL_DAY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ``events.list`` orderBy accepts only these two values (Google rejects
# anything else with a 400). Pinned client-side so a typo names the valid
# options rather than bouncing off a generic Google error. ``startTime``
# is only valid together with ``single_events=True`` (Google's own rule —
# we surface that as a clear ValueError rather than a 400).
_ORDER_BY_VALUES = frozenset({"startTime", "updated"})


def _event_datetime(value: str, *, time_zone: str | None) -> dict:
    """Build a Calendar ``EventDateTime`` from a date / datetime string.

    A bare ``YYYY-MM-DD`` becomes an all-day ``{"date": ...}``; anything
    else is treated as an RFC 3339 instant ``{"dateTime": ...}`` (with an
    optional IANA ``timeZone``). Centralised so create/update share one
    correct shape rather than each hand-rolling the branch.
    """
    if _ALL_DAY_DATE_RE.match(value):
        # All-day: Calendar ignores timeZone on a date-only value.
        return {"date": value}
    dt: dict[str, str] = {"dateTime": value}
    if time_zone:
        dt["timeZone"] = time_zone
    return dt


def _attendees(emails: list[str] | None) -> list[dict] | None:
    """Map a list of emails to Calendar's ``[{"email": ...}, ...]`` shape.

    Returns ``None`` for an empty / omitted list so the caller can omit
    the ``attendees`` key entirely (sending ``attendees: []`` on a patch
    would CLEAR existing attendees — not what "no change" means).
    """
    if not emails:
        return None
    return [{"email": e} for e in emails]


def list_events(
    creds: Credentials,
    *,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    single_events: bool = True,
    order_by: str = "startTime",
) -> dict:
    """List events in a time window via ``events.list``.

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        calendar_id: Which calendar to read. Defaults to ``"primary"``
            (the user's main calendar). Use a calendar id from
            ``list_calendars`` to target a secondary / shared one.
        time_min / time_max: RFC 3339 lower / upper bounds on event
            START time (e.g. ``"2026-06-20T00:00:00Z"``). ``time_min`` is
            inclusive, ``time_max`` exclusive. Omit either for an
            unbounded side.
        query: Free-text search over event fields (summary, description,
            location, attendees). Omit for no text filter.
        max_results: Page size (1..2500). Defaults to 250.
        single_events: When ``True`` (default), recurring events are
            EXPANDED into individual instances — the right shape for "what
            is on my calendar". ``False`` returns the recurring event as a
            single series object.
        order_by: ``"startTime"`` (default; requires ``single_events=True``)
            or ``"updated"``.

    Returns:
        ``{calendar_id, events: [...], next_page_token}`` — ``events`` is
        the raw Calendar event list (each a v3 Event resource);
        ``next_page_token`` is ``None`` when there are no more pages.

    Raises:
        ValueError: invalid ``max_results`` / ``order_by``, or
            ``order_by="startTime"`` without ``single_events=True``
            (Google's own constraint, surfaced client-side).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not 1 <= max_results <= _MAX_RESULTS_CAP:
        raise ValueError(
            f"max_results must be between 1 and {_MAX_RESULTS_CAP}; "
            f"got {max_results}."
        )
    if order_by not in _ORDER_BY_VALUES:
        raise ValueError(
            f"order_by must be one of {sorted(_ORDER_BY_VALUES)}; "
            f"got {order_by!r}."
        )
    if order_by == "startTime" and not single_events:
        raise ValueError(
            "order_by='startTime' requires single_events=True (Calendar "
            "only orders expanded instances by start time). Either set "
            "single_events=True or use order_by='updated'."
        )

    calendar = get_service("calendar", "v3", credentials=creds)
    params: dict[str, Any] = {
        "calendarId": calendar_id,
        "maxResults": max_results,
        "singleEvents": single_events,
        "orderBy": order_by,
    }
    if time_min is not None:
        params["timeMin"] = time_min
    if time_max is not None:
        params["timeMax"] = time_max
    if query is not None:
        params["q"] = query

    # events.list is a pure read — idempotent, safe to retry on 429/5xx.
    resp = execute_with_retry(
        lambda: calendar.events().list(**params).execute(),
        idempotent=True,
        op_name="calendar.events.list",
    )
    return {
        "calendar_id": calendar_id,
        "events": resp.get("items", []),
        "next_page_token": resp.get("nextPageToken"),
    }


def get_event(
    creds: Credentials,
    event_id: str,
    *,
    calendar_id: str = DEFAULT_CALENDAR_ID,
) -> dict:
    """Fetch a single event by id via ``events.get``.

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        event_id: The event's id (from ``list_events`` / ``create_event``).
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.

    Returns:
        ``{calendar_id, event}`` — ``event`` is the raw v3 Event resource.

    Raises:
        ValueError: blank ``event_id``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated
            (notably 404 if the id doesn't exist on this calendar).
    """
    if not event_id or not event_id.strip():
        raise ValueError("event_id cannot be empty.")

    calendar = get_service("calendar", "v3", credentials=creds)
    resp = execute_with_retry(
        lambda: calendar.events().get(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute(),
        idempotent=True,
        op_name="calendar.events.get",
    )
    return {"calendar_id": calendar_id, "event": resp}


def create_event(
    creds: Credentials,
    *,
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
    """Create an event via ``events.insert``.

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        summary: The event title (required — a blank title is rejected).
        start / end: RFC 3339 timestamps (e.g.
            ``"2026-06-20T09:00:00-07:00"`` or ``"...Z"``) for a timed
            event, OR bare ``"YYYY-MM-DD"`` for an all-day event. Both must
            be the SAME kind (Calendar 400s on a date start + dateTime end).
        calendar_id: Which calendar to add to. Defaults to ``"primary"``.
        description / location: Optional event body + place.
        attendees: Optional list of attendee emails. Omit for none.
        time_zone: IANA tz (e.g. ``"America/Los_Angeles"``) applied to
            timed start/end. Ignored for all-day events. Omit to let
            Calendar use the calendar's default tz.
        send_updates: Who gets an email invite/notification —
            ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``: create silently, no emails).

    Returns:
        ``{calendar_id, event_id, html_link, summary}`` — ``event_id`` is
        the id Calendar assigned (feed it to get/update/delete);
        ``html_link`` is the event's web URL.

    Raises:
        ValueError: blank ``summary`` / ``start`` / ``end``, or an invalid
            ``send_updates``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched WITHOUT retry: ``events.insert`` is NOT idempotent — a
        transient-error replay could create the same event twice. Honors
        the ``execute_with_retry`` safety floor for non-idempotent
        mutations (plain ``.execute()``).
    """
    if not summary or not summary.strip():
        raise ValueError("summary cannot be empty.")
    if not start or not start.strip():
        raise ValueError("start cannot be empty (RFC 3339 datetime or YYYY-MM-DD).")
    if not end or not end.strip():
        raise ValueError("end cannot be empty (RFC 3339 datetime or YYYY-MM-DD).")
    _check_send_updates(send_updates)

    body: dict[str, Any] = {
        "summary": summary.strip(),
        "start": _event_datetime(start, time_zone=time_zone),
        "end": _event_datetime(end, time_zone=time_zone),
    }
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    attendee_list = _attendees(attendees)
    if attendee_list is not None:
        body["attendees"] = attendee_list

    calendar = get_service("calendar", "v3", credentials=creds)
    # No execute_with_retry: insert is non-idempotent (a replay duplicates
    # the event). Let HttpError propagate to the tool-layer envelope.
    resp = calendar.events().insert(
        calendarId=calendar_id,
        body=body,
        sendUpdates=send_updates,
    ).execute()
    return {
        "calendar_id": calendar_id,
        "event_id": resp.get("id"),
        "html_link": resp.get("htmlLink"),
        "summary": resp.get("summary", summary.strip()),
    }


def update_event(
    creds: Credentials,
    event_id: str,
    *,
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
    """Partially update an event via ``events.patch`` (field-mask semantics).

    Only the fields you pass are changed — every omitted field is left
    untouched (``events.patch``, not ``update``, so this is a true partial
    merge rather than a full-resource overwrite).

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        event_id: The event to modify (required).
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.
        summary / description / location: New values for those fields;
            omit to leave unchanged.
        start / end: New RFC 3339 timestamps (or ``YYYY-MM-DD`` for
            all-day); omit to leave the time unchanged. (Changing only one
            side is allowed but Calendar may 400 if it makes start/end
            inconsistent — pass both when moving an event.)
        attendees: New COMPLETE attendee list (emails). NOTE: this
            REPLACES the attendee list, it does not merge — pass the full
            desired set. Omit to leave attendees unchanged.
        time_zone: IANA tz applied to any timed start/end you pass.
        send_updates: ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``).

    Returns:
        ``{calendar_id, event_id, html_link, summary}`` — echoes the
        patched event's id + web link + (possibly updated) summary.

    Raises:
        ValueError: blank ``event_id``, no updatable field supplied (an
            empty patch is a caller bug — reject rather than issue a no-op
            round-trip), or an invalid ``send_updates``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.

    Note:
        Dispatched with ``idempotent=True`` — re-applying the SAME patch
        yields the same event state, so it is safe to retry on a transient
        429/5xx (unlike ``create_event``'s insert).
    """
    if not event_id or not event_id.strip():
        raise ValueError("event_id cannot be empty.")
    _check_send_updates(send_updates)

    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        body["start"] = _event_datetime(start, time_zone=time_zone)
    if end is not None:
        body["end"] = _event_datetime(end, time_zone=time_zone)
    attendee_list = _attendees(attendees)
    if attendee_list is not None:
        body["attendees"] = attendee_list

    if not body:
        raise ValueError(
            "no fields to update — pass at least one of summary / start / "
            "end / description / location / attendees."
        )

    calendar = get_service("calendar", "v3", credentials=creds)
    resp = execute_with_retry(
        lambda: calendar.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates=send_updates,
        ).execute(),
        idempotent=True,
        op_name="calendar.events.patch",
    )
    return {
        "calendar_id": calendar_id,
        "event_id": resp.get("id", event_id),
        "html_link": resp.get("htmlLink"),
        "summary": resp.get("summary"),
    }


def delete_event(
    creds: Credentials,
    event_id: str,
    *,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    send_updates: str = "none",
) -> dict:
    """Delete an event via ``events.delete``.

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        event_id: The event to remove (required).
        calendar_id: The calendar the event lives on. Defaults to
            ``"primary"``.
        send_updates: Whether to email attendees about the cancellation —
            ``"all"`` / ``"externalOnly"`` / ``"none"`` (default
            ``"none"``).

    Returns:
        ``{calendar_id, deleted_event_id}`` — ``deleted_event_id`` echoes
        the id that was removed (``events.delete`` returns an empty body
        on success).

    Raises:
        ValueError: blank ``event_id`` / invalid ``send_updates``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated
            (notably 404/410 if the event is already gone).

    Note:
        ``events.delete`` is DESTRUCTIVE. Deleting an already-deleted event
        returns 410 rather than double-deleting, so the OUTCOME is
        idempotent in intent — but the dispatch stays non-retried to honor
        the destructive-op safety floor (matching ``gsheets_delete_sheet``).
    """
    if not event_id or not event_id.strip():
        raise ValueError("event_id cannot be empty.")
    _check_send_updates(send_updates)

    calendar = get_service("calendar", "v3", credentials=creds)
    # No execute_with_retry: destructive-op safety floor (a delete is not
    # blanket-retried even though re-deleting 410s). Let HttpError propagate.
    calendar.events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates=send_updates,
    ).execute()
    return {"calendar_id": calendar_id, "deleted_event_id": event_id}


def list_calendars(
    creds: Credentials,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """List the calendars on the user's calendar list via ``calendarList.list``.

    The way to discover the ids of secondary / shared / subscribed
    calendars so event ops can target them (event ops default to
    ``"primary"``, this surfaces everything else).

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        max_results: Page size (1..250 for calendarList). Defaults to 250.

    Returns:
        ``{calendars: [{id, summary, primary, access_role}, ...],
        next_page_token}`` — one flattened entry per calendar (the four
        load-bearing fields), plus the page token (``None`` when no more).

    Raises:
        ValueError: invalid ``max_results``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    # calendarList caps maxResults at 250; reuse the same 1-bound check.
    if not 1 <= max_results <= 250:
        raise ValueError(
            f"max_results must be between 1 and 250 for calendarList; "
            f"got {max_results}."
        )

    calendar = get_service("calendar", "v3", credentials=creds)
    resp = execute_with_retry(
        lambda: calendar.calendarList().list(maxResults=max_results).execute(),
        idempotent=True,
        op_name="calendar.calendarList.list",
    )
    calendars = [
        {
            "id": c.get("id"),
            "summary": c.get("summary"),
            "primary": c.get("primary", False),
            "access_role": c.get("accessRole"),
        }
        for c in resp.get("items", [])
    ]
    return {
        "calendars": calendars,
        "next_page_token": resp.get("nextPageToken"),
    }


def freebusy(
    creds: Credentials,
    *,
    time_min: str,
    time_max: str,
    calendar_ids: list[str] | None = None,
) -> dict:
    """Query busy intervals across calendars via ``freebusy.query``.

    The availability primitive: given a window and a set of calendars,
    Calendar returns each calendar's BUSY time ranges within the window
    (so a scheduler can find the gaps).

    Args:
        creds: OAuth credentials carrying the ``calendar`` scope.
        time_min / time_max: RFC 3339 bounds of the window to check
            (both required — a free/busy query is meaningless without a
            window).
        calendar_ids: Calendars to include. Omit / empty to check just
            ``"primary"``. Use ids from ``list_calendars`` for others.

    Returns:
        ``{time_min, time_max, calendars: {<id>: {"busy": [{start, end},
        ...]}, ...}}`` — Calendar's per-calendar busy map (passed through
        from the API's ``calendars`` field), echoed alongside the window.

    Raises:
        ValueError: blank ``time_min`` / ``time_max``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not time_min or not time_min.strip():
        raise ValueError("time_min cannot be empty (RFC 3339 datetime).")
    if not time_max or not time_max.strip():
        raise ValueError("time_max cannot be empty (RFC 3339 datetime).")

    ids = calendar_ids or [DEFAULT_CALENDAR_ID]
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": cid} for cid in ids],
    }
    calendar = get_service("calendar", "v3", credentials=creds)
    resp = execute_with_retry(
        lambda: calendar.freebusy().query(body=body).execute(),
        idempotent=True,
        op_name="calendar.freebusy.query",
    )
    return {
        "time_min": time_min,
        "time_max": time_max,
        "calendars": resp.get("calendars", {}),
    }


# Calendar's ``sendUpdates`` enum — who receives email notifications when
# an event is created / changed / deleted. Pinned client-side so a typo
# names the valid options rather than bouncing off a generic Google 400.
_SEND_UPDATES_VALUES = frozenset({"all", "externalOnly", "none"})


def _check_send_updates(send_updates: str) -> None:
    """Reject an unknown ``sendUpdates`` value client-side."""
    if send_updates not in _SEND_UPDATES_VALUES:
        raise ValueError(
            f"send_updates must be one of {sorted(_SEND_UPDATES_VALUES)} "
            f"(who gets emailed about the change); got {send_updates!r}."
        )
