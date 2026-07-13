"""Tests for services/apps_script/doc_menu.py (PR-Δ8).

``as_install_doc_menu`` is the first use-case tool COMPOSING the PR-Δ7
bound-script primitive. Coverage splits into:

  * **Pure script generation** (``build_menu_script``) — the onOpen
    menu-builder + per-item handler functions are emitted correctly and
    safely (labels escaped, function names verbatim).
  * **Manifest scope derivation** — reusing #138's ``build_manifest``
    with a ``menu`` key derives the ``script.container.ui`` scope.
  * **Validation** — empty title / no items / malformed items raise
    ValueError before any API call.
  * **Tool happy-path** — end-to-end at the ``@workspace_tool(creds=True,
    scopes=...)`` boundary via ``InMemoryGoogleAPIClient``.

Fixture pattern copied from ``tests/unit/services/apps_script/
test_tools.py``: because the tool DECLARES ``scopes=GAS_BOUND_SCOPES``,
the decorator takes the SCOPE-AWARE credential path
(``auth.load_credentials`` in stdio test mode), so the fixture patches
``auth.load_credentials`` (NOT just ``_get_credentials_fn``).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from appscriptly import decorators
from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.apps_script import doc_menu
from appscriptly.services.apps_script.doc_menu import build_menu_script


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=...) envelope doesn't launch real
    OAuth. This tool declares scopes, so resolution flows through the
    scope-aware path (``auth.load_credentials(..., extra_scopes=scopes)``
    in stdio test mode) — patch THAT, plus the no-scope fallbacks for
    belt-and-suspenders (same as test_tools.py)."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """Apps Script v1 stub: create / updateContent / versions /
    deployments pre-wired to plausible defaults."""
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "DOC1",
    }
    script.projects().updateContent().execute.return_value = {}
    script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEPLOY-1",
    }
    return script


@pytest.fixture
def with_script_client():
    """Apps Script stub wired for the full create→push→deploy flow.

    NOTE: as_install_doc_menu knows the container is a Doc (a
    DocumentApp menu is Docs-specific), so it does NOT auto-detect the
    container kind — no Drive stub is needed (contrast with
    as_generate_bound_script's with_docs_container fixture)."""
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        yield script


# A reusable, valid items list for happy-path tests.
_ITEMS = [
    {
        "label": "Insert signature",
        "function_name": "insertSignature",
        "function_body": (
            "DocumentApp.getActiveDocument().getBody()"
            ".appendParagraph('Signed');"
        ),
    },
    {
        "label": "Clear doc",
        "function_name": "clearDoc",
        "function_body": "DocumentApp.getActiveDocument().getBody().clear();",
    },
]


# ---------------------------------------------------------------------
# Pure script generation — build_menu_script
# ---------------------------------------------------------------------


def test_script_has_onopen_that_builds_the_named_menu():
    """The generated body defines onOpen and creates the named menu via
    DocumentApp.getUi().createMenu(...).addToUi()."""
    src = build_menu_script("My Tools", _ITEMS)
    assert "function onOpen(e) {" in src
    assert "DocumentApp.getUi()" in src
    assert 'createMenu("My Tools")' in src
    assert ".addToUi();" in src


def test_script_adds_one_addItem_per_menu_item():
    """Each item becomes an .addItem(label, function_name) on the menu."""
    src = build_menu_script("My Tools", _ITEMS)
    assert '.addItem("Insert signature", "insertSignature")' in src
    assert '.addItem("Clear doc", "clearDoc")' in src
    assert src.count(".addItem(") == len(_ITEMS)


def test_script_emits_a_handler_function_per_item_with_its_body():
    """Each item's function_name + function_body becomes a top-level
    handler function in the generated source."""
    src = build_menu_script("My Tools", _ITEMS)
    assert "function insertSignature() {" in src
    assert "function clearDoc() {" in src
    # The caller-authored bodies are present verbatim (modulo indent).
    assert "appendParagraph('Signed');" in src
    assert "getBody().clear();" in src


def test_script_escapes_labels_to_prevent_injection():
    """A label containing quotes / special chars is emitted as a safe JS
    string literal — it can't break out of the createMenu/addItem call."""
    items = [{
        "label": 'Say "hi" \\ now',
        "function_name": "sayHi",
        "function_body": "Logger.log('hi');",
    }]
    src = build_menu_script('Menu "X"', items)
    # json.dumps escaping: embedded quotes become \" and backslash \\.
    assert r'.addItem("Say \"hi\" \\ now", "sayHi")' in src
    assert r'createMenu("Menu \"X\"")' in src


def test_script_is_deterministic():
    """Same input → byte-identical output (pure function)."""
    assert build_menu_script("T", _ITEMS) == build_menu_script("T", _ITEMS)


def test_script_handles_empty_function_body():
    """An empty function_body yields a valid (no-op) handler function."""
    items = [{
        "label": "Noop", "function_name": "noop", "function_body": "",
    }]
    src = build_menu_script("T", items)
    assert "function noop() {" in src


# ---------------------------------------------------------------------
# Validation errors (raised before any API call)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_title", ["", "   ", "\t\n"])
def test_empty_menu_title_rejected(with_script_client, bad_title):
    """A blank menu_title is rejected client-side."""
    with pytest.raises(ValueError, match="menu_title cannot be empty"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title=bad_title, items=_ITEMS,
        )


def test_empty_items_list_rejected(with_script_client):
    """An empty items list is rejected — a menu needs ≥1 item."""
    with pytest.raises(ValueError, match="at least one menu item"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools", items=[],
        )


def test_item_missing_label_rejected(with_script_client):
    """An item without a label is rejected, naming the index."""
    with pytest.raises(ValueError, match=r"items\[0\] is missing a non-empty string 'label'"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools",
            items=[{"function_name": "f", "function_body": "x();"}],
        )


def test_item_missing_function_name_rejected(with_script_client):
    """An item without a function_name is rejected."""
    with pytest.raises(ValueError, match="function_name"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools",
            items=[{"label": "Go", "function_body": "x();"}],
        )


def test_item_invalid_function_name_rejected(with_script_client):
    """A function_name that isn't a valid JS identifier is rejected
    (prevents injecting code through the addItem call)."""
    with pytest.raises(ValueError, match="not a valid Apps Script function identifier"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools",
            items=[{
                "label": "Go", "function_name": "bad name();",
                "function_body": "x();",
            }],
        )


def test_item_reserved_function_name_rejected(with_script_client):
    """A handler named onOpen (owned by the generated builder) is
    rejected so it can't shadow the menu builder."""
    with pytest.raises(ValueError, match="reserved Apps Script trigger name"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools",
            items=[{
                "label": "Go", "function_name": "onOpen",
                "function_body": "x();",
            }],
        )


def test_duplicate_function_names_rejected(with_script_client):
    """Two items mapping to the same function_name collide in the
    generated .gs — rejected."""
    with pytest.raises(ValueError, match="duplicated"):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="Tools",
            items=[
                {"label": "A", "function_name": "run", "function_body": "a();"},
                {"label": "B", "function_name": "run", "function_body": "b();"},
            ],
        )


