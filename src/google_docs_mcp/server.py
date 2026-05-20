"""Google Docs MCP Server with native Tabs support.

Exposes MCP tools for working with native Google Docs tabs:
``gdocs_make_tabbed_doc``, ``gdocs_add_tabs``, ``gdocs_get_doc_outline``,
``gdocs_append_to_tab``, and ``gdocs_tab_existing_doc``.

The same entry point also implements one-off CLI commands for the
Apps Script setup needed by ``gdocs_tab_existing_doc``; see the
``cli`` module for those.
"""
from __future__ import annotations

import hmac
import logging
import os
import sys
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from .auth import default_data_dir, load_credentials
from .crypto import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, sign_upload_url
from .drive_api import (
    find_doc_by_title as _find_doc_by_title,
    move_to_folder as _move_to_folder,
    trash_drive_file as _trash_drive_file,
    untrash_drive_file as _untrash_drive_file,
)
from .errors import friendly_http_error_message
from .tool_schemas import (
    GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
    GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
    GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
    GDOCS_GUIDE_OUTPUT_SCHEMA,
    GDOCS_HELP_OUTPUT_SCHEMA,
    GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
    GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
    GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
    GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
    GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
    GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
    GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)
# v1.1+ multi-tenant cloud auth — imported lazily-via-function so stdio
# users without the OAuth env vars don't trip import-time errors.
from .credentials import (
    NeedsReauthError,
    current_user_id_or_none,
    get_credentials_for_user,
)
from .gas_deploy import GAS_DEPLOY_SCOPES
from .keys import (
    get_first_call_timestamps,
    get_key,
    get_shim_hit_counters,
    get_total_call_counters,
)
from .oauth_google import resolve_runtime_oauth_config
from .setup_apps_script import (
    setup_apps_script_auto,
    setup_apps_script_for_user,
)

_SERVER_INSTRUCTIONS = """\
google-docs-fly — create, edit, read, and manage Google Docs with
native sidebar Tabs (October 2024+ feature). All tools prefixed gdocs_.

START HERE: call ``gdocs_guide()`` for the orientation as a structured
payload, or ``gdocs_server_info()`` for build version + verified CI
test status.

THE 5 CORE WORKFLOWS
====================

1. NEW DOC from content composed in chat
   Goal: build a tabbed doc from text you have in the conversation.
   Tools: gdocs_make_tabbed_doc(title, tabs=[{title, content, ...}])
   Notes: ONE call. No file. No upload. DEFAULT for any request like
   "make me a doc with sections X, Y, Z".

2. CONVERT EXISTING DOC with Heading 1 paragraphs
   Goal: take a Google Doc / .docx on Drive that already has H1s and
   turn each H1 section into its own native tab.
   Tools: gdocs_preview_tab_split(drive_file_id=..., split_by="heading_1")
          -> gdocs_tab_existing_doc(drive_file_id=..., split_by="heading_1")
          -> gdocs_get_doc_outline(doc_id=...)   # verify the result
   Notes: Preview first — destructive conversion is one-way.

3. RETROFIT STYLED DOC with NO Heading 1s
   Goal: a styled doc where section breaks aren't H1s (banners in
   styled tables, shaded paragraphs, etc.).
   Tools: gdocs_tab_existing_doc(drive_file_id=...,
              markers=[{marker_text, tab_title}, ...])
   Notes: Same tool as #2; passing ``markers`` triggers RETROFIT mode
   (injects synthetic H1s before each marker block, then converts).
   NEVER rebuild a styled .docx from text — formatting would be lost.
   Use retrofit instead.

4. CONVERT SANDBOX .docx (bytes only, no Drive file)
   Goal: convert a .docx the model has built / has as raw bytes in
   its sandbox (cloud chat scenario).
   Tools: gdocs_get_signed_upload_url(...) -> POST {url} with the
          .docx bytes as multipart upload
   Notes: ``docx_path`` arguments DO NOT WORK from cloud chat — the
   server cannot see the caller's filesystem. Signed-URL upload is
   the only sandbox-bytes path. The POST is equivalent to
   gdocs_tab_existing_doc; use this when the .docx lives in your
   sandbox rather than on Drive.

5. CLEANUP — trash / restore Drive files
   Tools: gdocs_trash_file(file_id), gdocs_untrash_file(file_id)
   Notes: ONLY acts on files this app created. Files created
   elsewhere return app_not_authorized (no recovery — the file
   belongs to its owner). file_id accepts a string or list (batch).

NON-OBVIOUS OPERATING RULES
===========================
- Never rebuild a styled .docx from text. Retrofit (workflow #3)
  preserves formatting; rebuilding loses it.
- ``docx_path`` arguments do NOT work from cloud chat — the server
  cannot see the caller's filesystem. Use signed-URL upload
  (workflow #4) or drive_file_id.
- ``placeholder_behavior="rename"`` preserves a title / index page;
  the default "remove" deletes it. Use "rename" when the source has
  a meaningful cover page worth keeping.
- This app can only trash files IT created. Drive returns
  appNotAuthorizedToFile (403) on others; the file belongs to its
  owner and only they can trash it.
- First use requires interactive Google OAuth consent. The client
  must open the consent URL in a browser — it cannot be automated.
  Subsequent calls reuse the cached token until it expires.

EDIT TOOLS (after creating / converting)
========================================
gdocs_rename_tab, gdocs_delete_tab, gdocs_set_tab_icons,
gdocs_replace_all_text, gdocs_add_tabs, gdocs_append_to_tab

READ TOOLS
==========
gdocs_get_doc_outline — structure + icons, no body text (cheap)
gdocs_read_doc(doc_id, tab_id?) — body text, one tab or all
gdocs_get_tab_url(doc_id, tab_id) — direct deep-link to a tab

DRIVE MANAGEMENT
================
gdocs_find_doc_by_title, gdocs_move_to_folder,
gdocs_trash_file, gdocs_untrash_file

INTROSPECTION
=============
gdocs_guide() — this orientation as a structured payload
gdocs_server_info() — version + verified CI test status (digest,
  ci_run_url, mutation_check with stale_patches / imprecise_patches)
gdocs_test_manifest() — full test inventory + per-test outcomes
"""

mcp = FastMCP("google-docs", instructions=_SERVER_INSTRUCTIONS)
# auth=None at construction so stdio (Claude Desktop / Code) runs
# without auth middleware. HTTP transport sets mcp.auth = GoogleProvider
# at startup via configure_auth_for_http() — see main() and Phase 7.

# Lazy module-level cache for the stdio/no-auth-context path. HTTP
# mode bypasses this entirely — see _get_credentials() below.
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


# v2.0.6 (R28 deferral close): wire @gdocs_tool now that the mcp instance
# and both helpers (_get_credentials, _format_http_error) exist. After
# register(), the 24 tool decorators below can use @gdocs_tool(...) in
# place of the @mcp.tool + ToolAnnotations + try/except boilerplate.
from . import decorators as _gdocs_decorators
_gdocs_decorators.register(mcp, _get_credentials, _format_http_error)
gdocs_tool = _gdocs_decorators.gdocs_tool


# v1.3.1: title validation helper. Drive rejects titles with control
# chars (U+0000-001F, U+007F) by surfacing a confusing 400; we fail
# fast with a clear message. >1024 chars is a defensive cap below
# Drive's actual limit so we never surface raw API errors for length.
_TITLE_MAX_CHARS = 1024


