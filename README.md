# google-docs-mcp

A local stdio [MCP](https://modelcontextprotocol.io/) server for **Claude
Desktop** and **Claude Code** that creates Google Docs with the native
**Tabs** feature (October 2024) — each tab is a separately-navigable
section in the Docs sidebar, not just an outline heading.

## Why this exists

Google Docs Tabs are a Google-Docs-native concept. They do **not** exist
in the `.docx` / OOXML spec, so any pipeline that round-trips through
`.docx` collapses to one tab. The only way to create them
programmatically is to call the Google Docs API directly:
`addDocumentTab` requests for structure + `insertText` requests scoped
to each tab via `location.tabId`. This server wraps that flow and
exposes one MCP tool.

## Tool reference

### `create_tabbed_doc(title, tabs) -> {doc_id, url, tabs}`

| Param | Type | Description |
|---|---|---|
| `title` | `string` | Document title (shown in Google Drive). |
| `tabs`  | `[{title: string, content: string}]` | One entry per tab. Order preserved; first entry becomes the default tab. |

Returns the new document's ID and URL plus the generated tab IDs.

## Setup

### Requirements

- Python 3.10+
- A Google Cloud project with the **Google Docs API** enabled
- An OAuth 2.0 Client ID of type **Desktop app** (downloaded as JSON
  from Google Cloud Console → APIs & Services → Credentials)

### Install

**Recommended — `pipx`** (isolated, single global command):

```bash
pipx install git+https://github.com/Sundeepg98/google-docs-mcp.git
```

This installs the `google-docs-mcp` command into its own isolated venv,
available globally. Upgrade later with `pipx upgrade google-docs-mcp`.

**Alternative — clone and `pip install -e .`** (for development):

```bash
git clone https://github.com/Sundeepg98/google-docs-mcp.git
cd google-docs-mcp
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e .
```

### OAuth client config

The server looks for the OAuth client config (the JSON downloaded from
Cloud Console) in this order:

1. Path in `GOOGLE_DOCS_OAUTH_PATH` environment variable
2. `./credentials/credentials.json` (relative to the server)
3. `~/.gmail-mcp/gcp-oauth.keys.json` (reuse existing
   [gmail-mcp](https://github.com/GongRzhe/Gmail-MCP-Server) keys —
   same Cloud project = same OAuth client)

Pick whichever fits. If you already use gmail-mcp, option 3 means zero
extra setup. Otherwise:

```bash
mkdir credentials
mv ~/Downloads/client_secret_*.json credentials/credentials.json
```

User tokens are always written to `./credentials/token.json` regardless
of where the client config came from. Both files are in `.gitignore`.

### Wire to Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS).

**With `pipx` install** (the command is on your PATH):

```json
{
  "mcpServers": {
    "google-docs": {
      "command": "google-docs-mcp"
    }
  }
}
```

**With dev install** (point at the venv's entry-point script):

```json
{
  "mcpServers": {
    "google-docs": {
      "command": "/absolute/path/to/google-docs-mcp/.venv/bin/google-docs-mcp"
    }
  }
}
```

On Windows the script lives at `.venv\\Scripts\\google-docs-mcp.exe` —
escape backslashes in JSON.

Restart Claude Desktop. The tool icon should now list
`create_tabbed_doc` under `google-docs`.

### Wire to Claude Code

Add the same `mcpServers` entry to `~/.claude.json` (user-scope) or to
a project's `.mcp.json` (project-scope). After editing, run `/mcp`
inside Claude Code and reconnect, or restart the session.

### First-run OAuth

The first call opens your default browser. Sign in to Google → grant
the Docs scope. The token is cached at `~/.google-docs-mcp/token.json`
(override with `GOOGLE_DOCS_DATA_DIR`). After that no browser dance —
refresh tokens are used silently.

Required scopes:
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/drive.readonly` (added v0.8.0, for
  reading `.docx` files uploaded by other apps — e.g. Claude.ai cloud
  chat's Drive connector)

## Remote HTTP mode (Fly.io)

For workflows where the caller can't reach your local machine (e.g.
Claude.ai cloud chat with files in its sandbox), the same package runs
as a remote HTTP server.

Endpoints:
- `GET  /health` — Fly.io health check, unauthenticated
- `POST /api/convert` — multipart `.docx` upload + conversion. Form
  fields: `file` (the .docx), `split_by` (optional), `title` (optional).
  Returns the same JSON shape as the MCP tool.
- `POST /mcp/*` — proper MCP-over-HTTP transport for any future
  Claude surface that supports MCP custom connectors.

All non-health endpoints require an `Authorization: Bearer <token>`
header (token set via `MCP_BEARER_TOKEN` env var).

### Deploy to Fly.io

```bash
# One-time
fly launch --no-deploy --copy-config
fly volumes create gdmcp_data --size 1 --region <your-region>
fly secrets set MCP_BEARER_TOKEN=$(openssl rand -hex 32)

# Deploy
fly deploy
```

Then upload your existing OAuth token and (optional) Apps Script config
to the persistent volume:

```bash
# Use the Fly SSH SFTP shell
fly ssh sftp shell
# Inside the shell:
put ~/.google-docs-mcp/token.json /data/google-docs-mcp/token.json
put ~/.gmail-mcp/gcp-oauth.keys.json /data/google-docs-mcp/credentials.json
put ~/.google-docs-mcp/config.json /data/google-docs-mcp/config.json
exit
fly apps restart
```

After deploy, `curl https://<your-app>.fly.dev/health` should return
`{"ok":true,...}`.

### Cloud chat workflow

In Claude.ai cloud chat, ask Claude to write a Python script that POSTs
the `.docx` to your Fly.io endpoint:

```python
import requests, os
with open('/mnt/user-data/outputs/QA_Bank.docx', 'rb') as f:
    r = requests.post(
        'https://<your-app>.fly.dev/api/convert',
        headers={'Authorization': f'Bearer {os.environ["MCP_TOKEN"]}'},
        files={'file': ('QA_Bank.docx', f,
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        data={'split_by': 'heading_1', 'title': 'Q&A Bank tabified'},
    )
print(r.json()['url'])
```

The script runs in cloud chat's sandbox; the bytes flow through HTTPS
to your Fly.io endpoint without going through MCP tool args (which
have practical size limits).

## Usage example

In Claude Desktop or Claude Code:

> Use google-docs to create a doc titled "Onboarding" with three tabs:
> "Day 1" with the day-one checklist, "Tools" with the tool setup
> instructions, and "Contacts" with the team contact info.

Claude shapes your input into the `tabs` parameter and returns the doc
URL.

## Customizing content rendering

The interesting design decision lives in
`src/google_docs_mcp/docs_api.py`, function
`render_content_to_requests(content, tab_id)`. The default inserts
plain text. Replace it with whatever rendering you want:

| Input convention | Implementation |
|---|---|
| Markdown `# heading` | `insertText` + `updateParagraphStyle` with `HEADING_1` |
| Markdown `- bullets` | `insertText` then `createParagraphBullets` |
| Fenced ``` ``` ``` blocks | `insertText` + `updateTextStyle` with monospace `weightedFontFamily` |
| Tables | `insertTable` + `insertText` per cell |

Full list of 43 request types:
https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request

## Caveats

- Tab support in the Docs API (`addDocumentTab`, `deleteTab`,
  `updateDocumentTabProperties`) is relatively recent. If a request
  errors, check the [release notes](https://developers.google.com/workspace/docs/release-notes).
- Tokens in `credentials/token.json` are stored unencrypted on disk.
  The `.gitignore` excludes the directory — keep it that way.
- This is a single-user local server. For multi-user / cloud use,
  refactor to a remote HTTP MCP with proper per-user OAuth (CIMD/DCR).

## License

MIT — see [LICENSE](LICENSE).
