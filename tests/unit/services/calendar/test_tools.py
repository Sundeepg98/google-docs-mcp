"""Per-tool behavior tests for services/calendar/tools.py (v2.4.0).

Mirrors ``tests/unit/services/sheets/test_tools.py``: the canonical
per-tool happy-path coverage at the decorator-envelope boundary, using
the same ``InMemoryGoogleAPIClient`` + monkeypatched
``_get_credentials_fn`` fixture pattern.

Per-tool API-shape coverage (EventDateTime branch, body shape,
sendUpdates, freebusy body, response envelopes) lives in ``test_api.py``;
this file covers the tool-layer envelope: the decorator's
``_get_credentials_fn`` injection, ``@workspace_tool(creds=True,
scopes=[CALENDAR_SCOPE])`` wrapping, and parameter forwarding from the
decorated function into the api module.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.calendar import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.

    Calendar tools declare ``scopes=[CALENDAR_SCOPE]``. In stdio test
    context ``current_user_id_or_none()`` is None, so the decorator's
    scope branch takes the ``load_credentials(..., extra_scopes=...)``
    path. We monkeypatch ``auth.load_credentials`` (imported lazily inside
    the decorator) to return the stub so no real OAuth / token file is
    touched — while still exercising the scoped-resolution code path."""
    import appscriptly.auth as auth_mod

    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(
        auth_mod, "load_credentials",
        lambda *a, **k: stub_creds,
    )


@pytest.fixture
def calendar_stub():
    """A Calendar v3 Resource stub with every method chain pre-wired."""
    cal = MagicMock(name="calendar-v3-stub")
    cal.events().list().execute.return_value = {"items": [], "nextPageToken": None}
    cal.events().get().execute.return_value = {"id": "e1", "summary": "Sync"}
    cal.events().insert().execute.return_value = {
        "id": "NEW-1",
        "htmlLink": "https://calendar.google.com/event?eid=NEW-1",
        "summary": "Sync",
    }
    cal.events().patch().execute.return_value = {
        "id": "e1", "htmlLink": "https://calendar.google.com/event?eid=e1",
        "summary": "Renamed",
    }
    cal.events().delete().execute.return_value = ""
    cal.calendarList().list().execute.return_value = {
        "items": [{"id": "primary", "summary": "Me", "primary": True,
                   "accessRole": "owner"}],
        "nextPageToken": None,
    }
    cal.freebusy().query().execute.return_value = {
        "calendars": {"primary": {"busy": []}},
    }
    return cal


@pytest.fixture
def with_calendar_stub(calendar_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("calendar", "v3"): calendar_stub,
    })):
        yield calendar_stub


# ---------------------------------------------------------------------
# 1. gcal_list_events
# ---------------------------------------------------------------------


def test_gcal_list_events_envelope(with_calendar_stub):
    result = tools.gcal_list_events()
    assert result == {"calendar_id": "primary", "events": [], "next_page_token": None}


def test_gcal_list_events_forwards_args(with_calendar_stub):
    tools.gcal_list_events(calendar_id="cal-x", query="standup")
    real = [
        c for c in with_calendar_stub.events().list.call_args_list
        if "calendarId" in c.kwargs
    ]
    assert real and real[-1].kwargs["calendarId"] == "cal-x"
    assert real[-1].kwargs["q"] == "standup"


# ---------------------------------------------------------------------
# 2. gcal_get_event
# ---------------------------------------------------------------------


def test_gcal_get_event_envelope(with_calendar_stub):
    result = tools.gcal_get_event(event_id="e1")
    assert result == {"calendar_id": "primary", "event": {"id": "e1", "summary": "Sync"}}


# ---------------------------------------------------------------------
# 3. gcal_create_event
# ---------------------------------------------------------------------


def test_gcal_create_event_happy_path(with_calendar_stub):
    result = tools.gcal_create_event(
        summary="Sync",
        start="2026-06-20T09:00:00Z",
        end="2026-06-20T09:30:00Z",
    )
    assert result == {
        "calendar_id": "primary",
        "event_id": "NEW-1",
        "html_link": "https://calendar.google.com/event?eid=NEW-1",
        "summary": "Sync",
    }


def test_gcal_create_event_validation_propagates(with_calendar_stub):
    with pytest.raises(ValueError, match="summary cannot be empty"):
        tools.gcal_create_event(
            summary="", start="2026-06-20T09:00:00Z", end="2026-06-20T10:00:00Z",
        )


def test_gcal_create_event_forwards_attendees(with_calendar_stub):
    tools.gcal_create_event(
        summary="Review",
        start="2026-06-20T09:00:00Z",
        end="2026-06-20T10:00:00Z",
        attendees=["a@x.com"],
    )
    real = [
        c for c in with_calendar_stub.events().insert.call_args_list
        if "calendarId" in c.kwargs
    ]
    assert real[-1].kwargs["body"]["attendees"] == [{"email": "a@x.com"}]


# ---------------------------------------------------------------------
# 4. gcal_update_event
# ---------------------------------------------------------------------


def test_gcal_update_event_happy_path(with_calendar_stub):
    result = tools.gcal_update_event(event_id="e1", summary="Renamed")
    assert result["event_id"] == "e1"
    assert result["summary"] == "Renamed"


def test_gcal_update_event_empty_patch_raises(with_calendar_stub):
    with pytest.raises(ValueError, match="no fields to update"):
        tools.gcal_update_event(event_id="e1")


# ---------------------------------------------------------------------
# 5. gcal_delete_event
# ---------------------------------------------------------------------


def test_gcal_delete_event_envelope(with_calendar_stub):
    result = tools.gcal_delete_event(event_id="e1")
    assert result == {"calendar_id": "primary", "deleted_event_id": "e1"}


# ---------------------------------------------------------------------
# 6. gcal_list_calendars
# ---------------------------------------------------------------------


def test_gcal_list_calendars_envelope(with_calendar_stub):
    result = tools.gcal_list_calendars()
    assert result["calendars"] == [
        {"id": "primary", "summary": "Me", "primary": True,
         "access_role": "owner"},
    ]
    assert result["next_page_token"] is None


# ---------------------------------------------------------------------
# 7. gcal_freebusy
# ---------------------------------------------------------------------


def test_gcal_freebusy_envelope(with_calendar_stub):
    result = tools.gcal_freebusy(
        time_min="2026-06-20T00:00:00Z", time_max="2026-06-21T00:00:00Z",
    )
    assert result == {
        "time_min": "2026-06-20T00:00:00Z",
        "time_max": "2026-06-21T00:00:00Z",
        "calendars": {"primary": {"busy": []}},
    }


# ---------------------------------------------------------------------
# scope annotation — every calendar tool declares the calendar scope
# ---------------------------------------------------------------------


def test_calendar_scope_constant_is_sensitive_full_calendar():
    """CALENDAR_SCOPE is the full read/write /auth/calendar scope —
    SENSITIVE (not restricted → no CASA). Pinned so a future narrowing /
    swap to a restricted scope is a deliberate, reviewed change."""
    assert tools.CALENDAR_SCOPE == "https://www.googleapis.com/auth/calendar"
