"""Tests for services/apps_script/custom_function.py (PR-Δ10).

``as_install_custom_function`` is a convenience tool that COMPOSES the
PR-Δ7 bound-script generator primitive to install a custom spreadsheet
function (``=FUNCTION_NAME(...)``) into a Google Sheet. These tests cover
the two layers the tool adds on top of the reused #138 machinery:

1. **Pure shaping/validation** (``build_custom_function_script`` +
   helpers) — the ``@customfunction`` tag is present (and not
   double-added when the caller already wrote one), the description is
   woven into the generated JSDoc, ``function_name`` is validated as a
   JS identifier (rejecting reserved words / illegal chars / empty), and
   ``function_body`` is validated to actually define ``function_name``
   (across declaration / function-expression / arrow forms).

2. **Tool-layer orchestration** — happy path through the
   ``@workspace_tool(creds=True, scopes=...)`` decorator boundary via the
   ``InMemoryGoogleAPIClient`` + monkeypatched scope-aware creds fixture
   (same pattern as ``test_tools.py``), plus the manifest-has-no-extra-
   scope invariant, the non-Sheet rejection, the API-HttpError → ToolError
   mapping, and the scope-aware creds-resolution canary.

IMPORTANT — this tool declares ``scopes=GAS_BOUND_SCOPES``, so its
decorator takes the SCOPE-AWARE resolution path (stdio mode calls
``auth.load_credentials(..., extra_scopes=scopes)``). The fixture patches
``auth.load_credentials`` (the deferred-import target inside the
decorator), NOT ``_get_credentials_fn`` — exactly as #138's test_tools.py
documents.
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
from appscriptly.services.apps_script import custom_function as cf

_UI_SCOPE = "https://www.googleapis.com/auth/script.container.ui"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"


# =====================================================================
# Pure layer: build_custom_function_script + validation helpers
# =====================================================================


def test_build_script_prepends_customfunction_tag():
    """A body with no JSDoc gets a @customfunction tag prepended so Sheets
    recognizes it as a cell-callable function."""
    body = "function MY_FUNC(x) { return x * 2; }"
    out = cf.build_custom_function_script("MY_FUNC", body)
    assert "@customfunction" in out
    # The original body is preserved verbatim after the prepended JSDoc.
    assert body in out
    # The tag precedes the function definition.
    assert out.index("@customfunction") < out.index("function MY_FUNC")


def test_build_script_does_not_double_tag_when_already_present():
    """If the caller's body already carries a @customfunction JSDoc, the
    function returns it verbatim — no second tag, no rewrite."""
    body = (
        "/**\n * Doubles a number.\n * @customfunction\n */\n"
        "function MY_FUNC(x) { return x * 2; }"
    )
    out = cf.build_custom_function_script("MY_FUNC", body)
    assert out == body
    assert out.count("@customfunction") == 1


def test_build_script_weaves_description_into_jsdoc():
    """A supplied description shows up in the generated JSDoc (Sheets
    formula-help text)."""
    out = cf.build_custom_function_script(
        "SCORE", "function SCORE(t) { return t.length; }",
        description="Score text against the brand guide.",
    )
    assert "Score text against the brand guide." in out
    assert "@customfunction" in out


def test_build_script_description_cannot_break_out_of_jsdoc_comment():
    """SECURITY (code injection): a description containing ``*/`` must NOT
    close the generated JSDoc comment early — otherwise the text after it
    would deploy as LIVE Apps Script in the bound script. The ``*/`` is
    neutralized to ``* /`` and the injected `function` is rendered inert
    inside the comment block."""
    body = "function SCORE(t) { return t.length; }"
    malicious = "harmless */ function onOpen(){ stealData(); } /*"
    out = cf.build_custom_function_script("SCORE", body, description=malicious)

    # The generated JSDoc region is everything before the caller's body.
    jsdoc_region = out[: out.index(body)]
    # It must contain EXACTLY ONE `*/` — the JSDoc's own intended
    # terminator. The description's `*/` (which would have added a SECOND,
    # early terminator and escaped the comment) must be defanged.
    assert jsdoc_region.count("*/") == 1, (
        f"description broke out of the JSDoc comment — expected exactly one "
        f"(the terminator) `*/` in the comment region, got "
        f"{jsdoc_region.count('*/')}: {jsdoc_region!r}"
    )
    # …and that single `*/` is the LAST thing in the comment region (the
    # terminator), not an early break-out mid-description.
    assert jsdoc_region.rstrip().endswith("*/")
    # The injected payload text is still present (as inert comment text),
    # but defanged: the `*/` that would have escaped is now `* /`.
    assert "* /" in out
    # And the @customfunction tag + body remain intact.
    assert "@customfunction" in out
    assert body in out


def test_build_script_multiline_description_stays_inside_comment():
    """A multi-line description must stay inside the JSDoc block — every
    line is prefixed with `` * `` so a newline can't position text outside
    the comment. (Defense-in-depth alongside the ``*/`` escape.)"""
    body = "function F(x){ return x; }"
    out = cf.build_custom_function_script(
        "F", body, description="line one\nline two\nline three",
    )
    jsdoc_region = out[: out.index(body)]
    for fragment in ("line one", "line two", "line three"):
        assert f" * {fragment}" in jsdoc_region, (
            f"multi-line description line {fragment!r} not properly "
            f"prefixed inside the JSDoc: {jsdoc_region!r}"
        )
    # Only the JSDoc's own terminator `*/` — the benign multi-line text
    # introduced none of its own.
    assert jsdoc_region.count("*/") == 1
    assert jsdoc_region.rstrip().endswith("*/")


def test_build_script_rejects_invalid_identifier():
    """A function_name that isn't a valid JS identifier is rejected before
    any deploy — Sheets calls it as =NAME(...), so it must be legal."""
    with pytest.raises(ValueError, match="not a valid JavaScript identifier"):
        cf.build_custom_function_script(
            "my-func", "function x() {}"
        )


def test_build_script_rejects_identifier_starting_with_digit():
    with pytest.raises(ValueError, match="not a valid JavaScript identifier"):
        cf.build_custom_function_script("2FAST", "function x() {}")


def test_build_script_rejects_reserved_word():
    """A reserved JS word can't be a function name (syntax error in .gs)."""
    with pytest.raises(ValueError, match="reserved JavaScript word"):
        cf.build_custom_function_script("return", "function x() {}")


