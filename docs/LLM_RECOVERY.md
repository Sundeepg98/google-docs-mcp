# LLM Recovery Matrix

Source-of-truth mapping from server failure shapes to LLM recovery
actions. Mirrored at runtime by `_RECOVERY_TABLE` in
`src/google_docs_mcp/resources.py` and surfaced via:

- MCP resource `gdocs://error-recovery` (full table)
- MCP resource `gdocs://error-recovery/{key}` (single entry)
- MCP tool `gdocs_help(error_message=...)` (substring lookup,
  case-insensitive)

A CI guard (`test_recovery_table_matches_doc`) asserts the key set in
this file equals the key set in `_RECOVERY_TABLE` — drift is a build
failure, not a silent skew. A second CI guard
(`test_round_trip_realistic_response_matches`) constructs a realistic
failing tool response for each key, `json.dumps` it, and asserts
`gdocs_help` matches — catches "pattern doesn't match the wire form"
regressions before they ship.

## How patterns work

Patterns are literal substrings checked against the error string the
LLM passes back. The matcher (`gdocs_help`) lowercases both sides
before comparing, so case skew is tolerated. The wire form of dict
returns is JSON (not Python repr), so patterns for dict-shaped
errors should target the JSON form, e.g. `"reason": "not_found"`
(JSON, lowercase `false`/`true`, double-quoted keys) — NOT
`'reason': 'not_found'` (Python repr).

## How to extend

1. Pick a stable, snake_case **key** (matches `## key:` heading below
   AND `_RECOVERY_TABLE` dict key in `resources.py`).
2. Read the real emitter in source to confirm the wire form. Grep
   for the field name across `src/` — find every code path that
   produces it.
3. Add a section here using the template:
   ```
   ## key: <snake_case_key>

   **Pattern:** literal substring agents will see in `error_message`
   **Severity:** info | warning | error
   **Retriable:** true | false
   **Wait seconds:** <int or null>
   **Related tool:** `gdocs_<name>` (or `null`)
   **Planned:** true | false  (omit if false)

   **Do:** what the LLM should do next. MUST reference REAL kwargs
   and enum values from the actual tool signatures.
   **User message:** what the LLM should say to the user.
   ```
4. Add the matching dict entry to `_RECOVERY_TABLE` in
   `resources.py`. Re-run `uv run pytest tests/unit/test_llm_recovery.py`.

---

## key: no_split_points_found

**Pattern:** `no_splits`
**Severity:** warning
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_preview_tab_split`

**Do:** The .docx had no headings or visible split markers, so the
tabbifier could not auto-segment it. Re-run `gdocs_preview_tab_split`
with a different `split_by` value (try `"heading_2"`, `"page_break"`,
or `"auto"` instead of the default `"heading_1"`), OR fall back to
retrofit mode by calling `gdocs_tab_existing_doc` with an explicit
`markers=[{"marker_text": str, "tab_title": str}, ...]` list — that
bypasses heading detection entirely. If the document is truly
single-section, call `gdocs_make_tabbed_doc` with one tab whose
content is the full document text.

**User message:** "I could not detect any natural section breaks in
that document. Want me to try a different split style (e.g. Heading
2 or page breaks), or do you have specific phrases I should use as
section markers?"

**Emitter:** `src/google_docs_mcp/restructure.gs:69` returns
`warnings: ['no_splits']` which serializes over JSON as
`"warnings": ["no_splits"]`. Pattern `no_splits` matches the bare
token — robust across JSON / Python repr / future array renames.

---

## key: owned_by_app_false

**Pattern:** `"owned_by_app": false`
**Severity:** warning
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_find_doc_by_title`

**Do:** The matched Drive file exists but was NOT created by this MCP
server, so write operations (trash, move, append) will return
`appNotAuthorizedToFile` (403). Do not attempt destructive writes.
Either ask the user to re-upload the file via this server, or restrict
the workflow to read-only tools (`gdocs_read_doc`, `gdocs_get_doc_outline`).

**User message:** "I found that document, but it was not created
through this connection — Google's permissions stop me from editing or
deleting files I do not own. I can still read it. Would you like me to
read it, or re-create it through me?"

**Emitter:** `src/google_docs_mcp/drive_api.py:392` sets
`m["owned_by_app"] = write_results.get(m["file_id"], False)`. MCP
transports return JSON, so the LLM sees `"owned_by_app": false`
(lowercase boolean, double-quoted key). Pattern targets the JSON form;
case-insensitive matcher handles either casing.

---

## key: needs_authorization

**Pattern:** `Click here to authorize`
**Severity:** error
**Retriable:** true
**Wait seconds:** null
**Related tool:** `gdocs_reset_authorization`

**Do:** The user's Google OAuth grant is missing, expired, or revoked.
The `ToolError` body contains a Markdown link to the consent URL.
Surface that URL to the user as a clickable link — do NOT silently
retry the tool call. After the user completes consent, retry the
original tool call once.

**User message:** "Your Google authorization needs a refresh. Please
open the 'Click here to authorize' link in the error message to
re-consent, then tell me to retry."