def test_validation_failure_makes_no_api_call(with_script_client):
    """A client-side validation error must fire BEFORE any project is
    created — no orphaned Apps Script project on bad input."""
    with pytest.raises(ValueError):
        doc_menu.as_install_doc_menu(
            doc_id="DOC1", menu_title="", items=_ITEMS,
        )
    create_body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert not create_body_calls


# ---------------------------------------------------------------------
# Tool happy-path — end-to-end via the decorator boundary
# ---------------------------------------------------------------------


def test_happy_path_returns_envelope(with_script_client):
    """create → push → deploy → the documented return envelope."""
    result = doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Contract Tools", items=_ITEMS,
    )
    assert result == {
        "script_id": "SCRIPT-1",
        "deployment_id": "DEPLOY-1",
        "on_conflict": "new",
        "reused_existing": False,
        "replaced_count": 0,
        "doc_id": "DOC1",
        "menu_title": "Contract Tools",
        "item_count": 2,
        "project_url": "https://script.google.com/d/SCRIPT-1/edit",
    }


def test_binds_via_parent_id_to_the_doc(with_script_client):
    """The create call must pass parentId=doc_id — the BINDING that
    attaches the menu script to THIS Doc."""
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "DOC1"


def test_pushes_generated_menu_script_as_server_js(with_script_client):
    """The generated onOpen + handlers reach updateContent as a
    SERVER_JS file."""
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files, "no SERVER_JS file pushed"
    src = code_files[-1]["source"]
    assert "function onOpen(e) {" in src
    assert "function insertSignature() {" in src


def test_manifest_declares_ui_scope(with_script_client):
    """A menu requires script.container.ui — build_manifest must derive
    it and it must reach the pushed appsscript.json. The internal
    __plan__ echo must be stripped from the real manifest."""
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert (
        "https://www.googleapis.com/auth/script.container.ui"
        in parsed["oauthScopes"]
    )
    assert "__plan__" not in parsed


def test_uses_custom_name_when_given(with_script_client):
    """A supplied name becomes the project title on create."""
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Tools", items=_ITEMS,
        name="My Menu Project",
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "My Menu Project"


def test_default_name_includes_menu_title(with_script_client):
    """Without a name, the default project title references the menu."""
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Contract Tools", items=_ITEMS,
    )
    body_calls = [
        c for c in with_script_client.projects().create.call_args_list
        if "body" in c.kwargs
    ]
    assert "Contract Tools" in body_calls[-1].kwargs["body"]["title"]


# ---------------------------------------------------------------------
# Error path — API HttpError → ToolError via the decorator envelope
# ---------------------------------------------------------------------


def test_api_httperror_maps_to_tool_error():
    """An Apps Script HttpError on create → the @workspace_tool envelope
    translates it to ToolError (standard creds=True behavior)."""
    script = MagicMock(name="script-v1-stub-erroring")
    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            doc_menu.as_install_doc_menu(
                doc_id="DOC1", menu_title="Tools", items=_ITEMS,
            )


# ---------------------------------------------------------------------
# Decorator-envelope cross-check: scope-aware creds resolution fires
# ---------------------------------------------------------------------


def test_resolves_creds_via_scope_aware_path(with_script_client, monkeypatch):
    """Canary: because this tool DECLARES scopes, creds resolution flows
    through the scope-aware path (auth.load_credentials in stdio test
    mode) with the tool's scopes passed as extra_scopes — exactly once."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    doc_menu.as_install_doc_menu(
        doc_id="DOC1", menu_title="Tools", items=_ITEMS,
    )

    assert len(calls) == 1, (
        "auth.load_credentials was not called exactly once — the "
        "scope-aware decorator path may have changed or the fixture missed."
    )
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
