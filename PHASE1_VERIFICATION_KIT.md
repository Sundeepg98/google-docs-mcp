# appscriptly — Google OAuth Free Verification + Dedicated OAuth Client (Phase-1 Action Kit)

> Generated 2026-05-30. **Refreshed 2026-06-13** for the **66-tool surface** (per-scope→tool map + demo shotlist below updated; the 8 scopes are UNCHANGED). Free sensitive-scope verification path (zero restricted scopes → no CASA).
>
> **⚠️ STATUS: verification was already SUBMITTED (~2026-06-01) and is UNDER REVIEW** (per `START_HERE.md`). This kit is now the *strategy/why* reference; the **exact field values** to enter/confirm in the Console live in the companion **`VERIFICATION_SUBMISSION.md`**. The operator punch-list (§1) below was executed; treat it as the record of what was done + a re-do checklist if Google bounces the submission.

**Target repo:** `Sundeepg98/google-docs-mcp` (module `appscriptly`).
**Deployed:** Fly app `sundeepg98-docs-mcp` → `https://sundeepg98-docs-mcp.fly.dev` (new app `appscriptly` reserved, not yet cut over).
**Tool surface:** **66 tools** (`origin/main` golden = 62 + the 4 sheets tools in open PR #191). All 66 stay within the 8 scopes below — zero verification impact.

## 0. Scope set submitted in the FIRST verification round (8 scopes; `drive.readonly` removed in #148, MERGED + deployed + live-verified)

> **Round-1 subset vs current code set (read before cross-referencing the repo).** The 8 scopes below are the set submitted in the FIRST verification round (the consent screen currently under review). Since this kit was first written, the code added four more SENSITIVE (zero restricted, so still no CASA) services to `auth.WORKSPACE_SCOPES`: Forms (`forms.body`, `forms.responses.readonly`), Tasks (`tasks`), Calendar (`calendar`), and Contacts (`contacts`). So the current code set is **13 scopes** (the source of truth is `auth.py:WORKSPACE_SCOPES` + `oauth_google.py:IDENTITY_SCOPES`, pinned by `tests/unit/test_scope_union_single_source.py`). Those four services reach existing users via Google's incremental-consent flow and their live rollout is held by the CI deploy gate (`DEPLOY_ENABLED=false`) until their OWN verification round (this project verifies LAST). This kit's §1 punch-list and the table below intentionally still reflect the Round-1 subset that was actually submitted; do NOT treat 8 as the whole code surface.

| # | Scope | Google class |
|---|-------|--------------|
| 1 | `openid` | Non-sensitive |
| 2 | `…/auth/userinfo.email` | Non-sensitive |
| 3 | `…/auth/documents` | **Sensitive** |
| 4 | `…/auth/spreadsheets` | **Sensitive** |
| 5 | `…/auth/presentations` | **Sensitive** |
| 6 | `…/auth/drive.file` | Non-sensitive (per-file) |
| 7 | `…/auth/script.projects` | **Sensitive** |
| 8 | `…/auth/script.deployments` | **Sensitive** |

**Verification class: FREE.** 5 sensitive, **zero restricted**. No CASA / no third-party audit. Needs: verified domain + privacy policy + demo video + per-scope justifications (~3 to 5 business days). The four later services (Forms / Tasks / Calendar / Contacts) are all sensitive too, so the FREE / no-CASA class holds for their later round as well.

> `script.container.ui` / `script.external` / `script.scriptapp` are **NOT** in appscriptly's OAuth client — they live in the *generated bound script's own manifest* (a separate end-user consent). Do NOT add them to the consent screen.

## 1. OPERATOR PUNCH-LIST (one sitting, in order; 🔒 = needs your password/2FA/human click)

**Pre-flight:** logo PNG 120×120; a support email you own; confirm publicly-reachable URL (today `https://sundeepg98-docs-mcp.fly.dev`); home `https://appscriptly.com/` + privacy `https://appscriptly.com/privacy` live before submit.

1. **🔒 Create dedicated GCP project** — console.cloud.google.com/projectcreate → name `appscriptly` → select it. (A clean project = a consent screen listing ONLY the 8 scopes, shedding the gmail-mcp inheritance.)
2. **🔒 Enable APIs** — APIs & Services → Library → enable: Docs, Sheets, Slides, Drive, **Apps Script**.
3. **🔒 Branding** — Google Auth Platform → Branding: app name `appscriptly`; support email; logo; home `https://appscriptly.com/`; privacy `https://appscriptly.com/privacy`; authorized domains `appscriptly.com` (+ `fly.dev` if keeping the Fly URL during transition); developer contact email.
4. **🔒 Audience** — User type **External**; leave Publishing = Testing for now; add your test Google account(s).
5. **🔒 Data Access** — paste exactly these 8 scopes; confirm 5 under Sensitive, **0 under Restricted**; Save:
   ```
   openid
   https://www.googleapis.com/auth/userinfo.email
   https://www.googleapis.com/auth/documents
   https://www.googleapis.com/auth/spreadsheets
   https://www.googleapis.com/auth/presentations
   https://www.googleapis.com/auth/drive.file
   https://www.googleapis.com/auth/script.projects
   https://www.googleapis.com/auth/script.deployments
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

> Steps 1–6 are independent of the code. The `drive.readonly`-removal PR (#148) is now **live in prod** — the base already requests the final 8-scope set with no `drive.readonly`, so submission (10) is unblocked. The demo must show that final 8-scope consent (no `drive.readonly`).

## 2. PER-SCOPE JUSTIFICATIONS (refreshed for the 66-tool surface)

> Full per-scope → tool map (all 66 tools, with service counts) is in **`VERIFICATION_SUBMISSION.md` §6**. Summary counts: documents 17, spreadsheets 13, presentations 8, drive.file 10, script.projects+script.deployments 11, admin/meta 7 (no Google scope).

- **openid / userinfo.email** — obtain a stable user id (`sub`) + email solely to key per-user OAuth token storage and show the connected account; no profile data, no marketing, not shared. Used by every authenticated tool call.
- **documents** (17 docs tools) — create/read/edit Docs at user instruction via `documents.batchUpdate`: `gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_add_tabs`/`delete_tab`/`rename_tab`/`set_tab_icons`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, **`gdocs_edit_range`** (UTF-16-correct ranged edit), `gdocs_insert_table`/`insert_markdown_table`, `gdocs_format_range`/`format_paragraph`. Read-only insufficient: core feature creates/restructures docs.
- **spreadsheets** (13 sheets tools) — create/mutate Sheets via `spreadsheets.batchUpdate` / `values.*`: `gsheets_create_spreadsheet`, `gsheets_write_range`, `gsheets_append_rows`, **`gsheets_clear_range`**, `gsheets_add_sheet`/`delete_sheet`/`rename_sheet`/**`duplicate_sheet`**, `gsheets_format_range`/`apply_conditional_format`/**`freeze`**/**`protect_range`**, plus `gsheets_read_range`. Read-only insufficient: the majority write/format/protect.
- **presentations** (8 slides tools) — create + batch-edit Slides via `presentations.batchUpdate`: `gslides_create_presentation`, `gslides_add_slide`, `gslides_replace_all_text`, `gslides_create_image`/`create_table`/**`create_shape`**/**`create_line`**, `gslides_get_outline`. Read-only insufficient: creates + inserts shapes/lines/tables.
- **drive.file** (10 drive tools) — per-file scope (only app-created/opened files): `gdocs_move_to_folder`, `gdocs_create_folder`, `gdocs_trash_file`/`untrash_file`, `gdocs_share_file`/`list_permissions`/`revoke_permission`, `gdocs_find_doc_by_title`/`find_file`, `gdocs_export_doc` + video-pipeline frame staging. Chosen over broad Drive to minimize access.
- **script.projects** (headline) — create/update Apps Script projects bound to user's doc/sheet: `gdocs_install_automation`, `gdocs_setup_apps_script`, `as_generate_bound_script`, `as_install_doc_menu`/`custom_function`/`sheet_dashboard`, **`as_install_edit_trigger`** (onEdit), **`as_install_form_handler`** (onFormSubmit). No narrower scope exists.
- **script.deployments** — deploy the created script (web app / installable triggers) so the automation runs: `as_deploy_web_app`, the deploy step of `gdocs_install_automation`, and the installable triggers `as_install_edit_trigger`/`as_install_form_handler`. Projects scope alone can't deploy.
- **calendar** (7 calendar tools, SENSITIVE, no CASA) — list, read, create, update, and delete the user's Google Calendar events and list their calendars at user instruction: `gcal_list_events`, `gcal_get_event`, `gcal_create_event`, `gcal_update_event`, `gcal_delete_event`, `gcal_list_calendars`, `gcal_freebusy`. The full `calendar` scope (not `calendar.readonly` / `calendar.events`) is requested because the service both reads the calendar list and creates, patches, and deletes events. Read-only is insufficient: most tools write.
- **tasks** (7 tasks tools, SENSITIVE, no CASA) — list, create, update, complete, and delete the user's Google Tasks and task lists: `gtasks_list_tasklists`, `gtasks_create_tasklist`, `gtasks_list_tasks`, `gtasks_create_task`, `gtasks_update_task`, `gtasks_complete_task`, `gtasks_delete_task`. The full `tasks` scope (not `tasks.readonly`) is requested because the service creates, updates, completes, and deletes tasks. Read-only is insufficient.
- **forms.body** (5 forms-structure tools, SENSITIVE, no CASA) — create and edit the user's Google Forms via `forms.create` / `forms.get` / `forms.batchUpdate`: `gforms_create_form`, `gforms_get_form`, `gforms_add_question`, `gforms_update_item`, `gforms_delete_item`. Read-only is insufficient: the service creates and restructures forms.
- **forms.responses.readonly** (2 forms-response tools, SENSITIVE, no CASA, read-only) — read the responses people submit to the user's forms so appscriptly can summarize results: `gforms_list_responses`, `gforms_get_response`. Read-only by design: appscriptly never alters or deletes responses, so the narrower read-only scope is used rather than a broader one.
- **contacts** (6 contacts tools, SENSITIVE, no CASA) — list, search, read, create, update, and delete the user's Google Contacts via People API v1: `gcontacts_list`, `gcontacts_search`, `gcontacts_get`, `gcontacts_create`, `gcontacts_update`, `gcontacts_delete`. The full `contacts` scope (not `contacts.readonly`) is requested because the service creates, updates, and deletes contacts. Read-only is insufficient.

## 3. PRIVACY POLICY — host at https://appscriptly.com/privacy

(Full draft below; replace the support email. SOURCE OF TRUTH for the served
`appscriptly-site/privacy.html` — keep the two in sync. Updated for the 13-scope
go-live: added Calendar, Tasks, Forms + form responses, and Contacts data
categories; removed the now-false "does NOT access contacts or calendar" claim.)

**appscriptly — Privacy Policy** · Last updated 14 June 2026

appscriptly is a Google Workspace automation tool that creates, edits, and manages Google Docs, Sheets, Slides, and Drive files, manages your Google Calendar events, Google Tasks, Google Forms (and reads the responses people submit to them), and your Google Contacts, and installs Apps Script automations, all on your behalf and at your explicit direction. appscriptly's use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

**Google data we access** (and nothing more): email and account id (`openid`, `userinfo.email`) to identify your account and store authorization per-user; Docs (`documents`), Sheets (`spreadsheets`), Slides (`presentations`) to create and edit what you ask; Drive files created by or opened with appscriptly (`drive.file`), a per-file scope, so **appscriptly cannot see, read, or search the rest of your Drive**; your Google Calendar (`calendar`) to list, create, update, and delete events you ask about; your Google Tasks (`tasks`) to list, create, update, complete, and delete tasks; your Google Forms (`forms.body`) to create and edit forms, and your form responses (`forms.responses.readonly`), read-only, to summarize results for you; your Google Contacts (`contacts`) to list, search, create, update, and delete contacts at your request; and Apps Script projects and deployments (`script.projects`, `script.deployments`) to install the automations you request. **appscriptly does NOT request or access:** Gmail or any email content, or your full Drive.

**Calendar, Tasks, Forms, and Contacts (the data we handle for these services):**

- **Calendar.** What we access: the events on the calendars you ask about (title, time, location, description, attendees) and your calendar list, through the `calendar` scope. How we use it: only to perform the action you request (list events, create or reschedule an event, check free and busy times). Storage: appscriptly does not copy or warehouse your events; they stay in your Google Calendar. Sharing: not shared, not sold. Retention: nothing event-related is retained after the request completes.
- **Tasks.** What we access: your task lists and tasks (title, notes, due date, completion status), through the `tasks` scope. How we use it: only to create, update, complete, delete, or list tasks as you direct. Storage: tasks stay in Google Tasks; not copied or warehoused. Sharing: not shared, not sold. Retention: nothing task-related is retained after the request completes.
- **Forms and form responses.** What we access: the forms you ask appscriptly to build or edit (questions, titles, descriptions) through `forms.body`, and the responses people submit through the read-only `forms.responses.readonly` scope. How we use it: only to create and edit forms and to read and summarize their responses for you. The response scope is read-only, so appscriptly cannot alter or delete responses. Storage: forms and responses stay in your Google account; not copied or warehoused. Sharing: not shared, not sold. Retention: nothing form-related or response-related is retained after the request completes.
- **Contacts.** What we access: your contacts (names, email addresses, phone numbers, organization) through the `contacts` scope. How we use it: only to list, search, create, update, or delete contacts as you direct. Storage: contacts stay in Google Contacts; not copied or warehoused. Sharing: not shared, not sold. Retention: nothing contact-related is retained after the request completes.

**Use:** solely to perform the specific automation you request. No advertising, profiling, ML/AI training, or unrelated use. No human reads your data except for security, legal compliance, or with your explicit consent.

**Storage/retention:** your content lives in YOUR Google account; we do not copy or warehouse it. That includes Docs, Sheets, Slides, Drive files, calendar events, tasks, forms, form responses, and contacts. The only Google-derived data stored is your encrypted OAuth token and account id, on a private persistent volume, retained until you revoke or after inactivity.

**Sharing:** no sale; no sharing except strictly-necessary sub-processors (hosting, error monitoring).

**Revoke:** run `gdocs_reset_authorization`, or Google Account, then Security, then Third-party apps, then appscriptly, then Remove access.

**Security:** tokens encrypted at rest, HTTPS only, narrowest scopes that work (`drive.file` not broad Drive, read-only `forms.responses.readonly`).

**Contact:** support@appscriptly.com *(replace)*.

## 4. DEMO VIDEO SHOTLIST (unlisted YouTube, English, record against prod — already serving the final 8-scope set; 3–5 min)

> **A demo was already recorded + submitted: `https://youtu.be/hBuuDemD8Js`** (unlisted, on `sundeepg8@`), against the earlier ~41-tool surface. The **8 scopes are unchanged** since, so it almost certainly still satisfies Google (reviewers check scope-usage, not tool count). **Re-record only if Google asks.** Shotlist below is canonical for any re-record and now references the newer 66-tool surface.

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
