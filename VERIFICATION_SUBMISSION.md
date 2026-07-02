# appscriptly - Google OAuth Verification SUBMISSION (paint-by-numbers)

> **Refreshed 2026-06-29 to the 17-scope / 134-tool reality.** Companion to `PHASE1_VERIFICATION_KIT.md` (strategy/why); this doc is the **exact-values** sheet for the Google Cloud Console OAuth consent screen + the OAuth Verification Center, for the **final 134-tool surface**.
>
> **Target app:** `appscriptly` · **Live server:** `https://mcp.appscriptly.com/mcp` (custom-domain serving URL since the 2026-06-14 cutover, backed by the Fly app `sundeepg98-docs-mcp` at `https://sundeepg98-docs-mcp.fly.dev`) · **Repo:** `Sundeepg98/google-docs-mcp` (module `src/appscriptly/`).

---

## STATE CHECK FIRST - this is a 17-scope RE-SUBMISSION

The app is build-complete on `main` (134 tools, 12 services, 17 connector scopes, 0 restricted = no CASA). A prior round was submitted with an 8-scope demo; that demo was REJECTED by Google as chat-only / insufficient, redone at 8 scopes, and the scope set has since grown to 17. The T&S verification thread with Google is warm. The remaining go-live work is to bring the consent screen, the served privacy page, and the demo to the SAME 17-scope set, then reply in the thread and submit. Before the orchestrator drives the Console, confirm which of these is true:

1. **Consent screen, privacy page, and demo all at 17** -> reply in the T&S thread + submit; watch the `sundeepg8@` inbox and answer Google's follow-ups fast (the gating item).
2. **A surface is still behind** -> bring it to 17 first. Per the go-live runbook the consent screen and privacy page are DONE; the 17-scope demo is the open item (PENDING re-record).
3. **A field genuinely needs (re)entering** -> use the values in sections 1 to 3 below verbatim.

**Live state (authoritative, 2026-06-29):**
- Code / `main`: **134 tools**, **17 connector scopes** (15 workspace + `openid` + `userinfo.email`), **0 restricted** (no CASA).
- GCP consent screen: raised to the **17 scopes** plus a 13-sensitive-scope justification (DONE).
- Served privacy page `https://appscriptly.com/privacy`: the **17-scope** version, live (DONE). Home `https://appscriptly.com/` and `https://appscriptly.com/terms` also live (200).
- Demo: a NEW **17-scope** video is PENDING (not yet recorded). The original `hBuuDemD8Js` (REJECTED, chat-only / insufficient) and the 13-scope interim `r7ZB1YeT3SE` are both SUPERSEDED; do not present either as current.
- Deployed server: HELD at **13 scopes** (a Fly billing hold), so the live `.well-known/oauth-protected-resource` still advertises 13 (not 17) until the hold clears and `main` redeploys. Submit only once the deployed server, consent screen, privacy page, and demo all show the SAME 17-scope set.

> **Serving-URL note (2026-06-14 cutover):** the live serving URL is the custom domain **`https://mcp.appscriptly.com/mcp`** (TLS cert + Cloudflare DNS + redirect URIs + `TRUSTED_HOSTS` secret), backed by the Fly app `sundeepg98-docs-mcp` (the `…fly.dev` host remains the backing origin and the `fly`-command handle).

---

## 1. OAuth consent screen - Branding (exact values to enter)

