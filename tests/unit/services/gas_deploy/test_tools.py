"""Co-located tool-layer tests for services/gas_deploy/tools.py.

Per the M3 Phase A/B test-architect pattern: per-service tool-layer
tests live next to the service. Registration guards (the
"is the tool registered?" check) live in the multi-service file
``tests/unit/services/test_tool_registration.py``; this file holds
tests that exercise the tool's body shape — the things that are
SPECIFIC to the install-automation tools and don't belong in the
registration guard.

**Post-PR-α surface** (v2.3.4+): TWO MCP tools live here, sharing
a single underlying installer:

  1. ``gdocs_install_automation`` — canonical, user-facing
  2. ``gdocs_setup_apps_script`` — deprecation alias; emits a
     ``DeprecationWarning`` on call and delegates to the same
     underlying ``_install_automation_runtime`` helper

Both must satisfy the same invariants (creds=False, structured
NeedsReauthError response, zero-arg signature). Tests below run on
both via parametrize where the assertion is identical.

**CRITICAL invariant verified here: ``creds=False`` preservation.**

Both tools opt out of the standard creds-injection envelope. The
shared underlying body has its own ``NeedsReauthError`` →
structured-response path: on cloud-mode auth failure it returns
``{status: "needs_authorization", auth_url, message}`` rather than
raising ``ToolError``. Re-applying the standard ``creds=True``
envelope would short-circuit at the credential-fetch step and lose
that structured shape — silently breaking the OAuth-first-run UX
in cloud chat. Phase C explicitly preserved this at the new site;
PR-α preserves it on the new canonical site AND on the alias; these
tests pin both so a future "everything to creds=True for
consistency" refactor can't quietly regress.
"""
from __future__ import annotations

import asyncio
import inspect


def test_gdocs_setup_apps_script_preserves_creds_false_opt_out():
    """The tool's REGISTERED signature must be zero-arg.

    Under ``creds=True``, the decorator INJECTS a ``creds`` positional
    argument before the body runs, and the visible (registered)
    signature has the leading ``creds`` parameter STRIPPED so MCP
    clients don't see it. Under ``creds=False`` (our case), the
    function is registered as-is with whatever signature it declares.

    ``gdocs_setup_apps_script`` declares ``def gdocs_setup_apps_script()
    -> dict:`` — zero params. If a future refactor flips to
    ``creds=True``, the decorator would wrap the body in the standard
    creds-injection envelope, and the FIRST CALL would short-circuit
    on a NeedsReauthError before the body's structured-response code
    ever ran. The signature itself stays zero-arg either way (because
    the decorator strips ``creds`` from the visible signature), so we
    cross-check via the registered tool's input schema instead.
    """
    from appscriptly.server import mcp

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "gdocs_setup_apps_script" in by_name, (
        "gdocs_setup_apps_script not registered — services/gas_deploy/"
        "tools.py side-effect import is missing from server.py"
    )
    tool = by_name["gdocs_setup_apps_script"]
    # The registered input schema must have NO required properties
    # (the tool takes no MCP-visible arguments). Under creds=True the
    # decorator strips the injected ``creds`` from the schema too —
    # so a schema with required args would be the smoking gun for
    # "someone added an argument by accident", which is its own bug.
    # FastMCP exposes the JSON-Schema via ``tool.parameters`` (the
    # underlying field FunctionTool models in pydantic; the wire
    # representation surfaces it as ``inputSchema`` per MCP spec).
    schema = tool.parameters or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    assert properties == {}, (
        f"gdocs_setup_apps_script registered with unexpected properties: "
        f"{properties!r}. Was a required arg added without updating "
        f"this guard? Or did a refactor accidentally surface the "
        f"injected creds arg?"
    )
    assert required == [], (
        f"gdocs_setup_apps_script registered with required args: "
        f"{required!r}. The tool is zero-arg by contract."
    )


