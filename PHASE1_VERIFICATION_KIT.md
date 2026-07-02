# appscriptly — Google OAuth Free Verification + Dedicated OAuth Client (Phase-1 Action Kit)

> Generated 2026-05-30. **Refreshed 2026-06-29** for the **134-tool surface** and the full **17-scope** set (15 workspace + openid + userinfo.email). Free sensitive-scope verification path (zero restricted scopes, no CASA).
>
> **STATUS: 17-scope RE-SUBMISSION in progress.** The app is build-complete (134 tools, 17 connector scopes, 0 restricted); a prior round was submitted at 8 scopes, the rejected demo was redone, and the scope set has since grown to 17. This kit is the *strategy/why* reference; the **exact field values** to enter/confirm in the Console live in the companion **`VERIFICATION_SUBMISSION.md`**. The operator punch-list (section 1) below is the re-do checklist.

**Target repo:** `Sundeepg98/google-docs-mcp` (module `appscriptly`).
**Deployed:** Fly app `sundeepg98-docs-mcp`, now served at the custom domain **`https://mcp.appscriptly.com/mcp`** (live serving URL as of the 2026-06-14 cutover: TLS cert + Cloudflare DNS + redirect URIs + the consent screen (raised to 17 scopes at go-live) + `TRUSTED_HOSTS` secret), still backed by the same Fly app. The raw `https://sundeepg98-docs-mcp.fly.dev` host remains the backing origin (and the Fly-app handle for `fly` commands / redirect-URI registration below). (New app name `appscriptly` reserved, not yet cut over.)
**Tool surface:** **134 tools** across **12 services** (`main` golden = `tests/golden/tool_surface.json`). All 134 stay within the 17 scopes below, zero verification impact.

## 0. Scope set for THIS verification round (17 connector scopes; 13 sensitive, 0 restricted)