def test_build_script_rejects_empty_body():
    with pytest.raises(ValueError, match="function_body cannot be empty"):
        cf.build_custom_function_script("FOO", "   ")


def test_build_script_rejects_body_not_defining_named_function():
    """If function_body doesn't define a function matching function_name,
    the =NAME() cell could never resolve — reject early."""
    with pytest.raises(ValueError, match="does not define a function named"):
        cf.build_custom_function_script(
            "FOO", "function bar() { return 1; }"
        )


def test_build_script_accepts_arrow_function_form():
    """An arrow-function assignment matching the name is accepted (the
    body-defines-name check recognizes arrow forms, not just
    declarations)."""
    body = "const ADD = (a, b) => a + b;"
    out = cf.build_custom_function_script("ADD", body)
    assert "@customfunction" in out
    assert body in out


def test_build_script_accepts_function_expression_form():
    """A `NAME = function(...)` expression matching the name is accepted."""
    body = "var TRIPLE = function(x) { return x * 3; };"
    out = cf.build_custom_function_script("TRIPLE", body)
    assert "@customfunction" in out


def test_build_script_word_boundary_avoids_substring_false_match():
    """A body defining `computeFoo` must NOT satisfy a request for `Foo`
    (the \\b word boundary prevents a substring match)."""
    with pytest.raises(ValueError, match="does not define a function named"):
        cf.build_custom_function_script(
            "Foo", "function computeFoo() { return 1; }"
        )


# =====================================================================
# Tool layer: as_install_custom_function (decorator boundary)
# =====================================================================


@pytest.fixture
def stub_creds():
    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def inject_stub_creds(stub_creds, monkeypatch):
    """Swap creds-resolution at the decorator boundary so the
    @workspace_tool(creds=True, scopes=...) envelope doesn't try real
    OAuth. This tool DECLARES scopes, so resolution flows through the
    scope-aware path (auth.load_credentials in stdio test mode) — patch
    THAT, per #138's test_tools.py note. The other two patches keep the
    no-scope path covered (belt-and-suspenders)."""
    from appscriptly import auth

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)
    monkeypatch.setattr(cf, "_get_credentials", lambda: stub_creds)


def _make_script_stub() -> MagicMock:
    """An Apps Script v1 stub with create / updateContent / versions /
    deployments pre-wired to plausible defaults."""
    script = MagicMock(name="script-v1-stub")
    script.projects().create().execute.return_value = {
        "scriptId": "SCRIPT-1", "title": "T", "parentId": "SHEET1",
    }
    script.projects().updateContent().execute.return_value = {}
    script.projects().versions().create().execute.return_value = {
        "versionNumber": 1,
    }
    script.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEPLOY-1",
    }
    return script


def _make_drive_stub(mimetype: str) -> MagicMock:
    drive = MagicMock(name="drive-v3-stub")
    drive.files().get().execute.return_value = {
        "id": "SHEET1", "name": "container", "mimeType": mimetype,
    }
    return drive


@pytest.fixture
def with_sheet_container():
    """Drive resolves the container to a Google Sheet; Apps Script stub
    wired for the full create→push→deploy flow."""
    drive = _make_drive_stub("application/vnd.google-apps.spreadsheet")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        yield drive, script


def test_install_custom_function_happy_path_returns_envelope(with_sheet_container):
    """End-to-end: verify-sheet → shape body → create → push → deploy →
    Sheets-friendly envelope."""
    result = cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="BRAND_CHECK",
        function_body="function BRAND_CHECK(text) { return text.length; }",
    )
    assert result == {
        "script_id": "SCRIPT-1",
        "deployment_id": "DEPLOY-1",
        "on_conflict": "new",
        "reused_existing": False,
        "replaced_count": 0,
        "sheet_id": "SHEET1",
        "function_name": "BRAND_CHECK",
        "usage_hint": "=BRAND_CHECK(...)",
        "project_url": "https://script.google.com/d/SCRIPT-1/edit",
    }