def test_gdocs_setup_apps_script_body_returns_structured_needs_authorization_on_needs_reauth(
    monkeypatch,
):
    """The cloud-mode HTTP path must return a structured
    ``{status: "needs_authorization", ...}`` dict on NeedsReauthError,
    NOT raise ``ToolError``. This is the load-bearing behavior the
    ``creds=False`` opt-out exists to protect.

    Setup: simulate cloud-mode by making ``current_user_id_or_none``
    return a value, then make ``get_credentials_for_user`` raise
    ``NeedsReauthError``. The body should catch it and return a dict.
    """
    from appscriptly.credentials import NeedsReauthError
    from appscriptly.services.gas_deploy import tools as gas_deploy_tools

    # Cloud-mode branch: current_user_id_or_none returns "cloud-user".
    monkeypatch.setattr(
        gas_deploy_tools, "current_user_id_or_none", lambda: "cloud-user"
    )
    # resolve_runtime_oauth_config must not raise — return a stub config
    # so the code path proceeds to get_credentials_for_user.
    monkeypatch.setattr(
        gas_deploy_tools,
        "resolve_runtime_oauth_config",
        lambda: {
            "client_config": {"client_id": "X.apps.googleusercontent.com"},
            "signing_key": b"x" * 32,
            "base_url": "https://example.fly.dev",
        },
    )
    # get_credentials_for_user raises NeedsReauthError; the body must
    # catch and convert.
    fake_url = "https://accounts.google.com/o/oauth2/auth?fake=1"
    def raises(*args, **kwargs):
        raise NeedsReauthError(
            "cloud-user", auth_url=fake_url, reason="missing scope"
        )
    monkeypatch.setattr(
        gas_deploy_tools, "get_credentials_for_user", raises
    )

    result = gas_deploy_tools.gdocs_setup_apps_script()
    # Structured response shape — NOT a ToolError raise.
    assert isinstance(result, dict)
    assert result["status"] == "needs_authorization"
    assert result["auth_url"] == fake_url
    assert "message" in result
    # The message includes the auth URL so Claude renders a clickable link.
    assert fake_url in result["message"]


def test_gdocs_setup_apps_script_module_is_services_gas_deploy_tools():
    """Defense-in-depth on the M3 Phase C migration: the function's
    ``__module__`` MUST resolve to the per-service folder, not back
    to server.py.

    This is the same invariant ``test_tool_registration.py``'s gas-
    deploy guard asserts; duplicated here so a developer working in
    ``tests/unit/services/gas_deploy/`` sees the assertion locally
    without needing to know about the multi-service file."""
    from appscriptly.services.gas_deploy.tools import gdocs_setup_apps_script

    assert gdocs_setup_apps_script.__module__ == (
        "appscriptly.services.gas_deploy.tools"
    ), (
        f"gdocs_setup_apps_script.__module__ is "
        f"{gdocs_setup_apps_script.__module__!r}; expected "
        f"'appscriptly.services.gas_deploy.tools'. M3 Phase C "
        f"moved this tool out of server.py — confirm the extraction."
    )
    # Also confirm it's a plain function (no decorator wrapping changed
    # the callable type) — guards against a future refactor that wraps
    # the tool in a class or partial.
    assert inspect.isfunction(gdocs_setup_apps_script), (
        f"gdocs_setup_apps_script is {type(gdocs_setup_apps_script)}, "
        f"expected a function. A decorator may have changed the type."
    )


# ---------------------------------------------------------------------
# PR-α (v2.3.4) — reframe: gdocs_install_automation canonical surface
# ---------------------------------------------------------------------
#
# The reframe is a copy + name change, not a behavior change. The
# canonical tool MUST satisfy the same invariants as the deprecation
# alias (zero-arg, creds=False structured response on NeedsReauthError,
# __module__ in the service folder), AND its registered surface
# (annotations title, returned message strings) MUST reflect the new
# user-facing framing rather than the old infrastructure framing.


