# google-docs-mcp

[![tests](https://github.com/Sundeepg98/google-docs-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/Sundeepg98/google-docs-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io/) server for **Claude Desktop**,
**Claude Code**, and **claude.ai** that creates and edits Google Docs
using the native **Tabs** feature (October 2024+) — each tab is a
separately-navigable section in the Docs sidebar, not just an outline
heading.

Works as: local stdio MCP (Claude Desktop / Code) **or** remote
HTTP MCP (claude.ai custom connector via Fly.io).

## Why this exists

Google Docs Tabs are a Google-Docs-native concept. They do **not** exist
in the `.docx` / OOXML spec, so any pipeline that round-trips through
`.docx` collapses to one tab. The only way to create tabs
programmatically is to call the Google Docs API directly. This server
wraps that flow + the supporting Drive operations into 18 tools
(`gdocs_*`-prefixed) covering the full lifecycle: create, edit, read,
find, retrofit, trash/untrash, and convert existing docs.

## Tool index

All tools prefixed `gdocs_` for namespacing. Call `gdocs_server_info`
on a live server to get the authoritative list with descriptions.

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
| **Server identity / inventory** | `gdocs_server_info()` |

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
    "google-docs": { "command": "google-docs-mcp" }
  }
}
```

For dev installs, point at the venv entry-point: `/abs/path/google-docs-mcp/.venv/bin/google-docs-mcp` (or `.venv\Scripts\google-docs-mcp.exe` on Windows).

Restart Claude Desktop. The 18 `gdocs_*` tools should appear.

### Wire to Claude Code

Add the same `mcpServers` entry to `~/.claude.json` (user-scope) or to a project's `.mcp.json` (project-scope). Run `/mcp` and reconnect.

### First-run OAuth

First tool call opens the browser. Sign in → grant scopes. Tokens cached after that; no more browser dance. Required scopes:
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/drive.readonly`

## Apps Script setup (required for converting existing docs)

`gdocs_tab_existing_doc` and the retrofit path use a helper Apps Script Web App for lossless content restructuring (tables, drawings, cell shading — content REST can't re-emit). **Skip this section** if you only use `gdocs_make_tabbed_doc` and edit tools.

### Automated (recommended)

```bash
google-docs-mcp setup-apps-script-auto
```

Does everything end-to-end via the Apps Script REST API: creates the project, pushes `restructure.gs`, deploys as a Web App with `executeAs: USER_DEPLOYING / access: MYSELF`, and saves the resulting `/exec` URL to `~/.google-docs-mcp/config.json`. First run triggers one OAuth consent screen to add Apps Script scopes (`script.projects`, `script.deployments`); subsequent runs reuse the token.

The plumbing lives in `src/google_docs_mcp/gas_deploy/` as a clean sub-package boundary — if a second project ever needs Apps Script project management, that folder can be `git mv`'d out and published as a standalone package.

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
4. Save → start a new chat. The 18 `gdocs_*` tools appear.

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
pytest tests/unit -v              # 62 unit tests, no network
pytest tests/integration --live   # 4 live tests, real Drive + OAuth
```

Unit tests run on every push/PR via GitHub Actions (Python 3.10–3.13 matrix). `deploy.sh` runs them locally before any Fly deploy; override with `SKIP_TESTS=1` for emergency hotfixes only.

## Architecture / source map

| File | What it does |
|---|---|
| `src/google_docs_mcp/server.py` | FastMCP tool wrappers; routing + validation |
| `src/google_docs_mcp/docs_api.py` | Google Docs API: tab/content operations |
| `src/google_docs_mcp/drive_api.py` | Google Drive API: upload, trash, search, move |
| `src/google_docs_mcp/docx_import.py` | `.docx` → tabbed Google Doc pipeline |
| `src/google_docs_mcp/retrofit.py` | Inject Heading 1 markers into styled `.docx` |
| `src/google_docs_mcp/preview.py` | Dry-run tab-split detection |
| `src/google_docs_mcp/restructure.gs` | Apps Script Web App for lossless content moves |
| `src/google_docs_mcp/http_server.py` | Starlette REST + signed-URL middleware |
| `src/google_docs_mcp/crypto.py` | HMAC signing for upload URLs |
| `src/google_docs_mcp/errors.py` | Friendly error mapping for known Google API failures |

## Known limitations

- **`setActiveTab` (persistent default-tab setting) not exposed.** The Docs REST API doesn't support it; the Apps Script `DocumentApp.setActiveTab()` does but would require extending our Web App. For the common case (linking the user to a specific tab), `gdocs_get_tab_url` returns a `?tab=t.xxx` deep link that achieves the same UX.
- **Drive's converter 500s on `.docx` files using `<w:sym w:char="00A0"/>` for NBSP.** Our retrofit handles this construct correctly in-memory (the run-fragmentation fix), but Drive can't convert the document. Use the literal `\xa0` character inside `<w:t>` (the form Word actually produces) instead.
- **Per-tab headers/footers not supported.** Tabs render as continuous scroll in the Docs UI; page headers only matter for paginated PDF export — a narrow case not worth the Apps Script complexity.

## Caveats

- Tokens are stored unencrypted at `~/.google-docs-mcp/token.json`. Don't sync that path to a shared drive.
- This is a single-user server (one OAuth identity). For multi-tenant use, refactor to per-user OAuth at the connector layer.
- Drive's `drive.file` scope restricts writes to files this app created. Trash/untrash/move on externally-uploaded files returns `reason: "app_not_authorized"` (soft-failure, not raised — see `gdocs_find_doc_by_title`'s `owned_by_app` flag).
- Apps Script Web App is a hard prerequisite for `gdocs_tab_existing_doc` and retrofit — the script does what REST can't (preserve drawings/equations/cell shading during content moves).

## Security

See [SECURITY.md](SECURITY.md) for vulnerability disclosure.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE).