> **Single source of truth:** `auth.py:WORKSPACE_SCOPES` (15 workspace scopes) + `oauth_google.py:IDENTITY_SCOPES` (`openid`, `userinfo.email`), unioned as `oauth_google.py:GOOGLE_API_SCOPES` (17 connector scopes), pinned by `tests/unit/test_scope_union_single_source.py` and the no-restricted guard `tests/unit/test_base_tier_scopes.py`. The earlier rounds submitted an 8-scope subset; the scope set has since grown to the full 17 (wave 1 added Forms/Tasks/Calendar/Contacts; wave 2 added `gmail.send`, `gmail.labels`, `contacts.other.readonly`, `script.processes` via PR #207). This round submits all 17.

| # | Scope | Google class |
|---|-------|--------------|
| 1 | `openid` | Non-sensitive |
| 2 | `…/auth/userinfo.email` | Non-sensitive |
| 3 | `…/auth/documents` | **Sensitive** |
| 4 | `…/auth/drive.file` | Non-sensitive (per-file) |
| 5 | `…/auth/spreadsheets` | **Sensitive** |
| 6 | `…/auth/presentations` | **Sensitive** |
| 7 | `…/auth/forms.body` | **Sensitive** |
| 8 | `…/auth/forms.responses.readonly` | **Sensitive** |
| 9 | `…/auth/tasks` | **Sensitive** |
| 10 | `…/auth/script.projects` | **Sensitive** |
| 11 | `…/auth/script.deployments` | **Sensitive** |
| 12 | `…/auth/calendar` | **Sensitive** |
| 13 | `…/auth/contacts` | **Sensitive** |
| 14 | `…/auth/gmail.send` | **Sensitive** |
| 15 | `…/auth/gmail.labels` | Non-sensitive |
| 16 | `…/auth/contacts.other.readonly` | **Sensitive** |
| 17 | `…/auth/script.processes` | **Sensitive** |

**Verification class: FREE.** 13 sensitive, **zero restricted**. No CASA / no third-party audit. Needs: verified domain + privacy policy + demo video + per-scope justifications (~3 to 5 business days). Every added scope (Forms, Tasks, Calendar, Contacts, `gmail.send`, `gmail.labels`, `contacts.other.readonly`, `script.processes`) is sensitive or non-sensitive, none restricted, so the FREE / no-CASA class holds.

> `script.container.ui` / `script.external` / `script.scriptapp` are **NOT** in appscriptly's OAuth client — they live in the *generated bound script's own manifest* (a separate end-user consent). Do NOT add them to the consent screen.

## 1. OPERATOR PUNCH-LIST (one sitting, in order; 🔒 = needs your password/2FA/human click)

**Pre-flight:** logo PNG 120×120; a support email you own; confirm publicly-reachable URL (now the custom domain `https://mcp.appscriptly.com/mcp`, backed by the Fly app `sundeepg98-docs-mcp`); home `https://appscriptly.com/` + privacy `https://appscriptly.com/privacy` live before submit.

1. **🔒 Create dedicated GCP project** — console.cloud.google.com/projectcreate → name `appscriptly` → select it. (A clean project = a consent screen listing ONLY the 17 scopes, shedding the gmail-mcp inheritance.)
2. **🔒 Enable APIs** - APIs & Services, Library, enable: Docs, Sheets, Slides, Drive, **Apps Script**, **Calendar**, **Tasks**, **Forms**, **People** (Contacts), **Gmail**.
3. **🔒 Branding** — Google Auth Platform → Branding: app name `appscriptly`; support email; logo; home `https://appscriptly.com/`; privacy `https://appscriptly.com/privacy`; authorized domains `appscriptly.com` (+ `fly.dev` if keeping the Fly URL during transition); developer contact email.
4. **🔒 Audience** — User type **External**; leave Publishing = Testing for now; add your test Google account(s).
5. **🔒 Data Access** - paste exactly these 17 scopes; confirm 13 under Sensitive, **0 under Restricted**; Save:
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
6. **🔒 Create OAuth client** — Credentials → Create → **Web application** (NOT Desktop) → name `appscriptly-server`. Authorized redirect URIs (BOTH needed — server runs two OAuth flows):
   - `https://sundeepg98-docs-mcp.fly.dev/auth/callback`  (FastMCP GoogleProvider connector callback)
   - `https://sundeepg98-docs-mcp.fly.dev/oauth/google/api/callback`  (the second/API grant, `CALLBACK_PATH`)
   - (Optionally pre-register the `appscriptly.fly.dev` + `appscriptly.com` pairs now for the later rename.)
   - Copy Client ID + secret for §5. Don't commit. (`…/start` is NOT a route — don't register it.)
7. **🔒 Verify domain** — search.google.com/search-console → Domain `appscriptly.com` → DNS TXT → Verify. Use the SAME Google account that owns the GCP project.
8. **Host privacy policy + home page** at appscriptly.com (Google fetches both; same domain; no auth wall).
9. **🔒 Publish to Production** — Audience → Publish app (flips Testing → In production; removes 7-day cap; still "unverified" until step 10).
10. **🔒 Submit for verification** — Verification Center: confirm branding + verified domain, paste per-scope justifications (§2), paste unlisted YouTube demo URL (§4), submit. ~3–5 business days.