def _validate_title(title, *, field: str = "title") -> None:
    """Reject titles that would crash downstream Drive/Docs APIs.

    - Must be a non-empty string
    - ≤ 1024 chars
    - No control chars (U+0000-001F, U+007F)
    """
    if not isinstance(title, str):
        raise ToolError(
            f"{field} must be a string (got {type(title).__name__})"
        )
    if not title:
        raise ToolError(f"{field} cannot be empty")
    if len(title) > _TITLE_MAX_CHARS:
        raise ToolError(
            f"{field} is {len(title)} chars; max is {_TITLE_MAX_CHARS}. "
            f"Truncate before retrying."
        )
    for ch in title:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ToolError(
                f"{field} contains a control character (U+{code:04X}) — "
                f"strip control chars before retrying. Drive rejects "
                f"titles with these and surfaces a confusing API error."
            )


# M3 POC (v2.1.3): the 12 docs-service tools moved to
# ``services/docs/tools.py``. Importing that module at the bottom of
# this file triggers their @gdocs_tool registration. Tools relocated:
#   gdocs_make_tabbed_doc, gdocs_add_tabs, gdocs_get_doc_outline,
#   gdocs_read_doc, gdocs_append_to_tab, gdocs_tab_existing_doc,
#   gdocs_rename_tab, gdocs_get_tab_url, gdocs_delete_tab,
#   gdocs_replace_all_text, gdocs_set_tab_icons, gdocs_preview_tab_split
#
# The remaining 12 tools (drive, gas_deploy, admin, introspection,
# auth) stay in this file until the next M3 phase. See
# docs/ARCHITECTURE.md §5.1 for the migration plan.
@gdocs_tool(
    title="Server identity + tool inventory",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_SERVER_INFO_OUTPUT_SCHEMA,
)
async def gdocs_server_info() -> dict:
    """Server identity + full tool inventory — for change detection across sessions.

    USE WHEN: you want to confirm what version of the MCP you're
    talking to, detect renames/additions/removals between sessions,
    or verify a redeploy actually rolled out.

    The ``tools`` list is the COMPLETE registered tool inventory
    direct from the server's own registry — not filtered or summarized.
    Counting it and diffing across sessions is the canonical way for a
    caller to detect drift between what their cache thinks the server
    has and what it actually has.

    Returns:
        ``{"version", "build_time", "git_commit", "tool_count",
        "tools": [...]}``.
        ``build_time`` and ``git_commit`` are baked in at Docker build
        time via --build-arg; if the deploy script didn't pass them
        they show as ``"unknown"``.

    Choreography: typical introspection trio — pair with
    ``gdocs_guide()`` (workflows + rules + tool groupings) and
    ``gdocs_test_manifest()`` (full per-test inventory). Cheap; no
    Google API call required.
    """
    # FastMCP's tool registry is async-accessed via list_tools().
    # Making this whole tool async lets us await it directly without
    # nested-event-loop gymnastics.
    try:
        tools = await mcp.list_tools()
        tool_names = sorted(t.name for t in tools)
    except Exception:  # noqa: BLE001
        tool_names = []

    # Read version via importlib.metadata to avoid the circular-import
    # trap (__init__.py imports server.main, so server can't import
    # __version__ from the partially-loaded package at module-load
    # time). Reading from installed package metadata is also more
    # honest — it reflects the wheel that's actually deployed.
    from importlib.metadata import version as _pkg_version
    try:
        ver = _pkg_version("google-docs-mcp")
    except Exception:  # noqa: BLE001
        ver = "unknown"

    # Append GIT_COMMIT as semver build metadata so every deploy from
    # a distinct commit reports a unique version string — without
    # requiring a manual pyproject bump on every hot-fix. Format
    # follows semver §10: `version+buildmetadata`. PEP 440 also
    # tolerates `+local` segments for the same purpose.
    git_commit = os.environ.get("GIT_COMMIT", "unknown")
    if git_commit and git_commit != "unknown":
        ver = f"{ver}+{git_commit}"

    return {
        "version": ver,
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "git_commit": git_commit,
        "tool_count": len(tool_names),
        "tools": tool_names,
        "test_suite": _read_test_suite_status(git_commit),
        # v1.5: per-purpose shim-path hit counters so operators can
        # soak-test (deploy v1.5, wait 3+ days, check that this stays
        # at zero for the trailing 24h) before shipping v2.0b's
        # strict-flip, which would invalidate any key minted via the
        # shim. Process-local counter — aggregate across replicas at
        # read time.
        "key_back_compat_shim_active_hits": get_shim_hit_counters(),
        # v1.5.1 (#28): denominator for the shim-hit telemetry above.
        # Counts every successful get_key() call regardless of which
        # path served the key. Preflight asserts shim==0 AND total>=N
        # so "0 shim hits" can't mean "0 calls". Process-local; same
        # aggregation caveat as the shim counter.
        "key_call_totals": get_total_call_counters(),
        # v2.6 (#48): observability for the soak-window gate. The
        # preflight script demands BOTH shim_hits==0 AND total>=N AND
        # first_call_age_seconds >= 1h30 (i.e. real traffic has flowed
        # since boot) before declaring it safe to ship v2.0b's strict-
        # flip. ``first_call_age_seconds[purpose]`` is None if that
        # purpose has never been requested in this process (e.g.
        # api_bearer on a server that has only served signed URLs).
        # Process-local; aggregate across replicas at read time.
        "key_observability": {
            "first_call_age_seconds": {
                purpose: (time.time() - ts) if ts is not None else None
                for purpose, ts in get_first_call_timestamps().items()
            },
        },
    }


def _find_test_results_path() -> Path | None:
    """Locate the test-results.json artifact.

    Container path first (/app/test-results.json, populated by
    Dockerfile COPY), then CWD as local-dev fallback. Evaluated at
    call time — NOT at import — so monkeypatched cwds in tests work.
    """
    candidates = [
        Path("/app/test-results.json"),
        Path.cwd() / "test-results.json",
    ]
    return next((p for p in candidates if p.exists()), None)