| Field | EXACT value | Source / flag |
|---|---|---|
| **App name** | `appscriptly` | kit section 1.3 |
| **User support email** | **OPERATOR** - the support address on the `sundeepg8@gmail.com` account that owns the GCP project. Console only lets you pick an address you control; the kit's draft uses `support@appscriptly.com` for the *privacy policy* but Google's support-email dropdown is account-bound. **Confirm which address was actually selected at submit.** | FLAG |
| **App logo** | **FLAG - confirm a logo was uploaded.** Kit pre-flight requires a 120x120 PNG. **No logo file exists in the repo** (searched: no `*logo*`/`*icon*.png`). If a logo was uploaded to the Console it lives only there. A logo is **required** for verification; if missing, this blocks approval. | FLAG / BLOCKER if absent |
| **Application home page** | `https://appscriptly.com/` | live 200 |
| **Application privacy policy URL** | `https://appscriptly.com/privacy` | live 200 |
| **Application terms of service URL** | `https://appscriptly.com/terms` | live 200. Terms is optional for Google; include it since it exists. |
| **Authorized domains** | `appscriptly.com` (primary). Optionally also `fly.dev` **only if** keeping the Fly URL as a serving/redirect host during the transition. | kit section 1.3 |
| **Developer contact email** | **OPERATOR** - the developer/admin email (typically `sundeepg8@gmail.com`). Google uses this to contact you about the app; **must be monitored**. | FLAG |

> The public brand is anchored to **appscriptly.com**, which is why the Fly app name (`sundeepg98-docs-mcp`) and repo name (`google-docs-mcp`) do not need to match the brand for verification. The live MCP is served at the **`mcp.appscriptly.com`** subdomain (`https://mcp.appscriptly.com/mcp`), backed by the Fly app. The **apex `appscriptly.com`** is a Cloudflare Pages landing/branding site only (home + `/privacy` + `/terms`); do NOT point the MCP serving/redirect URL at the apex, use the `mcp.` subdomain (or the backing `…fly.dev` host).

## 2. OAuth client - Web application (exact values)

| Field | EXACT value |
|---|---|
| **Application type** | **Web application** (NOT Desktop) |
| **Name** | `appscriptly-server` |
| **Authorized redirect URIs** (BOTH required - the server runs two OAuth flows on one client) | `https://mcp.appscriptly.com/auth/callback`  (FastMCP `GoogleProvider` connector callback, claude.ai connector flow)<br>`https://mcp.appscriptly.com/oauth/google/api/callback`  (per-user Workspace grant, `CALLBACK_PATH` in `oauth_google.py`) |

> The live serving URL is `mcp.appscriptly.com` (2026-06-14 cutover), so the Console redirect URIs must be the `mcp.appscriptly.com` pair above. The backing-origin pair (`https://sundeepg98-docs-mcp.fly.dev/auth/callback` + `…/oauth/google/api/callback`) may also be registered while the Fly host is kept as the backing origin. `…/start` is NOT a route, do not register it.
> **Same client_id MUST feed both flows** (the `sub` claim must match across them or per-user token keying breaks). One `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` secret on Fly drives both.

## 3. Data Access - the 17 scopes (paste exactly)

```
openid
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/presentations
https://www.googleapis.com/auth/forms.body
https://www.googleapis.com/auth/forms.responses.readonly
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/script.projects
https://www.googleapis.com/auth/script.deployments
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/contacts
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.labels
https://www.googleapis.com/auth/contacts.other.readonly
https://www.googleapis.com/auth/script.processes
```

**Expected Console classification: 13 Sensitive, 0 Restricted.**
- **Non-sensitive (4):** `openid`, `userinfo.email`, `drive.file` (per-file), `gmail.labels`.
- **Sensitive (13):** `documents`, `spreadsheets`, `presentations`, `forms.body`, `forms.responses.readonly`, `tasks`, `script.projects`, `script.deployments`, `calendar`, `contacts`, `gmail.send`, `contacts.other.readonly`, `script.processes`.

**Zero restricted means the FREE verification path, no CASA.** `gmail.send` and `gmail.labels` are INTENTIONAL now (send-only + label-object management; neither can read the mailbox). The only true STOP trigger is a RESTRICTED scope appearing under Restricted: any of `gmail.readonly`, `gmail.modify`, `gmail.metadata`, `mail.google.com`, or `drive` / `drive.readonly` / `drive.metadata`. If one of those shows up, STOP (it would force CASA and break the free path) and confirm the dedicated `appscriptly-server` client is the one selected (a stray restricted scope usually means a residual gmail-mcp credential leaked in).

