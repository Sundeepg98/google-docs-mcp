"""Google API OAuth flow handler.

Handles the one-time browser consent dance and caches the resulting
tokens so subsequent runs are silent.

Client config discovery order (first match wins):
  1. ``GOOGLE_DOCS_OAUTH_PATH`` environment variable
  2. ``<creds_dir>/credentials.json``
  3. ``~/.gmail-mcp/gcp-oauth.keys.json`` (reused from gmail-mcp)
"""
import json
import os
from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ---------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for the Workspace OAuth consent scope set.
#
# Hardening-P1 (ROADMAP_SPECS #7): historically ``auth.SCOPES`` (the
# stdio/baseline Workspace scopes) and ``oauth_google.GOOGLE_API_SCOPES``
# (the HTTP/connector set = these scopes + the OIDC identity scopes) were
# TWO independently hand-edited lists carrying "keep in sync BY HAND"
# comments — a textbook drift trap (add a service scope to one, forget the
# other, and stdio vs HTTP consent silently diverge).
#
# They are now derived from THIS one list:
#   * ``auth.SCOPES``                = WORKSPACE_SCOPES                 (15)
#   * ``oauth_google.GOOGLE_API_SCOPES`` = OIDC identity scopes
#                                          + WORKSPACE_SCOPES           (17)
# (``oauth_google`` imports ``WORKSPACE_SCOPES`` from here — ``auth`` is a
# leaf module so there's no import cycle.)
#
# Adding a new Workspace service scope is now a ONE-LINE edit here; both
# consent sets pick it up automatically. ``tests/unit/test_scope_union_
# single_source.py`` asserts the derived sets equal the exact prior
# literal scopes (frozenset equality) so any accidental drift fails CI.
#
# ⚠️ This MCP is mid-OAuth-verification (verify-LAST): the consent scope
# SET is operator-gated. Do NOT add/remove/restrict a scope here as a
# drive-by — a change to this list IS a change to the consent screen.
#
# Ordering: this list's order is preserved verbatim into ``auth.SCOPES``,
# and prefixed (not interleaved) with the OIDC scopes for
# ``GOOGLE_API_SCOPES``, so both derived lists are byte-identical to the
# prior hand-maintained literals. Google ignores scope order on the
# consent screen, but preserving it keeps diffs (and any log/metadata
# snapshots) stable.
# ---------------------------------------------------------------------
WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    # NOTE: drive.readonly was REMOVED here for the free base tier
    # (base-tier redesign). It's Google's only RESTRICTED scope in our
    # set — keeping it would force CASA security assessment + the
    # Testing-mode 7-day refresh-token cap, blocking a free
    # "sensitive scopes only" verification. Its two consumers were
    # re-plumbed off drive.readonly:
    #   * legacy .docx ingest (drive_file_id / docx_drive_file_id) →
    #     deprecated in favor of the signed-URL upload path
    #     (gdocs_get_signed_upload_url → POST → /api/convert), which
    #     stages bytes on the server with no Drive read scope.
    #   * slides→video frame handoff → the bound render script now POSTs
    #     frames to the server's signed staging endpoint instead of a
    #     Drive folder the app would need drive.readonly to re-read.
    # Existing tokens that still carry drive.readonly keep working
    # (OAUTHLIB_RELAX_TOKEN_SCOPE); new consents won't request it. A
    # FUTURE "read ANY Drive file" feature will reintroduce drive.readonly
    # on a SEPARATE restricted tier (out of scope here).
    # v2.3.1 — Sheets read/write/create for the 2nd new service. The
    # full ``spreadsheets`` scope (not the narrower
    # ``spreadsheets.readonly``) is needed because gsheets_write_range
    # and gsheets_create_spreadsheet mutate the sheet. Existing users
    # pick this up automatically on next token refresh via the
    # ``include_granted_scopes=true`` incremental-consent flow (same
    # pattern that handled the earlier drive.readonly + Apps Script
    # scope additions); no forced re-consent.
    "https://www.googleapis.com/auth/spreadsheets",
    # v2.3.2 — Slides read + batchUpdate (replaceAllText) + create for
    # the 3rd new service. Same incremental-consent semantics as the
    # Sheets scope addition (PR #119) — existing users get the new
    # scope automatically on next token refresh via the include-
    # granted-scopes flow. No forced re-consent.
    "https://www.googleapis.com/auth/presentations",
    # Forms (new service) — create/edit forms + read responses. BOTH are
    # Google-SENSITIVE, NOT RESTRICTED → no CASA security assessment (only
    # restricted scopes trigger CASA; Forms scopes are not on Google's
    # restricted list — see tests/unit/test_base_tier_scopes.py::_RESTRICTED).
    # The base tier stays CASA-free.
    #   * forms.body              — forms.create / forms.get / batchUpdate
    #     (createItem / updateItem / deleteItem). Operator-directed scope
    #     expansion paired with the as_install_form_handler submit trigger.
    #   * forms.responses.readonly — forms.responses.list / .get (read-only
    #     access to submitted responses).
    # Same incremental-consent semantics as the Sheets (#119) / Slides
    # (#120) additions — existing users pick the new scopes up
    # automatically on next token refresh via include_granted_scopes=true.
    # No forced re-consent.
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    # Tasks read/write/create/delete for the 4th new service (tasklists +
    # tasks CRUD). The full ``tasks`` scope (not the narrower
    # ``tasks.readonly``) is needed because gtasks_create_task /
    # gtasks_update_task / gtasks_delete_task mutate. This scope is
    # SENSITIVE, NOT RESTRICTED — it is absent from Google's closed
    # restricted-scope list (Gmail / Drive / Fit / Chat / Data
    # Portability / Photos / Health), so it adds NO CASA requirement and
    # keeps the free base tier eligible for the sensitive-only
    # verification. Existing users pick it up automatically on next token
    # refresh via the ``include_granted_scopes=true`` incremental-consent
    # flow (same pattern as the Sheets / Slides / Apps Script additions);
    # no forced re-consent. DEPLOY NOTE: the Google Tasks API must be
    # enabled in the GCP project before the gtasks_* tools work live.
    "https://www.googleapis.com/auth/tasks",
    # PR-Δ1 (v2.3.4) — Apps Script management scopes promoted from
    # the per-tool GAS_DEPLOY_SCOPES list into the baseline union.
    # Reasoning: the Workspace automation runtime install
    # (``gdocs_install_automation``, see PR-α reframe) is now framed
    # as headline functionality rather than hidden infrastructure;
    # bundling its scopes into the first-consent screen kills the
    # "scary second consent" moment. The per-tool
    # ``required_scopes=GAS_DEPLOY_SCOPES`` parameter in
    # ``services/gas_deploy/tools.py`` is now redundant but kept
    # for explicit documentation — ``_check_scopes_or_raise`` will
    # pass on first call because the scopes are baseline-granted.
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    # v2.4.0 — Google Calendar read/write (events + calendar metadata) for
    # the 4th new service (services/calendar/). The full ``calendar`` scope
    # (not the narrower ``calendar.readonly`` / ``calendar.events``) is
    # requested because the service creates / patches / deletes events AND
    # reads the calendar list. ``calendar`` is a Google **SENSITIVE** scope,
    # NOT restricted — it does NOT trigger the CASA security assessment that
    # restricted scopes (gmail.*, drive[full]/.readonly) require, so it
    # stays on this MCP's free "sensitive scopes only" verification track.
    # Existing users pick it up automatically on next token refresh via the
    # ``include_granted_scopes=true`` incremental-consent flow (same pattern
    # that handled the Sheets / Slides scope additions); no forced
    # re-consent.
    "https://www.googleapis.com/auth/calendar",
    # Contacts service (services/contacts/) — People API v1 read/write.
    # The FULL ``contacts`` scope (not the narrower ``contacts.readonly``)
    # is required because gcontacts_create / gcontacts_update /
    # gcontacts_delete MUTATE the user's contacts. Google classifies
    # ``contacts`` as a SENSITIVE scope, NOT restricted — so it needs
    # sensitive-scope OAuth verification but NOT a CASA security
    # assessment (CASA targets the RESTRICTED scopes — full Gmail/Drive,
    # etc.). This keeps the "sensitive scopes only, no CASA" verification
    # posture intact (same rationale that kept drive.readonly OUT — that
    # one IS restricted). Existing user grants pick this up automatically
    # on next token refresh via Google's ``include_granted_scopes=true``
    # incremental-consent flow (same pattern as the Sheets/Slides/Apps
    # Script scope additions in earlier PRs); no forced re-consent.
    "https://www.googleapis.com/auth/contacts",
    # ---- CASA-free scope growth (this PR) — 4 scopes, each with a tool ----
    # Verified against Google's restricted-scope list: NONE of the four
    # below is restricted, so they add NO CASA security assessment and keep
    # the free "sensitive scopes only" verification posture intact. The
    # goal is a MAXIMAL app in one verification pass. Same incremental-
    # consent semantics as every prior addition (include_granted_scopes=
    # true) — existing users pick them up on next token refresh; no forced
    # re-consent.
    #
    # Gmail (new service services/gmail/) — send + label management.
    #   * gmail.send (SENSITIVE, not restricted → NO CASA). Google's own
    #     classification: "Send email on your behalf." Send-only; it does
    #     NOT grant mailbox READ. The full-mailbox / read / modify Gmail
    #     scopes (mail.google.com, gmail.readonly, gmail.modify,
    #     gmail.metadata, gmail.insert, gmail.compose, gmail.settings.*)
    #     ARE restricted and are deliberately NOT requested. Backs
    #     gmail_send_message (users.messages.send, RFC822/MIME).
    "https://www.googleapis.com/auth/gmail.send",
    #   * gmail.labels (NON-sensitive — no verification or CASA at all).
    #     Google's classification: "See and edit your email labels." This
    #     manages LABEL OBJECTS only (labels.create / list / delete); it
    #     does NOT permit reading messages or changing a message's labels
    #     (that needs gmail.modify, which is RESTRICTED and intentionally
    #     omitted). Backs gmail_create_label / gmail_list_labels /
    #     gmail_delete_label.
    "https://www.googleapis.com/auth/gmail.labels",
    # Contacts (existing services/contacts/) — "other contacts" read.
    #   * contacts.other.readonly (SENSITIVE, not restricted → NO CASA).
    #     Read-only access to the auto-saved "Other contacts" list (people
    #     the user has interacted with but never explicitly saved). Strictly
    #     narrower than the full ``contacts`` scope already requested above.
    #     Backs gcontacts_list_other_contacts (People API otherContacts.list).
    "https://www.googleapis.com/auth/contacts.other.readonly",
    # Apps Script (existing services/apps_script/) — execution history read.
    #   * script.processes (SENSITIVE, not restricted → NO CASA). Google's
    #     classification: "View Google Apps Script processes." Read-only
    #     access to a script project's execution history. Backs
    #     as_list_script_processes (Apps Script API processes.list /
    #     processes.listScriptProcesses) — the observability companion to
    #     the existing as_generate_bound_script / as_deploy_web_app
    #     create+deploy levers (script.projects / script.deployments).
    "https://www.googleapis.com/auth/script.processes",
]

