"""``@workspace_tool`` — composite tool decorator (canonical post-M4).

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
blocks for the local tools. ``@workspace_tool`` collapses the repetitive
parts AND adds a required ``service=`` parameter (M4 / v2.2.0):

    @workspace_tool(
        title="<human-readable>",
        service="docs",        # M4: per-tool service tag (required)
        readonly=True,
        destructive=False,
        idempotent=True,
        external=True,         # openWorldHint
        creds=True,            # inject creds + wrap HttpError
    )
    def gdocs_xxx(creds, ...) -> dict:
        return <work>(creds, ...)

The ``service=`` value:

- Matches the per-service-folder layout (``"docs"`` for tools in
  ``services/docs/tools.py``, ``"drive"`` for ``services/drive/tools.py``,
  ``"gas_deploy"`` for ``services/gas_deploy/tools.py``).
- Tags the 7 stay-in-server admin / introspection / auth tools with
  ``service="admin"`` (single bucket; the split into 3 buckets was
  considered and rejected — see PR #N body's "judgment call" section
  for the reasoning).
- Lives on the ``ToolAnnotations`` instance as an extra field (the
  ``ToolAnnotations`` pydantic model has ``extra: "allow"``). Read it
  back via ``tool.annotations.service`` from ``mcp.list_tools()``.
- Lights up future telemetry / per-service routing without needing
  another schema migration.

Migration semantics (R28 deferral context, see CHANGELOG v1.4.2 +
server.py comment):

- For tools that opt into ``creds=True``, the decorator injects
  ``creds`` as the FIRST positional argument and wraps the body in
  ``try/except HttpError → ToolError(_format_http_error(e))``. The
  v1.x comment warned about NeedsReauthError → ToolError DOUBLE-mapping
  — this decorator does NOT touch NeedsReauthError; the existing
  single mapping inside ``_get_credentials`` is preserved.

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
(``test_workspace_tool_decorator.py``). Mirrors the
``tool_schemas.py`` split (PR #80) and the ``google_clients.py``
seam (PR #48 / #75).

**Deprecation:** ``@gdocs_tool`` is preserved as a deprecation-warning
shim that delegates to ``workspace_tool(service="docs", ...)``.
One-release window; planned removal in v2.2.x per Hex specialist
Round 2 guidance ("give a one-release deprecation window, then
remove"). New code should use ``@workspace_tool`` directly.
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


def _resolve_credentials_for_scopes(scopes: list[str] | None) -> Any:
    """Resolve OAuth credentials for the calling user, optionally with extra scopes.

    Two branches:

    - ``scopes is None`` (the default for ``@workspace_tool(creds=True)``
      sites that don't declare per-tool scopes): delegate to the
      bound ``_get_credentials_fn`` (which is
      ``_tool_helpers._get_credentials`` in production). Preserves the
      existing standard envelope behavior bit-for-bit; existing tools
      and tests are unaffected.

    - ``scopes`` is a non-empty list (PR A / Gmail-style per-tool scope
      declaration): perform explicit two-mode resolution inline with
      the scopes asserted. Stdio mode passes ``extra_scopes`` to
      ``auth.load_credentials``; HTTP mode passes ``required_scopes``
      to ``credentials.get_credentials_for_user`` (which internally
      calls ``_check_scopes_or_raise`` → ``NeedsReauthError`` with a
      re-auth URL on partial grant). The ``NeedsReauthError`` is
      mapped to ``ToolError`` with a Markdown auth-URL link — same
      exact shape the standard ``_get_credentials`` uses, just
      threaded with the scope assertion.

    This is the precise pattern ``services/gas_deploy/tools.py`` uses
    explicitly (creds=False + body-level resolution). The decorator
    absorbs that boilerplate for the standard ``creds=True`` envelope
    so per-tool scope declarations (Gmail tri-scope, Calendar future
    scopes, etc.) live at the decorator site, not in tool bodies.

    Imports are deferred to call time to avoid pulling
    ``_tool_helpers`` / ``credentials`` / ``auth`` into the module
    load graph of every consumer that imports
    ``appscriptly.decorators`` (e.g. ``register()`` callers in
    test scaffolding that don't need the runtime resolution path).
    """
    if scopes is None:
        # Existing behavior — delegate. Asserts narrow the type so
        # pyright sees the call site as well-typed.
        assert _get_credentials_fn is not None
        return _get_credentials_fn()

    # Deferred import to keep the decorator module free of a top-level
    # dependency on credentials/auth (mirrors the deferred binding
    # pattern documented in register()'s docstring).
    from fastmcp.exceptions import ToolError as _ToolError

    from .auth import default_data_dir, load_credentials
    from .credentials import (
        NeedsReauthError,
        current_user_id_or_none,
        get_credentials_for_user,
    )
    from .oauth_google import resolve_runtime_oauth_config

    user_id = current_user_id_or_none()

    if user_id is None:
        # Stdio / single-tenant: extra_scopes adds to the baseline.
        # load_credentials handles the cache-vs-fresh-consent dance,
        # including deleting a stale token whose granted scopes don't
        # cover the new required set (forces a fresh OAuth consent).
        return load_credentials(default_data_dir(), extra_scopes=scopes)

    # HTTP / multi-tenant: required_scopes triggers the explicit
    # _check_scopes_or_raise inside get_credentials_for_user. On a
    # partial grant (e.g. user previously consented to gmail.readonly
    # but this tool needs gmail.send), it raises NeedsReauthError
    # carrying a fresh auth_url that includes the missing scope via
    # include_granted_scopes=true.
    try:
        return get_credentials_for_user(
            user_id,
            required_scopes=scopes,
            **resolve_runtime_oauth_config(),
        )
    except NeedsReauthError as e:
        # Identical shape to the standard envelope mapping in
        # _tool_helpers._get_credentials so the user experience
        # (clickable Markdown re-auth link) is uniform whether the
        # tool declared scopes or not.
        raise _ToolError(
            f"Google API access required.\n\n"
            f"**[Click here to authorize]({e.auth_url})**\n\n"
            f"After granting access, re-run this tool."
        ) from e


def workspace_tool(
    *,
    title: str,
    service: str,
    readonly: bool,
    destructive: bool,
    idempotent: bool,
    external: bool,
    output_schema: dict | None = None,
    creds: bool = False,
    scopes: list[str] | None = None,
) -> Callable[[F], F]:
    """Composite decorator: ``@mcp.tool`` + ``ToolAnnotations`` + service tag.

    Canonical post-M4 form. ``@gdocs_tool`` is preserved as a
    deprecation-warning shim that delegates to
    ``workspace_tool(service="docs", ...)``.

    Args:
        title: Human-readable label for MCP client UI (becomes
            ``ToolAnnotations.title``).
        service: Per-service tag (M4 / v2.2.0). REQUIRED. Stored on
            the ``ToolAnnotations`` instance as an extra field so
            ``tool.annotations.service`` round-trips through
            ``mcp.list_tools()``. Canonical values:
            ``"docs"``, ``"drive"``, ``"gas_deploy"``, ``"admin"``.
            The first three match ``services/<svc>/tools.py`` folders;
            ``"admin"`` is the single bucket for the 7 stay-in-server
            tools (admin / introspection / auth / signed URLs).
            Future Workspace services (Sheets, Slides, Gmail, Calendar)
            will register additional values as they land.
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
        scopes: Optional list of OAuth scope URLs the tool requires
            BEYOND ``auth.SCOPES`` baseline. Only consulted when
            ``creds=True``. When set, the wrapper resolves credentials
            with the scopes asserted: stdio mode passes ``extra_scopes``
            to ``auth.load_credentials``; HTTP mode passes
            ``required_scopes`` to ``credentials.get_credentials_for_user``
            (which calls ``_check_scopes_or_raise`` — raises
            ``NeedsReauthError`` with a re-auth URL on partial grant).
            The existing ``NeedsReauthError → ToolError(markdown
            auth URL)`` mapping handles the user-facing recovery
            response; no per-tool boilerplate needed.

            The scope list is ALSO stamped onto ``ToolAnnotations`` as
            an extra ``scopes`` field — same plumbing as ``service=``.
            ``tool.annotations.scopes`` is then machine-readable from
            ``mcp.list_tools()`` for observability / dynamic consent
            UI / lint of per-tool scope creep.

            Pattern matches ``services/gas_deploy/tools.py``'s explicit
            ``required_scopes=GAS_DEPLOY_SCOPES`` resolution; the
            decorator absorbs that boilerplate for the standard
            ``creds=True`` envelope so future per-tool scope-narrow
            additions (Gmail tri-scope: ``gmail.readonly`` / send /
            modify) can declare scopes at the decorator site rather
            than reach back into ``credentials.py``.

    Returns:
        A decorator that registers the function with the FastMCP
        instance, attaches ``ToolAnnotations`` (including ``service``
        and optional ``scopes`` as extra fields), and (optionally)
        wraps the body with creds injection + HttpError translation.
    """
    if _mcp_instance is None:
        raise RuntimeError(
            "workspace_tool used before register() — call decorators.register("
            "mcp, _get_credentials, _format_http_error) in server.py first."
        )

    # M4 / v2.2.0: pydantic ToolAnnotations has extra="allow" so
    # ``service=service`` round-trips through model_dump + attribute
    # access. Verified at M4 ship time: ``tool.annotations.service``
    # is readable from ``mcp.list_tools()`` at the receiving end.
    #
    # pyright doesn't know about pydantic's extra="allow" — the
    # generated stub for ToolAnnotations lists only the 5 declared
    # fields (title + 4 *Hint), so passing ``service=`` / ``scopes=``
    # trips reportCallIssue. The runtime accepts both; the silencer is
    # narrowly scoped to this specific call (any future named-arg
    # typo on the 5 declared fields will still fire normally).
    #
    # ``scopes`` is stamped here even when ``creds=False`` so the
    # annotation reflects intent uniformly (a creds=False tool with
    # scopes= declared would still surface scope info to clients —
    # though it's expected to be a no-op since the wrapper does the
    # actual scope assertion). In practice, every site that passes
    # scopes= will also pass creds=True; the declaration is the
    # SRP-aligned half (annotation) and the wrapping is the
    # imperative half (resolution).
    annotation_kwargs: dict[str, Any] = {
        "title": title,
        "readOnlyHint": readonly,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": external,
        "service": service,
    }
    if scopes is not None:
        # Freeze into a tuple to discourage downstream mutation of the
        # annotation surface. ToolAnnotations' pydantic model serializes
        # it as a JSON array either way.
        annotation_kwargs["scopes"] = tuple(scopes)
    annotations = ToolAnnotations(
        **annotation_kwargs,  # pyright: ignore[reportCallIssue]  # extra="allow"
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
            # scopes= without creds=True is a no-op for resolution but
            # still gets stamped into ToolAnnotations above so the
            # declaration is honest. (No-op-with-declaration is
            # preferred over silent-drop — a future creds=False tool
            # that does its own resolution can read its own annotation
            # to discover the declared scopes.)
            return _mcp_instance.tool(**tool_kwargs)(fn)

        # creds=True: wrap the body with the standard creds + HttpError
        # envelope. Preserves the function's __name__, __doc__,
        # __annotations__ via @functools.wraps so FastMCP's
        # signature-derived input schema stays correct.
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            assert _format_http_error_fn is not None
            try:
                creds_obj = _resolve_credentials_for_scopes(scopes)
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


# ---------------------------------------------------------------------
# Deprecation shim — keep @gdocs_tool callable for one release.
# ---------------------------------------------------------------------
#
# M4 (v2.2.0) renames the canonical decorator from ``@gdocs_tool`` to
# ``@workspace_tool(service=...)``. ``@gdocs_tool`` is preserved as a
# deprecation-warning shim that delegates to
# ``workspace_tool(service="docs", ...)`` — that ``service="docs"`` is
# the right default because every pre-M4 ``@gdocs_tool`` site in the
# repo was either in ``services/docs/tools.py`` or in ``server.py``
# during a state where docs was the only service. Per Hex specialist
# Round 2: one-release deprecation window, then remove. Planned
# removal in v2.2.x.
#
# IMPORTANT: this shim does NOT fire during normal import or test
# collection — all in-repo callers have been migrated to
# ``@workspace_tool``. The shim exists for downstream forks or
# external code that still imports ``gdocs_tool``. Verified at M4
# ship time by running pytest with ``-W error::DeprecationWarning``.


def gdocs_tool(
    *,
    title: str,
    readonly: bool,
    destructive: bool,
    idempotent: bool,
    external: bool,
    output_schema: dict | None = None,
    creds: bool = False,
    scopes: list[str] | None = None,
) -> Callable[[F], F]:
    """DEPRECATED — use ``@workspace_tool(service="docs", ...)`` instead.

    Preserved as a thin compatibility shim that delegates to
    ``workspace_tool`` with ``service="docs"``. Emits a
    ``DeprecationWarning`` at call time so any remaining downstream
    callers see the migration path.

    Planned removal in v2.2.x per Hex specialist Round 2 guidance
    ("one-release deprecation window, then remove"). New code MUST
    use ``@workspace_tool(service=..., ...)`` directly with an
    explicit service tag.
    """
    import warnings

    warnings.warn(
        "@gdocs_tool is deprecated since v2.2.0; use "
        "@workspace_tool(service=\"<svc>\", ...) instead. The bare "
        "@gdocs_tool form will be removed in v2.2.x. See "
        "docs/ARCHITECTURE.md §5.2.",
        DeprecationWarning,
        stacklevel=2,
    )
    return workspace_tool(
        title=title,
        service="docs",
        readonly=readonly,
        destructive=destructive,
        idempotent=idempotent,
        external=external,
        output_schema=output_schema,
        creds=creds,
        scopes=scopes,
    )
