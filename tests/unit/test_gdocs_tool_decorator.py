"""``@gdocs_tool`` decorator behavior tests (v2.0.6 / R28 deferral close).

Pre-v2.0.6: every Google-API tool repeated ~5 lines of
``try: creds = _get_credentials(); return work(creds, ...); except
HttpError: raise ToolError(_format_http_error(e))`` boilerplate. R28's
decorator subsumes that envelope.

This test exercises the decorator in isolation so its contract is
guarded independently of any specific tool's migration.

Coverage:
- ``register()`` must be called before use (RuntimeError on misuse)
- ``creds=True`` injects credentials as the first positional arg and
  strips the ``creds`` param from the visible MCP input schema
- ``creds=True`` translates HttpError → ToolError(_format_http_error(e))
- ``creds=False`` is a pure passthrough (no auth fetch, no exception
  rewrapping) — required for tools with custom auth shapes like
  gdocs_setup_apps_script and gdocs_reset_authorization
- ToolError raised inside the function body propagates verbatim
  (the decorator must NOT wrap pre-validation errors)
- Function metadata (__name__, __doc__) survives the wrap
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError


@pytest.fixture
def fresh_decorator_module():
    """Reload decorators module to reset its global bindings between tests."""
    import importlib

    from appscriptly import decorators
    importlib.reload(decorators)
    yield decorators


@pytest.fixture
def stub_mcp_and_helpers(fresh_decorator_module):
    """A minimal FastMCP instance + fake creds/format helpers, registered."""
    mcp = FastMCP("decorator-tests")
    creds_calls = []
    format_calls = []

    def fake_creds():
        creds_calls.append(True)
        return "fake-creds-sentinel"

    def fake_format(e):
        format_calls.append(e)
        return f"formatted: {type(e).__name__}"

    fresh_decorator_module.register(mcp, fake_creds, fake_format)
    return mcp, fresh_decorator_module, creds_calls, format_calls


# ---------------------------------------------------------------------
# register() guard
# ---------------------------------------------------------------------


def test_gdocs_tool_raises_before_register(fresh_decorator_module):
    """Misuse: calling the decorator before register() must fail loudly."""
    with pytest.raises(RuntimeError, match="register"):
        fresh_decorator_module.gdocs_tool(
            title="x", readonly=True, destructive=False,
            idempotent=True, external=False,
        )


# ---------------------------------------------------------------------
# creds=True: injection, signature stripping, HttpError translation
# ---------------------------------------------------------------------


def test_creds_true_injects_creds_as_first_arg(stub_mcp_and_helpers):
    _mcp, decorators, creds_calls, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def my_tool(creds, doc_id: str) -> dict:
        return {"creds": creds, "doc_id": doc_id}

    result = my_tool("DOC123")
    assert result == {"creds": "fake-creds-sentinel", "doc_id": "DOC123"}
    assert len(creds_calls) == 1


def test_creds_true_strips_creds_from_mcp_input_schema(stub_mcp_and_helpers):
    """``creds`` is server-injected; clients must not see it as a tool param."""
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def my_tool(creds, doc_id: str, *, verbose: bool = False) -> dict:
        return {"doc_id": doc_id, "verbose": verbose}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    schema_props = set(t.parameters.get("properties", {}).keys())
    assert "creds" not in schema_props
    assert schema_props == {"doc_id", "verbose"}


def test_creds_true_translates_httperror_to_toolerror(stub_mcp_and_helpers):
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        resp = MagicMock(status=404, reason="Not Found")
        raise HttpError(resp, content=b"")

    with pytest.raises(ToolError, match="formatted: HttpError"):
        my_tool(5)
    assert len(format_calls) == 1


def test_creds_true_propagates_tool_error_verbatim(stub_mcp_and_helpers):
    """Pre-validation errors inside the body must NOT be re-wrapped."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        if x < 0:
            raise ToolError("x must be non-negative")
        return {"x": x}

    with pytest.raises(ToolError, match="non-negative"):
        my_tool(-1)
    # _format_http_error must not have been called.
    assert format_calls == []


