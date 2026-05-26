"""Co-located tool-layer tests for services/gas_deploy/tools.py.

Per the M3 Phase A/B test-architect pattern: per-service tool-layer
tests live next to the service. Registration guards (the
"is the tool registered?" check) live in the multi-service file
``tests/unit/services/test_tool_registration.py``; this file holds
tests that exercise the tool's body shape — the things that are
SPECIFIC to ``gdocs_setup_apps_script`` and don't belong in the
registration guard.

**CRITICAL invariant verified here: ``creds=False`` preservation.**

``gdocs_setup_apps_script`` is the one ``@gdocs_tool`` site that
opts out of the standard creds-injection envelope. Its body has its
own ``NeedsReauthError`` → structured-response path: on cloud-mode
auth failure it returns ``{status: "needs_authorization", auth_url,
message}`` rather than raising ``ToolError``. Re-applying the
standard ``creds=True`` envelope would short-circuit at the credential-
fetch step and lose that structured shape — silently breaking the
OAuth-first-run UX in cloud chat. Phase C explicitly preserved this
at the new site; this test pins it so a future "everything to
creds=True for consistency" refactor can't quietly regress.
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
    from google_docs_mcp.server import mcp

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
    from google_docs_mcp.credentials import NeedsReauthError
    from google_docs_mcp.services.gas_deploy import tools as gas_deploy_tools

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
    from google_docs_mcp.services.gas_deploy.tools import gdocs_setup_apps_script

    assert gdocs_setup_apps_script.__module__ == (
        "google_docs_mcp.services.gas_deploy.tools"
    ), (
        f"gdocs_setup_apps_script.__module__ is "
        f"{gdocs_setup_apps_script.__module__!r}; expected "
        f"'google_docs_mcp.services.gas_deploy.tools'. M3 Phase C "
        f"moved this tool out of server.py — confirm the extraction."
    )
    # Also confirm it's a plain function (no decorator wrapping changed
    # the callable type) — guards against a future refactor that wraps
    # the tool in a class or partial.
    assert inspect.isfunction(gdocs_setup_apps_script), (
        f"gdocs_setup_apps_script is {type(gdocs_setup_apps_script)}, "
        f"expected a function. A decorator may have changed the type."
    )