> `script.container.ui` / `script.external` / `script.scriptapp` are **NOT** appscriptly's client scopes; they live in the generated bound script's own manifest (a separate end-user consent). Do NOT add them here.

---

## 4. Per-scope justifications (1 to 2 sentences each, tied to the 134-tool surface)

Paste one justification per scope in the Verification Center. Each is tied to the specific tools that exercise it. (Full per-service tool map in section 6.)

- **`openid`** - Obtain a stable, opaque per-account identifier (`sub`) solely to key each user's encrypted OAuth-token row in our store. No profile data is read or retained.

- **`…/auth/userinfo.email`** - Obtain the account email only to populate the FastMCP JWT `email` claim used for request routing and to show the connected account in tool responses. The email is **not** persisted (only the `sub` is); no marketing, no sharing.

- **`…/auth/documents`** - Create, read, and structurally edit Google Docs on the user's explicit instruction via `documents.batchUpdate` (Docs tools: `gdocs_make_tabbed_doc`, `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, `gdocs_edit_range`, `gdocs_insert_table` / `gdocs_insert_markdown_table`, `gdocs_format_range` / `gdocs_format_paragraph`, `gdocs_insert_image`, `gdocs_list_comments` / `gdocs_create_comment` / `gdocs_reply_to_comment`, plus the Tabs tools). Read-only is insufficient because the core feature creates and restructures documents.

- **`…/auth/spreadsheets`** - Create and mutate Google Sheets at the user's request via `spreadsheets.batchUpdate` / `values.*` (Sheets tools: `gsheets_create_spreadsheet`, `gsheets_write_range`, `gsheets_append_rows`, `gsheets_add_sheet` / `delete_sheet` / `rename_sheet` / `duplicate_sheet`, `gsheets_format_range` / `apply_conditional_format` / `add_chart` / `freeze` / `protect_range` / `merge_cells` / `insert_dimension` / `delete_dimension` / `set_data_validation` / `clear_range`, plus `gsheets_read_range`). The full scope (not `spreadsheets.readonly`) is required because the majority of tools write, format, protect, and restructure sheets.

- **`…/auth/presentations`** - Create and batch-edit Google Slides at the user's request via `presentations.batchUpdate` (Slides tools: `gslides_create_presentation`, `gslides_add_slide`, `gslides_replace_all_text`, `gslides_create_image` / `create_table` / `create_shape` / `create_line`, `gslides_set_speaker_notes`, plus `gslides_get_outline`). Read-only is insufficient because the feature creates decks and inserts shapes/lines/tables/images.

- **`…/auth/drive.file`** - Per-file Drive scope (only files the app created or the user explicitly opened) backing file organization and sharing (Drive tools: `gdrive_move_to_folder`, `gdrive_create_folder`, `gdrive_trash_file` / `untrash_file`, `gdrive_share_file` / `list_permissions` / `revoke_permission`, `gdrive_find_doc_by_title` / `find_file`, `gdrive_export_file`) plus video-pipeline frame staging. Deliberately chosen over broad Drive to minimize access; appscriptly cannot see or search the rest of the user's Drive.

- **`…/auth/forms.body`** - Create and edit Google Forms on explicit request (`gforms_create_form`, `gforms_add_question`, `gforms_update_item`, `gforms_delete_item`, `gforms_get_form`) via forms.create and forms.batchUpdate. Read-only is insufficient because the feature builds and restructures forms.

- **`…/auth/forms.responses.readonly`** - Read-only access to responses submitted to the user own forms (`gforms_list_responses`, `gforms_get_response`) so the user can review or grade submissions. No write access.

