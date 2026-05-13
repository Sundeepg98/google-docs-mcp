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

```bash
git clone https://github.com/Sundeepg98/google-docs-mcp.git
cd google-docs-mcp
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
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
(macOS) and add:

```json
{
  "mcpServers": {
    "google-docs": {
      "command": "/absolute/path/to/google-docs-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/google-docs-mcp/server.py"]
    }
  }
}
```

On Windows escape backslashes (`C:\\path\\to\\...\\python.exe`) and
point at `.venv\\Scripts\\python.exe`.

Restart Claude Desktop. The tool icon should now list
`create_tabbed_doc` under `google-docs`.

### Wire to Claude Code

Add the same `mcpServers` entry to `~/.claude.json` (user-scope) or to
a project's `.mcp.json` (project-scope). After editing, run `/mcp`
inside Claude Code and reconnect, or restart the session.

### First-run OAuth

The first call opens your default browser. Sign in to Google → grant
the Docs scope. The token is cached to `credentials/token.json`. After
that no browser dance — refresh tokens are used silently.

Required scopes:
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/drive.file`

## Usage example

In Claude Desktop or Claude Code:

> Use google-docs to create a doc titled "Onboarding" with three tabs:
> "Day 1" with the day-one checklist, "Tools" with the tool setup
> instructions, and "Contacts" with the team contact info.

Claude shapes your input into the `tabs` parameter and returns the doc
URL.

## Customizing content rendering

The interesting design decision lives in `docs_api.py`, function
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