**Emitter:** `src/google_docs_mcp/server.py:206-210` raises
`ToolError("Google API access required.\n\n**[Click here to
authorize](...)**\n\nAfter granting access, re-run this tool.")` —
also `server.py:1807-1808` for the setup_apps_script path. The
class name `NeedsReauthError` does NOT appear in the LLM-visible
string (it's the internal exception type, wrapped before surfacing).
Pattern targets the rendered Markdown link text, which is identical
in both emitter paths.

---

## key: apps_script_modified

**Pattern:** `Apps Script Web App was modified outside this server`
**Severity:** error
**Retriable:** true
**Wait seconds:** null
**Related tool:** `gdocs_install_automation`
**Planned:** true

**Do:** PLANNED v2.0 entry — no current code path emits this string.
When v2.0 ships, the Workspace automation runtime attached to the
user will be hash-checked; an out-of-band edit will surface this
error. Recovery: call `gdocs_install_automation` to re-install the
runtime and refresh cached state, then retry the original tool once.

**User message:** "Your Workspace automation runtime was changed
outside this connection. I am re-installing it now — one moment."

**Emitter:** none yet (planned for v2.0 strict-flip). Round-trip test
skips `planned=true` entries because there's no real failure
response to construct.

---

## key: rate_limited

**Pattern:** `Google API error: 429`
**Severity:** warning
**Retriable:** true
**Wait seconds:** 60
**Related tool:** `null`

**Do:** Google returned HTTP 429 (per-user / per-project quota
exceeded). Wait `wait_seconds` (default 60) before retrying the SAME
call. If retry also 429s, batch nearby operations into a single
`gdocs_make_tabbed_doc` or fewer `gdocs_append_to_tab` calls instead
of looping per-paragraph.

**User message:** "Google rate-limited me. I will wait about a minute
and retry — or I can batch the remaining work into one call if you
want to skip the wait."

**Emitter:** `src/google_docs_mcp/errors.py:69`
`friendly_http_error_message` returns
`f"Google API error: {status_code} {reason}. Details: {details}"`.
For HTTP 429 that's `"Google API error: 429 Too Many Requests. ..."`.

---

## key: app_not_authorized

**Pattern:** `"reason": "app_not_authorized"`
**Severity:** warning
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_trash_file`

**Do:** Soft-failure return (NOT a raised exception). The Drive file
exists but was not created by this MCP, so Google denied the
trash/move/update. Do not retry — Google's policy will not change on
retry. Tell the user the file is external; offer to read-only it or
ask them to delete it from Drive UI themselves.

**User message:** "I cannot delete that file because it was not
created through me. You can remove it directly from Google Drive, or
ask me to read its contents instead."

**Emitter:** `src/google_docs_mcp/drive_api.py:260` (trash path),
`drive_api.py:493` (move path), `drive_api.py:606` (untrash path) all
return soft-failure dicts `{"reason": "app_not_authorized", ...}`
serialized to JSON wire as `"reason": "app_not_authorized"`.

---

## key: not_found

**Pattern:** `"reason": "not_found"`
**Severity:** warning
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_find_doc_by_title`

**Do:** Soft-failure return. The file_id passed does not resolve in
the user's Drive — either it was trashed, the user lacks access, or
the ID is wrong. Do NOT raise; surface the friendly message. Offer
to call `gdocs_find_doc_by_title` with the user's recollection of
the doc title to recover the correct ID.

**User message:** "I could not find that document. It may have been
deleted, or the ID I have is wrong. Do you remember the title? I can
search by name."

**Emitter:** `src/google_docs_mcp/drive_api.py:229` (trash path),
`drive_api.py:426` (move path), `drive_api.py:571` (untrash path) all
return soft-failure dicts `{"reason": "not_found", ...}` serialized
to JSON wire as `"reason": "not_found"`.

---

## key: unexpected_exception

**Pattern:** `KeyError`
**Severity:** error
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_server_info`

**Do:** An exception leaked through the soft-failure boundary
(`KeyError`, `AttributeError`, `TypeError`, etc.). This is a bug in
the server, NOT a user error. Do not auto-retry — the same call will
fail the same way. Capture `gdocs_server_info()` (version + commit)
and the exception text and ask the user to file an issue. If urgent,
suggest a workaround using a sibling tool (e.g. read instead of
edit, or single-tab create instead of multi-tab).

**User message:** "Something unexpected went wrong on the server side.
This looks like a bug — would you mind sharing the error text so it
can be reported? I can try a simpler workaround in the meantime."

**Emitter:** any unhandled `KeyError` / `AttributeError` / `TypeError`
that leaks past tool boundaries gets wrapped by FastMCP as a
`ToolError` whose message contains the exception class name.
Substring `KeyError` matches the most common shape; the
`suggestion` field nudges callers toward `gdocs_server_info` for
bug reports when the pattern misses.

---

## key: placeholder_behavior

**Pattern:** `placeholder_behavior`
**Severity:** info
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_tab_existing_doc`

**Do:** The response refers to the `placeholder_behavior` kwarg
(controls what `gdocs_tab_existing_doc` does with the leading
placeholder tab after retrofit). The real enum
(`src/google_docs_mcp/server.py:509`) is
`Literal["delete", "rename", "keep"]`. Choose by intent:
`"delete"` removes the placeholder once content is split (default,
cleanest for human-readable docs); `"rename"` keeps it as the tab
named by `placeholder_title` (e.g. `"Overview"`); `"keep"` leaves
it as-is. Re-run the tool with the explicit enum value rather than
guessing.

**User message:** "One of the tabs had no content. Should I leave it
visibly empty (keep), rename it to 'Overview' (rename), or remove it
entirely (delete — the default)?"

**Emitter:** any tool docstring / error mentioning the kwarg.
Pattern is the bare kwarg name; the `do` field lists the ACTUAL
enum values, NOT the hypothetical `"keep_empty"` / `"add_placeholder"`
that earlier drafts referenced (those would cause `ValidationError`).