- **`…/auth/tasks`** - Create, read, update, complete, and delete the user Google Tasks and task lists on explicit request (`gtasks_create_tasklist`, `gtasks_create_task`, `gtasks_update_task`, `gtasks_complete_task`, `gtasks_delete_task`, `gtasks_list_tasklists`, `gtasks_list_tasks`). The full tasks scope is required because the service mutates tasks; tasks.readonly is insufficient.

- **`…/auth/script.projects`** - The headline feature: create and update Google Apps Script projects bound to the user's Doc/Sheet so persistent automations (custom menus, custom `=FUNCTION()`s, scheduled dashboards, onEdit/onFormSubmit triggers, web-app endpoints) can be installed (`as_generate_bound_script`, `as_install_doc_menu` / `custom_function` / `sheet_dashboard` / `sheet_menu` / `slides_menu`, `as_install_edit_trigger` / `form_handler`, `as_install_automation`, `gdocs_setup_apps_script`). No narrower scope exists for managing Apps Script project content.

- **`…/auth/script.deployments`** - Deploy the Apps Script project the user asked us to create, as a Web App / installable trigger, so the automation actually runs (`as_deploy_web_app`, the deploy step of `as_install_automation`, and the installable-trigger tools `as_install_edit_trigger` / `as_install_form_handler`). The `script.projects` scope alone cannot create deployments.

- **`…/auth/calendar`** - Create, edit, read, and delete the user Google Calendar events and read their calendar list on explicit request (`gcal_create_event`, `gcal_update_event`, `gcal_delete_event`, `gcal_get_event`, `gcal_list_events`, `gcal_list_calendars`, `gcal_freebusy`). The full calendar scope is required because the service writes and deletes events, not only reads; calendar.readonly is insufficient.

- **`…/auth/contacts`** - Create, edit, read, and delete the user Google Contacts via the People API on explicit request (`gcontacts_create`, `gcontacts_update`, `gcontacts_delete`, `gcontacts_get`, `gcontacts_list`, `gcontacts_search`). The full contacts scope is required because the service mutates contacts; contacts.readonly is insufficient.

- **`…/auth/gmail.send`** - Send email the user composes, on their behalf, via users.messages.send (`gmail_send_message`). Send-only: it grants no ability to read, search, or modify the mailbox; the restricted Gmail read/modify scopes are deliberately not requested.

- **`…/auth/gmail.labels`** - Manage Gmail label objects (create, list, delete) via labels.create/list/delete (`gmail_create_label`, `gmail_list_labels`, `gmail_delete_label`). Manages label names only; it cannot read messages or relabel messages (that needs the restricted gmail.modify, not requested).

- **`…/auth/contacts.other.readonly`** - Read-only access to the auto-saved Other contacts list (`gcontacts_list_other_contacts`, People API otherContacts.list) so the user can reference people they interacted with but never saved. Strictly narrower than contacts; no write access.

- **`…/auth/script.processes`** - Read-only access to the user Apps Script execution history (`as_list_script_processes`, Apps Script API processes.list) so the user can see which of their automations ran and when. Read-only observability companion to the create/deploy levers; no write access.

---

## 5. Demo-video script (shot-by-shot, against the LIVE 134-tool app)

Record unlisted on YouTube, English narration, against prod `https://mcp.appscriptly.com/mcp`. Target 4 to 6 min. **Every sensitive scope must be shown being used by a real tool**, plus the consent screen with the app name + client_id.

> **Demo status: a NEW 17-scope video is PENDING (not yet recorded).** The original `https://youtu.be/hBuuDemD8Js` was REJECTED by Google as chat-only / insufficient; `https://youtu.be/r7ZB1YeT3SE` was a later interim recording (8-then-13-scope era). BOTH are SUPERSEDED by the pending 17-scope re-record; do not submit either as current. The shotlist below is the base script; the re-record must additionally exercise the wave-1 scopes (Forms, Calendar, Tasks, Contacts) and the wave-2 scopes (`gmail.send`, `gmail.labels`, `contacts.other.readonly`, `script.processes`) so every one of the 13 sensitive scopes is shown in use, plus the full 17-scope consent screen. The authoritative scene list is the recorder at `D:/Sundeep/projects/_demo_rec/recorder.js` (go-live runbook STEP 5).

