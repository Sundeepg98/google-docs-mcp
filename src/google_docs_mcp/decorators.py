"""``@gdocs_tool`` — composite tool decorator (v2.0.6 / R28 deferral close).

The pre-v2.0.6 boilerplate for every Google-API-touching MCP tool:

    @mcp.tool(annotations=ToolAnnotations(
        title="<human-readable>",
        readOnlyHint=...,
        destructiveHint=...,
        idempotentHint=...,
        openWorldHint=...,
    ))
    def gdocs_xxx(...) -> dict:
        # ...possibly pre-validation that raises ToolError...
        try:
            creds = _get_credentials()
            return <work>(creds, ...)
        except HttpError as e:
            raise ToolError(_format_http_error(e)) from e

repeated 15 times for the API-touching tools + 9 more annotations-only
blocks for the local tools. ``@gdocs_tool`` collapses the repetitive
parts:

    @gdocs_tool(
        title="<human-readable>",
        readonly=True,
        destructive=False,
        idempotent=True,
        external=True,         # openWorldHint
        creds=True,             # inject creds + wrap HttpError
    )
    def gdocs_xxx(creds, ...) -> dict:
        return <work>(creds, ...)

Migration semantics (R28 deferral context, see CHANGELOG v1.4.2 +
server.py:208 comment):

- For tools that opt into ``creds=True``, the decorator injects
  ``creds`` as the FIRST positional argument and wraps the body in
  ``try/except HttpError → ToolError(_format_http_error(e))``. The
  comment at server.py:208 warned about NeedsReauthError → ToolError
  DOUBLE-mapping — this decorator does NOT touch NeedsReauthError;
  the existing single mapping inside ``_get_credentials`` is preserved.

- For tools that need custom credential / response shaping
  (``gdocs_setup_apps_script``, ``gdocs_reset_authorization``,
  ``gdocs_get_signed_upload_url``, ``gdocs_admin_audit``,
  ``gdocs_help``, ``gdocs_guide``, ``gdocs_server_info``,
  ``gdocs_test_manifest``, ``gdocs_get_tab_url``,
  ``gdocs_preview_tab_split``), pass ``creds=False`` (the default).
  The decorator still wires annotations but the function body is
  passed through unchanged.

**Why a separate module?** Keeps server.py focused on tool bodies
and makes the decorator independently testable
(``test_gdocs_tool_decorator.py``). Mirrors the
``tool_schemas.py`` split (PR #80) and the ``google_clients.py``
seam (PR #48 / #75).
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

F = TypeVar("F", bound=Callable[..., Any])


# Bound late to avoid a circular import — server.py imports decorators
# at module load, decorators bind back into server.py at first @gdocs_tool
# invocation. Set by ``register(mcp, get_credentials, format_http_error)``
# in server.py module init.
_mcp_instance: Any = None
_get_credentials_fn: Callable[[], Any] | None = None
_format_http_error_fn: Callable[[HttpError], str] | None = None


def register(
    mcp_instance: Any,
    get_credentials: Callable[[], Any],
    format_http_error: Callable[[HttpError], str],
) -> None:
    """Bind the decorator to the live FastMCP instance + helpers.

    Called once from server.py after the FastMCP instance is created
    and the helpers are defined. Splitting this out keeps the
    decorator module free of a top-level server.py import (circular).
    """
    global _mcp_instance, _get_credentials_fn, _format_http_error_fn
    _mcp_instance = mcp_instance
    _get_credentials_fn = get_credentials
    _format_http_error_fn = format_http_error


def gdocs_tool(
    *,
    title: str,
    readonly: bool,
    destructive: bool,
    idempotent: bool,
    external: bool,
    output_schema: dict | None = None,
    creds: bool = False,
) -> Callable[[F], F]:
    """Composite decorator: ``@mcp.tool`` + ``ToolAnnotations`` + optional creds.

    Args:
        title: Human-readable label for MCP client UI (becomes
            ``ToolAnnotations.title``).
        readonly: True for read-only tools (``readOnlyHint``).
        destructive: True for tools that delete / revoke state
            (``destructiveHint``).
        idempotent: True for tools whose re-call produces the same
            outcome (``idempotentHint``).
        external: True for tools that call Google APIs (``openWorldHint``).
            False ONLY for ``gdocs_help`` and ``gdocs_guide`` — pure-local
            introspection helpers.
        output_schema: Optional JSON Schema for the tool's return shape.
            Passed through to ``@mcp.tool(output_schema=...)``. See
            ``tool_schemas.py`` for the per-tool constants (PR #80 /
            R33 F6). Must be ``type: object`` at the root per MCP spec.
        creds: If True, the decorator injects fresh Google API
            ``Credentials`` as the first positional argument of the
            wrapped function and converts ``HttpError`` into
            ``ToolError(_format_http_error(e))``. Pre-validation that
            raises ``ToolError`` still runs OUTSIDE the try/except,
            so pre-validation errors propagate verbatim (the v2.0.6
            decorator preserves the v1.x behavior). Default False —
            tools that need custom credential / response shaping (e.g.
            ``gdocs_setup_apps_script`` with NeedsReauthError →
            structured response) opt out and handle their own auth.

    Returns:
        A decorator that registers the function with the FastMCP
        instance, attaches ``ToolAnnotations``, and (optionally) wraps
        the body with creds injection + HttpError translation.
    """
    if _mcp_instance is None:
        raise RuntimeError(
            "gdocs_tool used before register() — call decorators.register("
            "mcp, _get_credentials, _format_http_error) in server.py first."
        )

    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=readonly,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=external,
    )

    # Build the kwargs dict for @mcp.tool once; passing output_schema=None
    # is not the same as omitting it (FastMCP's default sentinel is NotSet,
    # not None). Only include the key when caller actually supplied a schema.
    tool_kwargs: dict[str, Any] = {"annotations": annotations}
    if output_schema is not None:
        tool_kwargs["output_schema"] = output_schema

    def decorator(fn: F) -> F:
        if not creds:
            # Pure passthrough — only attach the annotations + register.
            return _mcp_instance.tool(**tool_kwargs)(fn)

        # creds=True: wrap the body with the standard creds + HttpError
        # envelope. Preserves the function's __name__, __doc__,
        # __annotations__ via @functools.wraps so FastMCP's
        # signature-derived input schema stays correct.
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            assert _get_credentials_fn is not None  # narrowing
            assert _format_http_error_fn is not None
            try:
                creds_obj = _get_credentials_fn()
                return fn(creds_obj, *args, **kwargs)
            except HttpError as e:
                raise ToolError(_format_http_error_fn(e)) from e

        # Trim the leading ``creds`` parameter from the visible signature
        # so FastMCP's input-schema generator doesn't expose it as a
        # required tool argument. The tool's PUBLIC contract is its
        # signature minus the injected creds.
        import inspect

        original_sig = inspect.signature(fn)
        public_params = [
            p for name, p in original_sig.parameters.items()
            if name != "creds"
        ]
        wrapper.__signature__ = original_sig.replace(parameters=public_params)
        # Drop ``creds`` from __annotations__ too so any downstream
        # introspection (e.g. pydantic schema generators) doesn't see it.
        wrapper.__annotations__ = {
            k: v for k, v in fn.__annotations__.items() if k != "creds"
        }

        return _mcp_instance.tool(**tool_kwargs)(wrapper)

    return decorator
