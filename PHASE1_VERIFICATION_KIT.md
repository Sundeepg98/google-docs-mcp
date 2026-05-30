# appscriptly — Google OAuth Free Verification + Dedicated OAuth Client (Phase-1 Action Kit)

> Generated 2026-05-30. Verified against `origin/main` @ `e7222e5` (post-#148/#149/#150). Free sensitive-scope verification path (zero restricted scopes → no CASA).

**Target repo:** `Sundeepg98/google-docs-mcp` (module `google_docs_mcp`).
**Deployed:** Fly app `sundeepg98-docs-mcp` → `https://sundeepg98-docs-mcp.fly.dev` (new app `appscriptly` reserved, not yet cut over).

## 0. Confirmed final scope set (8 scopes — `drive.readonly` removed in #148, MERGED + deployed + live-verified)

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

**Verification class: FREE.** 5 sensitive, **zero restricted**. No CASA / no third-party audit. Needs: verified domain + privacy policy + demo video + per-scope justifications (~3–5 business days).

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

## 2. PER-SCOPE JUSTIFICATIONS

- **openid / userinfo.email** — obtain a stable user id (`sub`) + email solely to key per-user OAuth token storage and show the connected account; no profile data, no marketing, not shared.
- **documents** — create/edit Docs at user instruction (`gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, `gdocs_rename_tab`). Read-only insufficient: core feature creates/restructures docs via `documents.batchUpdate`.
- **spreadsheets** — `gsheets_create_spreadsheet`, `gsheets_write_range`, `gsheets_read_range`. Read-only insufficient: two of three mutate.
- **presentations** — `gslides_create_presentation`, `gslides_replace_all_text` (batchUpdate), `gslides_get_outline`. Read-only insufficient: creates + batch-edits.
- **drive.file** — per-file scope: only app-created/opened files. Backs `gdocs_move_to_folder`/`trash`/`untrash`/`share`/`list_permissions`/`find_doc_by_title` + video-pipeline staging. Chosen over broad Drive to minimize access.
- **script.projects** — headline feature: create/update Apps Script projects bound to user's doc/sheet (`gdocs_install_automation`, `as_generate_bound_script`, `as_install_doc_menu`/`custom_function`/`sheet_dashboard`). No narrower scope exists.
- **script.deployments** — deploy the created script (web app / installable triggers) so the automation runs. Projects scope alone can't deploy.

## 3. PRIVACY POLICY — host at https://appscriptly.com/privacy

(Full draft below; replace the support email.)

**appscriptly — Privacy Policy** · Last updated 30 May 2026

appscriptly is a Google Workspace automation tool that creates, edits, and manages Google Docs, Sheets, Slides, and Drive files, and installs Apps Script automations, on your behalf and at your explicit direction. appscriptly's use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

**Google data we access** (and nothing more): email + account id (`openid`, `userinfo.email`) to identify your account and store authorization per-user; Docs (`documents`), Sheets (`spreadsheets`), Slides (`presentations`) to create/edit what you ask; Drive files created by or opened with appscriptly (`drive.file`) — a per-file scope; **appscriptly cannot see, read, or search the rest of your Drive**; Apps Script projects/deployments (`script.projects`, `script.deployments`) to install the automations you request. **appscriptly does NOT request or access:** Gmail/email, your full Drive, contacts, or calendar.

**Use:** solely to perform the specific automation you request. No advertising, profiling, ML/AI training, or unrelated use. No human reads your data except for security, legal compliance, or with your explicit consent.

**Storage/retention:** your content lives in YOUR Google account — we don't copy or warehouse it. The only Google-derived data stored is your encrypted OAuth token + account id, on a private persistent volume, retained until you revoke or after inactivity.

**Sharing:** no sale; no sharing except strictly-necessary sub-processors (hosting, error monitoring).

**Revoke:** run `gdocs_reset_authorization`, or Google Account → Security → Third-party apps → appscriptly → Remove access.

**Security:** tokens encrypted at rest, HTTPS only, minimum scope (`drive.file` not broad Drive).

**Contact:** support@appscriptly.com *(replace)*.

## 4. DEMO VIDEO SHOTLIST (unlisted YouTube, English, record against prod — already serving the final 8-scope set post-#148; 3–5 min)

0. Title card + say "appscriptly" (5s).
1. Trigger connect → Google consent screen, pause (15s) — **OAuth grant flow**.
2. Zoom: show "appscriptly wants access" + scope list; show address bar with `client_id=…apps.googleusercontent.com` (15s) — **app name + client id (required)**.
3. Click Allow → callback success (5s).
4. `gdocs_make_tabbed_doc` (3 tabs) → open doc, show native Tabs (35s) — **documents**.
5. `gsheets_create_spreadsheet` + `gsheets_write_range` → show values (30s) — **spreadsheets**.
6. `gslides_create_presentation` / `gslides_replace_all_text` → open deck (30s) — **presentations**.
7. `as_install_doc_menu` on the doc → show new custom menu after refresh + created script/deployment (50s) — **script.projects + script.deployments**.
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