def test_install_custom_function_pushes_tagged_body_as_server_js(with_sheet_container):
    """The deployed .gs must carry the @customfunction tag + the caller's
    function source as a SERVER_JS file."""
    _drive, script = with_sheet_container
    cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="ADDONE",
        function_body="function ADDONE(x) { return x + 1; }",
    )
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    code_files = [f for f in files if f["type"] == "SERVER_JS"]
    assert code_files, "no SERVER_JS file pushed"
    src = code_files[-1]["source"]
    assert "@customfunction" in src
    assert "function ADDONE" in src


def test_install_custom_function_manifest_has_no_extra_scope(with_sheet_container):
    """A custom function needs NO oauthScopes beyond the container binding
    — the pushed manifest must omit oauthScopes (and the UI / trigger
    scopes in particular)."""
    _drive, script = with_sheet_container
    cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="NOOP",
        function_body="function NOOP() { return 1; }",
    )
    body_calls = [
        c for c in script.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    files = body_calls[-1].kwargs["body"]["files"]
    manifest_file = next(f for f in files if f["type"] == "JSON")
    parsed = json.loads(manifest_file["source"])
    assert "oauthScopes" not in parsed
    assert parsed["runtimeVersion"] == "V8"
    # The internal __plan__ echo was stripped from the real manifest.
    assert "__plan__" not in parsed


def test_install_custom_function_binds_via_parent_id(with_sheet_container):
    """The create call must pass parentId=sheet_id — the binding that
    makes the function live IN this sheet."""
    _drive, script = with_sheet_container
    cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="X",
        function_body="function X() {}",
    )
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["parentId"] == "SHEET1"


def test_install_custom_function_uses_custom_name_when_given(with_sheet_container):
    """A supplied name becomes the project title on create."""
    _drive, script = with_sheet_container
    cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="X",
        function_body="function X() {}",
        name="My Custom Func Project",
    )
    body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert body_calls[-1].kwargs["body"]["title"] == "My Custom Func Project"


def test_install_custom_function_rejects_non_sheet_container():
    """A non-Spreadsheet target (here a Doc) → ValueError, before any
    project is created (custom =FUNCTION() only exists in Sheets)."""
    drive = _make_drive_stub("application/vnd.google-apps.document")
    script = _make_script_stub()
    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ValueError, match="only exist in Sheets"):
            cf.as_install_custom_function(
                sheet_id="DOC1",
                function_name="X",
                function_body="function X() {}",
            )
        # The create call must NOT have fired — verification failed first.
        create_body_calls = [
            c for c in script.projects().create.call_args_list if "body" in c.kwargs
        ]
        assert not create_body_calls


def test_install_custom_function_invalid_name_rejected_before_deploy(with_sheet_container):
    """An invalid function_name is rejected before the script project is
    created (no orphan project left behind)."""
    _drive, script = with_sheet_container
    with pytest.raises(ValueError, match="not a valid JavaScript identifier"):
        cf.as_install_custom_function(
            sheet_id="SHEET1",
            function_name="bad name",
            function_body="function x() {}",
        )
    create_body_calls = [
        c for c in script.projects().create.call_args_list if "body" in c.kwargs
    ]
    assert not create_body_calls


def test_install_custom_function_api_httperror_maps_to_tool_error():
    """An Apps Script HttpError on create → the @workspace_tool envelope
    translates it to ToolError (standard creds=True behavior)."""
    drive = _make_drive_stub("application/vnd.google-apps.spreadsheet")
    script = MagicMock(name="script-v1-stub-erroring")

    resp = MagicMock()
    resp.status = 403
    err = HttpError(resp=resp, content=b'{"error": {"message": "denied"}}')
    script.projects().create().execute.side_effect = err

    with with_google_api_client(InMemoryGoogleAPIClient({
        ("drive", "v3"): drive,
        ("script", "v1"): script,
    })):
        with pytest.raises(ToolError):
            cf.as_install_custom_function(
                sheet_id="SHEET1",
                function_name="X",
                function_body="function X() {}",
            )


def test_install_custom_function_resolves_creds_via_scope_aware_path(
    with_sheet_container, monkeypatch
):
    """Canary: because this tool DECLARES scopes, the @workspace_tool
    decorator resolves credentials through the scope-aware path
    (auth.load_credentials in stdio test mode) with the tool's declared
    scopes threaded as extra_scopes — exactly once."""
    from appscriptly import auth
    from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES

    calls: list[dict] = []

    def recording_load_credentials(*_args, **kwargs):
        calls.append(kwargs)
        return MagicMock(name="stub-creds-canary")

    monkeypatch.setattr(auth, "load_credentials", recording_load_credentials)
    cf.as_install_custom_function(
        sheet_id="SHEET1",
        function_name="X",
        function_body="function X() {}",
    )

    assert len(calls) == 1, (
        "auth.load_credentials was not called exactly once — the "
        "scope-aware decorator path may have changed or the fixture missed."
    )
    assert calls[0].get("extra_scopes") == GAS_BOUND_SCOPES