> Steps 1 to 6 are independent of the code. The `drive.readonly`-removal PR (#148) is live in prod, so the base requests zero restricted scopes; the demo must show the full 17-scope consent (no `drive.readonly`, no other restricted scope).

## 2. PER-SCOPE JUSTIFICATIONS (for the 134-tool surface)

> Full per-service tool map (all 134 tools) is in **`VERIFICATION_SUBMISSION.md` section 6**. Service counts: Docs 21, Sheets 18, Slides 9, Drive 20, Calendar 7, Forms 7, Tasks 7, Contacts 7, Gmail 4, Apps Script 16, GAS-deploy 4, Admin/meta 14 = 134 across 12 services.

- **openid / userinfo.email** — obtain a stable user id (`sub`) + email solely to key per-user OAuth token storage and show the connected account; no profile data, no marketing, not shared. Used by every authenticated tool call.
- **documents** (17 docs tools) — create/read/edit Docs at user instruction via `documents.batchUpdate`: `gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_add_tabs`/`delete_tab`/`rename_tab`/`set_tab_icons`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, **`gdocs_edit_range`** (UTF-16-correct ranged edit), `gdocs_insert_table`/`insert_markdown_table`, `gdocs_format_range`/`format_paragraph`. Read-only insufficient: core feature creates/restructures docs.
- **spreadsheets** (13 sheets tools) — create/mutate Sheets via `spreadsheets.batchUpdate` / `values.*`: `gsheets_create_spreadsheet`, `gsheets_write_range`, `gsheets_append_rows`, **`gsheets_clear_range`**, `gsheets_add_sheet`/`delete_sheet`/`rename_sheet`/**`duplicate_sheet`**, `gsheets_format_range`/`apply_conditional_format`/**`freeze`**/**`protect_range`**, plus `gsheets_read_range`. Read-only insufficient: the majority write/format/protect.
- **presentations** (8 slides tools) — create + batch-edit Slides via `presentations.batchUpdate`: `gslides_create_presentation`, `gslides_add_slide`, `gslides_replace_all_text`, `gslides_create_image`/`create_table`/**`create_shape`**/**`create_line`**, `gslides_get_outline`. Read-only insufficient: creates + inserts shapes/lines/tables.
- **drive.file** (10 drive tools) — per-file scope (only app-created/opened files): `gdocs_move_to_folder`, `gdocs_create_folder`, `gdocs_trash_file`/`untrash_file`, `gdocs_share_file`/`list_permissions`/`revoke_permission`, `gdocs_find_doc_by_title`/`find_file`, `gdocs_export_doc` + video-pipeline frame staging. Chosen over broad Drive to minimize access.
- **script.projects** (headline) — create/update Apps Script projects bound to user's doc/sheet: `gdocs_install_automation`, `gdocs_setup_apps_script`, `as_generate_bound_script`, `as_install_doc_menu`/`custom_function`/`sheet_dashboard`, **`as_install_edit_trigger`** (onEdit), **`as_install_form_handler`** (onFormSubmit). No narrower scope exists.
- **script.deployments** - deploy the created script (web app / installable triggers) so the automation runs: `as_deploy_web_app`, the deploy step of `as_install_automation`, and the installable triggers `as_install_edit_trigger`/`as_install_form_handler`. Projects scope alone can't deploy.
- **forms.body** - create and edit Google Forms (`gforms_create_form`, `gforms_add_question`, `gforms_update_item`, `gforms_delete_item`, `gforms_get_form`) via forms.create + forms.batchUpdate. Read-only is insufficient because the feature builds and restructures forms.
- **forms.responses.readonly** - read-only access to responses submitted to the user own forms (`gforms_list_responses`, `gforms_get_response`) so the user can review or grade submissions. No write access.
- **tasks** - create, read, update, complete, and delete the user Google Tasks and task lists (`gtasks_create_tasklist`, `gtasks_create_task`, `gtasks_update_task`, `gtasks_complete_task`, `gtasks_delete_task`, `gtasks_list_tasklists`, `gtasks_list_tasks`). The full tasks scope is required because the service mutates tasks; tasks.readonly is insufficient.
- **calendar** - create, edit, read, and delete the user Google Calendar events and read their calendar list (`gcal_create_event`, `gcal_update_event`, `gcal_delete_event`, `gcal_get_event`, `gcal_list_events`, `gcal_list_calendars`, `gcal_freebusy`). The full calendar scope is required because the service writes and deletes events, not only reads; calendar.readonly is insufficient.
- **contacts** - create, edit, read, and delete the user Google Contacts via the People API (`gcontacts_create`, `gcontacts_update`, `gcontacts_delete`, `gcontacts_get`, `gcontacts_list`, `gcontacts_search`). The full contacts scope is required because the service mutates contacts; contacts.readonly is insufficient.
- **contacts.other.readonly** - read-only access to the auto-saved Other contacts list (`gcontacts_list_other_contacts`, People API otherContacts.list) so the user can reference people they interacted with but never saved. Strictly narrower than contacts; no write access.
- **gmail.send** - send email the user composes, on their behalf, via users.messages.send (`gmail_send_message`). Send-only: it grants no ability to read, search, or modify the mailbox; the restricted Gmail read/modify scopes are deliberately not requested.
- **gmail.labels** - manage Gmail label objects (create, list, delete) via labels.create/list/delete (`gmail_create_label`, `gmail_list_labels`, `gmail_delete_label`). Manages label names only; it cannot read messages or relabel messages (that needs the restricted gmail.modify, not requested).
- **script.processes** - read-only access to the user Apps Script execution history (`as_list_script_processes`, Apps Script API processes.list) so the user can see which automations ran and when. No write access.

## 3. PRIVACY POLICY — host at https://appscriptly.com/privacy

(Full draft below; replace the support email.)

**appscriptly Privacy Policy** - Last updated 28 June 2026

appscriptly is a Google Workspace automation tool that creates, edits, and manages Google Docs, Sheets, Slides, and Drive files, and installs Apps Script automations, on your behalf and at your explicit direction. appscriptly's use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

**Google data we access (and nothing more):** email and account id (openid, userinfo.email) to identify your account and store authorization per user; Docs (documents), Sheets (spreadsheets), and Slides (presentations) to create and edit what you ask; Drive files created by or opened with appscriptly (drive.file), a per-file scope (appscriptly cannot see, read, or search the rest of your Drive); Apps Script projects and deployments (script.projects, script.deployments) to install the automations you request, and their execution history (script.processes, read-only) so you can see which automations ran; Google Calendar (calendar) to create, edit, and read the events and calendars you ask us to manage; Google Forms (forms.body to create and edit forms, forms.responses.readonly to read submitted responses); Google Tasks (tasks) to create and manage your task lists and tasks; Contacts (contacts to create, edit, and read your contacts, contacts.other.readonly to read your auto-saved other contacts); and Gmail for sending and labels only (gmail.send to send messages you compose, gmail.labels to manage label names). appscriptly Gmail access is send-and-label only: it cannot read, search, or modify the messages in your mailbox, and it does not request gmail.readonly, gmail.modify, or any full-mailbox scope. All of this data transits only during the specific tool call you trigger; it is never copied or warehoused. The only data appscriptly stores is your encrypted OAuth token and account id.

**appscriptly does NOT request or access:** the rest of your Drive (only files you create or open with appscriptly, via the per-file drive.file scope); the contents of your Gmail mailbox (no reading, searching, or modifying messages; sending and label management only); or any scope on Google restricted-scope list. appscriptly requests sensitive scopes only, so it requires no CASA security assessment.

**Use:** solely to perform the specific automation you request. No advertising, profiling, ML/AI training, or unrelated use. No human reads your data except for security, legal compliance, or with your explicit consent.

**Storage/retention:** your content lives in YOUR Google account — we don't copy or warehouse it. The only Google-derived data stored is your encrypted OAuth token + account id, on a private persistent volume, retained until you revoke or after inactivity.

**Sharing:** no sale; no sharing except strictly-necessary sub-processors (hosting, error monitoring).

**Revoke:** run `gdocs_reset_authorization`, or Google Account → Security → Third-party apps → appscriptly → Remove access.

**Security:** tokens encrypted at rest, HTTPS only, minimum scope (`drive.file` not broad Drive).

**Contact:** support@appscriptly.com *(replace)*.

## 4. DEMO VIDEO SHOTLIST (unlisted YouTube, English, record against prod at the full 17-scope set; 4 to 6 min)

> **Demo status: a NEW 17-scope video is PENDING (not yet recorded).** The original `https://youtu.be/hBuuDemD8Js` was REJECTED by Google as chat-only / insufficient; `https://youtu.be/r7ZB1YeT3SE` was a later interim recording (8-then-13-scope era). BOTH are SUPERSEDED by the pending 17-scope re-record; do not submit either as current. The shotlist below is the base script; the re-record must additionally exercise Forms, Calendar, Tasks, Contacts, `gmail.send`, `gmail.labels`, `contacts.other.readonly`, and `script.processes` so all 13 sensitive scopes are shown. The authoritative scene list is the recorder at `D:/Sundeep/projects/_demo_rec/recorder.js`.

0. Title card + say "appscriptly" (5s).
1. Trigger connect → Google consent screen, pause (15s) — **OAuth grant flow**.
2. Zoom: show "appscriptly wants access" + scope list; show address bar with `client_id=…apps.googleusercontent.com` (15s) — **app name + client id (required)**.
3. Click Allow → callback success (5s).
4. `gdocs_make_tabbed_doc` (3 tabs) → open doc, show native Tabs; optionally `gdocs_edit_range` for a ranged edit (35s) — **documents**.
5. `gsheets_create_spreadsheet` + `gsheets_write_range`; optionally `gsheets_freeze`/`gsheets_protect_range` → show values + frozen/protected result (30s) — **spreadsheets**.
6. `gslides_create_presentation` / `gslides_replace_all_text`; optionally `gslides_create_shape`/`create_line` → open deck (30s) — **presentations**.
7. `as_install_doc_menu` (or `as_install_edit_trigger` for an onEdit automation) on the doc → show new custom menu after refresh + created script/deployment (50s) — **script.projects + script.deployments**.
8. `gdocs_move_to_folder`/`share_file` or `as_generate_video_deck` staging (20s) — **drive.file**.
9. Show revoke (Google Account third-party apps, or `gdocs_reset_authorization`) (10s).

## 5. DEDICATED-CLIENT CUTOVER (server-side)

One `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` feeds BOTH OAuth flows (same-client-id invariant holds automatically). Cutover = swap that one secret:

1. Register BOTH redirect URIs on the new client first (else `redirect_uri_mismatch`).
2. `fly secrets set -a sundeepg98-docs-mcp GOOGLE_OAUTH_CLIENT_SECRETS_JSON='{"web":{"client_id":"NEW.apps.googleusercontent.com","client_secret":"GOCSPX-NEW","redirect_uris":["https://sundeepg98-docs-mcp.fly.dev/auth/callback","https://sundeepg98-docs-mcp.fly.dev/oauth/google/api/callback"]}}'` — triggers a rolling restart (the swap IS the deploy; minimal downtime).
3. Leave `GOOGLE_OAUTH_BASE_URL`, `FASTMCP_HOME`, and **`MCP_BEARER_TOKEN` (do NOT change — it derives signing/encryption keys)** untouched.

**Expected:** all existing users re-consent exactly once (old client's refresh tokens die on client change); the connector DCR store survives (encrypted with the unchanged `MCP_BEARER_TOKEN` on the `/data` volume) so no `invalid_client` storm. Do the client swap + publish/verify together so users re-consent once. Recommendation: swap the client on the CURRENT app first (one re-consent), verify, then treat the Fly app rename to `appscriptly` as a separate later migration.

### Key sources
- Sensitive-scope verification: https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- Verification requirements: https://support.google.com/cloud/answer/13464321
- Configure consent screen: https://developers.google.com/workspace/guides/configure-oauth-consent
- FastMCP Google OAuth: https://gofastmcp.com/integrations/google
- Scope sensitivity: https://developers.google.com/identity/protocols/oauth2/scopes