| # | Duration | Action | Scope demonstrated |
|---|---|---|---|
| 0 | 5s | Title card; say "appscriptly". | - |
| 1 | 20s | Trigger connect, land on the Google consent screen; scroll so ALL 17 scope lines are on camera; show the address bar with `client_id=…apps.googleusercontent.com`. | **App name + client_id + full scope list (required)** |
| 2 | 5s | Click **Allow**, callback success page. | OAuth grant flow |
| 3 | 35s | Run `gdocs_make_tabbed_doc` (3 tabs); open the doc; show native Tabs. Optionally `gdocs_edit_range`. | **documents** |
| 4 | 35s | Run `gsheets_create_spreadsheet` + `gsheets_write_range`; then `gsheets_freeze` and/or `gsheets_protect_range`. | **spreadsheets** |
| 5 | 30s | Run `gslides_create_presentation` + `gslides_replace_all_text`; optionally `gslides_create_shape`. | **presentations** |
| 6 | 40s | Run `gforms_create_form` + `gforms_add_question`; then `gforms_list_responses` on a form with real responses. | **forms.body + forms.responses.readonly** |
| 7 | 30s | Run `gcal_create_event` then `gcal_list_events`; show the event on the calendar. | **calendar** |
| 8 | 25s | Run `gtasks_create_tasklist` + `gtasks_create_task` + `gtasks_complete_task`. | **tasks** |
| 9 | 30s | Run `gcontacts_create` + `gcontacts_list`; then `gcontacts_list_other_contacts` and report the count. | **contacts + contacts.other.readonly** |
| 10 | 50s | Run `as_install_doc_menu` (or `as_install_edit_trigger`); refresh the doc; show the custom menu + the created script project + deployment; then `as_list_script_processes` and report the execution list. | **script.projects + script.deployments + script.processes** |
| 11 | 30s | Run `gmail_send_message` to a test address; then `gmail_create_label` + `gmail_list_labels`. | **gmail.send + gmail.labels** |
| 12 | 20s | Run `gdrive_move_to_folder` / `gdrive_share_file`; show the file organized/shared. | **drive.file** |
| 13 | 10s | Show revoke: Google Account, Security, Third-party apps, appscriptly, Remove access (or run `account_reset_authorization`). | - (data-control demo) |

---

## 6. Per-service tool map (full 134-tool surface, authoritative)

Built from each service's `_expected_tools.py` on `main` (the three tool-surface witnesses agree: live `mcp.list_tools()` == union of every `_expected_tools.py::EXPECTED` == `tests/golden/tool_surface.json`). 12 services, 134 tools. For the renamed tools, BOTH the legacy `gdocs_*` name and the newer clean-prefix name (`gdrive_*`, `server_*`, `admin_*`, `account_*`, `as_*`) are registered (dual-registration; deprecated aliases removed in v3.0), which is why the Drive and Admin buckets carry double counts.

### Docs service - `documents` (21 tools)
`gdocs_make_tabbed_doc`, `gdocs_add_tabs`, `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_append_to_tab`, `gdocs_tab_existing_doc`, `gdocs_rename_tab`, `gdocs_get_tab_url`, `gdocs_delete_tab`, `gdocs_replace_all_text`, `gdocs_insert_table`, `gdocs_insert_image`, `gdocs_list_comments`, `gdocs_create_comment`, `gdocs_reply_to_comment`, `gdocs_format_range`, `gdocs_format_paragraph`, `gdocs_edit_range`, `gdocs_insert_markdown_table`, `gdocs_set_tab_icons`, `gdocs_preview_tab_split`

