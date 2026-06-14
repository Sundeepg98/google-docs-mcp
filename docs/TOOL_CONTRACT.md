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

> **This section is an ABBREVIATED contract surface, not the full tool
> inventory.** It details one tool fully (§3.1) and names ~22 of the
> `gdocs_*` tools (§3.2–3.22). The **live** surface is much larger and
> includes the Sheets / Slides / Apps Script / Forms / Calendar /
> Contacts / Tasks / Gmail verticals (`gsheets_*`, `gslides_*`, `as_*`,
> `gforms_*`, `gcal_*`, `gcontacts_*`, `gtasks_*`, `gmail_*`) plus newer
> `gdocs_*` tools (e.g. `gdocs_insert_table`, `gdocs_format_range`,
> `gdocs_export_doc`, `gdocs_find_file`, `gdocs_create_folder`,
> `gdocs_share_file`, `gdocs_list_permissions`, `gdocs_revoke_permission`)
> that this abbreviated doc does not enumerate. **The authoritative,
> always-current inventory is `server_info()` (full `tools` list +
> contract metadata) and `server_guide()`** — treat those as the source
> of truth; this doc is a hand-written excerpt that trails the code.

### 3.0 New data categories (CASA-free scope growth)

The most recent scope additions are all **CASA-free** (none is on Google's
restricted list, so no security assessment is required). Each scope ships
with at least one tool and touches a distinct data category:

| Scope | Sensitivity | Data category | Tool(s) | One-line justification |
|---|---|---|---|---|
| `gmail.send` | Sensitive | Send email on the user's behalf | `gmail_send_message` | Lets the assistant send mail the user composes; send-only — grants **no** mailbox read (the restricted `gmail.readonly`/`gmail.modify` are deliberately not requested). |
| `gmail.labels` | Non-sensitive | Manage Gmail labels | `gmail_create_label`, `gmail_list_labels`, `gmail_delete_label` | Create/list/delete label **objects** for mail organization; cannot read messages or relabel them (that would need restricted `gmail.modify`). |
| `contacts.other.readonly` | Sensitive | Read auto-saved "other" contacts | `gcontacts_list_other_contacts` | Read-only access to the auto-saved "other contacts" list so the assistant can surface people the user has corresponded with but not explicitly saved. |
| `script.processes` | Sensitive | Read Apps Script execution history | `as_list_script_processes` | Read-only "did my automation run / did it fail?" observability over a script project's executions; runs nothing and mutates nothing. |

Return shapes for these tools follow §2's universal contract; per-tool
output schemas live in `tool_schemas.py` (queryable on a live server via
`server_info()`).

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

Tools 3.2 through 3.22 follow the same per-tool entry pattern. Reference the per-service `src/appscriptly/services/*/tools.py` modules (or `gdocs_server_info()` / `gdocs_guide()` on a live server) for the live docstrings + contract versions for: `gdocs_tab_existing_doc`, `gdocs_preview_tab_split`, `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_delete_tab`, `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_replace_all_text`, `gdocs_get_tab_url`, `gdocs_find_doc_by_title`, `gdocs_move_to_folder`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_get_signed_upload_url`, `gdocs_install_automation` (and its deprecated alias `gdocs_setup_apps_script`), `gdocs_reset_authorization`, `gdocs_guide`, `gdocs_server_info`, `gdocs_test_manifest`.

(NOTE: this is the abbreviated version of the full doc — see the callout at the top of §3. The per-tool docstrings live in the `services/*/tools.py` modules and are queryable via `gdocs_guide()` / `gdocs_server_info()`. Future PRs will expand this doc with full per-tool entries, including the `gsheets_*` / `gslides_*` / `as_*` surfaces.)

### Previously planned, now deferred indefinitely

- `gdocs_update_tabs(doc_id, updates)` — earlier roadmap considered this as a successor to `gdocs_rename_tab` + `gdocs_set_tab_icons`. **Deferred indefinitely**: YAGNI at current scale, and the existing pair works fine for the patterns in production use.
- `gdocs_set_trashed(file_id, trashed: bool)` — earlier roadmap considered this as a successor to `gdocs_trash_file` + `gdocs_untrash_file`. **Deferred indefinitely**: same rationale; the existing pair is clearer at call sites and has no observed pain point.

`gdocs_rename_tab`, `gdocs_set_tab_icons`, `gdocs_trash_file`, and `gdocs_untrash_file` are therefore first-class tools with no successor — they will not be deprecated by this roadmap.

## 4. Migration notes

No tool-surface migrations needed across v1 → v2: zero tools removed, zero tool-argument shapes tightened, zero return-shape breakages. The v2.0.1-cleanup PR (#37) walked back the previously-planned `gdocs_update_tabs` / `gdocs_set_trashed` superseders, so the v1.x tool surface is preserved in full under v2.x.

The v2.x cutover has operator-side migration steps (HKDF strict-flip + `apps_script_hmac_key` backfill — the per-request HMAC verify-path is wired as of v2.0c) but those are deploy concerns, not consumer-contract concerns. Operators: see `docs/MIGRATION_v1_to_v2.md`. Consumers (LLMs, tool-routing layers): no contract changes between v1 and v2.

For the broader policy on what counts as a breaking change and what does not, see `docs/COMPATIBILITY_POLICY.md`.

## 5. Cross-references

- Mutation guards that fence per-tool contracts: see `scripts/mutation_check.py` and `gdocs_server_info().test_suite.mutation_check.results`.
- Test-suite manifest covering each tool: see `gdocs_test_manifest()` output.
- For threat-model context on tool capabilities (especially write-tool risk), see `docs/THREAT_MODEL.md` §4.
