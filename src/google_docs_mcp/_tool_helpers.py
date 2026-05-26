"""Shared tool-layer helpers extracted from ``server.py``.

Per M3 Phase C (v2.1.5) — the 3-consumer extraction trigger:
``services/docs/tools.py``, ``services/drive/tools.py``, and
``services/gas_deploy/tools.py`` all need the same two helpers, so
they live here rather than each service reaching back into
``server.py`` via the (now-removed) ``_get_server_helpers()`` shim.

**What's in this module:**

- ``_get_credentials()`` — resolves valid Google API ``Credentials``
  for the calling user. Two transparent modes (stdio cache vs HTTP
  per-user via ``credentials.get_credentials_for_user``); raises
  ``ToolError`` with a clickable consent URL on ``NeedsReauthError``.

- ``_format_http_error()`` — thin wrapper around
  ``errors.friendly_http_error_message`` so all tool-layer
  ``HttpError → ToolError`` mappings share one entry point.

**What's NOT here (intentional):**

- ``_validate_title`` — docs-only (TabSpec titles, Drive file names).
  Hex specialist Round 4: *"Empirically-validated common subset
  is {_get_credentials, _format_http_error} — NOT _validate_title."*
  Stays in ``server.py``; ``services/docs/tools.py`` imports it
  lazily via its remaining (simplified) shim.

**Import discipline:** this module has ZERO dependency on
``server.py``. It depends only on stable peer modules
(``auth``, ``credentials``, ``errors``, ``oauth_google``,
``fastmcp``, ``googleapiclient``). That keeps the import graph
acyclic regardless of import order: every service can import
from ``_tool_helpers`` at the top of its module without the
deferred-binding shim that the pre-Phase-C arrangement required.

**Module-level state:** ``_creds_cache`` is a stdio-mode lazy cache
for the operator's OAuth token. Moves out of ``server.py`` with
``_get_credentials`` to keep the cache scope identical (one
process-wide cache, lifetime = process). The HTTP-mode path
bypasses the cache entirely.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from .auth import default_data_dir, load_credentials
from .credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from .errors import friendly_http_error_message
from .oauth_google import resolve_runtime_oauth_config


# Lazy module-level cache for the stdio/no-auth-context path. HTTP
# mode bypasses this entirely — see ``_get_credentials`` below.
_creds_cache = None


def _get_credentials():
    """Return valid Google API Credentials for the caller.

    Two modes, transparently:

    - **HTTP / multi-tenant** (FastMCP has an auth provider, calling
      user identified by ``get_access_token().claims["sub"]``):
      resolve via ``credentials.get_credentials_for_user``. Refreshes
      per-user, persists back to user_store. On NeedsReauthError,
      raises ToolError with a Markdown link to the consent URL — the
      Claude client renders the URL as clickable.

    - **Stdio / single-tenant** (no auth context, local trust model):
      operator's cached OAuth token at ``~/.google-docs-mcp/token.json``,
      lazy-loaded and cached in-process. Preserves the v1.0 stdio
      experience bit-for-bit.

    The mode branch is observable via ``current_user_id_or_none()``
    returning a value vs None. Until Phase 7 wires GoogleProvider, the
    HTTP path is dormant and all callers fall into the stdio branch.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        global _creds_cache
        if _creds_cache is None or not _creds_cache.valid:
            _creds_cache = load_credentials(default_data_dir())
        return _creds_cache

    try:
        return get_credentials_for_user(
            user_id, **resolve_runtime_oauth_config(),
        )
    except NeedsReauthError as e:
        # PRESERVE THIS BLOCK through v1.5.0. The v1.5 @gdocs_tool
        # decorator will also map NeedsReauthError → ToolError; ONE
        # of the two layers must be removed to avoid double-mapping
        # (ToolError raised → wrapped as ToolError again, losing the
        # auth_url markdown link). Removal plan: v1.5.0 deletes this
        # block when the decorator subsumes it. Until then, this is
        # the load-bearing mapping for the HTTP-mode auth-required
        # path. See R27 audit surprise #2.
        raise ToolError(
            f"Google API access required.\n\n"
            f"**[Click here to authorize]({e.auth_url})**\n\n"
            f"After granting access, re-run this tool."
        ) from e


def _format_http_error(e: HttpError) -> str:
    return friendly_http_error_message(e)
