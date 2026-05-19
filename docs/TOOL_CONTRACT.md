# Tool Contract — google-docs-mcp

**Audience:** consumers integrating with the MCP (LLM tool-routing layers, downstream pipelines).
**Version pin:** check `gdocs_server_info().version` to confirm the deployed build matches the contract version below.

## 1. Versioning and deprecation policy

- The MCP follows [Semantic Versioning](https://semver.org/). MAJOR = breaking tool surface; MINOR = additive; PATCH = bug fix.
- Each tool has its own contract version (table in §3). Tool-level contract bumps appear in CHANGELOG.md under `### Changed` or `### Removed`.
- **Deprecated tools live ≥2 minor releases before removal.** Their description starts with `DEPRECATED:` and points at the replacement.
- Consumers pin against contract version via the per-tool entry below. When a deprecated tool's removal release ships, calls to it return `ToolError("tool 'X' was removed in vN.M; use 'Y' instead")`.

## 2. Universal return-shape contract

Every tool returns one of three shapes:

**2.1 Success:** a JSON-serializable dict. Per-tool fields documented in §3.

**2.2 Soft-failure:** a JSON-serializable dict with a `reason` field of type `str` and a `message` field of type `str`. Used when the operation cannot complete but the caller should not treat it as exceptional (e.g., a file isn't trash-able because this app didn't create it). Soft-failures preserve any context fields the tool would have returned on success (e.g., `file_id`, `name`).

**2.3 Hard-fatal:** raises `ToolError(message)`. FastMCP wraps this as `result.isError=True` with `content=[TextContent(text=msg)]` in the MCP protocol. The LLM should render the message to the user and not retry without intervention.

Special hard-fatal: `NeedsReauthError → ToolError` containing a Markdown link beginning with `Google API access required.` Consumers should treat this as a "user must click and re-consent" signal, not a retriable error.

## 3. Per-tool contracts

### 3.1 `gdocs_make_tabbed_doc`

**Added:** v0.5.0. **Contract version:** 1.3. **Status:** stable.

**Input schema:**
```python
class TabSpec(TypedDict, total=False):
    title: Required[str]        # ≤1024 chars; no control chars (U+0000–U+001F, U+007F)
    content: Required[str]      # markdown source
    icon_emoji: NotRequired[str]
    children: NotRequired[list[TabSpec]]  # up to 3 levels deep

# Top-level:
title: str                       # same validation as TabSpec.title
tabs: list[TabSpec]              # ≥1 item
```

**Success return:**
```python
{"doc_id": str, "url": str, "tabs": [{"tab_id": str, "title": str, ...}, ...]}
```

**Hard-fatal `ToolError`:** `"Must provide at least one tab"`, `"title contains control characters"`, `"title must be ≤1024 chars"`, plus wrapped Google API errors via `_format_http_error`.

### 3.2–3.22

Tools 3.2 through 3.22 follow the same per-tool entry pattern. Reference `src/google_docs_mcp/server.py` for the live docstrings + contract versions for: `gdocs_tab_existing_doc`, `gdocs_preview_tab_split`, `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_delete_tab`, `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_replace_all_text`, `gdocs_get_tab_url`, `gdocs_find_doc_by_title`, `gdocs_move_to_folder`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_get_signed_upload_url`, `gdocs_setup_apps_script`, `gdocs_reset_authorization`, `gdocs_guide`, `gdocs_server_info`, `gdocs_test_manifest`.

(NOTE: this is the abbreviated version of the full doc. The full per-tool table is documented inline in `server.py` and queryable via `gdocs_guide()`. Future PRs will expand this doc with full per-tool entries for each.)

### Planned tools (v2.0 — not yet shipping)

- `gdocs_update_tabs(doc_id, updates)` — supersedes `gdocs_rename_tab` + `gdocs_set_tab_icons`. Target shapes: `{by_id: tab_id}` or `{title_contains: substr}`.
- `gdocs_set_trashed(file_id, trashed: bool)` — supersedes `gdocs_trash_file` + `gdocs_untrash_file`.
- `gdocs_help(error_message)` — LLM-callable error-recovery lookup. Wraps the table in `LLM_RECOVERY.md` (shipping v2.2). Pure lookup; no creds required.

The four superseded tools (`gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_trash_file`, `gdocs_untrash_file`) become DEPRECATED shims in v2.0 and are REMOVED in v2.2.

## 4. Migration notes

(Empty until v2.0 ships. At that point this section enumerates the field-shape changes per tool and the recommended migration path for consumers pinned to v1.x contracts.)

## 5. Cross-references

- Mutation guards that fence per-tool contracts: see `scripts/mutation_check.py` and `gdocs_server_info().test_suite.mutation_check.results`.
- Test-suite manifest covering each tool: see `gdocs_test_manifest()` output.
- For threat-model context on tool capabilities (especially write-tool risk), see `docs/THREAT_MODEL.md` §4.