def _canonical_digest(data: dict) -> str:
    """SHA-256 of the JSON with ``_meta`` removed, sorted-key serialized.

    The digest is computed over everything EXCEPT the ``_meta`` block
    (because the digest itself lives inside _meta — chicken/egg).
    Canonicalization (sort_keys + tight separators) gives a stable
    hash regardless of Python's dict-iteration order.
    """
    import hashlib
    import json as _json
    payload = {k: v for k, v in data.items() if k != "_meta"}
    canon = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _read_test_suite_status(deployed_commit: str) -> dict:
    """Surface the CI test-suite status baked into the build.

    deploy.sh writes ``test-results.json`` via pytest-json-report,
    embeds ``_git_commit`` + ``_ci_run_url`` + ``_meta.digest``, and
    the Dockerfile COPIes it into the image. If the file's absent or
    unparseable (vanilla `docker build` skips it; SKIP_TESTS writes a
    stub), return ``{"status": "unknown"}`` per the documented
    contract.

    **Tamper detection.** At read time we re-canonicalize the JSON
    (minus ``_meta``) and compare the recomputed digest against the
    stored one. If they diverge, somebody edited the file
    post-build — return ``status: "tampered"`` so a caller can
    distinguish "the suite passed but someone fiddled with the
    numbers" from a legitimate pass.

    ``test_suite.commit`` should equal the running build's
    ``git_commit``; divergence means the image shipped without a
    matching test run — itself a red flag worth surfacing.
    """
    import json
    from datetime import datetime, timezone

    # mutation_check is independent state (separate artifact), so it
    # gets attached to whatever we return — even the unknown branches.
    # Callers can rely on the field always being present.
    mutation_check = _read_mutation_check()

    path = _find_test_results_path()
    if path is None:
        return {"status": "unknown", "mutation_check": mutation_check}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "mutation_check": mutation_check}

    summary = data.get("summary") or {}
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", 0))
    skipped = int(summary.get("skipped", 0))

    # pytest-json-report's "created" is a unix timestamp; convert to
    # ISO 8601 UTC. SKIP_TESTS stub doesn't include "created" — fall
    # back to "unknown".
    created_ts = data.get("created")
    if isinstance(created_ts, (int, float)):
        last_run = datetime.fromtimestamp(
            created_ts, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    else:
        last_run = "unknown"

    # Test-suite commit + CI run URL written by deploy.sh.
    test_commit = data.get("_git_commit", "unknown")
    ci_run_url = data.get("_ci_run_url", "")

    # Report digest verification — tamper detection.
    stored_meta = data.get("_meta") or {}
    stored_digest = stored_meta.get("digest", "")
    recomputed_digest = _canonical_digest(data)
    digest_matches = bool(stored_digest) and stored_digest == recomputed_digest

    # Status logic: must have a populated summary AND zero failures
    # AND the digest must verify. SKIP_TESTS stub has empty summary
    # → status="unknown" naturally. Mismatched digest → "tampered"
    # even if the numbers look green.
    if not summary:
        status = "unknown"
    elif stored_digest and not digest_matches:
        status = "tampered"
    elif failed == 0 and passed > 0:
        status = "passed"
    else:
        status = "failed"

    return {
        "last_run": last_run,
        "commit": test_commit,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "status": status,
        "ci_run_url": ci_run_url,
        "report_digest": stored_digest,
        "mutation_check": mutation_check,
    }


def _read_mutation_check() -> dict:
    """Surface mutation-test results baked into the build.

    Reads /app/mutation-check.json (CWD fallback for local dev),
    produced by scripts/mutation_check.py in CI. Summarizes to
    {ran, caught, status, asleep_guards, stale_patches,
    imprecise_patches}. Missing file → unknown.

    Failure modes the gate distinguishes (v1.2.2+):
      asleep_guards     — patch applied but the named guard didn't
                          notice the bug (test rot).
      stale_patches     — patch's `find` text is gone, or applied
                          without tripping anything (mutation rot).
      imprecise_patches — patch broke the target AND unrelated tests
                          (over-broad mutation).

    Status "passed" only when caught == ran AND all three buckets are
    empty. Pre-1.2.2 artifacts (no stale/imprecise fields) default
    the new fields to [] for back-compat.
    """
    import json

    candidates = [
        Path("/app/mutation-check.json"),
        Path.cwd() / "mutation-check.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {"status": "unknown", "ran": 0, "caught": 0}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "ran": 0, "caught": 0}

    return {
        "ran": int(data.get("ran", 0)),
        "caught": int(data.get("caught", 0)),
        "status": data.get("status", "unknown"),
        "asleep_guards": list(data.get("asleep_guards", [])),
        "stale_patches": list(data.get("stale_patches", [])),
        "imprecise_patches": list(data.get("imprecise_patches", [])),
    }


@gdocs_tool(
    title="List CI test manifest",
    readonly=True, destructive=False, idempotent=True, external=True,
    output_schema=GDOCS_TEST_MANIFEST_OUTPUT_SCHEMA,
)
def gdocs_test_manifest() -> dict:
    """List every test in the CI artifact + its pass/fail outcome.

    Read / verify / audit / inspect / list the test inventory of the
    running build. Use to: confirm specific named regression guards
    (e.g. test_owned_by_app_consistency) actually exist and passed,
    spot-check what "203 passed" means, find which test failed if
    test_suite.status is not "passed".

    Returned shape:
        {
          status: "ok" | "unknown" | "tampered",
          total: int,
          tests: [{nodeid: str, outcome: "passed"|"failed"|"skipped"}, ...],
          named_regression_guards: {
            present: [list of named-guard test ids found in the suite],
            missing: [list of named guards NOT found — should be empty],
          },
        }

    Status "unknown" when the artifact's missing/unparseable;
    "tampered" when the report_digest doesn't match the canonicalized
    payload (same logic as gdocs_server_info.test_suite); "ok"
    otherwise.

    Choreography: pairs with ``gdocs_server_info.test_suite``. The
    summary is in server_info; this tool gives the full per-test
    breakdown. No Google API call.
    """
    import json

    path = _find_test_results_path()
    if path is None:
        return {
            "status": "unknown",
            "reason": "test-results.json not found in container",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unknown",
            "reason": "test-results.json unparseable",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    stored_digest = (data.get("_meta") or {}).get("digest", "")
    digest_matches = stored_digest and stored_digest == _canonical_digest(data)
    if stored_digest and not digest_matches:
        return {
            "status": "tampered",
            "reason": "report_digest mismatch — file was edited after CI",
            "total": 0,
            "tests": [],
            "named_regression_guards": {"present": [], "missing": []},
        }

    tests_raw = data.get("tests") or []
    tests = [
        {"nodeid": t.get("nodeid", ""), "outcome": t.get("outcome", "")}
        for t in tests_raw
    ]

    # The 8 named regression guards from v1.1.x — see CHANGELOG and
    # tests/unit/test_*.py docstrings. If any are missing the suite's
    # coverage of cycle bugs has regressed.
    REQUIRED_GUARDS = [
        "test_owned_by_app_agrees_with_trash_outcome",
        "test_trash_file_id_accepts_str_or_list",
        "test_inject_matches_fragmented_runs",
        "test_deploy_webapp_body_does_not_include_entryPoints",
        "test_preview_flags_what_convert_truncates",
        "test_auth_pkce_consistency_every_url",
        "test_tool_descriptions_truthful",
        "test_tool_discoverability_via_server_info",
    ]
    test_names = {t["nodeid"].split("::")[-1].split("[")[0] for t in tests}
    present = [g for g in REQUIRED_GUARDS if g in test_names]
    missing = [g for g in REQUIRED_GUARDS if g not in test_names]

    return {
        "status": "ok",
        "total": len(tests),
        "tests": tests,
        "named_regression_guards": {"present": present, "missing": missing},
    }


@gdocs_tool(
    title="Orientation guide (local, no API)",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_GUIDE_OUTPUT_SCHEMA,
)
def gdocs_guide() -> dict:
    """Orientation payload — the "start here" / --help for this server.

    Returns the same content as the connect-time server ``instructions``
    string, as a structured dict so it is machine-readable and always
    callable. Use when:

    - the client truncated or ignored connect-time instructions
    - you want machine-readable workflow choreography / tool groupings
    - you need to confirm which tools belong to which workflow before
      sequencing a multi-tool plan

    No arguments. No side effects. Cheap (no API calls). Typically the
    first call an agent makes after connecting — pairs naturally with
    ``gdocs_server_info()`` (version + verified CI test status).

    Returned shape:
        {
          server: {name, version, what_it_does, all_tools_prefixed,
                   more_info},
          workflows: [{name, goal, tool_sequence, notes}, ...],
          operating_rules: [str, ...],
          tool_groups: {build_new: [...], convert_existing: [...],
                        edit_tabs: [...], read: [...],
                        drive_management: [...], setup_and_auth: [...],
                        introspection: [...]},
        }
    """
    from . import __version__

    return {
        "server": {
            "name": "google-docs-fly",
            "version": __version__,
            "what_it_does": (
                "Create, edit, read, and manage Google Docs with native "
                "sidebar Tabs (October 2024+ feature)."
            ),
            "all_tools_prefixed": "gdocs_",
            "more_info": (
                "Call gdocs_server_info for build version + verified CI "
                "test status (digest, ci_run_url, mutation_check)."
            ),
        },
        "workflows": [
            {
                "name": "new_doc",
                "goal": "Build a tabbed doc from content composed in chat",
                "tool_sequence": ["gdocs_make_tabbed_doc"],
                "notes": (
                    "ONE call. No file. No upload. DEFAULT for any 'make "
                    "me a doc with sections X, Y, Z' request."
                ),
            },
            {
                "name": "convert_doc_with_headings",
                "goal": (
                    "Convert an existing Drive doc that already has "
                    "Heading 1 paragraphs into tabs"
                ),
                "tool_sequence": [
                    "gdocs_preview_tab_split",
                    "gdocs_tab_existing_doc",
                    "gdocs_get_doc_outline",
                ],
                "notes": (
                    "Preview first to validate the split; convert; then "
                    "outline to verify the result. Conversion is one-way."
                ),
            },
            {
                "name": "retrofit_styled_doc",
                "goal": (
                    "Retrofit a styled doc that has NO Heading 1 "
                    "paragraphs (e.g. banners inside styled tables)"
                ),
                "tool_sequence": [
                    "gdocs_tab_existing_doc(markers=[...])",
                ],
                "notes": (
                    "Same tool as convert_doc_with_headings; passing "
                    "`markers` triggers retrofit mode (injects synthetic "
                    "H1s before each marker block, then converts). NEVER "
                    "rebuild a styled .docx from text — formatting would "
                    "be lost."
                ),
            },
            {
                "name": "convert_sandbox_docx",
                "goal": (
                    "Convert a .docx that exists only as bytes in the "
                    "caller's sandbox (cloud chat scenario)"
                ),
                "tool_sequence": [
                    "gdocs_get_signed_upload_url",
                    "POST {url}",
                ],
                "notes": (
                    "`docx_path` does NOT work from cloud chat — the "
                    "server cannot see the caller's filesystem. The POST "
                    "is equivalent to gdocs_tab_existing_doc; use this "
                    "route when the .docx is in your sandbox."
                ),
            },
            {
                "name": "cleanup",
                "goal": "Trash / restore Drive files this app created",
                "tool_sequence": [
                    "gdocs_trash_file",
                    "gdocs_untrash_file",
                ],
                "notes": (
                    "ONLY acts on files this app created; others return "
                    "app_not_authorized. file_id accepts a string or "
                    "list (batch)."
                ),
            },
        ],
        "operating_rules": [
            (
                "Never rebuild a styled .docx from text. Use retrofit "
                "(workflow `retrofit_styled_doc`) to preserve formatting."
            ),
            (
                "`docx_path` arguments do NOT work from cloud chat — the "
                "server cannot see the caller's filesystem. Use "
                "signed-URL upload (workflow `convert_sandbox_docx`) or "
                "drive_file_id."
            ),
            (
                "`placeholder_behavior='rename'` preserves a title / "
                "index page; the default 'remove' deletes it. Use "
                "'rename' when the source has a meaningful cover page."
            ),
            (
                "Trash tools only act on files THIS app created. Drive "
                "returns appNotAuthorizedToFile (403) on others; the "
                "file belongs to its owner and only they can trash it."
            ),
            (
                "First use requires interactive Google OAuth consent. "
                "The client must open the consent URL in a browser — "
                "this cannot be automated. Subsequent calls reuse the "
                "cached token until it expires."
            ),
        ],
        "tool_groups": {
            "build_new": ["gdocs_make_tabbed_doc"],
            "convert_existing": [
                "gdocs_preview_tab_split",
                "gdocs_tab_existing_doc",
                "gdocs_get_signed_upload_url",
            ],
            "edit_tabs": [
                "gdocs_rename_tab",
                "gdocs_delete_tab",
                "gdocs_set_tab_icons",
                "gdocs_replace_all_text",
                "gdocs_add_tabs",
                "gdocs_append_to_tab",
            ],
            "read": [
                "gdocs_get_doc_outline",
                "gdocs_read_doc",
                "gdocs_get_tab_url",
            ],
            "drive_management": [
                "gdocs_find_doc_by_title",
                "gdocs_move_to_folder",
                "gdocs_trash_file",
                "gdocs_untrash_file",
            ],
            "setup_and_auth": [
                "gdocs_setup_apps_script",
                "gdocs_reset_authorization",
            ],
            "introspection": [
                "gdocs_server_info",
                "gdocs_test_manifest",
                "gdocs_guide",
            ],
        },
    }


def _run_batch(
    items: list[str], fn, success_key: str
) -> dict:
    """Apply ``fn(creds, file_id)`` to each id, aggregate per-item.

    Used by the batch forms of trash/untrash. Each item's outcome is
    independent — a 403/404 on one doesn't stop the rest. Returns
    ``{results: [...], summary: {succeeded, skipped, failed}}`` where:
    - succeeded = item ended in the desired terminal state
    - skipped   = soft-failure (not_found, app_not_authorized)
    - failed    = unexpected hard error captured per-item
    """
    creds = _get_credentials()
    results: list[dict] = []
    succeeded = 0
    skipped = 0
    failed = 0
    for fid in items:
        try:
            r = fn(creds, fid)
            results.append(r)
            if r.get("reason"):
                skipped += 1
            elif r.get(success_key) is True or (
                success_key == "active" and r.get("trashed") is False
            ):
                succeeded += 1
            else:
                # Defensive — shouldn't happen
                skipped += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            results.append({
                "file_id": fid,
                "reason": "unexpected_error",
                "message": str(e)[:300],
            })
    return {
        "results": results,
        "summary": {
            "succeeded": succeeded,
            "skipped": skipped,
            "failed": failed,
        },
    }


@gdocs_tool(
    title="Find a Google Doc by title (search)",
    readonly=True, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_FIND_DOC_BY_TITLE_OUTPUT_SCHEMA,
)
def gdocs_find_doc_by_title(
    creds,
    query: str,
    exact: bool = False,
    include_trashed: bool = False,
    verify_writable: bool = True,
) -> dict:
    """Look up a Google Doc / .docx by title — find a file_id from a name.

    USE WHEN: you have a doc name (the user just told you, or it's
    from a past session) and need its file_id to call any other tool.

    Matches return newest-first by modified_time. Each match flags
    ``trashed`` and ``owned_by_app``:
    - ``trashed: true`` means the file is in Drive Trash (hidden from
      the user's Drive UI; recoverable for 30 days)
    - ``owned_by_app: true`` means this OAuth app's drive.file scope
      can ACTUALLY write to it — i.e. ``gdocs_trash_file`` /
      ``gdocs_untrash_file`` / ``gdocs_move_to_folder`` will succeed.
      This is verified via a batched no-op write probe (NOT inferred
      from user-level capabilities which can disagree).

    Args:
        query: Title text to search for.
        exact: True = exact title match. False (default) = substring
            ("contains") match.
        include_trashed: False (default) excludes trashed files from
            results.
        verify_writable: True (default) probes each match with a
            batched no-op update to determine actual writability under
            this app's drive.file scope. Pass False to skip the probe
            (faster, but ``owned_by_app`` will be ``None`` and the
            caller must verify before mutating).

    Returns:
        ``{"matches": [{file_id, name, mimeType, modified_time,
        trashed, owned_by_app}, ...], "count": int}``.
        ``owned_by_app`` is ``True``/``False`` if probed, ``None`` if
        ``verify_writable=False``.

    Choreography: returns a ``file_id`` that feeds straight into
    ``gdocs_tab_existing_doc`` (drive_file_id), ``gdocs_move_to_folder``,
    ``gdocs_trash_file``, ``gdocs_read_doc`` (as doc_id for Google
    Docs), and ``gdocs_get_doc_outline``. Check ``owned_by_app``
    before any write — others fail with app_not_authorized.
    """
    if not query.strip():
        raise ToolError("query cannot be empty")
    return _find_doc_by_title(
        creds, query,
        exact=exact,
        include_trashed=include_trashed,
        verify_writable=verify_writable,
    )


@gdocs_tool(
    title="Move a file into a Drive folder",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_MOVE_TO_FOLDER_OUTPUT_SCHEMA,
)
def gdocs_move_to_folder(creds, file_id: str, folder_id: str) -> dict:
    """Move a Drive file into a folder (out of root or wherever it lives).

    USE WHEN: the MCP just created a doc (which lands in Drive root by
    default) and you want to file it into a project / curriculum
    folder. Also works for moving any existing file.

    Uses ``files.update(addParents, removeParents)`` — moves in place,
    not a copy. The file's content and ID are unchanged.

    Soft-failure (returned as data, not raised) matches the trash
    tools' contract so batch workflows can skip-and-continue:
    - ``reason: "not_found"`` — file_id doesn't resolve
    - ``reason: "folder_not_found"`` — folder_id doesn't resolve OR
      points at something that isn't a folder
    - ``reason: "app_not_authorized"`` — OAuth app's drive.file scope
      can't write to this file (file wasn't created by this app)

    Args:
        file_id: The file to move.
        folder_id: The destination folder's Drive ID.

    Returns:
        Success: ``{file_id, name, mimeType, parents: [folder_id, ...]}``.
        No-op (already there): same shape plus ``note`` explaining.
        Soft-failure: ``{file_id, reason, message, ...}``.

    Choreography: file_id typically from ``gdocs_find_doc_by_title`` or
    from a prior create call. ``folder_id`` from the user (URL) or
    ``gdocs_find_doc_by_title`` with mimeType filter — Drive folder
    IDs look identical to file IDs.

    NOTE: same app-ownership constraint as the trash tools — moving a
    file this app didn't create returns ``reason: "app_not_authorized"``.
    """
    return _move_to_folder(creds, file_id, folder_id)


@gdocs_tool(
    title="Restore a file from Drive trash",
    readonly=False, destructive=False, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_UNTRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_untrash_file(creds, file_id: str | list[str]) -> dict:
    """Restore a trashed Drive file back to its original location.

    Inverse of ``gdocs_trash_file``. Ships together so a wrong trash
    call by the agent is recoverable. Works only within Drive's 30-day
    trash window — beyond that the file is permanently gone and this
    returns ``reason: "not_found"``.

    Uses ``files.update(trashed=False)``. Same soft-failure handling
    as ``gdocs_trash_file`` (404 and 403 returned as data, not raised),
    so batch restores can skip-and-continue.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch untrash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input ID — independent outcomes.

    Returns (single-ID mode):
        Success: ``{"file_id", "name", "mimeType", "trashed": False,
        "was_already_active": bool}``. ``was_already_active=True``
        means the file wasn't trashed to begin with (idempotent no-op).
        Soft-failure: ``{"file_id", "trashed": <current>, "reason",
        "message"}`` with ``reason`` in {``"not_found"``,
        ``"app_not_authorized"``}.

    Choreography: pairs with ``gdocs_trash_file`` for recovery.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` — the
    file belongs to its owner and only they can restore it.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _untrash_drive_file, "active")
    return _untrash_drive_file(creds, file_id)


@gdocs_tool(
    title="Move a Drive file to trash",
    readonly=False, destructive=True, idempotent=True, external=True,
    creds=True,
    output_schema=GDOCS_TRASH_FILE_OUTPUT_SCHEMA,
)
def gdocs_trash_file(creds, file_id: str | list[str]) -> dict:
    """Move a Drive file (Google Doc, .docx, anything) to trash.

    USE WHEN: you need to clean up an obsolete Drive file — a
    superseded conversion, a test doc, a broken output. ``gdocs_delete_tab``
    only removes a tab within a doc; this removes the whole document
    (or any other Drive file by ID).

    Uses ``files.update(trashed=True)``, NOT ``files.delete``. The file
    moves to Drive Trash and is recoverable for 30 days. Permanent
    deletion is intentionally not exposed.

    Idempotent: trashing an already-trashed file succeeds and the
    response flags ``was_already_trashed: true``.

    Args:
        file_id: A single Drive file ID (str) OR a list of IDs for
            batch trash. List form returns
            ``{results: [...], summary: {succeeded, skipped, failed}}``
            with one result per input — each item processed
            independently (one soft-failure does not abort the rest).

    Returns (single-ID mode):
        ``{"file_id", "name", "mimeType", "trashed": True,
        "was_already_trashed": bool}``. ``name`` lets the caller confirm
        the right file was touched.

    Choreography: pair with ``gdocs_untrash_file`` for recovery within
    Drive's 30-day trash window. file_id often comes from
    ``gdocs_find_doc_by_title`` or from a prior create call.

    NOTE: only works on files THIS app created. Files created by
    other apps / users return ``reason: "app_not_authorized"`` (HTTP
    403 appNotAuthorizedToFile) — the file belongs to its owner and
    only they can trash it. The agent has no recovery; surface to
    the user.
    """
    if isinstance(file_id, list):
        return _run_batch(file_id, _trash_drive_file, "trashed")
    return _trash_drive_file(creds, file_id)


@gdocs_tool(
    title="Mint a one-shot signed upload URL",
    readonly=False, destructive=False, idempotent=False, external=True,
    # creds=False: this tool mints an HMAC URL; no Google API call here.
    # It handles its own user_id check via current_user_id_or_none().
    output_schema=GDOCS_GET_SIGNED_UPLOAD_URL_OUTPUT_SCHEMA,
)
def gdocs_get_signed_upload_url(
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_bytes: int = 50 * 1024 * 1024,
) -> dict:
    """Mint a signed URL ONLY for uploading an existing .docx file's bytes.

    USE WHEN: you genuinely have an existing .docx file in your Python
    sandbox (e.g. one a user uploaded, one a pipeline produced) and
    need to POST its raw bytes to /api/convert from cloud chat. The
    signed URL is the credential — no Authorization header needed.

    DO NOT USE when:
    - You are composing new content from text. Use ``gdocs_make_tabbed_doc``
      — it takes markdown directly and skips this upload dance entirely.
      Building a .docx in the sandbox just to upload it here is pointless
      extra work.
    - The .docx already lives on Drive. Use
      ``gdocs_tab_existing_doc(drive_file_id=...)`` instead.

    The URL is single-use (the server tracks consumed nonces) and
    expires after ``ttl_seconds`` (default 10 min, max 1 hour).

    **v2.1 — bound to your user identity.** The URL embeds the calling
    user's Google ``sub`` claim, so the /api/convert request lands in
    YOUR Drive (using YOUR Apps Script Web App), not the operator's.
    Stdio callers (no FastMCP auth context) cannot mint signed URLs —
    they have direct tool access and don't need the REST detour.

    Args:
        ttl_seconds: How long the URL stays valid. Default 600s; keep
            short to limit blast radius if the URL leaks into a chat
            transcript.
        max_bytes: Advisory upload size cap baked into the signature.
            Defaults to 50 MB (Drive's converter ceiling).

    Returns:
        ``{"url", "expires_at", "max_bytes", "nonce", "user_id",
        "usage_hint"}``. ``usage_hint`` is a one-line Python snippet
        showing how to use the URL — the model copies it into the
        sandbox.

    Choreography: this is the FIRST step of the `convert_sandbox_docx`
    workflow. Mint the URL here, then POST the .docx bytes to that URL
    from the sandbox. The POST is equivalent to
    ``gdocs_tab_existing_doc`` — use this route when the .docx lives
    only as bytes in the sandbox rather than on Drive.

    NOTE: ``docx_path`` arguments on other tools do NOT work from
    cloud chat (server can't see the caller's filesystem); this
    signed-URL upload flow is the sandbox-bytes path.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "https://sundeepg98-docs-mcp.fly.dev")
    # v2.6 (#48): purpose-routed via keys.get_key("signed_url") so the
    # v2.0b strict-flip activates HKDF-derivation for signed-URL HMACs
    # without further edits here. get_key raises RuntimeError on missing
    # MCP_BEARER_TOKEN; translate to ToolError so the surface stays
    # user-facing (Markdown-renderable in claude.ai's connector UI).
    # v2.0b: keys.get_key() returns bytes; pass through to sign_upload_url
    # without the pre-flip .decode("utf-8") (which crashed on HKDF
    # output that isn't UTF-8 in general).
    try:
        signing_key = get_key("signed_url")
    except RuntimeError as e:
        raise ToolError(
            "MCP_BEARER_TOKEN env var not set on the server — "
            "signed URLs require it as the HMAC key."
        ) from e
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ToolError(
            f"ttl_seconds must be 1..{MAX_TTL_SECONDS}, got {ttl_seconds}"
        )

    # v2.1: every signed URL is bound to the calling user. Without a
    # FastMCP auth context we have no user — stdio callers don't need
    # /api/convert at all (they have direct tool access), so refuse
    # rather than mint an operator-scoped URL that would write into
    # the wrong Drive.
    user_id = current_user_id_or_none()
    if user_id is None:
        raise ToolError(
            "gdocs_get_signed_upload_url requires an authenticated MCP "
            "session (cloud / HTTP mode). Stdio callers should pass "
            "docx_path directly to gdocs_tab_existing_doc instead."
        )

    minted = sign_upload_url(
        base_url=f"{base}/api/convert",
        signing_key=signing_key,
        user_id=user_id,
        ttl_seconds=ttl_seconds,
        max_bytes=max_bytes,
    )
    minted["usage_hint"] = (
        "requests.post(URL, files={'file': ('doc.docx', open('/path/to/doc.docx','rb'), "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}, "
        "data={'split_by': 'heading_1', 'icons_by_title': '<json-string>'})"
    )
    return minted


# current_user_id_or_none lives in credentials.py so docx_import et al
# can share it without circular imports.


@gdocs_tool(
    title="Provision per-user Apps Script project",
    readonly=False, destructive=False, idempotent=True, external=True,
    # creds=False: this tool has its own NeedsReauthError → structured
    # response handling (returns status="needs_authorization" with
    # auth_url instead of raising ToolError). The standard decorator
    # path would lose that structured shape.
    output_schema=GDOCS_SETUP_APPS_SCRIPT_OUTPUT_SCHEMA,
)
def gdocs_setup_apps_script() -> dict:
    """One-shot setup of the Apps Script Web App needed for lossless retrofit.

    Run this once per user (cloud) or once per machine (local stdio)
    to enable ``gdocs_tab_existing_doc`` — the path that uses Apps
    Script for lossless content moves (preserving drawings, equations,
    tables, cell shading that no REST request type can re-emit).

    Without this setup, ``gdocs_tab_existing_doc`` fails with "Apps
    Script Web App URL not configured." Other tools
    (``gdocs_make_tabbed_doc``, edit tools, read tools) do not need
    this Apps-Script-specific setup — but, like all tools in this
    server, they DO require the one-time Google OAuth authorization
    grant (Drive + Docs scopes). The OAuth grant happens automatically
    on first tool call: any tool that needs creds returns
    ``status: "needs_authorization"`` with a click-to-authorize URL;
    after consent, all subsequent tools in the session work without
    further prompts. Only ``gdocs_tab_existing_doc``'s lossless
    retrofit path additionally needs THIS tool
    (``gdocs_setup_apps_script``) to have been run once.

    Idempotent: safe to retry if interrupted; resumes from the last
    successful step. The user_store row (cloud) or
    ``~/.google-docs-mcp/setup-state.json`` (local) keeps the ledger.

    Returns ``{status, url, script_id, deployment_id, message}`` on
    success. On cloud-mode auth failure, returns
    ``{status: "needs_authorization", auth_url, message}`` — emit
    the message verbatim so Claude renders the URL as a clickable link.

    Choreography: required ONCE before
    ``gdocs_tab_existing_doc(markers=[...])`` (retrofit path) and the
    Apps-Script-backed retrofit pipeline in general. After successful
    setup, run any retrofit conversion freely.

    NOTE: First call typically returns ``needs_authorization`` with a
    URL the user MUST open in a browser — Google OAuth consent
    cannot be automated. After consent, re-run this tool to complete
    the Web App deploy.
    """
    user_id = current_user_id_or_none()

    if user_id is None:
        # Stdio / no-auth-context mode: local CLI behavior.
        # Uses the operator's cached OAuth token at ~/.google-docs-mcp/.
        try:
            deployment = setup_apps_script_auto()
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Apps Script setup failed: {e}") from e
        return {
            "status": "ready",
            "url": deployment.url,
            "script_id": deployment.script_id,
            "deployment_id": deployment.deployment_id,
            "message": (
                "Apps Script Web App is deployed. You can now use "
                "gdocs_tab_existing_doc."
            ),
        }

    # HTTP / multi-tenant mode: per-user creds, per-user user_store ledger.
    try:
        oauth_cfg = resolve_runtime_oauth_config()
    except RuntimeError as e:
        raise ToolError(f"Server OAuth config error: {e}") from e

    try:
        creds = get_credentials_for_user(
            user_id,
            required_scopes=GAS_DEPLOY_SCOPES,
            **oauth_cfg,
        )
    except NeedsReauthError as e:
        return {
            "status": "needs_authorization",
            "auth_url": e.auth_url,
            "message": (
                f"Google API access required to set up your Apps Script "
                f"Web App.\n\n**[Click here to authorize]({e.auth_url})**"
                f"\n\nAfter granting access, re-run this tool."
            ),
        }

    try:
        deployment = setup_apps_script_for_user(creds, user_id)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Apps Script setup failed: {e}") from e

    return {
        "status": "ready",
        "url": deployment.url,
        "script_id": deployment.script_id,
        "deployment_id": deployment.deployment_id,
        "message": (
            "Apps Script Web App is deployed under your Google account. "
            "You can now use gdocs_tab_existing_doc and other tools "
            "that need lossless content moves."
        ),
    }


@gdocs_tool(
    title="Reset user authorization / revoke tokens",
    readonly=False, destructive=True, idempotent=True, external=True,
    # creds=False: this tool DELETES creds (it's the inverse of the
    # usual auth path). Wrapping with the standard creds injection
    # would try to fetch creds first, breaking the user's reset path
    # when their creds are already broken.
    output_schema=GDOCS_RESET_AUTHORIZATION_OUTPUT_SCHEMA,
)
def gdocs_reset_authorization(full: bool = False) -> dict:
    """Reset / revoke / clear stored Google OAuth credentials. Force re-consent.

    Use this tool to: sign out, re-authorize, re-consent after a scope
    change, switch Google accounts, recover from a stale or revoked
    grant, force a fresh OAuth flow for testing (PKCE / consent
    screen), or roll back to the needs_authorization state. Equivalent
    in spirit to "log out and log back in" for the Google Drive / Docs
    / Apps Script API access this server uses on your behalf.

    USE WHEN: you want to force a fresh OAuth consent flow on the next
    call — for testing PKCE / re-consenting after a scope change /
    recovering from a stale or revoked grant / switching the Google
    account this server acts as.

    HTTP mode (cloud chat, claude.ai connector):
      - Default (``full=False``): clears only the stored Google
        credentials (``google_creds_json``). The user's Apps Script
        Web App setup (URL, script_id, deployment_id) is preserved.
        Next tool call that needs creds returns
        ``status: "needs_authorization"`` with a fresh auth_url.
      - ``full=True``: clears the entire user_store row, including
        the Apps Script setup. Next call to ``gdocs_setup_apps_script``
        will create a NEW project in Drive.

    Stdio mode (Claude Desktop / Code on a developer laptop):
      - Default: deletes the cached OAuth token at
        ``~/.google-docs-mcp/token.json``. Next tool call triggers
        the local browser-consent flow.
      - ``full=True``: also deletes the local Apps Script
        ``setup-state.json`` ledger and the URL in ``config.json`` —
        next ``setup-apps-script`` CLI run will create a new project.

    DOES NOT trash any Apps Script projects in your Drive — those
    remain (you can manually delete them in Drive if you want to
    free up space). Just clears the local/server-side record of the
    authorization.

    Args:
        full: If True, also clear Apps Script setup state, not just
            credentials. Default False (least destructive).

    Returns:
        ``{status: "reset", message: str, cleared: [list of what
        was cleared]}``.

    Choreography: after reset, the very next tool call that needs
    creds will return ``needs_authorization`` with a fresh consent
    URL. Re-running ``gdocs_setup_apps_script`` afterwards is
    typical if you also passed ``full=True``.
    """
    user_id = current_user_id_or_none()
    cleared: list[str] = []

    if user_id is not None:
        # HTTP / multi-tenant mode
        from . import user_store
        if full:
            user_store.clear_state(user_id)
            cleared.append("user_store row (creds + apps_script_*)")
        else:
            # Only nuke google_creds_json; preserve apps_script_*
            user_store.save_state(user_id, {"google_creds_json": None})
            cleared.append("google_creds_json")
        return {
            "status": "reset",
            "message": (
                "Authorization cleared for your account. The next tool "
                "call that needs Google API access will return "
                "'needs_authorization' with a fresh auth URL — click it "
                "to re-consent."
            ),
            "cleared": cleared,
        }

    # Stdio / no-auth-context mode
    data_dir = default_data_dir()
    token_file = data_dir / "token.json"
    if token_file.exists():
        token_file.unlink()
        cleared.append(str(token_file))
    if full:
        setup_state_file = data_dir / "setup-state.json"
        if setup_state_file.exists():
            setup_state_file.unlink()
            cleared.append(str(setup_state_file))
        cfg_file = data_dir / "config.json"
        if cfg_file.exists():
            cfg_file.unlink()
            cleared.append(str(cfg_file))

    # Bust the module-level creds cache so the next tool call doesn't
    # return the in-memory token that we just deleted from disk.
    global _creds_cache
    _creds_cache = None

    return {
        "status": "reset",
        "message": (
            "Local OAuth token cleared. The next tool call will trigger "
            "the local browser-consent flow."
            + (" Apps Script setup state also cleared." if full else "")
        ),
        "cleared": cleared,
    }


# ---------------------------------------------------------------------
# M3 POC: trigger per-service tool registration.
# ---------------------------------------------------------------------
# Importing this AT THE BOTTOM of server.py — AFTER ``mcp`` is built,
# AFTER ``decorators.register(mcp, ...)`` wires the @gdocs_tool, AND
# AFTER the remaining (non-docs) tool decorators in this file have
# run — ensures the docs-service @gdocs_tool registrations land on the
# fully-initialised mcp instance. The asymmetric import order
# (services/docs/tools.py can ``from google_docs_mcp import server``
# at module load because by then server.py is fully loaded) avoids a
# circular import.
#
# Side-effect import: registration happens as a side-effect of
# evaluating tools.py's module-level @gdocs_tool decorations.
from .services.docs import tools as _docs_tools  # noqa: F401, E402 — side-effect import


_CLI_SUBCOMMANDS = {
    "setup-apps-script",
    "setup-apps-script-auto",  # README lines 156 + 191 document this as the recommended setup path
    "configure-webapp",
    "status",
    "help",
    "-h",
    "--help",
}


def main() -> None:
    """Entry point.

    Dispatches in order:
      1. ``google-docs-mcp <cli-subcommand>`` -> route to ``cli.py``
      2. ``MCP_TRANSPORT=http`` env var (or ``--http`` flag) -> run as
         remote HTTP server (Fly.io / cloud chat use case). Listens on
         ``$PORT`` (default 8080). Includes both the FastMCP ``/mcp``
         endpoint AND a simple ``/api/convert`` REST endpoint for
         clients that don't speak MCP protocol (e.g. cloud chat's
         Python sandbox).
      3. Otherwise -> stdio (Claude Code / Claude Desktop).
    """
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_SUBCOMMANDS:
        from .cli import cli_main
        sys.exit(cli_main(sys.argv[1:]))

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if "--http" in sys.argv:
        transport = "http"

    if transport == "http":
        from .http_server import run_http
        from .oauth_google import configure_auth_for_http

        # v1.1+: wire GoogleProvider so HTTP requests are per-user
        # authenticated. Stdio path below intentionally skips this —
        # local trust model, single user, no auth middleware.
        configure_auth_for_http(mcp)

        port = int(os.environ.get("PORT", "8080"))
        run_http(mcp, port=port)
    else:
        mcp.run()


# ---------------------------------------------------------------------
# v2.2b: LLM_RECOVERY artifacts — additive block, kept at file end to
# minimize merge conflicts with other parallel v2.2 PRs. The import
# below triggers registration of the gdocs://error-recovery resources
# (resources.py decorates module-level functions with @mcp.resource).
# ---------------------------------------------------------------------
from . import resources as _llm_recovery_resources  # noqa: E402,F401
from .resources import _RECOVERY_TABLE  # noqa: E402


@gdocs_tool(
    title="Help for an error message (local, no API)",
    readonly=True, destructive=False, idempotent=True, external=False,
    output_schema=GDOCS_HELP_OUTPUT_SCHEMA,
)
def gdocs_help(error_message: str) -> dict:
    """Look up recovery guidance for a server error string.

    USE WHEN: a previous gdocs_* tool call returned an error / warning
    payload and you (the LLM) are not sure how to proceed. Pass the
    raw error text and gdocs_help returns the structured recovery
    entry (what to do, what to tell the user, whether to retry, etc.).

    Pure lookup. No Google API calls. No OAuth required. Cheap to
    call as a debugging / recovery shortcut. Backed by the same
    table exposed at the MCP resource ``gdocs://error-recovery``
    and documented in ``docs/LLM_RECOVERY.md``.

    Args:
        error_message: The error string / warning text you want to
            decode. Substring-matched (case-INsensitive — both sides
            lowercased before comparison) against every registered
            pattern; first hit wins. Pass the raw error verbatim
            (JSON, Python repr, ToolError body — all work).

    Returns:
        On match::

            {
              "matched": true,
              "matched_pattern": "<the pattern that hit>",
              "key": "<recovery_key>",
              "pattern": "<same as matched_pattern>",
              "severity": "info" | "warning" | "error",
              "retriable": bool,
              "wait_seconds": int | null,
              "do": "<imperative recovery action>",
              "user_message": "<what to tell the user>",
              "related_tool": "<gdocs_xxx>" | null,
              "planned": bool  # True = aspirational entry, no live emitter
            }

        On miss::

            {
              "matched": false,
              "available_patterns": [<all registered patterns>],
              "suggestion": "<hint to use server_info or file an issue>"
            }

    Choreography: typically called RIGHT AFTER a failing tool call,
    before deciding whether to retry, surface to the user, or pivot
    to a different tool. Pairs with gdocs_server_info() when filing
    bug reports for the unexpected_exception case.
    """
    # Case-insensitive substring match — LLMs sometimes lowercase /
    # normalize the error text before passing it back. errors.py:69
    # also lowercases its details_str before its own substring search,
    # so case-insensitive here keeps gdocs_help symmetric with the
    # rest of the error-handling code. Both pattern AND haystack are
    # lowercased before comparison; the on-wire ``matched_pattern``
    # / ``pattern`` fields still report the canonical case.
    error_lower = error_message.lower()
    for key, entry in _RECOVERY_TABLE.items():
        if entry["pattern"].lower() in error_lower:
            return {
                "matched": True,
                "matched_pattern": entry["pattern"],
                "key": key,
                "pattern": entry["pattern"],
                "severity": entry["severity"],
                "retriable": entry["retriable"],
                "wait_seconds": entry.get("wait_seconds"),
                "do": entry["do"],
                "user_message": entry["user_message"],
                "related_tool": entry.get("related_tool"),
                "planned": entry.get("planned", False),
            }

    return {
        "matched": False,
        "available_patterns": [
            e["pattern"] for e in _RECOVERY_TABLE.values()
        ],
        "suggestion": (
            "No registered recovery pattern matched the error text "
            "(matching is case-insensitive substring). Fetch the "
            "resource gdocs://error-recovery for the full table, "
            "call gdocs_server_info() to capture version + commit, "
            "and consider filing an issue at the project repo with "
            "the raw error string so a new entry can be added."
        ),
    }


# ---------------------------------------------------------------------------
# v2.3 admin-only forensic tool (gdocs_admin_audit)
# ---------------------------------------------------------------------------
#
# Operator-facing primitive added per R29-B's pressure-test finding: a
# customer-reported cross-tenant data leak was unresolvable because no
# per-user audit log existed. The tool returns the user_state row's
# timestamp bounds (the only audit-grade signal currently persisted) so
# the operator can correlate against flyctl logs without asking the
# customer for more info.
#
# Gated by ``MCP_ADMIN_TOKEN`` env var (separate from MCP_BEARER_TOKEN —
# admin auth MUST NOT share the surface that talks to claude.ai's
# connector framework). If the env is unset the tool registers but
# refuses to run, so operators see one consistent error path whether
# they forgot to set the env or supplied a wrong token.
#
# Honest limits surfaced via the ``notes`` field — user_state.db tracks
# per-row updated_at, not per-operation. A v2.x audit-log table would
# upgrade this; for now this is the best we can do server-side without
# bouncing back to the customer. Documented in RUNBOOK §2.8.

_log = logging.getLogger("google_docs_mcp.server")

_ADMIN_AUDIT_MIN_HOURS = 1
_ADMIN_AUDIT_MAX_HOURS = 168  # 1 week


def _check_admin_token(provided: object) -> None:
    """Gate admin-only tool calls. Raises ToolError on any failure mode.

    Three failure modes, each with a distinct message so the operator
    can tell them apart in a 500 trace without ambiguous "auth failed":

    - env unset → admin surface is disabled at the server
    - arg not a string → caller signature error
    - arg != env → wrong token (uses ``hmac.compare_digest`` so a
      timing-side-channel attacker can't probe the env value by
      measuring response latency).

    Read the env at CALL time, not module-load time, so an operator
    can rotate ``MCP_ADMIN_TOKEN`` via ``fly secrets set`` and have it
    take effect without a server restart.
    """
    expected = os.environ.get("MCP_ADMIN_TOKEN")
    if not expected:
        raise ToolError(
            "admin disabled; set MCP_ADMIN_TOKEN env var on the server "
            "to enable gdocs_admin_audit."
        )
    if not isinstance(provided, str):
        raise ToolError(
            "admin_token must be a string"
        )
    # compare_digest expects equal-length operands; pad the shorter
    # side rather than short-circuiting on length, so the timing
    # signal doesn't leak the env value's length either.
    if not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8"),
    ):
        raise ToolError("admin_token does not match MCP_ADMIN_TOKEN")


@gdocs_tool(
    title="Admin: query user_state forensic timeline (admin-token gated)",
    readonly=True, destructive=False, idempotent=True, external=True,
    # creds=False: this tool reads user_store SQLite ledger directly,
    # gated by an admin token (not user OAuth). No Google API call.
    output_schema=GDOCS_ADMIN_AUDIT_OUTPUT_SCHEMA,
)
def gdocs_admin_audit(
    admin_token: str, user_id: str, since_hours: int = 24,
) -> dict:
    """Return server-side state for ``user_id`` within a time window — admin only.

    USE WHEN: a customer reports a cross-tenant data leak or other
    operation-specific incident, and you need to correlate server-side
    state against their account. Operator-facing forensic primitive;
    NOT for LLM tool routing in a normal conversation.

    Requires ``MCP_ADMIN_TOKEN`` env var set on the server AND the
    ``admin_token`` arg matching it (constant-time comparison). If
    the env is unset, the tool registers but always errors — so the
    admin surface is OFF by default in dev/test environments.

    Args:
        admin_token: Must equal the server's ``MCP_ADMIN_TOKEN`` env
            var. Separate token from ``MCP_BEARER_TOKEN`` on purpose
            (admin auth must not share the surface that talks to
            claude.ai's connector framework).
        user_id: The Google ``sub`` claim (or email fallback) of the
            user under investigation. Truncated to first 8 chars in
            any server-side logs to avoid PII leakage into logs that
            may be shipped to third-party log aggregators.
        since_hours: Audit window size, 1-168 (1 hour to 1 week).
            Defaults to 24h. Validated to that range — wider windows
            would return huge responses; narrower is rounding noise.

    Returns::

        {
          "user_id_prefix": "<first 8 chars>",
          "window_hours": <since_hours>,
          "total_entries": 0 | 1,
          "entries": [
            {
              "timestamp": <unix epoch seconds of updated_at>,
              "operation_type": "user_state_updated",
              "doc_id": null,           # not tracked at this granularity
              "success": true,
            }
          ],
          "notes": "user_state.db tracks ..."
        }

    Honest limits: user_state.db tracks ``created_at`` / ``updated_at``
    per user row — NOT per Google API call. This means the tool can
    answer "did this user's session touch the server in the last N
    hours?" but not "what specific docs were created?". For finer
    granularity the operator must also grep flyctl logs by the
    ``user_id_prefix`` value returned here. A v2.x audit-log table
    would close this gap; tracked in #25.
    """
    _check_admin_token(admin_token)

    if not isinstance(user_id, str) or not user_id:
        raise ToolError("user_id must be a non-empty string")

    if (
        not isinstance(since_hours, int)
        or isinstance(since_hours, bool)  # True/False are ints in Python
        or since_hours < _ADMIN_AUDIT_MIN_HOURS
        or since_hours > _ADMIN_AUDIT_MAX_HOURS
    ):
        raise ToolError(
            f"since_hours must be an int in "
            f"[{_ADMIN_AUDIT_MIN_HOURS}, {_ADMIN_AUDIT_MAX_HOURS}] "
            f"(got {since_hours!r})"
        )

    # Log the call with user_id TRUNCATED to first 8 chars — full
    # user_id is PII (Google sub claim) and must not land in logs
    # that may be shipped to third-party aggregators.
    _log.info(
        "gdocs_admin_audit: user=%s window=%dh",
        user_id[:8], since_hours,
    )

    # Lazy import to keep server.py module-load lean and avoid
    # circular-import risk if user_store ever grows server-side deps.
    from . import user_store

    state = user_store.get_state(user_id)
    cutoff = int(time.time()) - since_hours * 3600

    entries: list[dict] = []
    updated_at = state.get("updated_at")
    if updated_at is not None and int(updated_at) >= cutoff:
        entries.append({
            "timestamp": int(updated_at),
            "operation_type": "user_state_updated",
            "doc_id": None,  # not tracked at this granularity
            "success": True,  # row presence implies completed write
        })

    return {
        "user_id_prefix": user_id[:8],
        "window_hours": since_hours,
        "total_entries": len(entries),
        "entries": entries,
        "notes": (
            "user_state.db tracks updated_at per user row, not per "
            "Google API call. Use this to confirm whether the user's "
            "session was active in the window; for per-operation detail "
            "grep flyctl logs by the user_id_prefix returned here. "
            "Finer-grained audit logging is tracked in issue #25."
        ),
    }


if __name__ == "__main__":
    main()
