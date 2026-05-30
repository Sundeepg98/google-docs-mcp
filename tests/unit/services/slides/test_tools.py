"""Per-tool behavior tests for services/slides/tools.py (v2.3.2).

Mirrors ``tests/unit/services/sheets/test_tools.py`` (PR #119) exactly:
canonical per-tool happy-path coverage at the decorator-envelope
boundary, using the same ``InMemoryGoogleAPIClient`` + monkeypatched
``_get_credentials_fn`` fixture pattern.

The 3 slides tools (v2.3.2 minimal start):

  1. gslides_get_outline         — presentations.get
  2. gslides_replace_all_text    — batchUpdate (replaceAllText)
  3. gslides_create_presentation — presentations.create

Per-tool API-shape coverage (body shapes, ``matchCase`` plumbing,
response envelope mapping) lives in ``test_api.py``; this file
covers the tool-layer envelope: decorator's ``_get_credentials_fn``
injection, ``@workspace_tool(creds=True)`` wrapping, parameter
forwarding from the decorated function into the api module.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.slides import tools


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True) envelope doesn't try real OAuth.
    Sister to the same fixture in tests/unit/services/sheets/test_tools.py."""
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(tools, "_get_credentials", lambda: stub_creds)


@pytest.fixture
def slides_stub():
    """A Slides v1 Resource stub with all three method chains pre-wired
    to return plausible default responses. Individual tests override
    per-call as needed."""
    slides = MagicMock(name="slides-v1-stub")
    slides.presentations().get().execute.return_value = {
        "presentationId": "D1",
        "title": "Deck",
        "slides": [],
    }
    slides.presentations().batchUpdate().execute.return_value = {
        "presentationId": "D1",
        "replies": [{"replaceAllText": {"occurrencesChanged": 0}}],
    }
    slides.presentations().create().execute.return_value = {
        "presentationId": "NEW-1",
        "title": "T",
    }
    return slides


@pytest.fixture
def with_slides_stub(slides_stub):
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("slides", "v1"): slides_stub,
    })):
        yield slides_stub


# ---------------------------------------------------------------------
# 1. gslides_get_outline — happy path through the decorator envelope
# ---------------------------------------------------------------------


def test_gslides_get_outline_returns_envelope_for_empty_deck(with_slides_stub):
    """Default Slides stub returns an empty slides list; tool
    surfaces the ``{presentation_id, title, url, slides: []}``
    envelope through the standard ``@workspace_tool(creds=True)``
    boundary."""
    result = tools.gslides_get_outline(presentation_id="DECK1")
    assert result == {
        "presentation_id": "DECK1",
        "title": "Deck",
        "url": "https://docs.google.com/presentation/d/DECK1/edit",
        "slides": [],
    }


def test_gslides_get_outline_surfaces_slides_when_present(with_slides_stub):
    """When Slides returns slides with text shapes, the tool
    flattens text via ``_extract_slide_text`` and exposes the
    per-slide envelope to the agent."""
    with_slides_stub.presentations().get().execute.return_value = {
        "presentationId": "DECK1",
        "title": "Forecast",
        "slides": [
            {
                "objectId": "S001",
                "slideProperties": {"layoutObjectId": "L_TITLE"},
                "pageElements": [
                    {"shape": {"text": {"textElements": [
                        {"textRun": {"content": "Hello"}},
                    ]}}},
                ],
            },
        ],
    }
    result = tools.gslides_get_outline(presentation_id="DECK1")
    assert len(result["slides"]) == 1
    assert result["slides"][0]["object_id"] == "S001"
    assert result["slides"][0]["text"] == "Hello"


# ---------------------------------------------------------------------
# 2. gslides_replace_all_text — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_replace_all_text_happy_path(with_slides_stub):
    """Standard replace returns the ``{presentation_id,
    occurrences_changed}`` envelope. Tool layer pass-through of
    api function."""
    with_slides_stub.presentations().batchUpdate().execute.return_value = {
        "presentationId": "DECK1",
        "replies": [{"replaceAllText": {"occurrencesChanged": 7}}],
    }
    result = tools.gslides_replace_all_text(
        presentation_id="DECK1",
        find_text="{{Name}}",
        replace_text="Acme Corp",
    )
    assert result == {"presentation_id": "DECK1", "occurrences_changed": 7}


def test_gslides_replace_all_text_validation_propagates_through_tool(
    with_slides_stub,
):
    """Pre-API validation (empty find_text) bubbles from the api
    module through the decorator envelope as ValueError. The
    decorator wraps it for cloud-mode callers, but raises the bare
    ValueError in test contexts."""
    with pytest.raises(ValueError, match="find_text cannot be empty"):
        tools.gslides_replace_all_text(
            presentation_id="DECK1",
            find_text="",
            replace_text="x",
        )


def test_gslides_replace_all_text_default_match_case_true(with_slides_stub):
    """Verify that the default ``match_case=True`` reaches the
    Slides API as ``matchCase=True``."""
    tools.gslides_replace_all_text(
        presentation_id="DECK1",
        find_text="foo",
        replace_text="bar",
    )
    last_call = with_slides_stub.presentations().batchUpdate.call_args_list[-1]
    request = last_call.kwargs["body"]["requests"][0]
    assert request["replaceAllText"]["containsText"]["matchCase"] is True


# ---------------------------------------------------------------------
# 3. gslides_create_presentation — happy path + validation
# ---------------------------------------------------------------------


def test_gslides_create_presentation_happy_path(with_slides_stub):
    """Create returns the flat ``{presentation_id, url, title}``
    envelope ready for piping into get_outline / replace_all_text."""
    with_slides_stub.presentations().create().execute.return_value = {
        "presentationId": "DECK-NEW",
        "title": "Q3 Plan",
    }
    result = tools.gslides_create_presentation(title="Q3 Plan")
    assert result == {
        "presentation_id": "DECK-NEW",
        "url": "https://docs.google.com/presentation/d/DECK-NEW/edit",
        "title": "Q3 Plan",
    }


def test_gslides_create_presentation_rejects_blank_title(with_slides_stub):
    """Blank-title rejection from the api module bubbles up cleanly."""
    with pytest.raises(ValueError, match="title cannot be empty"):
        tools.gslides_create_presentation(title="   ")


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: _get_credentials_fn is invoked
# ---------------------------------------------------------------------


def test_gslides_get_outline_invokes_get_credentials_fn(
    with_slides_stub, monkeypatch,
):
    """Canary identical to the sheets test_tools.py pattern: the
    @workspace_tool(creds=True) decorator MUST call
    _get_credentials_fn before delegating to the body."""
    call_count = {"n": 0}

    def counting_creds_fn():
        call_count["n"] += 1
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(
        decorators, "_get_credentials_fn", counting_creds_fn
    )
    tools.gslides_get_outline(presentation_id="DECK1")
    assert call_count["n"] == 1, (
        "_get_credentials_fn was not called exactly once — the "
        "decorator envelope may have changed or the fixture missed."
    )