### Sheets service - `spreadsheets` (18 tools)
`gsheets_read_range`, `gsheets_write_range`, `gsheets_create_spreadsheet`, `gsheets_format_range`, `gsheets_apply_conditional_format`, `gsheets_append_rows`, `gsheets_add_sheet`, `gsheets_delete_sheet`, `gsheets_rename_sheet`, `gsheets_clear_range`, `gsheets_duplicate_sheet`, `gsheets_freeze`, `gsheets_protect_range`, `gsheets_insert_dimension`, `gsheets_delete_dimension`, `gsheets_merge_cells`, `gsheets_set_data_validation`, `gsheets_add_chart`

### Slides service - `presentations` (9 tools)
`gslides_get_outline`, `gslides_replace_all_text`, `gslides_create_presentation`, `gslides_add_slide`, `gslides_create_image`, `gslides_create_table`, `gslides_create_shape`, `gslides_create_line`, `gslides_set_speaker_notes`

### Drive service - `drive.file` (20 tools: 10 canonical + 10 deprecated aliases)
canonical `gdrive_*`: `gdrive_find_doc_by_title`, `gdrive_move_to_folder`, `gdrive_trash_file`, `gdrive_untrash_file`, `gdrive_create_folder`, `gdrive_export_file`, `gdrive_find_file`, `gdrive_share_file`, `gdrive_list_permissions`, `gdrive_revoke_permission`
deprecated `gdocs_*` aliases: `gdocs_find_doc_by_title`, `gdocs_move_to_folder`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_create_folder`, `gdocs_export_doc`, `gdocs_find_file`, `gdocs_share_file`, `gdocs_list_permissions`, `gdocs_revoke_permission`

### Calendar service - `calendar` (7 tools)
`gcal_list_events`, `gcal_get_event`, `gcal_create_event`, `gcal_update_event`, `gcal_delete_event`, `gcal_list_calendars`, `gcal_freebusy`

### Forms service - `forms.body` + `forms.responses.readonly` (7 tools)
`gforms_create_form`, `gforms_get_form`, `gforms_add_question`, `gforms_update_item`, `gforms_delete_item`, `gforms_list_responses`, `gforms_get_response`

### Tasks service - `tasks` (7 tools)
`gtasks_list_tasklists`, `gtasks_create_tasklist`, `gtasks_list_tasks`, `gtasks_create_task`, `gtasks_update_task`, `gtasks_complete_task`, `gtasks_delete_task`

### Contacts service - `contacts` + `contacts.other.readonly` (7 tools)
`gcontacts_list`, `gcontacts_list_other_contacts`, `gcontacts_search`, `gcontacts_get`, `gcontacts_create`, `gcontacts_update`, `gcontacts_delete` (six use the full `contacts` scope; `gcontacts_list_other_contacts` uses the read-only `contacts.other.readonly`)

### Gmail service - `gmail.send` + `gmail.labels` (4 tools)
`gmail_send_message` (`gmail.send`), `gmail_create_label`, `gmail_list_labels`, `gmail_delete_label` (the three label tools use `gmail.labels`). Send-only plus label-object management; no mailbox read.

### Apps Script service - `script.projects` + `script.deployments` + `script.processes` (16 tools)
`as_generate_bound_script`, `as_list_script_processes` (`script.processes`, read-only), `as_install_doc_menu`, `as_install_custom_function`, `as_install_sheet_dashboard`, `as_install_edit_trigger`, `as_install_form_handler`, `as_install_sheet_menu`, `as_install_slides_menu`, `as_refresh_linked_slides`, `as_grade_form_responses`, `as_generate_video_deck`, `as_encode_video`, `as_install_calendar_sync`, `as_install_task_rollover`, `as_install_contact_sync`
(The generated bound scripts carry their OWN manifest scopes, a separate end-user consent; appscriptly's client does not request them.)

### GAS-deploy service - `script.projects` + `script.deployments` (4 tools)
`as_install_automation` (canonical), `gdocs_install_automation` (deprecated alias), `gdocs_setup_apps_script` (deprecated alias), `as_deploy_web_app`

### Admin / meta - no Google API scope (14 tools: 7 canonical + 7 deprecated aliases)
canonical: `server_info`, `server_test_manifest`, `server_guide`, `server_help`, `admin_audit`, `gdrive_get_signed_upload_url`, `account_reset_authorization`
deprecated `gdocs_*` aliases: `gdocs_server_info`, `gdocs_test_manifest`, `gdocs_guide`, `gdocs_help`, `gdocs_admin_audit`, `gdocs_get_signed_upload_url`, `gdocs_reset_authorization`
(These do not call Google Workspace APIs, so they drive no consent scope.)

**Totals:** Docs 21 + Sheets 18 + Slides 9 + Drive 20 + Calendar 7 + Forms 7 + Tasks 7 + Contacts 7 + Gmail 4 + Apps Script 16 + GAS-deploy 4 + Admin/meta 14 = **134 tools across 12 services**. The `openid` + `userinfo.email` identity scopes are consumed by every authenticated tool call (per-user token keying via `sub`, routing via `email`) and surface no standalone tools.

---

## 7. Operator-identity-gated vs orchestrator-drivable steps

Honest classification of the remaining Console/Verification-Center work (per the decide-drive-batch guardrail; only true identity/capability gates go to the operator).

### OPERATOR-only (Google login / human click / monitored inbox - cannot be delegated)
- **Google account login + 2FA** on `sundeepg8@gmail.com` (the account owning the GCP project + verified domain), plus the demo-recording consent password/2FA.
- **The final "Submit for verification" click** in the Verification Center.
- **Responding to Google's review emails** in the `sundeepg8@` inbox, the single most time-sensitive item while under review (a slow reply stalls the whole review).
- **Selecting the user-support email + developer-contact email** (Console only offers addresses the logged-in account controls).
- **Uploading the app logo** (if it needs (re)uploading, see section 1 FLAG).

### Orchestrator-drivable (via Playwright, on the authorized go-live state-changing flow)
- **Reading current Console/Verification-Center state** (entered fields, scope classification, review status).
- **Typing the section 1 branding values, section 2 redirect URIs, and section 3 scope list** into the forms.
- **Pasting the section 4 per-scope justifications** into the Verification Center text boxes.
- **Pasting the demo-video URL** into the submission form, and editing the consent screen / privacy page / recorder / redeploy.

> Per the go-live runbook this is the operator-authorized state-changing flow; the agent still hard-stops at the Google login/2FA wall (never enters credentials) and at the final Submit click.

---

## 8. PLACEHOLDER / UNKNOWN values needing the operator (consolidated)

| Item | Status | Action needed |
|---|---|---|
| **App logo** | **UNKNOWN, likely BLOCKER** | No logo file in repo. Confirm a 120x120 PNG was uploaded to the Console; if not, create + upload one (verification requires a logo). |
| **User-support email** | Needs confirmation | Confirm the exact address selected in the Console (account-bound). |
| **Developer-contact email** | Needs confirmation | Confirm it is a monitored address (likely `sundeepg8@gmail.com`); Google contacts you here. |
| **Privacy-policy contact email** | Minor | The live `appscriptly.com/privacy` should show a real support email, not a placeholder. Spot-check the live page. |
| **Demo video** | **PENDING (17-scope re-record)** | A NEW 17-scope demo must be recorded; the prior `hBuuDemD8Js` (REJECTED) and the 13-scope interim `r7ZB1YeT3SE` are SUPERSEDED. See section 5. |
| **Deployed server scope set** | **HELD at 13 (Fly billing hold)** | Clear the Fly billing hold and redeploy `main` so the live server advertises all 17 scopes before submitting. |

Everything else (app name, home/privacy/terms URLs, the 17 scopes, both redirect URIs, per-scope justifications) is **known and verified** above.