def test_gdocs_install_automation_is_registered_and_zero_arg():
    """The PR-α canonical tool MUST appear in mcp.list_tools() with
    zero input args. Same invariant as the alias — both opt out of
    the standard creds envelope via ``creds=False`` so the registered
    signature is the function's own (zero params)."""
    import asyncio
    from appscriptly.server import mcp

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "gdocs_install_automation" in by_name, (
        "gdocs_install_automation not registered — PR-α added it as "
        "the canonical user-facing surface for the Workspace automation "
        "runtime installer. Check services/gas_deploy/tools.py."
    )
    tool = by_name["gdocs_install_automation"]
    schema = tool.parameters or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    assert properties == {}, (
        f"gdocs_install_automation registered with unexpected properties: "
        f"{properties!r}. The tool is zero-arg by contract."
    )
    assert required == [], (
        f"gdocs_install_automation registered with required args: "
        f"{required!r}. The tool is zero-arg by contract."
    )


def test_gdocs_install_automation_module_is_services_gas_deploy_tools():
    """Same defense-in-depth invariant as the alias — confirms the
    canonical tool also lives in the per-service folder, not in
    server.py. Symmetric with
    ``test_gdocs_setup_apps_script_module_is_services_gas_deploy_tools``."""
    from appscriptly.services.gas_deploy.tools import gdocs_install_automation

    assert gdocs_install_automation.__module__ == (
        "appscriptly.services.gas_deploy.tools"
    ), (
        f"gdocs_install_automation.__module__ is "
        f"{gdocs_install_automation.__module__!r}; expected "
        f"'appscriptly.services.gas_deploy.tools'."
    )
    assert inspect.isfunction(gdocs_install_automation), (
        f"gdocs_install_automation is {type(gdocs_install_automation)}, "
        f"expected a function."
    )


def test_gdocs_install_automation_body_returns_structured_needs_authorization_on_needs_reauth(
    monkeypatch,
):
    """The canonical tool MUST preserve the structured
    ``{status: "needs_authorization", ...}`` response on
    NeedsReauthError — identical contract to the alias. The reframe
    is in the user-facing copy; the behavior is unchanged.

    Bonus assertion: the returned ``message`` uses the PR-α reframe
    copy (mentions "Install ... Workspace automation runtime" rather
    than "set up your Apps Script Web App"). Catches a regression
    where someone restores the old copy under the new tool name."""
    from appscriptly.credentials import NeedsReauthError
    from appscriptly.services.gas_deploy import tools as gas_deploy_tools

    monkeypatch.setattr(
        gas_deploy_tools, "current_user_id_or_none", lambda: "cloud-user"
    )
    monkeypatch.setattr(
        gas_deploy_tools,
        "resolve_runtime_oauth_config",
        lambda: {
            "client_config": {"client_id": "X.apps.googleusercontent.com"},
            "signing_key": b"x" * 32,
            "base_url": "https://example.fly.dev",
        },
    )
    fake_url = "https://accounts.google.com/o/oauth2/auth?fake=2"
    def raises(*args, **kwargs):
        raise NeedsReauthError(
            "cloud-user", auth_url=fake_url, reason="missing scope"
        )
    monkeypatch.setattr(
        gas_deploy_tools, "get_credentials_for_user", raises
    )

    result = gas_deploy_tools.gdocs_install_automation()
    assert isinstance(result, dict)
    assert result["status"] == "needs_authorization"
    assert result["auth_url"] == fake_url
    assert fake_url in result["message"]
    # Reframe assertion: the user-facing copy frames this as
    # installing an automation runtime, NOT as Apps-Script-Web-App
    # setup. Two phrases that MUST appear; one phrase that MUST NOT.
    msg_lower = result["message"].lower()
    assert "workspace automation runtime" in msg_lower, (
        f"PR-α reframe regression: needs_authorization message did "
        f"not include 'Workspace automation runtime'. Message:\n"
        f"{result['message']!r}"
    )
    assert "workflow installer" in msg_lower, (
        f"PR-α reframe regression: needs_authorization message did "
        f"not include 'workflow installer'. Message:\n"
        f"{result['message']!r}"
    )
    assert "apps script web app" not in msg_lower, (
        f"PR-α reframe regression: the old 'Apps Script Web App' "
        f"copy leaked back into the needs_authorization message. "
        f"Message:\n{result['message']!r}"
    )


