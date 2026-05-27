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

import logging

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

# PR-Δ5: dedicated logger for tenant-isolation assertion failures.
# Distinct from the credential-dispatch audit logger in
# ``credentials.py`` so an assertion fire (a code-correctness bug,
# not a normal auth event) is visually distinct in log review.
_isolation_log = logging.getLogger("google_docs_mcp.audit.tenant_isolation")


# Lazy module-level cache for the stdio/no-auth-context path. HTTP
# mode bypasses this entirely — see ``_get_credentials`` below.
_creds_cache = None


class TenantIsolationError(AssertionError):
    """Raised when ``assert_tenant_match`` detects a cross-tenant mismatch.

    Today this is belt-and-suspenders: the storage layer is the
    source of truth and the storage layer is correct, so this
    exception should NEVER fire in production. If it ever does
    fire, treat it as a security incident and stop processing
    immediately — a downstream tool was about to operate on the
    wrong tenant's data.

    Subclass of ``AssertionError`` (not generic ``Exception``) so
    the standard ``@workspace_tool`` envelope, which catches
    ``HttpError`` and re-raises everything else verbatim, lets
    this propagate as a hard fault rather than translating it
    into a user-facing 400. The intent: never let cross-tenant
    data flow downstream silently.
    """


def assert_tenant_match(creds, expected_user_id: str | None) -> None:
    """Defensive check: confirm ``creds`` are stamped for ``expected_user_id``.

    PR-Δ5 multi-tenant hardening. Reads the ``_google_docs_mcp_user_id``
    attribute that ``credentials._stamp_tenant`` writes onto every
    HTTP-mode credentials object before returning it.

    Args:
        creds: A Google API ``Credentials`` instance. Stdio-mode
            credentials (operator's local token, no per-user
            binding) are NOT stamped — when ``expected_user_id`` is
            None this function is a no-op for them.
        expected_user_id: The user_id the caller asked for. None in
            stdio mode (no auth context); a real ``sub`` value in
            HTTP / multi-tenant mode.

    Behavior:
        - ``expected_user_id is None`` (stdio mode): no-op. The
          operator's cached creds aren't tenant-bound; the assertion
          would always fail there and is meaningless anyway because
          stdio is single-tenant by construction.
        - ``expected_user_id`` set + creds stamp matches: returns
          silently.
        - ``expected_user_id`` set + stamp ABSENT: logs a warning
          (the stamp should always be present in HTTP mode; absence
          means someone bypassed ``get_credentials_for_user`` and
          built creds another way). Returns without raising — the
          absent stamp doesn't prove a cross-tenant bug, only that
          the defensive check can't run. Treat it as "monitoring
          gap detected" rather than "incident."
        - ``expected_user_id`` set + stamp MISMATCH: raises
          ``TenantIsolationError`` immediately. This is the
          load-bearing assertion: a future caching bug or storage-
          layer bug that returns Alice's creds when Bob was
          requested fires here BEFORE any user data is touched.

    The contract: this function is called automatically by
    ``_get_credentials`` so every tool that goes through the
    standard envelope gets the check for free. Tools that bypass
    the standard envelope (the ``creds=False`` opt-outs) are
    expected to call it themselves at their credential-resolution
    site if they want the same guarantee.
    """
    if expected_user_id is None:
        # Stdio mode: no per-tenant binding to check. Returning
        # without comment matches the "stdio is single-tenant"
        # design intent.
        return

    stamped = getattr(creds, "_google_docs_mcp_user_id", None)

    if stamped is None:
        # Stamp absent. Could mean: (a) creds built outside
        # get_credentials_for_user (bypass), (b) future refactor
        # dropped the stamp accidentally. Log + continue — the
        # absence is a monitoring gap, not proof of cross-tenant
        # leak.
        _isolation_log.warning(
            "tenant_isolation: creds for expected_user_id=%s... "
            "lack the tenant stamp (monitoring gap; check whether "
            "creds were resolved outside get_credentials_for_user)",
            expected_user_id[:8] if expected_user_id else "-",
        )
        return

    if stamped != expected_user_id:
        # The load-bearing assertion. A mismatch means the
        # credential dispatch layer returned the WRONG tenant's
        # creds. Treat as a security incident — log explicitly +
        # raise immediately so no downstream tool touches the
        # wrong user's data.
        _isolation_log.error(
            "tenant_isolation: MISMATCH stamped=%s... expected=%s... — "
            "refusing to dispatch cross-tenant creds",
            stamped[:8] if stamped else "-",
            expected_user_id[:8] if expected_user_id else "-",
        )
        raise TenantIsolationError(
            f"tenant isolation breach: credentials stamped for a "
            f"different user_id than the caller requested. This is a "
            f"defensive check; if you see this in production, treat "
            f"it as a security incident and audit the call chain "
            f"between get_credentials_for_user and this assertion."
        )


def _get_credentials():
    """Return valid Google API Credentials for the caller.

    Two modes, transparently:

    - **HTTP / multi-tenant** (FastMCP has an auth provider, calling
      user identified by ``get_access_token().claims["sub"]``):
      resolve via ``credentials.get_credentials_for_user``. Refreshes
      per-user, persists back to user_store. On NeedsReauthError,
      raises ToolError with a Markdown link to the consent URL — the
      Claude client renders the URL as clickable. PR-Δ5: every
      returned credentials object is verified via ``assert_tenant_match``
      to confirm the storage layer didn't accidentally hand back a
      different tenant's creds — belt-and-suspenders against future
      caching / SQL bugs.

    - **Stdio / single-tenant** (no auth context, local trust model):
      operator's cached OAuth token at ``~/.google-docs-mcp/token.json``,
      lazy-loaded and cached in-process. Preserves the v1.0 stdio
      experience bit-for-bit. PR-Δ5: ``assert_tenant_match`` is a
      no-op in stdio mode (expected_user_id is None) so the check
      is correctly skipped for the single-tenant path.

    The mode branch is observable via ``current_user_id_or_none()``
    returning a value vs None. Until Phase 7 wires GoogleProvider, the
    HTTP path is dormant and all callers fall into the stdio branch.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        global _creds_cache
        if _creds_cache is None or not _creds_cache.valid:
            _creds_cache = load_credentials(default_data_dir())
        # Stdio path: assert_tenant_match no-ops when user_id is None,
        # but we call it explicitly to document the invariant + ensure
        # the call-site discipline stays uniform (so a future stdio→
        # multi-tenant refactor can't accidentally drop the check).
        assert_tenant_match(_creds_cache, user_id)
        return _creds_cache

    try:
        creds = get_credentials_for_user(
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

    # PR-Δ5: defensive tenant-match check on every HTTP-mode dispatch.
    # The storage layer is the source of truth; this assertion catches
    # a future bug where storage returns the wrong tenant's row.
    # Raises TenantIsolationError on mismatch (subclass of
    # AssertionError) — the @workspace_tool envelope lets it propagate
    # rather than translating to a user-facing 400, so cross-tenant
    # leaks fail loudly rather than silently flowing downstream.
    assert_tenant_match(creds, user_id)
    return creds


def _format_http_error(e: HttpError) -> str:
    return friendly_http_error_message(e)
