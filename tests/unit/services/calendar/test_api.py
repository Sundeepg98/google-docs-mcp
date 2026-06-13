"""Co-located tests for services/calendar/api.py (v2.4.0).

Mirrors ``tests/unit/services/sheets/test_api.py``: exercise the module
via ``with_google_api_client(InMemoryGoogleAPIClient)`` so the real
``get_service`` chokepoint runs but Calendar's HTTP boundary is stubbed.
No real OAuth, no real Calendar round-trip.

Tests cover four surfaces:

1. **Module-level constants** — pin ``DEFAULT_CALENDAR_ID`` /
   ``DEFAULT_MAX_RESULTS`` as the public surface.
2. **Pre-API validation** — the ``ValueError`` branches (blank ids /
   times / summary, invalid max_results / order_by / send_updates, empty
   patch, startTime-needs-single-events).
3. **Calendar call shape** — the right method chain
   (``events().list/get/insert/patch/delete`` / ``calendarList().list`` /
   ``freebusy().query``) receives the right kwargs (calendarId default
   ``"primary"``, the all-day-vs-timed EventDateTime branch, sendUpdates,
   the freebusy body).
4. **Response envelope shape** — the flat dicts the tool layer surfaces.

This is the new-service proof for v2.4.0: the M2 chokepoint +
per-service-folder pattern + M4 ``@workspace_tool`` annotation surface
scale to Calendar (``("calendar", "v3")``) without infra rework.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.calendar.api import (
    DEFAULT_CALENDAR_ID,
    DEFAULT_MAX_RESULTS,
    create_event,
    delete_event,
    freebusy,
    get_event,
    list_calendars,
    list_events,
    update_event,
)


# ---------------------------------------------------------------------
# Module-level constants — public surface canary
# ---------------------------------------------------------------------


def test_default_calendar_id_is_primary():
    """``primary`` = the Calendar reserved alias for the user's main
    calendar. Pinned so a stray edit doesn't silently retarget every
    event op."""
    assert DEFAULT_CALENDAR_ID == "primary"


def test_default_max_results_is_250():
    assert DEFAULT_MAX_RESULTS == 250


# ---------------------------------------------------------------------
# Shared stub fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def calendar_stub():
    """A Calendar v3 Resource stub with every method chain pre-wired to a
    plausible default response. Individual tests override per-call."""
    cal = MagicMock(name="calendar-v3-stub")
    cal.events().list().execute.return_value = {
        "items": [{"id": "e1"}, {"id": "e2"}],
        "nextPageToken": None,
    }
    cal.events().get().execute.return_value = {"id": "e1", "summary": "Sync"}
    cal.events().insert().execute.return_value = {
        "id": "NEW-1",
        "htmlLink": "https://calendar.google.com/event?eid=NEW-1",
        "summary": "Sync",
    }
    cal.events().patch().execute.return_value = {
        "id": "e1",
        "htmlLink": "https://calendar.google.com/event?eid=e1",
        "summary": "Renamed",
    }
    cal.events().delete().execute.return_value = ""
    cal.calendarList().list().execute.return_value = {
        "items": [
            {"id": "primary", "summary": "Me", "primary": True,
             "accessRole": "owner"},
            {"id": "team@group.calendar.google.com", "summary": "Team",
             "accessRole": "writer"},
        ],
        "nextPageToken": None,
    }
    cal.freebusy().query().execute.return_value = {
        "calendars": {"primary": {"busy": [
            {"start": "2026-06-20T14:00:00Z", "end": "2026-06-20T15:00:00Z"},
        ]}},
    }
    return cal


@pytest.fixture
def with_calendar_stub(calendar_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("calendar", "v3"): calendar_stub,
    })):
        yield calendar_stub


def _last_real_kwargs(method_mock) -> dict:
    """Most recent call to ``method_mock`` that carried a ``calendarId``
    (skips the fixture-priming ``()`` calls with no kwargs)."""
    for call in reversed(method_mock.call_args_list):
        if "calendarId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no call captured calendarId")


# ---------------------------------------------------------------------
# list_events — call shape + envelope + validation
# ---------------------------------------------------------------------


def test_list_events_defaults_to_primary(with_calendar_stub):
    list_events(MagicMock())
    kw = _last_real_kwargs(with_calendar_stub.events().list)
    assert kw["calendarId"] == "primary"


def test_list_events_forwards_time_window_and_query(with_calendar_stub):
    list_events(
        MagicMock(),
        calendar_id="cal-x",
        time_min="2026-06-20T00:00:00Z",
        time_max="2026-06-21T00:00:00Z",
        query="standup",
    )
    kw = _last_real_kwargs(with_calendar_stub.events().list)
    assert kw["calendarId"] == "cal-x"
    assert kw["timeMin"] == "2026-06-20T00:00:00Z"
    assert kw["timeMax"] == "2026-06-21T00:00:00Z"
    assert kw["q"] == "standup"
    # single_events / orderBy defaults flow through.
    assert kw["singleEvents"] is True
    assert kw["orderBy"] == "startTime"


def test_list_events_envelope_shape(with_calendar_stub):
    result = list_events(MagicMock())
    assert result == {
        "calendar_id": "primary",
        "events": [{"id": "e1"}, {"id": "e2"}],
        "next_page_token": None,
    }


def test_list_events_rejects_bad_max_results():
    with pytest.raises(ValueError, match="max_results must be between"):
        list_events(MagicMock(), max_results=0)
    with pytest.raises(ValueError, match="max_results must be between"):
        list_events(MagicMock(), max_results=9999)


def test_list_events_rejects_bad_order_by():
    with pytest.raises(ValueError, match="order_by must be one of"):
        list_events(MagicMock(), order_by="title")


def test_list_events_rejects_startTime_without_single_events():
    """Google's own constraint, surfaced client-side as a clear error."""
    with pytest.raises(ValueError, match="requires single_events=True"):
        list_events(MagicMock(), order_by="startTime", single_events=False)