def test_gdocs_setup_apps_script_alias_emits_deprecation_warning_and_returns_same_result(
    monkeypatch,
):
    """PR-α regression: calling the deprecated ``gdocs_setup_apps_script``
    alias MUST (a) emit a ``DeprecationWarning`` instructing the
    caller to use ``gdocs_install_automation``, AND (b) return the
    SAME structured response the canonical tool would (i.e. delegate
    to the shared underlying installer rather than diverge).

    Catches: a future refactor that splits the alias and canonical
    into two separate implementations and lets them drift; or one
    that removes the deprecation warning before the v3.0 removal
    window closes; or one that swallows the warning silently."""
    import warnings

    from appscriptly.credentials import NeedsReauthError
    from appscriptly.services.gas_deploy import tools as gas_deploy_tools

    # Same NeedsReauthError monkeypatch shape as the canonical test
    # above so we can compare structured response equivalence.
    monkeypatch.setattr(
        gas_deploy_tools, "current_user_id_or_none", lambda: "cloud-user"
    )
    monkeypatch.setattr(
        gas_deploy_tools,
        "resolve_runtime_oauth_config",
        lambda: {
            "client_config": {"client_id": "X.apps.googleusercontent.com"},
            "signing_key": b"x" * 32,
            "base_url": "https://example.fly.dev",
        },
    )
    fake_url = "https://accounts.google.com/o/oauth2/auth?fake=3"
    def raises(*args, **kwargs):
        raise NeedsReauthError(
            "cloud-user", auth_url=fake_url, reason="missing scope"
        )
    monkeypatch.setattr(
        gas_deploy_tools, "get_credentials_for_user", raises
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result_alias = gas_deploy_tools.gdocs_setup_apps_script()

    # (a) DeprecationWarning was emitted, mentioning the new tool name.
    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert deprecation_warnings, (
        "Calling gdocs_setup_apps_script did not emit a "
        "DeprecationWarning. PR-α requires the alias to nudge "
        "callers toward gdocs_install_automation."
    )
    warning_text = str(deprecation_warnings[0].message)
    assert "gdocs_install_automation" in warning_text, (
        f"DeprecationWarning text did not name the canonical "
        f"replacement (``gdocs_install_automation``). Message:\n"
        f"{warning_text!r}"
    )

    # (b) Same structured response shape as the canonical tool.
    assert result_alias["status"] == "needs_authorization"
    assert result_alias["auth_url"] == fake_url
    # And it carries the PR-α reframe copy — the alias delegates to
    # the same shared helper, so the new copy reaches the alias path
    # too. This is the load-bearing "no divergence" assertion: if a
    # future refactor copies the body and lets the alias's copy drift
    # back to the pre-PR-α wording, this fires.
    assert "workspace automation runtime" in result_alias["message"].lower()


def test_alias_and_canonical_share_underlying_implementation():
    """``gdocs_setup_apps_script`` MUST delegate to the same
    ``_install_automation_runtime`` helper the canonical tool uses.
    Catches a regression where the two functions get copy-pasted
    implementations that can drift over time.

    Inspecting the function source is a structural check — if a
    future refactor inlines the helper into each tool body, the
    test fires and forces the author to either restore the shared
    helper or update this guard with an explicit rationale."""
    from appscriptly.services.gas_deploy import tools as gas_deploy_tools

    canonical_src = inspect.getsource(gas_deploy_tools.gdocs_install_automation)
    alias_src = inspect.getsource(gas_deploy_tools.gdocs_setup_apps_script)

    assert "_install_automation_runtime" in canonical_src, (
        "gdocs_install_automation no longer references the shared "
        "_install_automation_runtime helper. If the body was inlined "
        "for some reason, update this test with the rationale; "
        "otherwise restore the shared helper so the alias can't drift."
    )
    assert "_install_automation_runtime" in alias_src, (
        "gdocs_setup_apps_script no longer references the shared "
        "_install_automation_runtime helper. The alias is supposed "
        "to delegate; an independent implementation would let the "
        "two responses drift over time."
    )


# ---------------------------------------------------------------------
# ROADMAP 59 — as_deploy_web_app (deploy a doGet/doPost project as a
# Web App). Unlike the install tools above, this one uses creds=True
# (standard envelope; no NeedsReauthError structured path).
# ---------------------------------------------------------------------


def test_as_deploy_web_app_is_registered():
    """as_deploy_web_app must appear in the live registry — confirms the
    services/gas_deploy/tools.py side-effect import wired it."""
    from appscriptly.server import mcp

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "as_deploy_web_app" in by_name, (
        "as_deploy_web_app not registered — check services/gas_deploy/"
        "tools.py (ROADMAP 59)."
    )


def test_as_deploy_web_app_module_is_services_gas_deploy_tools():
    """Lives in the gas_deploy service folder (the task extended
    gas_deploy), not server.py or apps_script."""
    from appscriptly.services.gas_deploy.tools import as_deploy_web_app

    assert as_deploy_web_app.__module__ == (
        "appscriptly.services.gas_deploy.tools"
    )
    assert inspect.isfunction(as_deploy_web_app)


def _stub_creds_and_script_svc(monkeypatch):
    """Inject stub creds at the decorator boundary + a stubbed script v1
    service via the GoogleAPIClient port, wired for the full deploy
    chain. Returns the script-svc MagicMock for call inspection.

    IMPORTANT — as_deploy_web_app declares ``scopes=_WEB_APP_DEPLOY_SCOPES``,
    so its ``@workspace_tool(creds=True)`` decorator takes the SCOPE-AWARE
    resolution path, which (in stdio test context) calls
    ``auth.load_credentials(...)`` — NOT the plain ``_get_credentials_fn``
    path the no-scope sheets/slides tools use. Patching only
    ``_get_credentials_fn`` lets the real loader run and raises
    ``FileNotFoundError: No OAuth client config found``. So patch
    ``auth.load_credentials`` (the real target) plus the other two creds
    entry points belt-and-suspenders. Mirrors
    ``services/apps_script/test_tools.py::inject_stub_creds``."""
    from unittest.mock import MagicMock

    from appscriptly import auth, decorators
    from appscriptly.google_api_client import (
        InMemoryGoogleAPIClient,
        with_google_api_client,
    )

    _creds = MagicMock(name="creds")
    # The scope-aware creds path for this tool resolves through
    # auth.load_credentials (stdio context) — that's the real target to
    # stub. _get_credentials_fn is patched too in case a future refactor
    # flips the branch. (Unlike apps_script/sheets tools, gas_deploy/tools
    # does NOT import a module-level _get_credentials, so there's nothing
    # to patch there.)
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: _creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: _creds)
    svc = MagicMock(name="script-v1")
    svc.projects().create().execute.return_value = {"scriptId": "SID-9"}
    svc.projects().updateContent().execute.return_value = {}
    svc.projects().versions().create().execute.return_value = {"versionNumber": 1}
    svc.projects().deployments().create().execute.return_value = {
        "deploymentId": "DEP-9",
        "entryPoints": [
            {"webApp": {"url": "https://script.google.com/macros/s/z/exec"}}
        ],
    }
    return svc, with_google_api_client, InMemoryGoogleAPIClient