# ``SCOPES`` is the stdio/baseline Workspace consent set. It IS the
# single-source ``WORKSPACE_SCOPES`` — kept as a distinct public name
# because callers across the codebase (and external forks) import
# ``auth.SCOPES``. Same list object identity is fine; nothing mutates it.
SCOPES = WORKSPACE_SCOPES


def default_data_dir() -> Path:
    """User-scoped directory for OAuth tokens.

    Override with ``GOOGLE_DOCS_DATA_DIR`` env var. Default mirrors the
    gmail-mcp convention (``~/.google-docs-mcp/``) for predictability
    across pipx installs.
    """
    override = os.environ.get("GOOGLE_DOCS_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".google-docs-mcp"


def find_client_config(creds_dir: Path) -> Path:
    """Locate the OAuth client config (a.k.a. credentials.json / gcp-oauth.keys.json).

    Same Google Cloud project = same OAuth client, so reusing the
    gmail-mcp keys is fine. Token files stay separate per-app.
    """
    env_path = os.environ.get("GOOGLE_DOCS_OAUTH_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    local = creds_dir / "credentials.json"
    if local.exists():
        return local

    gmail_mcp = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
    if gmail_mcp.exists():
        return gmail_mcp

    raise FileNotFoundError(
        "No OAuth client config found. Tried:\n"
        f"  $GOOGLE_DOCS_OAUTH_PATH ({env_path or 'unset'})\n"
        f"  {local}\n"
        f"  {gmail_mcp}\n"
        "Either copy your existing gmail-mcp keys to one of these paths, "
        "set the env var, or download fresh ones from Google Cloud Console."
    )


def load_credentials(
    creds_dir: Path,
    extra_scopes: list[str] | None = None,
) -> Credentials:
    """Return valid Google OAuth credentials, running the consent flow if needed.

    Tokens are written to ``<creds_dir>/token.json`` regardless of where
    the client config came from — keeping app-specific scopes isolated.

    ``extra_scopes`` (optional) adds to the runtime ``SCOPES`` list — used
    by one-off privileged operations like ``setup-apps-script-auto``
    that need Apps Script management scopes which pure runtime callers
    don't need. If the cached token lacks any of these, it's deleted
    and a fresh consent flow runs.
    """
    required = SCOPES + (list(extra_scopes) if extra_scopes else [])

    creds_dir.mkdir(parents=True, exist_ok=True)
    token_file = creds_dir / "token.json"

    # Check the actual granted scopes in the token file BEFORE loading
    # via google-auth — ``from_authorized_user_file(file, SCOPES)`` echoes
    # the SCOPES arg back as ``creds.scopes``, masking missing grants
    # until the refresh attempt fails with ``invalid_scope``.
    if token_file.exists() and not _token_has_all_scopes(token_file, required):
        token_file.unlink()  # stale scope set — force fresh OAuth

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), required)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
            return creds

    client_config = find_client_config(creds_dir)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_config), required)
    # google_auth_oauthlib types run_local_server as returning the union
    # `external_account.Credentials | oauth2.Credentials`. In practice
    # an InstalledAppFlow always returns oauth2.Credentials (external-
    # account flows use a different Flow subclass). Cast to narrow the
    # return type to what this function actually returns.
    creds = cast(Credentials, flow.run_local_server(port=0))
    token_file.write_text(creds.to_json())
    return creds


