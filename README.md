# appscriptly

> **Workspace Automation MCP** — generates persistent workflows (time-driven jobs, custom menus, reactive automations) that live IN your Google Workspace and run on Google's infrastructure. Also covers Docs / Sheets / Slides / Drive create + edit + read + retrofit.

[![tests](https://github.com/Sundeepg98/google-docs-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Sundeepg98/google-docs-mcp/actions/workflows/test.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Sundeepg98/google-docs-mcp/badge)](https://scorecard.dev/viewer/?uri=github.com/Sundeepg98/google-docs-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Security posture:** see [SECURITY.md](SECURITY.md) (disclosure policy) ·
[docs/security-posture.md](docs/security-posture.md) (narrative) ·
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) (STRIDE + bounded blast radius) ·
[docs/asvs-level-1-checklist.md](docs/asvs-level-1-checklist.md) (OWASP ASVS L1 self-attestation).

> **Note on the rename (2026-05-27):** this project was previously published as `google-docs-mcp`. The original name reflected the Docs-first v1.0 scope; subsequent releases grew to cover six Google services + Apps Script-backed automation, so the rename to `appscriptly` matches the positioning. The **Python module path stays at `appscriptly`** (renaming would break every existing import); the **CLI binary** is now `appscriptly` with `google-docs-mcp` preserved as a backward-compat alias; **`gdocs_*` tool names are unchanged** (renaming would break existing claude.ai connector users); the Fly deployment is mid-migration with the legacy URL still live. See [docs/adr/2026-05-27-rename-to-appscriptly.md](docs/adr/2026-05-27-rename-to-appscriptly.md) for the staged plan.

**appscriptly is the [MCP](https://modelcontextprotocol.io/) server that puts the Apps Script automation moat behind a Claude-friendly interface.** It creates and edits Google Docs with native sidebar Tabs (Google's October 2024 feature), losslessly retrofits existing `.docx` documents into tabbed format (preserving tables, drawings, and equations that text-only round-trips would destroy), and — the headline capability post-PR-α — installs a per-user **Workspace Automation runtime** so Claude can build persistent workflows that fire on a schedule, when data changes, or from a custom menu inside any of your docs / sheets / slides.

Tabs are a Google-Docs-native concept; they do **not** exist in the `.docx` / OOXML spec. Any pipeline that round-trips through `.docx` collapses to one tab. The only way to create or preserve Tabs programmatically is to call the Google Docs API directly — which is what this server does, packaged as an MCP server for **Claude Desktop**, **Claude Code**, and **claude.ai** custom connectors.

- **For end-users:** see [docs/USER_GUIDE.md](docs/USER_GUIDE.md) to start using it with Claude Desktop / Claude Code / claude.ai — no install required if your operator has already deployed a connector URL.
- **For developers + operators:** continue reading below.

**Tested with:** Claude Desktop, Claude Code, claude.ai custom connectors. Other
MCP-speaking clients (Cursor, Zed, Windsurf, Cline, Continue) likely work but
aren't in the test matrix — please file an issue if you hit client-specific
surprises. **ChatGPT Deep Research mode is NOT supported** — that mode requires
tools literally named `search` and `fetch`; our tool surface is prefixed
(`gdocs_find_doc_by_title`, `gdocs_read_doc`). ChatGPT regular MCP mode works.

---

Works as: local stdio MCP (Claude Desktop / Code) **or** remote
HTTP MCP (claude.ai custom connector via Fly.io). The cloud
deployment is fully multi-tenant as of v1.1 — each user operates on
their own Drive with their own Google identity.

## Why this exists

Google Docs Tabs are a Google-Docs-native concept. They do **not** exist
in the `.docx` / OOXML spec, so any pipeline that round-trips through
`.docx` collapses to one tab. The only way to create tabs
programmatically is to call the Google Docs API directly. This server
wraps that flow + the supporting Drive / Sheets / Slides / Apps Script
operations into 153 tools (primarily `gdocs_*`, plus `gsheets_*` /
`gslides_*` / `as_*` for the newer services) covering the full
lifecycle: create, edit, read, find, retrofit, trash/untrash, convert
existing docs, one-shot per-user Apps Script Web App setup, plus
Sheets / Slides editing and introspection tools that
surface the server's CI test status over the MCP interface (so an
agent can verify the running build was actually tested, not just
trust a green badge). v1.3.0+: the server is **self-documenting** —
connect-time `instructions` carry workflow choreography, and
`gdocs_guide()` returns the same content as a structured payload.
No external reference file required.

## Tool index

Most tools are prefixed `gdocs_`; the Sheets / Slides verticals use
`gsheets_` / `gslides_` and the appscriptly-native automation tools use
`as_`. Call `gdocs_server_info` on a live server to get the
authoritative list (all 153 tools) with descriptions.

| Purpose | Tool |
|---|---|
| **Create from text** (default for new content) | `gdocs_make_tabbed_doc(title, tabs)` |
| **Convert existing .docx or Google Doc** | `gdocs_tab_existing_doc(drive_file_id?, docx_path?, split_by?, markers?, ...)` |
| **Preview** what conversion would produce | `gdocs_preview_tab_split(docx_path?/drive_file_id?, split_by)` |
| **Add tabs** to an existing doc | `gdocs_add_tabs(doc_id, tabs, parent_tab_id?)` |
| **Append to a tab** | `gdocs_append_to_tab(doc_id, tab_id, content)` |
| **Rename / icon a tab** | `gdocs_rename_tab(doc_id, tab_id, title?, icon_emoji?)` |
| **Set icons on multiple tabs by title** | `gdocs_set_tab_icons(doc_id, icons_by_title)` |
| **Delete a tab** | `gdocs_delete_tab(doc_id, tab_id)` |
| **Find/replace across tabs** | `gdocs_replace_all_text(doc_id, find, replace, tab_ids?)` |
| **Read tab structure** | `gdocs_get_doc_outline(doc_id)` |
| **Read body text** (one tab or all) | `gdocs_read_doc(doc_id, tab_id?)` |
| **Find docs by title** | `gdocs_find_doc_by_title(query, exact?, include_trashed?)` |
| **Trash / untrash** (single or batch) | `gdocs_trash_file(file_id)` / `gdocs_untrash_file(file_id)` |
| **Move into a folder** | `gdocs_move_to_folder(file_id, folder_id)` |
| **Get deep link to a tab** | `gdocs_get_tab_url(doc_id, tab_id)` |
| **Sandbox upload URL** (cloud chat) | `gdocs_get_signed_upload_url()` |
| **Install Workspace automation runtime** (per-user, one-time) | `gdocs_install_automation()` |
| **Reset / revoke OAuth credentials** (force re-consent) | `gdocs_reset_authorization(full?)` |
| **Server identity + CI test status** | `gdocs_server_info()` |
| **CI test inventory + per-test outcomes** | `gdocs_test_manifest()` |
| **Orientation: workflows + rules + tool groups** (v1.3.0+) | `gdocs_guide()` |
| **LLM error-recovery lookup** (v2.2b+) | `gdocs_help(error_message)` |

## Self-evidencing CI gate (v1.2+)

`gdocs_server_info` returns a `test_suite` block that lets any caller
verify the running build was actually tested — not just trust an
opaque count. Four layers:

| Layer | Field | Proves |
|---|---|---|
| Count not faked | `test_suite.report_digest` | sha256 of canonical `test-results.json`; runtime recomputes and compares — mismatch → `status: "tampered"` |
| CI actually ran | `test_suite.ci_run_url` | URL of the GitHub Actions run that produced this artifact (`"local"` for manual `./deploy.sh` deploys, never empty) |
| Right tests exist | `gdocs_test_manifest()` | full test inventory + outcomes + named-regression-guard presence check |
| Tests catch bugs | `test_suite.mutation_check` | `{ran, caught, status, asleep_guards, stale_patches, imprecise_patches}` — CI applies known bug patches per build, runs the full unit suite per patch, and fails if any patch is asleep / stale / over-broad |

Every push to `main` flows through GitHub Actions: `unit` → `mutation` →
`deploy`. A commit that breaks any test can't reach production. The
mutation gate itself is self-checking — `stale_patches` flags
mutations whose `find` text moved out from under them (preventing
silent "caught" reports for bugs never actually injected); v1.2.2
added that. See `.github/workflows/deploy.yml`,
`scripts/mutation_check.py`, and the CHANGELOG for the full story.

## Setup — local stdio (Claude Desktop / Claude Code)

### Requirements

- Python 3.10+
- A Google Cloud project with the **Google Docs API** and **Google Drive API** enabled
- An OAuth 2.0 Client ID of type **Desktop app** (downloaded as JSON from Cloud Console → APIs & Services → Credentials)
- (Optional, for convert/retrofit only) An Apps Script Web App — see [Apps Script setup](#apps-script-setup-required-for-converting-existing-docs)

### Install

**With pipx** (isolated, single global command — recommended):

```bash
pipx install git+https://github.com/Sundeepg98/google-docs-mcp.git
```

**Or clone for dev**:

```bash
git clone https://github.com/Sundeepg98/google-docs-mcp.git
cd google-docs-mcp
python -m venv .venv && source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -e ".[test]"
```

### OAuth client config

The server looks for the OAuth client config (JSON from Cloud Console) in this order:

1. Path in `GOOGLE_DOCS_OAUTH_PATH` env var
2. `./credentials/credentials.json` (relative to cwd)
3. `~/.gmail-mcp/gcp-oauth.keys.json` (reuse existing [gmail-mcp](https://github.com/GongRzhe/Gmail-MCP-Server) keys — same Cloud project)

User tokens cache at `~/.google-docs-mcp/token.json` (override with `GOOGLE_DOCS_DATA_DIR`).

### Wire to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "appscriptly": { "command": "appscriptly" }
  }
}
```

(The `"appscriptly"` key is the label that shows up in Claude's UI — pick whatever you want, the command must match the installed CLI binary. Pre-rename installs that use `"google-docs": { "command": "google-docs-mcp" }` keep working — both `appscriptly` and `google-docs-mcp` are installed as CLI binaries; see [`pyproject.toml`](pyproject.toml) `[project.scripts]`. The legacy `google-docs-mcp` alias stays through v3.0.)

For dev installs, point at the venv entry-point: `/abs/path/repo/.venv/bin/appscriptly` (or `.venv\Scripts\appscriptly.exe` on Windows).

Restart Claude Desktop. The `gdocs_*` tools should appear.

### Wire to Claude Code

Add the same `mcpServers` entry to `~/.claude.json` (user-scope) or to a project's `.mcp.json` (project-scope). Run `/mcp` and reconnect.

### First-run OAuth

First tool call opens the browser. Sign in → grant scopes. Tokens cached after that; no more browser dance. Required scopes (mirror of `auth.py:WORKSPACE_SCOPES`, the single source of truth):
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/spreadsheets`
- `https://www.googleapis.com/auth/presentations`
- `https://www.googleapis.com/auth/forms.body`
- `https://www.googleapis.com/auth/forms.responses.readonly`
- `https://www.googleapis.com/auth/tasks`
- `https://www.googleapis.com/auth/script.projects`
- `https://www.googleapis.com/auth/script.deployments`
- `https://www.googleapis.com/auth/calendar`
- `https://www.googleapis.com/auth/contacts`
- `https://www.googleapis.com/auth/gmail.send` — send email on the user's behalf (Gmail; **sensitive**, send-only, no mailbox read)
- `https://www.googleapis.com/auth/gmail.labels` — manage Gmail labels (create/list/delete label objects; **non-sensitive**)
- `https://www.googleapis.com/auth/contacts.other.readonly` — read auto-saved "other" contacts (**sensitive**, read-only)
- `https://www.googleapis.com/auth/script.processes` — read Apps Script execution history (**sensitive**, read-only)

Every one of these is a Google **sensitive** scope (except `gmail.labels`, which is **non-sensitive**); **none is restricted**, so the app needs sensitive-scope OAuth verification but no CASA security assessment. The four most recent (`gmail.send`, `gmail.labels`, `contacts.other.readonly`, `script.processes`) were added together as CASA-free scope growth so the verification covers the maximal app in one pass — each was checked against Google's restricted-scope list and confirmed not restricted. In particular the broad/read Gmail scopes (`mail.google.com`, `gmail.readonly`, `gmail.modify`, etc.) and `drive.readonly` (Google's only RESTRICTED scope this app ever requested) are deliberately NOT requested, to preserve the no-CASA posture (see `auth.py:WORKSPACE_SCOPES` for the per-scope rationale and the `drive.readonly` removal note). The HTTP/cloud connector flow additionally requests `openid` + `userinfo.email` for identity, for 17 scopes total; see `oauth_google.py:GOOGLE_API_SCOPES`.

> **Verification posture (why the code lists more scopes than the consent screen currently under review shows).** The live OAuth verification round currently covers the base set (Docs / Drive.file / Sheets / Slides / Apps Script + identity). The additional sensitive services (Calendar, Tasks, Forms, Contacts, Gmail send + the `contacts.other.readonly` / `script.processes` reads) reach existing users via Google's incremental-consent flow (`include_granted_scopes=true`), and their live rollout is held back by the CI deploy gate (`DEPLOY_ENABLED` repo variable set to `false`, which halts auto-deploy on push to `main`) until their own verification round (this project verifies LAST, after the surface is complete). So `auth.py:WORKSPACE_SCOPES` legitimately enumerates the full target set in code while the consent screen currently under review shows the subset already submitted. None of the additional scopes is restricted, so they add no CASA requirement. Each new scope still needs a demo scene in the verification recording (handled separately in the recorder).

## Apps Script setup (required for converting existing docs)

`gdocs_tab_existing_doc` and the retrofit path use a helper Apps Script Web App for lossless content restructuring (tables, drawings, cell shading — content REST can't re-emit). **Skip this section** if you only use `gdocs_make_tabbed_doc` and edit tools.

### Automated (recommended)

```bash
google-docs-mcp setup-apps-script-auto
```

Does everything end-to-end via the Apps Script REST API: creates the project, pushes `restructure.gs` (with a per-deployment HMAC key baked in), deploys as a Web App with `executeAs: USER_DEPLOYING / access: ANYONE_ANONYMOUS`, and saves the resulting `/exec` URL to `~/.google-docs-mcp/config.json`. The endpoint is anonymous because the server posts with no Google sign-in, but every request is authenticated by a per-deployment HMAC signature verified in `doPost` (v2.0c; see `docs/THREAT_MODEL.md` §4 row 5) — URL secrecy is no longer the access control. First run triggers one OAuth consent screen to add Apps Script scopes (`script.projects`, `script.deployments`); subsequent runs reuse the token.

The plumbing lives in `src/appscriptly/gas_deploy/` as a clean sub-package boundary — if a second project ever needs Apps Script project management, that folder can be `git mv`'d out and published as a standalone package.

### Manual (fallback)

If the automated path doesn't work (e.g. you can't run a local browser OAuth flow), print the manual recipe:

```bash
google-docs-mcp setup-apps-script    # prints step-by-step UI instructions
# then after deploying via the UI and copying the URL:
google-docs-mcp configure-webapp https://script.google.com/macros/s/.../exec
```

### Advanced: headless via Service Account + Domain-Wide Delegation

For CI pipelines, server-side batch document processing, or IT-managed multi-user provisioning — anywhere no human can click an OAuth consent. **Google Workspace only** (personal `@gmail.com` accounts have no Admin Console and cannot use DWD).

One-time admin setup:
1. Create a Service Account in GCP, download its JSON key
2. Admin Console → Security → Access and data control → API controls → **Manage Domain Wide Delegation** → Add new → paste the SA's numeric Client ID
3. Authorize these scopes for that DWD entry (comma-separated):
   ```
   https://www.googleapis.com/auth/script.projects,
   https://www.googleapis.com/auth/script.deployments,
   https://www.googleapis.com/auth/drive.file
   ```
4. Wait for propagation (usually minutes, up to 24h)

Then anyone with the SA key + permission to impersonate a Workspace user runs:

```bash
google-docs-mcp setup-apps-script-auto \
  --auth-mode=service-account \
  --sa-key=/path/to/sa-key.json \
  --impersonate-user=operator@yourdomain.com
```

The resulting Apps Script project is owned by the impersonated user (appears in their Drive). Truly zero-browser from the first call. Trade-off: 10-ish admin-gated setup steps replacing one OAuth tap — only worth it when "no human can click" is a hard constraint.

### Check status

```bash
google-docs-mcp status   # shows configured URL + pings the webapp
```

## Remote HTTP mode (Fly.io + claude.ai cloud chat)

For workflows where the caller can't reach your local machine, the same package runs as a remote HTTP MCP server.

### Endpoints

- `GET /health` — Fly health probe, unauthenticated
- `POST /mcp` — MCP-over-HTTP transport (claude.ai custom connector talks here)
- `POST /api/convert` — multipart `.docx` upload + conversion. Authenticated via bearer header OR via single-use signed URLs minted by `gdocs_get_signed_upload_url` (the recommended path — no secrets in chat).

### Deploy to Fly

```bash
# One-time
fly launch --no-deploy --copy-config
fly volumes create gdmcp_data --size 1 --region <your-region>
fly secrets set MCP_BEARER_TOKEN=$(openssl rand -hex 32)

# Deploy (use the wrapper — bakes in git_commit + build_time for gdocs_server_info)
./deploy.sh
```

Bootstrap your OAuth token onto the volume:

```bash
fly ssh sftp shell
# Inside:
put ~/.google-docs-mcp/token.json /data/google-docs-mcp/token.json
put ~/.google-docs-mcp/credentials.json /data/google-docs-mcp/credentials.json
put ~/.google-docs-mcp/config.json /data/google-docs-mcp/config.json
exit
fly apps restart
```

Verify: `curl https://<your-app>.fly.dev/health` returns `{"ok":true,...}`.

### Connect from claude.ai (custom connector)

1. Settings → Connectors → **Add custom connector**
2. URL: `https://<your-app>.fly.dev/mcp`
3. (No OAuth fields needed — the `/mcp` endpoint is open by convention; auth lives at `/api/*` and is bypassed by signed URLs)
4. Save → start a new chat. All 153 tools appear (`gdocs_*` plus the `gsheets_*` / `gslides_*` / `as_*` services).

**Also add `<your-app>.fly.dev` to Settings → Capabilities → Additional allowed domains** so cloud chat's Python sandbox can POST to `/api/convert`.

### Cloud chat workflow (signed URL — recommended)

The model calls `gdocs_get_signed_upload_url()` → receives a 10-minute single-use HMAC-signed URL → hands it to its Python sandbox → sandbox POSTs the `.docx` bytes. No bearer token in chat history; no Drive corruption from claude.ai's Drive connector.

```python
import io, json, requests
from docx import Document

URL = "<from get_signed_upload_url>"

doc = Document()
doc.add_heading("Alpha", level=1); doc.add_paragraph("...")
doc.add_heading("Beta", level=1);  doc.add_paragraph("...")
buf = io.BytesIO(); doc.save(buf); buf.seek(0)

r = requests.post(
    URL,
    files={"file": ("doc.docx", buf,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    data={
        "split_by": "heading_1",
        "title": "My tabbed doc",
        "icons_by_title": json.dumps({"Alpha": "🔀", "Beta": "💡"}),
        "placeholder_behavior": "delete",
    },
    timeout=300,
)
print(r.json()["url"])
```

For composing NEW content (no source `.docx`), skip the upload entirely and call `gdocs_make_tabbed_doc(title, tabs=[...])` directly — the markdown content goes through the tool args.

## Usage example (Claude Desktop / Code)

> Use google-docs to create a doc titled "Onboarding" with three tabs:
> "Day 1" with the checklist, "Tools" with setup instructions, and
> "Contacts" with team contact info.

Claude shapes the request into `gdocs_make_tabbed_doc(title, tabs)` and returns the doc URL.

## Testing

```bash
pip install -e ".[test]"
pytest tests/unit -v              # ~1,550 unit tests, no network
pytest tests/integration --live   # 21 live tests, real Drive + OAuth
```

The counts above are approximate; `gdocs_test_manifest()` / `gdocs_server_info()` report the authoritative live numbers (computed at runtime). Unit tests run on every push/PR via GitHub Actions (Python 3.10–3.13 matrix). `deploy.sh` runs them locally before any Fly deploy; override with `SKIP_TESTS=1` for emergency hotfixes only.

## Architecture / source map

| File | What it does |
|---|---|
| `src/appscriptly/server.py` | FastMCP tool wrappers; routing + validation |
| `src/appscriptly/services/docs/api.py` | Google Docs API: tab/content operations |
| `src/appscriptly/services/drive/api.py` | Google Drive API: upload, trash, search, move |
| `src/appscriptly/docx_import.py` | `.docx` → tabbed Google Doc pipeline |
| `src/appscriptly/retrofit.py` | Inject Heading 1 markers into styled `.docx` |
| `src/appscriptly/preview.py` | Dry-run tab-split detection |
| `src/appscriptly/restructure.gs` | Apps Script Web App for lossless content moves |
| `src/appscriptly/http_server/` | Starlette REST + signed-URL middleware (package: `app.py` + `middleware.py` + `routes/`) |
| `src/appscriptly/crypto.py` | HMAC signing for upload URLs |
| `src/appscriptly/errors.py` | Friendly error mapping for known Google API failures |

## Known limitations

- **`setActiveTab` (persistent default-tab setting) not exposed.** The Docs REST API doesn't support it; the Apps Script `DocumentApp.setActiveTab()` does but would require extending our Web App. For the common case (linking the user to a specific tab), `gdocs_get_tab_url` returns a `?tab=t.xxx` deep link that achieves the same UX.
- **Drive's converter 500s on `.docx` files using `<w:sym w:char="00A0"/>` for NBSP.** Our retrofit handles this construct correctly in-memory (the run-fragmentation fix), but Drive can't convert the document. Use the literal `\xa0` character inside `<w:t>` (the form Word actually produces) instead.
- **Per-tab headers/footers not supported.** Tabs render as continuous scroll in the Docs UI; page headers only matter for paginated PDF export — a narrow case not worth the Apps Script complexity.

## Caveats

- Tokens are stored unencrypted at `~/.google-docs-mcp/token.json`. Don't sync that path to a shared drive.
- **Stdio mode is single-user** (one OAuth identity per machine). HTTP mode (Fly deploy + claude.ai connector) is multi-tenant — each user's state is keyed by their Google `sub` claim in `user_state.db` (see `src/appscriptly/user_store.py`).
- Drive's `drive.file` scope restricts writes to files this app created. Trash/untrash/move on externally-uploaded files returns `reason: "app_not_authorized"` (soft-failure, not raised — see `gdocs_find_doc_by_title`'s `owned_by_app` flag).
- The central Apps Script Web App is NOT required by any tool at runtime since the pure-REST tab transplant (#222): `gdocs_tab_existing_doc`, retrofit, and `/api/convert` run entirely on the Docs/Drive REST APIs, and the `as_*` installers drive the Apps Script API with their own per-automation scripts. `server_health` reports the runtime's state for observability only.
- If a connector tool call returns "No approval received", that is claude.ai's own approval popup (shown on first use of new or destructive tools) - approve the prompt in the chat UI and retry. It is client-side, not a server error.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability disclosure.

**Privacy:** see [docs/PRIVACY.md](docs/PRIVACY.md) for what we store, how long, and your rights.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