def _captured_pushed_source(svc) -> str:
    """Return the SERVER_JS source pushed to updateContent (the deployed .gs).

    Lets a test inspect the source that was actually shipped — e.g. to
    confirm the HMAC guard was injected for an ANYONE_ANONYMOUS deploy.
    """
    body_calls = [
        c for c in svc.projects().updateContent.call_args_list
        if "body" in c.kwargs
    ]
    assert body_calls, "no updateContent() call captured a body"
    files = body_calls[-1].kwargs["body"]["files"]
    code = next(f for f in files if f["type"] == "SERVER_JS")
    return code["source"]


def test_as_deploy_web_app_anonymous_injects_hmac_guard(monkeypatch):
    """ANYONE_ANONYMOUS deploy: the tool returns a usable hmac_key + the
    deployed source is the caller's doPost WRAPPED by an HMAC verify gate.
    The signature scheme must match apps_script_hmac.compute_signature so a
    correctly-signed request would pass and the user's handler is delegated
    to under the new name."""
    from appscriptly.apps_script_hmac import compute_signature
    from appscriptly.services.gas_deploy import tools

    svc, with_client, InMem = _stub_creds_and_script_svc(monkeypatch)
    with with_client(InMem({("script", "v1"): svc})):
        result = tools.as_deploy_web_app(
            script_body="function doPost(e){ return ContentService.createTextOutput('ok'); }",
            title="Stripe hook",
            access="ANYONE_ANONYMOUS",
        )

    # Core envelope unchanged.
    assert result["script_id"] == "SID-9"
    assert result["exec_url"] == "https://script.google.com/macros/s/z/exec"
    assert result["access"] == "ANYONE_ANONYMOUS"
    # New: a 64-hex HMAC key + instructions are returned.
    key = result["hmac_key"]
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert "X-MCP-Signature" in result["hmac_instructions"]

    # The deployed source carries the guard, bakes the SAME key, renames the
    # caller's handler, and gates with a new doPost.
    pushed = _captured_pushed_source(svc)
    assert key in pushed
    assert "function __mcpUserDoPost(e)" in pushed
    assert "function doPost(e)" in pushed
    assert "computeHmacSha256Signature" in pushed
    # compute_signature is the server-side counterpart used by docx_import;
    # its presence here is a cross-check that the scheme name is stable.
    assert compute_signature(key, timestamp="0", body="{}")  # no raise