def load_service_account_credentials(
    key_path: Path,
    impersonate_user: str,
    scopes: list[str],
):
    """Load Service Account credentials + impersonate a Workspace user via DWD.

    Used by the opt-in headless setup path (``setup-apps-script-auto
    --auth-mode=service-account``) for environments where no human can
    click OAuth consent — CI, server-side batch document processing,
    multi-user IT provisioning. For interactive desktop use, the
    regular OAuth flow (``load_credentials``) is the right call.

    Empirically confirmed against the Apps Script REST API (e.g.
    shiftavenue/gas-action does this in CI). Apps Script API rejects
    raw SA tokens but accepts SA-impersonating-user tokens because, to
    the API, the token looks like a user token for the subject email.

    Prerequisites (one-time, on the Workspace admin's side):
      1. Service Account created in GCP project, JSON key downloaded
      2. SA's numeric Client ID added to:
         Admin Console → Security → Access and data control →
         API controls → Manage Domain Wide Delegation → Add new
      3. Scopes authorized in that DWD entry (must include all of
         ``scopes`` here — typically GAS_DEPLOY_SCOPES)
      4. Up to 24h propagation (usually minutes)

    Args:
        key_path: path to the SA's JSON key file.
        impersonate_user: email of the Workspace user the SA acts as.
            The resulting Apps Script project will be owned by them.
        scopes: full scope list the SA needs (must match what the
            admin authorized in the DWD console).

    Personal @gmail.com accounts have no Admin Console and can NOT use
    this path — they must use the OAuth flow.
    """
    if not key_path.exists():
        raise FileNotFoundError(
            f"Service account key file not found: {key_path}. "
            "Download it from GCP Console → IAM & Admin → Service Accounts "
            "→ <your SA> → Keys → Add Key → JSON."
        )
    sa_creds = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=scopes,
    )
    return sa_creds.with_subject(impersonate_user)


def _token_has_all_scopes(token_file: Path, required: list[str]) -> bool:
    """Inspect the raw token JSON to see what scopes were actually granted.

    Unlike ``Credentials.scopes`` (which mirrors whatever was passed in to
    ``from_authorized_user_file``), the raw JSON's ``scopes`` field holds
    the actual set the user consented to. Used to detect when a version
    bump adds a new scope and the cached token needs replacing.
    """
    try:
        data = json.loads(token_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    granted = set(data.get("scopes") or [])
    return all(scope in granted for scope in required)
