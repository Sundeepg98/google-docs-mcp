"""S2.3 regression (2026-07-10): the post-first-tab-delete Google 500.

Live-reproduced defect (see the classifier's comment block in
``services/docs/api.py``): once a document's original first tab
(id ``"t.0"``) is deleted, Google answers EVERY
``updateDocumentTabProperties`` batchUpdate on that document with a
deterministic 500 — icons and renames alike, forever. The old behavior
surfaced that as a generic "transient 500, often resolves on retry"
message, which provably wasted callers' retries (operator hit it 3/3).

The fix cannot make Google succeed; it reclassifies the failure. When
the batchUpdate 500s AND the (freshly fetched) root tab list carries no
``t.0``, ``set_tab_icons`` / ``rename_tab`` raise a diagnosed,
non-retryable error naming the defect and the workarounds. A 500 with
``t.0`` still present is NOT the defect and keeps the generic
(retryable) envelope.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.docs.api import (
    _classify_tab_props_500,
    rename_tab,
    set_tab_icons,
)


class _FakeResp(dict):
    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.reason = "Internal error encountered."


def _http_error(status: int) -> HttpError:
    return HttpError(resp=_FakeResp(status), content=b"Internal error encountered.")


def _root_tab(tab_id: str, title: str = "T") -> dict:
    return {
        "tabProperties": {"tabId": tab_id, "title": title},
        "documentTab": {"body": {"content": []}},
    }


# ---------------------------------------------------------------------
# The classifier itself
# ---------------------------------------------------------------------


def test_classifier_diagnoses_500_when_original_first_tab_is_gone():
    msg = _classify_tab_props_500(
        [_root_tab("t.abc"), _root_tab("t.def")], _http_error(500)
    )
    assert msg is not None
    assert "first_tab_deleted_500" in msg
    assert "Retryable: false" in msg
    # Callers must not chase adding-a-tab as a cure: live evidence shows
    # addDocumentTab succeeds on a poisoned doc WITHOUT clearing the
    # defect, so the diagnosis says so explicitly.
    assert "does NOT clear" in msg


def test_classifier_passes_500_through_when_t0_still_present():
    """A 500 on a doc that still has t.0 is NOT the known defect —
    fall back to the generic (retryable) handling."""
    assert (
        _classify_tab_props_500(
            [_root_tab("t.0"), _root_tab("t.abc")], _http_error(500)
        )
        is None
    )


@pytest.mark.parametrize("status", [400, 403, 429, 503])
def test_classifier_ignores_non_500_statuses(status):
    assert _classify_tab_props_500([_root_tab("t.abc")], _http_error(status)) is None


# ---------------------------------------------------------------------
# set_tab_icons wiring — uses its pre-fetched tab list
# ---------------------------------------------------------------------


def _docs_stub(tabs: list[dict]) -> MagicMock:
    docs = MagicMock(name="docs-v1-stub")
    docs.documents().get().execute.return_value = {
        "documentId": "DOC1",
        "tabs": tabs,
    }
    return docs


def test_set_tab_icons_500_on_poisoned_doc_raises_diagnosed_error():
    docs = _docs_stub([_root_tab("t.abc", "Networking")])
    docs.documents().batchUpdate().execute.side_effect = _http_error(500)
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        with pytest.raises(ValueError, match="first_tab_deleted_500"):
            set_tab_icons(
                MagicMock(), "DOC1", {"Networking": "\N{GLOBE WITH MERIDIANS}"}
            )


def test_set_tab_icons_500_with_t0_present_propagates_http_error():
    docs = _docs_stub(
        [_root_tab("t.0", "Tab 1"), _root_tab("t.abc", "Networking")]
    )
    docs.documents().batchUpdate().execute.side_effect = _http_error(500)
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        with pytest.raises(HttpError):
            set_tab_icons(
                MagicMock(), "DOC1", {"Networking": "\N{GLOBE WITH MERIDIANS}"}
            )


def test_set_tab_icons_tool_surfaces_diagnosis_as_tool_error():
    """Through the tool layer the diagnosis arrives as a ToolError (the
    tool body maps the api's ValueError), not a masked internal error."""
    from fastmcp.exceptions import ToolError

    from appscriptly import decorators
    from appscriptly.services.docs import tools

    docs = _docs_stub([_root_tab("t.abc", "Networking")])
    docs.documents().batchUpdate().execute.side_effect = _http_error(500)
    original = decorators._get_credentials_fn
    decorators._get_credentials_fn = lambda: MagicMock(name="creds")
    try:
        with with_google_api_client(
            InMemoryGoogleAPIClient({("docs", "v1"): docs})
        ):
            with pytest.raises(ToolError, match="first_tab_deleted_500"):
                tools.gdocs_set_tab_icons(
                    doc_id="DOC1",
                    icons_by_title={"Networking": "\N{GLOBE WITH MERIDIANS}"},
                )
    finally:
        decorators._get_credentials_fn = original


# ---------------------------------------------------------------------
# rename_tab wiring — fetches the tab list on the error path only
# ---------------------------------------------------------------------


def test_rename_tab_500_on_poisoned_doc_raises_diagnosed_error():
    """rename_tab shares the defect (live-proven with a title-only
    update); on a 500 it fetches root tab ids and runs the classifier."""
    docs = _docs_stub([_root_tab("t.abc", "Networking")])
    docs.documents().batchUpdate().execute.side_effect = _http_error(500)
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        with pytest.raises(ValueError, match="first_tab_deleted_500"):
            rename_tab(MagicMock(), "DOC1", "t.abc", title="Networks")


def test_rename_tab_500_with_t0_present_propagates_http_error():
    docs = _docs_stub([_root_tab("t.0", "Tab 1"), _root_tab("t.abc")])
    docs.documents().batchUpdate().execute.side_effect = _http_error(500)
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        with pytest.raises(HttpError):
            rename_tab(MagicMock(), "DOC1", "t.abc", title="Networks")


def test_rename_tab_non_500_does_not_classify_and_propagates():
    """A 4xx propagates untouched — no classification fetch happens."""
    docs = _docs_stub([_root_tab("t.abc")])
    docs.documents().batchUpdate().execute.side_effect = _http_error(400)
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs})
    ):
        with pytest.raises(HttpError):
            rename_tab(MagicMock(), "DOC1", "t.abc", title="X")
    # The only get() traffic allowed is none at all (400 path).
    assert not docs.documents().get().execute.called
