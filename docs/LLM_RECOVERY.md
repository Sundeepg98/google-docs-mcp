# LLM Recovery Matrix

Source-of-truth mapping from server failure shapes to LLM recovery
actions. Mirrored at runtime by `_RECOVERY_TABLE` in
`src/google_docs_mcp/resources.py` and surfaced via:

- MCP resource `gdocs://error-recovery` (full table)
- MCP resource `gdocs://error-recovery/{key}` (single entry)
- MCP tool `gdocs_help(error_message=...)` (substring lookup)

A CI guard (`test_recovery_table_matches_doc`) asserts the key set in
this file equals the key set in `_RECOVERY_TABLE` — drift is a build
failure, not a silent skew.

## How to extend

1. Pick a stable, snake_case **key** (matches `## key:` heading below
   AND `_RECOVERY_TABLE` dict key in `resources.py`).
2. Add a section here using the template:
   ```
   ## key: <snake_case_key>

   **Pattern:** literal substring agents will see in `error_message`
   **Severity:** info | warning | error
   **Retriable:** true | false
   **Wait seconds:** <int or null>
   **Related tool:** `gdocs_<name>` (or `null`)

   **Do:** what the LLM should do next.
   **User message:** what the LLM should say to the user.
   ```
3. Add the matching dict entry to `_RECOVERY_TABLE` in
   `resources.py`. Re-run `uv run pytest tests/unit/test_llm_recovery.py`.

---

## key: no_split_points_found

**Pattern:** `warnings: ["no_split_points_found"]`
**Severity:** warning
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_preview_tab_split`

**Do:** The .docx had no headings or visible split markers, so the
tabbifier could not auto-segment it. Re-run `gdocs_preview_tab_split`
with explicit `split_on` markers, OR fall back to a single-tab import
using `gdocs_retrofit_existing_docx` with `force_single_tab=true`.

**User message:** "I could not detect any natural section breaks in that
document. Want me to import it as a single tab, or do you have specific
heading text I should split on?"

---

## key: owned_by_app_false

**Pattern:** `owned_by_app: false`
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

---

## key: needs_authorization

**Pattern:** `NeedsReauthError`
**Severity:** error
**Retriable:** true
**Wait seconds:** null
**Related tool:** `gdocs_reset_authorization`

**Do:** The user's Google OAuth grant is missing, expired, or revoked.
The response shape includes a fresh `auth_url`. Surface that URL to
the user as a clickable link — do NOT silently retry the tool call.
After the user completes consent, retry the original tool call once.

**User message:** "Your Google authorization needs a refresh. Please
open this link to re-consent, then tell me to retry: <auth_url>"

---

## key: apps_script_modified

**Pattern:** `Apps Script Web App was modified outside this server`
**Severity:** error
**Retriable:** true
**Wait seconds:** null
**Related tool:** `gdocs_setup_apps_script`

**Do:** The Apps Script Web App attached to this user was edited (or
re-deployed) outside the MCP server's control, so the cached
deployment URL / script_id is stale. Call `gdocs_setup_apps_script`
to regenerate the deployment and refresh the cached state. Then retry
the original tool once.

**User message:** "Your Apps Script helper was changed outside this
connection. I am re-deploying a fresh copy now — one moment."

---

## key: rate_limited

**Pattern:** `Google API error: 429`
**Severity:** warning
**Retriable:** true
**Wait seconds:** 60
**Related tool:** null

**Do:** Google returned HTTP 429 (per-user / per-project quota
exceeded). Wait `wait_seconds` (default 60) before retrying the SAME
call. If retry also 429s, batch nearby operations into a single
`gdocs_make_tabbed_doc` or fewer `gdocs_append_to_tab` calls instead
of looping per-paragraph.

**User message:** "Google rate-limited me. I will wait about a minute
and retry — or I can batch the remaining work into one call if you
want to skip the wait."

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

---

## key: placeholder_behavior

**Pattern:** `placeholder_behavior`
**Severity:** info
**Retriable:** false
**Wait seconds:** null
**Related tool:** `gdocs_make_tabbed_doc`

**Do:** The response refers to ambiguous `placeholder_behavior` —
empty / null tab content was either kept as a literal empty tab or
auto-filled with a placeholder paragraph. Resolution depends on the
caller's intent: if the doc is a template skeleton, pass
`placeholder_behavior="keep_empty"`; if it is for human reading, pass
`placeholder_behavior="add_placeholder"`. Re-run the tool with the
explicit kwarg rather than guessing.

**User message:** "One of the tabs had no content. Should I leave it
visibly empty, or insert a short 'TBD' placeholder so the tab is not
confusing in the sidebar?"
