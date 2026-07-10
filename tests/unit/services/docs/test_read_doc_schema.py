"""BUG 3 / S2.1 regression (2026-07-10): gdocs_read_doc output schema.

The tool declares ``GDOCS_READ_DOC_OUTPUT_SCHEMA`` with ``doc_id``
REQUIRED, but the single-tab payload (``read_tab_content``) never
carried ``doc_id`` — so BOTH single-tab modes (``tab_id=`` and
``tab_title=``) failed FastMCP's output validation with
"'doc_id' is a required property" (4/4 deterministic in the field),
while the whole-doc mode passed. The existing per-tool tests called
the function directly and asserted individual keys, so the
schema-vs-payload mismatch was never exercised.

These tests validate the ACTUAL returned payload of every mode against
the DECLARED schema with ``jsonschema`` — the same check the MCP layer
applies — so a payload/schema drift in any mode fails here first.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import jsonschema
import pytest

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.docs import tools
from appscriptly.tool_schemas import GDOCS_READ_DOC_OUTPUT_SCHEMA


@pytest.fixture(autouse=True)
def inject_stub_creds(monkeypatch):
    monkeypatch.setattr(
        decorators, "_get_credentials_fn", lambda: MagicMock(name="creds")
    )


_DOC = {
    "documentId": "DOC1",
    "tabs": [
        {
            "tabProperties": {"tabId": "T0", "title": "Intro", "index": 0},
            "documentTab": {
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "paragraphStyle": {
                                    "namedStyleType": "NORMAL_TEXT"
                                },
                                "elements": [
                                    {"textRun": {"content": "hello\n"}}
                                ],
                            }
                        }
                    ]
                }
            },
        },
        {
            "tabProperties": {"tabId": "T1", "title": "Body", "index": 1},
            "documentTab": {"body": {"content": []}},
        },
    ],
}


@pytest.fixture
def with_doc_stubs():
    docs = MagicMock(name="docs-v1-stub")
    docs.documents().get().execute.return_value = _DOC
    drive = MagicMock(name="drive-v3-stub")
    drive.files().get().execute.return_value = {"trashed": False}
    with with_google_api_client(
        InMemoryGoogleAPIClient({("docs", "v1"): docs, ("drive", "v3"): drive})
    ):
        yield docs


def test_whole_doc_mode_satisfies_declared_schema(with_doc_stubs):
    result = tools.gdocs_read_doc(doc_id="DOC1")
    jsonschema.validate(result, GDOCS_READ_DOC_OUTPUT_SCHEMA)
    assert result["doc_id"] == "DOC1"


def test_single_tab_by_id_satisfies_declared_schema(with_doc_stubs):
    """The regression: tab_id mode must echo doc_id (schema-required)."""
    result = tools.gdocs_read_doc(doc_id="DOC1", tab_id="T0")
    jsonschema.validate(result, GDOCS_READ_DOC_OUTPUT_SCHEMA)
    assert result["doc_id"] == "DOC1"
    assert result["tab_id"] == "T0"


def test_single_tab_by_title_satisfies_declared_schema(with_doc_stubs):
    """The second single-tab branch (tab_title) — same schema contract."""
    result = tools.gdocs_read_doc(doc_id="DOC1", tab_title="Body")
    jsonschema.validate(result, GDOCS_READ_DOC_OUTPUT_SCHEMA)
    assert result["doc_id"] == "DOC1"
    assert result["tab_id"] == "T1"