def test_list_events_order_by_updated_allows_single_events_false(with_calendar_stub):
    """order_by='updated' is valid with single_events=False — no raise."""
    list_events(MagicMock(), order_by="updated", single_events=False)
    kw = _last_real_kwargs(with_calendar_stub.events().list)
    assert kw["orderBy"] == "updated"
    assert kw["singleEvents"] is False


# ---------------------------------------------------------------------
# get_event
# ---------------------------------------------------------------------


def test_get_event_passes_ids(with_calendar_stub):
    get_event(MagicMock(), "e1", calendar_id="cal-x")
    kw = _last_real_kwargs(with_calendar_stub.events().get)
    assert kw["calendarId"] == "cal-x"
    assert kw["eventId"] == "e1"


def test_get_event_envelope(with_calendar_stub):
    result = get_event(MagicMock(), "e1")
    assert result == {"calendar_id": "primary", "event": {"id": "e1", "summary": "Sync"}}


def test_get_event_rejects_blank_id():
    with pytest.raises(ValueError, match="event_id cannot be empty"):
        get_event(MagicMock(), "  ")


# ---------------------------------------------------------------------
# create_event — EventDateTime branch + body + envelope + validation
# ---------------------------------------------------------------------


def test_create_event_timed_builds_dateTime_body(with_calendar_stub):
    create_event(
        MagicMock(),
        summary="Sync",
        start="2026-06-20T09:00:00-07:00",
        end="2026-06-20T09:30:00-07:00",
        time_zone="America/Los_Angeles",
        attendees=["a@x.com", "b@x.com"],
        description="weekly",
        location="Room 4",
    )
    kw = _last_real_kwargs(with_calendar_stub.events().insert)
    body = kw["body"]
    assert body["summary"] == "Sync"
    assert body["start"] == {
        "dateTime": "2026-06-20T09:00:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    assert body["end"]["dateTime"] == "2026-06-20T09:30:00-07:00"
    assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@x.com"}]
    assert body["description"] == "weekly"
    assert body["location"] == "Room 4"
    # sendUpdates defaults to "none" (silent create).
    assert kw["sendUpdates"] == "none"


def test_create_event_all_day_builds_date_body(with_calendar_stub):
    """A bare YYYY-MM-DD start/end becomes an all-day {"date": ...} (no
    timeZone), not a timed event."""
    create_event(
        MagicMock(), summary="Launch", start="2026-06-20", end="2026-06-21",
        time_zone="America/Los_Angeles",  # ignored for all-day
    )
    body = _last_real_kwargs(with_calendar_stub.events().insert)["body"]
    assert body["start"] == {"date": "2026-06-20"}
    assert body["end"] == {"date": "2026-06-21"}


def test_create_event_envelope(with_calendar_stub):
    result = create_event(
        MagicMock(), summary="Sync",
        start="2026-06-20T09:00:00Z", end="2026-06-20T09:30:00Z",
    )
    assert result == {
        "calendar_id": "primary",
        "event_id": "NEW-1",
        "html_link": "https://calendar.google.com/event?eid=NEW-1",
        "summary": "Sync",
    }


def test_create_event_omits_attendees_when_none(with_calendar_stub):
    create_event(
        MagicMock(), summary="Solo",
        start="2026-06-20T09:00:00Z", end="2026-06-20T09:30:00Z",
    )
    body = _last_real_kwargs(with_calendar_stub.events().insert)["body"]
    assert "attendees" not in body


@pytest.mark.parametrize("field,kwargs", [
    ("summary", {"summary": "", "start": "2026-06-20T09:00:00Z",
                 "end": "2026-06-20T10:00:00Z"}),
    ("start", {"summary": "x", "start": "  ", "end": "2026-06-20T10:00:00Z"}),
    ("end", {"summary": "x", "start": "2026-06-20T09:00:00Z", "end": ""}),
])
def test_create_event_rejects_blank_required(field, kwargs):
    with pytest.raises(ValueError, match=f"{field} cannot be empty"):
        create_event(MagicMock(), **kwargs)


def test_create_event_rejects_bad_send_updates():
    with pytest.raises(ValueError, match="send_updates must be one of"):
        create_event(
            MagicMock(), summary="x",
            start="2026-06-20T09:00:00Z", end="2026-06-20T10:00:00Z",
            send_updates="maybe",
        )


# ---------------------------------------------------------------------
# update_event — patch body + empty-patch guard + envelope
# ---------------------------------------------------------------------


def test_update_event_patches_only_passed_fields(with_calendar_stub):
    update_event(MagicMock(), "e1", summary="Renamed")
    kw = _last_real_kwargs(with_calendar_stub.events().patch)
    assert kw["eventId"] == "e1"
    assert kw["body"] == {"summary": "Renamed"}


def test_update_event_builds_time_in_patch(with_calendar_stub):
    update_event(
        MagicMock(), "e1",
        start="2026-06-20T11:00:00Z", end="2026-06-20T12:00:00Z",
    )
    body = _last_real_kwargs(with_calendar_stub.events().patch)["body"]
    assert body["start"] == {"dateTime": "2026-06-20T11:00:00Z"}
    assert body["end"] == {"dateTime": "2026-06-20T12:00:00Z"}


def test_update_event_rejects_empty_patch():
    with pytest.raises(ValueError, match="no fields to update"):
        update_event(MagicMock(), "e1")


def test_update_event_rejects_blank_id():
    with pytest.raises(ValueError, match="event_id cannot be empty"):
        update_event(MagicMock(), "", summary="x")


def test_update_event_envelope(with_calendar_stub):
    result = update_event(MagicMock(), "e1", summary="Renamed")
    assert result == {
        "calendar_id": "primary",
        "event_id": "e1",
        "html_link": "https://calendar.google.com/event?eid=e1",
        "summary": "Renamed",
    }


# ---------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------


def test_delete_event_passes_ids_and_send_updates(with_calendar_stub):
    delete_event(MagicMock(), "e1", calendar_id="cal-x", send_updates="all")
    kw = _last_real_kwargs(with_calendar_stub.events().delete)
    assert kw["calendarId"] == "cal-x"
    assert kw["eventId"] == "e1"
    assert kw["sendUpdates"] == "all"


def test_delete_event_envelope(with_calendar_stub):
    result = delete_event(MagicMock(), "e1")
    assert result == {"calendar_id": "primary", "deleted_event_id": "e1"}


def test_delete_event_rejects_blank_id():
    with pytest.raises(ValueError, match="event_id cannot be empty"):
        delete_event(MagicMock(), "")


# ---------------------------------------------------------------------
# list_calendars — flattening + envelope + validation
# ---------------------------------------------------------------------


def test_list_calendars_flattens_entries(with_calendar_stub):
    result = list_calendars(MagicMock())
    assert result["calendars"] == [
        {"id": "primary", "summary": "Me", "primary": True,
         "access_role": "owner"},
        {"id": "team@group.calendar.google.com", "summary": "Team",
         "primary": False, "access_role": "writer"},
    ]
    assert result["next_page_token"] is None


def test_list_calendars_rejects_bad_max_results():
    with pytest.raises(ValueError, match="max_results must be between 1 and 250"):
        list_calendars(MagicMock(), max_results=0)


# ---------------------------------------------------------------------
# freebusy — body shape + default-primary + envelope + validation
# ---------------------------------------------------------------------


def test_freebusy_builds_body_with_explicit_calendars(with_calendar_stub):
    freebusy(
        MagicMock(),
        time_min="2026-06-20T00:00:00Z",
        time_max="2026-06-21T00:00:00Z",
        calendar_ids=["primary", "team@group.calendar.google.com"],
    )
    body = with_calendar_stub.freebusy().query.call_args_list[-1].kwargs["body"]
    assert body["timeMin"] == "2026-06-20T00:00:00Z"
    assert body["timeMax"] == "2026-06-21T00:00:00Z"
    assert body["items"] == [
        {"id": "primary"},
        {"id": "team@group.calendar.google.com"},
    ]


def test_freebusy_defaults_to_primary_when_no_ids(with_calendar_stub):
    freebusy(
        MagicMock(),
        time_min="2026-06-20T00:00:00Z", time_max="2026-06-21T00:00:00Z",
    )
    body = with_calendar_stub.freebusy().query.call_args_list[-1].kwargs["body"]
    assert body["items"] == [{"id": "primary"}]


def test_freebusy_envelope(with_calendar_stub):
    result = freebusy(
        MagicMock(),
        time_min="2026-06-20T00:00:00Z", time_max="2026-06-21T00:00:00Z",
    )
    assert result == {
        "time_min": "2026-06-20T00:00:00Z",
        "time_max": "2026-06-21T00:00:00Z",
        "calendars": {"primary": {"busy": [
            {"start": "2026-06-20T14:00:00Z", "end": "2026-06-20T15:00:00Z"},
        ]}},
    }


@pytest.mark.parametrize("field,kwargs", [
    ("time_min", {"time_min": "", "time_max": "2026-06-21T00:00:00Z"}),
    ("time_max", {"time_min": "2026-06-20T00:00:00Z", "time_max": "  "}),
])
def test_freebusy_rejects_blank_window(field, kwargs):
    with pytest.raises(ValueError, match=f"{field} cannot be empty"):
        freebusy(MagicMock(), **kwargs)