def test_as_deploy_web_app_non_public_access_no_hmac_key(monkeypatch):
    """A non-public access mode (MYSELF / DOMAIN) does NOT inject a guard
    and returns the original flat envelope with NO hmac_key — the endpoint
    isn't world-reachable so Google's own access control suffices."""
    from appscriptly.services.gas_deploy import tools

    svc, with_client, InMem = _stub_creds_and_script_svc(monkeypatch)
    with with_client(InMem({("script", "v1"): svc})):
        result = tools.as_deploy_web_app(
            script_body="function doPost(e){ return ContentService.createTextOutput('ok'); }",
            title="Internal hook",
            access="MYSELF",
        )
    assert result == {
        "script_id": "SID-9",
        "deployment_id": "DEP-9",
        "version": 1,
        "exec_url": "https://script.google.com/macros/s/z/exec",
        "execute_as": "USER_DEPLOYING",
        "access": "MYSELF",
        "project_url": "https://script.google.com/d/SID-9/edit",
    }
    # Original body shipped verbatim (no wrapper) for a non-public deploy.
    pushed = _captured_pushed_source(svc)
    assert "__mcpUserDoPost" not in pushed


def test_as_deploy_web_app_validation_propagates(monkeypatch):
    """A body without doGet/doPost is rejected. For the default
    (ANYONE_ANONYMOUS) access the rejection now comes from the HMAC-guard
    injector (no guardable doPost) — still a hard refusal to deploy an
    unauthenticated/unprotected public endpoint."""
    from appscriptly.services.gas_deploy import tools

    svc, with_client, InMem = _stub_creds_and_script_svc(monkeypatch)
    with with_client(InMem({("script", "v1"): svc})):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="doPost|doGet"):
            tools.as_deploy_web_app(
                script_body="function helper(){ return 1; }",
                title="No handler",
            )


def test_as_deploy_web_app_non_public_validation_propagates(monkeypatch):
    """For a non-public deploy (no guard injection), the handler-less body
    is rejected by the api layer's doGet/doPost requirement."""
    from appscriptly.services.gas_deploy import tools

    svc, with_client, InMem = _stub_creds_and_script_svc(monkeypatch)
    with with_client(InMem({("script", "v1"): svc})):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="doGet.*doPost|doGet\\(e\\) or doPost"):
            tools.as_deploy_web_app(
                script_body="function helper(){ return 1; }",
                title="No handler",
                access="MYSELF",
            )