def test_creds_true_propagates_other_exceptions(stub_mcp_and_helpers):
    """Non-HttpError exceptions must bubble; only HttpError is translated."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        my_tool(1)
    assert format_calls == []


def test_creds_true_preserves_function_metadata(stub_mcp_and_helpers):
    _mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def named_tool(creds, x: int) -> dict:
        """My docstring."""
        return {"x": x}

    assert named_tool.__name__ == "named_tool"
    assert named_tool.__doc__ == "My docstring."


# ---------------------------------------------------------------------
# creds=False: pure passthrough
# ---------------------------------------------------------------------


def test_creds_false_does_not_fetch_credentials(stub_mcp_and_helpers):
    _mcp, decorators, creds_calls, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=True, destructive=False,
        idempotent=True, external=False, creds=False,
    )
    def my_tool(x: int) -> dict:
        return {"x": x}

    result = my_tool(5)
    assert result == {"x": 5}
    assert creds_calls == []  # never invoked the auth helper


def test_creds_false_does_not_translate_httperror(stub_mcp_and_helpers):
    """``creds=False`` tools handle their own exception shape (e.g.
    gdocs_setup_apps_script returns structured ``status: "failed"``).
    The decorator must not interfere."""
    _mcp, decorators, _, format_calls = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="x", readonly=False, destructive=False,
        idempotent=False, external=True, creds=False,
    )
    def my_tool(x: int) -> dict:
        resp = MagicMock(status=500, reason="Boom")
        raise HttpError(resp, content=b"")

    with pytest.raises(HttpError):
        my_tool(5)
    assert format_calls == []  # decorator did NOT call _format_http_error


# ---------------------------------------------------------------------
# Annotations are wired to ToolAnnotations correctly
# ---------------------------------------------------------------------


def test_annotations_propagated_to_mcp_tool(stub_mcp_and_helpers):
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.gdocs_tool(
        title="A specific title",
        readonly=True, destructive=False, idempotent=True, external=False,
    )
    def my_tool(x: int) -> dict:
        return {"x": x}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    a = t.annotations
    assert a is not None
    assert a.title == "A specific title"
    assert a.readOnlyHint is True
    assert a.destructiveHint is False
    assert a.idempotentHint is True
    assert a.openWorldHint is False


# ---------------------------------------------------------------------
# PR A — @workspace_tool(scopes=...) per-tool scope declaration
# ---------------------------------------------------------------------
#
# The ``scopes`` parameter has two observable effects (the SOLID-
# motivated dual surface):
#
# 1. RESOLUTION: when set, the decorator's creds=True wrapper resolves
#    Google API credentials with the scopes asserted. Stdio mode →
#    ``auth.load_credentials(extra_scopes=scopes)``; HTTP mode →
#    ``credentials.get_credentials_for_user(required_scopes=scopes)``.
#    On a partial-grant (user has gmail.readonly but the tool needs
#    gmail.send), the HTTP path raises NeedsReauthError carrying a
#    re-auth URL; the decorator maps it to ToolError with a Markdown
#    auth link — same shape as the standard envelope's mapping.
#
# 2. DECLARATION: the scope list is stamped onto ToolAnnotations as an
#    extra ``scopes`` field. ``mcp.list_tools()`` callers see per-tool
#    scope requirements machine-readably for observability / dynamic
#    consent UI / lint-of-scope-creep without parsing tool source.
#
# Without (2) the decorator would do two things (one observable, one
# not) — SRP slip per the audit. The annotation IS the declaration;
# resolution is the imperative half.


def test_scopes_none_preserves_existing_creds_resolution(stub_mcp_and_helpers):
    """When ``scopes`` is omitted (the default), the wrapper MUST delegate
    to ``_get_credentials_fn`` exactly as before. No new code path runs
    for the existing ~24 tools that don't declare per-tool scopes.

    This is the backward-compatibility canary — if it fires, the PR A
    change accidentally broke the standard envelope path that every
    pre-Gmail tool relies on.
    """
    _mcp, decorators, creds_calls, _ = stub_mcp_and_helpers

    @decorators.workspace_tool(
        title="x", service="docs",
        readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
        # scopes omitted — uses the default None path.
    )
    def my_tool(creds, doc_id: str) -> dict:
        return {"creds": creds, "doc_id": doc_id}

    result = my_tool("DOC123")
    assert result == {"creds": "fake-creds-sentinel", "doc_id": "DOC123"}
    assert len(creds_calls) == 1, (
        "_get_credentials_fn should have been invoked exactly once "
        "via the standard scopes=None delegate path."
    )


def test_scopes_stamped_into_tool_annotations(stub_mcp_and_helpers):
    """The scope list MUST appear as an extra field on the registered
    tool's ToolAnnotations — observable via ``mcp.list_tools()``.
    Catches the SOLID-motivated half: declaration is the visible half
    of the dual surface; without it the decorator's behavior is only
    observable through actual credential resolution (which an external
    consumer of mcp.list_tools cannot see).
    """
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.workspace_tool(
        title="Gmail send",
        service="gmail",
        readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    def my_tool(creds, body: str) -> dict:
        return {"body": body}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    assert t.annotations is not None
    # The pydantic ToolAnnotations has extra="allow" so ``scopes``
    # rides as an attribute.
    declared = getattr(t.annotations, "scopes", None)
    assert declared is not None, (
        "scopes= was passed to @workspace_tool but did not appear on "
        "ToolAnnotations. The annotation half of the dual surface is "
        "missing — see the SOLID note in decorators.py."
    )
    # Frozen as a tuple to discourage downstream mutation; the
    # contents match the input list element-wise.
    assert list(declared) == ["https://www.googleapis.com/auth/gmail.send"]


def test_scopes_not_stamped_when_omitted(stub_mcp_and_helpers):
    """When ``scopes`` is omitted, ToolAnnotations MUST NOT carry a
    spurious ``scopes`` attribute. Catches a regression where the
    stamping code accidentally adds the field for every tool with a
    default ``scopes=None``."""
    mcp, decorators, _, _ = stub_mcp_and_helpers

    @decorators.workspace_tool(
        title="no scopes",
        service="docs",
        readonly=True, destructive=False,
        idempotent=True, external=True, creds=True,
    )
    def my_tool(creds, x: int) -> dict:
        return {"x": x}

    tools = asyncio.run(mcp.list_tools())
    [t] = [t for t in tools if t.name == "my_tool"]
    assert t.annotations is not None
    # ``getattr(..., None)`` returns None when the extra field was not
    # set; an empty list / tuple would also be wrong (a tool declaring
    # ``scopes=[]`` and a tool declaring nothing are not the same).
    declared = getattr(t.annotations, "scopes", None)
    assert declared is None, (
        f"scopes was omitted at the decorator call site but appeared on "
        f"ToolAnnotations as {declared!r}. The stamping code accidentally "
        f"added the field; only set it when scopes was passed explicitly."
    )


def test_scopes_resolution_stdio_mode_calls_load_credentials_with_extra_scopes(
    stub_mcp_and_helpers, monkeypatch,
):
    """In stdio mode (``current_user_id_or_none() is None``), the wrapper
    MUST call ``auth.load_credentials(creds_dir, extra_scopes=scopes)``.
    This is the path that augments the local operator's cached token
    with the per-tool scope set, triggering a fresh OAuth consent if
    the cached token doesn't already cover the requested scopes.
    """
    _mcp, decorators, _, _ = stub_mcp_and_helpers

    # Force stdio mode.
    monkeypatch.setattr(
        "appscriptly.credentials.current_user_id_or_none",
        lambda: None,
    )

    captured: dict = {}

    def fake_load_credentials(creds_dir, extra_scopes=None):
        captured["creds_dir"] = creds_dir
        captured["extra_scopes"] = extra_scopes
        return "stdio-scoped-creds"

    monkeypatch.setattr(
        "appscriptly.auth.load_credentials", fake_load_credentials,
    )

    requested_scope = "https://www.googleapis.com/auth/gmail.send"

    @decorators.workspace_tool(
        title="Send Gmail",
        service="gmail",
        readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
        scopes=[requested_scope],
    )
    def my_tool(creds, body: str) -> dict:
        return {"creds": creds, "body": body}

    result = my_tool("hello")
    assert result == {"creds": "stdio-scoped-creds", "body": "hello"}
    assert captured["extra_scopes"] == [requested_scope], (
        f"load_credentials was called with extra_scopes={captured.get('extra_scopes')!r}, "
        f"expected [{requested_scope!r}]. The decorator did not thread the "
        f"declared scopes through to the stdio resolution path."
    )


def test_scopes_resolution_http_mode_partial_grant_returns_reauth_url_not_500(
    stub_mcp_and_helpers, monkeypatch,
):
    """REGRESSION SCENARIO (per PR A spec): a user who consented to
    gmail.readonly but NOT gmail.send invokes a tool decorated with
    ``@workspace_tool(creds=True, scopes=[".../gmail.send"])``. The
    HTTP-mode credential resolution path's ``_check_scopes_or_raise``
    detects the missing scope and raises NeedsReauthError carrying a
    re-auth URL. The decorator MUST map that to ToolError with a
    Markdown auth-URL link — NOT propagate it as an unhandled 500.

    Prevents PR #47-shape failure recurrence: a partial-grant user
    seeing an opaque server error instead of an actionable consent URL.
    """
    from appscriptly.credentials import NeedsReauthError

    _mcp, decorators, _, _ = stub_mcp_and_helpers

    # Force HTTP mode by returning a user_id.
    monkeypatch.setattr(
        "appscriptly.credentials.current_user_id_or_none",
        lambda: "user-partial-grant",
    )
    # Stub the OAuth config resolution so we don't depend on env vars.
    monkeypatch.setattr(
        "appscriptly.oauth_google.resolve_runtime_oauth_config",
        lambda: {
            "client_config": {"web": {}},
            "signing_key": b"x" * 32,
            "base_url": "https://example.test",
        },
    )

    expected_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?scope=gmail.send"
    )

    def fake_get_credentials_for_user(
        user_id, *, client_config, signing_key, base_url,
        required_scopes=None,
    ):
        # Simulate _check_scopes_or_raise's behavior: granted scope set
        # is {gmail.readonly} but the tool requires gmail.send.
        assert required_scopes == [
            "https://www.googleapis.com/auth/gmail.send"
        ], (
            f"required_scopes was not threaded into get_credentials_for_user "
            f"— got {required_scopes!r}."
        )
        raise NeedsReauthError(
            user_id,
            auth_url=expected_auth_url,
            reason="Missing required scopes: ['.../gmail.send']",
        )

    monkeypatch.setattr(
        "appscriptly.credentials.get_credentials_for_user",
        fake_get_credentials_for_user,
    )

    @decorators.workspace_tool(
        title="Send Gmail",
        service="gmail",
        readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    def my_tool(creds, body: str) -> dict:
        # Should never reach here in this test — the scope check raises
        # before credential resolution completes.
        return {"body": body}

    with pytest.raises(ToolError) as exc:
        my_tool("hello")

    msg = str(exc.value)
    # The user-facing message must (a) be a ToolError (not a raw
    # NeedsReauthError leaking through), (b) carry a Markdown link with
    # the re-auth URL, (c) tell the user what to do next.
    assert expected_auth_url in msg, (
        f"ToolError message did not contain the re-auth URL.\n"
        f"Message: {msg!r}\n"
        f"Expected URL: {expected_auth_url!r}"
    )
    assert "Click here to authorize" in msg, (
        f"ToolError message did not include the user-facing "
        f"'Click here to authorize' prompt.\nMessage: {msg!r}"
    )
    assert "re-run this tool" in msg.lower(), (
        f"ToolError message did not tell the user to re-run after consenting."
    )


def test_scopes_resolution_http_mode_full_grant_returns_creds(
    stub_mcp_and_helpers, monkeypatch,
):
    """Counterpoint to the partial-grant test: when the user HAS the
    required scope, ``get_credentials_for_user`` returns the
    credentials directly and the wrapped function executes normally
    with the resolved creds injected. Confirms the success path of
    the scope-aware resolution branch."""
    _mcp, decorators, _, _ = stub_mcp_and_helpers

    monkeypatch.setattr(
        "appscriptly.credentials.current_user_id_or_none",
        lambda: "user-full-grant",
    )
    monkeypatch.setattr(
        "appscriptly.oauth_google.resolve_runtime_oauth_config",
        lambda: {
            "client_config": {"web": {}},
            "signing_key": b"x" * 32,
            "base_url": "https://example.test",
        },
    )

    def fake_get_credentials_for_user(
        user_id, *, client_config, signing_key, base_url,
        required_scopes=None,
    ):
        return f"creds-for-{user_id}-with-{required_scopes}"

    monkeypatch.setattr(
        "appscriptly.credentials.get_credentials_for_user",
        fake_get_credentials_for_user,
    )

    @decorators.workspace_tool(
        title="Send Gmail",
        service="gmail",
        readonly=False, destructive=False,
        idempotent=False, external=True, creds=True,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    def my_tool(creds, body: str) -> dict:
        return {"creds": creds, "body": body}

    result = my_tool("hello")
    assert result["body"] == "hello"
    assert "user-full-grant" in result["creds"]
    assert "gmail.send" in result["creds"], (
        "required_scopes were not forwarded to get_credentials_for_user."
    )
