# Changelog

All notable changes to `google-docs-mcp`.

This project follows [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-05-18

**Multi-tenant cloud auth.** The remote HTTP MCP (claude.ai connector
via Fly.io) is now genuinely multi-tenant — each cloud-chat user
operates on their *own* Drive with their *own* Google identity. Before
1.1, the entire Fly deployment was single-tenant: every cloud-chat
user's tool calls implicitly used the operator's cached OAuth token,
so docs were created in the operator's Drive and the retrofit Apps
Script Web App (deployed `access: MYSELF`) was unusable for anyone
but the operator.

### New: `gdocs_setup_apps_script` MCP tool

One-shot setup for the per-user Apps Script Web App needed by
`gdocs_tab_existing_doc` (the lossless content-move path). Run once
per user; idempotent on retry (resumes from the last successful
step). Stdio mode keeps calling the v1.0 local CLI; HTTP mode
deploys a Web App into the calling user's Drive.

### Cloud architecture: "Shape C"

- **FastMCP's `GoogleProvider`** handles claude.ai connector auth
  (identity-only `openid email` scopes, but `valid_scopes` advertises
  the full Workspace union so consent grants Docs/Drive/Apps Script
  at the same time)
- **Separate auth-code flow we own** (`/oauth/google/api/callback`)
  obtains tokens we can actually use for Google API calls
- **Per-user state** in SQLite on the Fly volume, keyed by Google
  `sub`
- **Per-user lock on refresh** so two concurrent tool calls don't
  rotate each other's refresh_token

Why not just rely on `GoogleProvider` for the API tokens too: the
upstream Google tokens it holds live in private attributes
(`_upstream_token_store`, no public getter, no API contract). The
MCP spec blesses two-flow designs in the "URL Mode Elicitation for
OAuth Flows" pattern; production reference is
[`taylorwilsdon/google_workspace_mcp`](https://github.com/taylorwilsdon/google_workspace_mcp).

### New env vars (HTTP deploy)

Required when running the Fly server with the new auth wiring:

- `GOOGLE_OAUTH_BASE_URL` — public HTTPS hostname of the deployment
  (e.g. `https://my-app.fly.dev`). Must exactly match the actual URL
  or claude.ai's connector OAuth discovery silently fails.
- `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` — full OAuth client JSON inline,
  or `GOOGLE_OAUTH_CLIENT_SECRETS_PATH` pointing at a file.
- `MCP_BEARER_TOKEN` (already required) — reused as the HMAC signing
  key for the OAuth state parameter.

Auto-set by `configure_auth_for_http`:
`OAUTHLIB_RELAX_TOKEN_SCOPE=1` (so partial-grant consents don't
crash the OAuth callback).

### Breaking: existing claude.ai connector users must reconnect

The OAuth scope set is changing. Existing connector connections need
to be disconnected + reconnected in claude.ai's connector settings to
pick up the new scopes.

### Internal modules added

- `user_store.py` — SQLite per-user state (WAL mode, per-path init
  guard, merge-semantics on save, typo-rejection)
- `oauth_state.py` — HMAC-signed, single-use state-param for OAuth
  callback (CSRF + replay protection)
- `oauth_google.py` — `google_auth_oauthlib.Flow` setup, callback
  code-exchange, GoogleProvider activation
- `credentials.py` — `get_credentials_for_user(sub)` with per-user
  refresh lock, `invalid_grant` → `NeedsReauthError` mapping
- `setup_apps_script_for_user` — cloud-side variant of
  `setup_apps_script_auto` using `user_store` as the per-user ledger

### Production bug fix in user_store

The concurrent-writes test surfaced a real `PRAGMA journal_mode=WAL`
race that `busy_timeout` doesn't mitigate. Fixed via per-path init
guard under `threading.Lock` — would have fired on Fly the moment
two cloud users finished OAuth simultaneously.

### Tests

+49 unit tests across the v1.1 modules:
`test_user_store.py` (13), `test_oauth_state.py` (12),
`test_oauth_google.py` (13), `test_credentials.py` (11),
`test_setup_apps_script_for_user.py` (9),
`test_phase6_consumer_branching.py` (7),
`test_configure_auth_for_http.py` (9). Total: 158 unit + 4 live.

### Dependencies

- `fastmcp>=2.13` (was `>=2.0`) — `GoogleProvider` + `valid_scopes` +
  `_default_scope_str` post-init patch all require 2.13+.

## [1.0.1] — 2026-05-18

**Fixed: orphan Apps Script projects on setup retry.**

`setup-apps-script-auto` now persists per-step state to
`~/.google-docs-mcp/setup-state.json`. If any step in the 4-step
pipeline (create project → push files → create version → deploy webapp)
fails, the next retry resumes from the first incomplete step instead
of creating a second Apps Script project in the user's Drive.

Handles three resume scenarios:
- Same content + same impersonate user → resume from first incomplete step
- Edited `restructure.gs` (different content hash) → start fresh
- User manually deleted the script in Drive (cached script_id 404s) →
  detect and start fresh

Caught preventively via the v1.0 architecture-review pass. Without the
ledger, a user retrying after a flaky network would have accumulated
"ghost scripts" requiring manual Drive cleanup.

+6 unit tests in `tests/unit/test_setup_idempotency.py` covering cold
start, mid-step crash + resume, content-change reset, and manual-delete
recovery. Total tests: 78 unit + 4 live.

## [1.0.0] — 2026-05-18

First stable release.

**Tool surface (18 tools, all `gdocs_`-prefixed):**

- Create / convert / retrofit: `gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_preview_tab_split`
- Edit tabs: `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_rename_tab`, `gdocs_delete_tab`, `gdocs_set_tab_icons`, `gdocs_replace_all_text`
- Read: `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_get_tab_url`
- Search / manage: `gdocs_find_doc_by_title`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_move_to_folder`
- Cloud-chat support: `gdocs_get_signed_upload_url`
- Server identity: `gdocs_server_info`

**Notable progression since pre-1.0 alpha:**

- Native Google Docs Tabs (Oct 2024 sidebar feature) — every tool operates at the tab level, not just outline headings.
- Both stdio (Claude Desktop / Code) and remote HTTP (claude.ai via Fly.io) transports.
- Apps Script Web App setup automated via `setup-apps-script-auto` CLI — collapses the 6-step UI dance into one OAuth consent. Opt-in `--auth-mode=service-account` adds Domain-Wide Delegation for Workspace users wanting truly headless setup.
- Signed-URL upload flow (`gdocs_get_signed_upload_url`) for claude.ai's sandbox — bypasses the bytes-via-tool-args size limit AND the Drive-connector .docx corruption issue.
- Soft-failure contracts on every mutate operation: 404 / 403 / `app_not_authorized` return as data, never raised. Batch operations skip-and-continue.
- Retrofit injects synthetic Heading 1s into styled `.docx` files with no headings (table-banner sections, etc.). Unicode-normalized + whitespace-collapsed + run-fragmentation-tolerant matching.
- 76 tests (72 unit + 4 live integration). CI runs on every push/PR across Python 3.10–3.13.
- Deploy script (`deploy.sh`) gates Fly.io deploys on local unit-test pass.
- `gas_deploy/` sub-package: clean boundary around Apps Script REST plumbing — extractable as a standalone package if a second consumer ever appears.

**Known limitations** (see README):

- `setActiveTab` (persistent default-tab setting) not exposed. Use `gdocs_get_tab_url` for per-link deep linking instead — covers the common case.
- Drive's converter 500s on `.docx` files using `<w:sym w:char="00A0"/>` for NBSP. Our retrofit handles this construct correctly in-memory; the limitation is purely Drive's. Use the literal `\xa0` character inside `<w:t>` (the form Word actually produces) instead.
- Per-tab headers/footers not supported. Tabs render as continuous scroll in the Docs UI; page headers only matter for paginated PDF export.

[1.0.0]: https://github.com/Sundeepg98/google-docs-mcp/releases/tag/v1.0.0
