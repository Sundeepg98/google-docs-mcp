# appscriptly — Google OAuth Verification SUBMISSION (paint-by-numbers)

> **Generated 2026-06-13.** Companion to `PHASE1_VERIFICATION_KIT.md` (strategy/why) — this doc is the **exact-values** sheet for the Google Cloud Console OAuth consent screen + the OAuth Verification Center, refreshed for the **final 66-tool surface**.
>
> **Target app:** `appscriptly` · **Live server:** `https://mcp.appscriptly.com/mcp` (custom-domain serving URL since the 2026-06-14 cutover, backed by the Fly app `sundeepg98-docs-mcp` at `https://sundeepg98-docs-mcp.fly.dev`) · **Repo:** `Sundeepg98/google-docs-mcp` (module `src/appscriptly/`).

---

## ⚠️ STATE CHECK FIRST — verification is ALREADY UNDER REVIEW

Per `START_HERE.md` (frozen 2026-06-01) and the project memory, **the verification was already SUBMITTED on/around 2026-06-01 and is "under review"** (first Google email expected ~3–5 days, full review up to ~6 weeks). **This document is therefore a REFERENCE / RE-SUBMISSION sheet, not a fresh first-time submission.** Before the orchestrator drives the Console, confirm which of these is true:

1. **Still under review, nothing to do** → use this doc only to *answer Google's follow-up emails fast* (watch the `sundeepg8@` inbox — that's the gating item).
2. **Google bounced it / asked for changes** → use this doc to correct the exact field(s) and re-submit.
3. **A field genuinely needs (re)entering** → use §1–§3 below verbatim.

**Live state re-verified 2026-06-13 (probed, not assumed):**
- `GET https://sundeepg98-docs-mcp.fly.dev/health` → `{"ok":true,"service":"appscriptly"}` ✅
- `GET …/.well-known/oauth-protected-resource` advertises **exactly the 8 scopes** below (5 sensitive, 0 restricted) ✅
- `https://appscriptly.com/` → 200 ✅ · `https://appscriptly.com/privacy` → 200 ✅ · `https://appscriptly.com/terms` → 200 ✅
- Demo video (CURRENT) `https://youtu.be/r7ZB1YeT3SE` resolves ✅ — supersedes the REJECTED `https://youtu.be/hBuuDemD8Js` (chat-only / insufficient); T&S reply with the new link sent 2026-06-14, awaiting re-review.

> **Serving-URL cutover (2026-06-14):** the live serving URL is now the custom domain **`https://mcp.appscriptly.com/mcp`** (TLS cert + Cloudflare DNS + redirect URIs + 13 scopes on the consent screen + `TRUSTED_HOSTS` secret), still backed by the Fly app `sundeepg98-docs-mcp` (the `…fly.dev` host above remains the backing origin and the `fly`-command handle). The 2026-06-13 probes above were taken against the Fly host before the cutover and still reflect the backing origin.

---

## 1. OAuth consent screen — Branding (exact values to enter)

| Field | EXACT value | Source / flag |
|---|---|---|
| **App name** | `appscriptly` | kit §1.3 |
| **User support email** | ⚠️ **OPERATOR** — the support address on the `sundeepg8@gmail.com` account that owns the GCP project. Console only lets you pick an address you control; the kit's draft uses `support@appscriptly.com` for the *privacy policy* but Google's support-email dropdown is account-bound. **Confirm which address was actually selected at submit.** | FLAG |
| **App logo** | ⚠️ **FLAG — confirm a logo was uploaded.** Kit pre-flight requires a 120×120 PNG. **No logo file exists in the repo** (searched: no `*logo*`/`*icon*.png`). If a logo was uploaded to the Console it lives only there. A logo is **required** for verification; if missing, this blocks approval. | FLAG / BLOCKER if absent |
| **Application home page** | `https://appscriptly.com/` | live 200 ✅ |
| **Application privacy policy URL** | `https://appscriptly.com/privacy` | live 200 ✅ |
| **Application terms of service URL** | `https://appscriptly.com/terms` | live 200 ✅ — **NOTE:** the kit (2026-05-30) said terms was missing/optional, but a terms page is now LIVE. Terms is optional for Google; include it since it exists. |
| **Authorized domains** | `appscriptly.com` (primary). Optionally also `fly.dev` **only if** keeping the Fly URL as a serving/redirect host during the transition. | kit §1.3 |
| **Developer contact email** | ⚠️ **OPERATOR** — the developer/admin email (typically `sundeepg8@gmail.com`). Google uses this to contact you about the app; **must be monitored**. | FLAG |

> The public brand is anchored to **appscriptly.com**, which is why the Fly app name (`sundeepg98-docs-mcp`) and repo name (`google-docs-mcp`) don't need to match the brand for verification. **Serving-URL note (post 2026-06-14 cutover):** the live MCP is now served at the **`mcp.appscriptly.com`** subdomain (`https://mcp.appscriptly.com/mcp`), backed by the Fly app. The **apex `appscriptly.com`** is still a Cloudflare Pages landing/branding site only (home + `/privacy` + `/terms`) — do NOT point the MCP serving/redirect URL at the apex; use the `mcp.` subdomain (or the backing `…fly.dev` host).

## 2. OAuth client — Web application (exact values)

| Field | EXACT value |
|---|---|
| **Application type** | **Web application** (NOT Desktop) |
| **Name** | `appscriptly-server` |
| **Authorized redirect URIs** (BOTH required — the server runs two OAuth flows on one client) | `https://sundeepg98-docs-mcp.fly.dev/auth/callback`  ← FastMCP `GoogleProvider` connector callback (claude.ai connector flow)<br>`https://sundeepg98-docs-mcp.fly.dev/oauth/google/api/callback`  ← per-user Workspace grant (`CALLBACK_PATH` in `oauth_google.py`) |

> Both callback paths are verified present in code: `oauth_google.py:87` (`CALLBACK_PATH = "/oauth/google/api/callback"`) and FastMCP `GoogleProvider`'s default `/auth/callback` (wired in `http_server/`). `…/start` is NOT a route — do not register it.
> **Same client_id MUST feed both flows** (the `sub` claim must match across them or per-user token keying breaks). One `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` secret on Fly drives both.
> Optionally pre-register the `appscriptly.fly.dev` + `appscriptly.com` redirect pairs now for the deferred rename (Migration #4) — harmless to add early.

## 3. Data Access — the 8 scopes (paste exactly)

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

**Expected Console classification: 5 Sensitive, 0 Restricted** (`documents`, `spreadsheets`, `presentations`, `script.projects`, `script.deployments` = Sensitive; `drive.file` = non-sensitive per-file; `openid` + `userinfo.email` = non-sensitive). **Zero restricted ⇒ FREE verification path, no CASA.** If the Console shows anything under **Restricted**, STOP — something (likely residual gmail scopes from the reused gmail-mcp credential) leaked in; that means the dedicated `appscriptly-server` client isn't the one selected.

> `script.container.ui` / `script.external` / `script.scriptapp` are **NOT** appscriptly's client scopes — they live in the generated bound script's own manifest (a separate end-user consent). Do NOT add them here.

---

## 4. Per-scope justifications (1–2 sentences each, tied to the 66-tool surface)

Paste one justification per scope in the Verification Center. Each is tied to the specific tools that exercise it. (Full per-scope→tool map in §6.)

- **`openid`** — Obtain a stable, opaque per-account identifier (`sub`) solely to key each user's encrypted OAuth-token row in our store. No profile data is read or retained.

- **`…/auth/userinfo.email`** — Obtain the account email only to populate the FastMCP JWT `email` claim used for request routing and to show the connected account in tool responses. The email is **not** persisted (only the `sub` is); no marketing, no sharing.

- **`…/auth/documents`** — Create, read, and structurally edit Google Docs on the user's explicit instruction via `documents.batchUpdate` (17 docs tools: `gdocs_make_tabbed_doc`, `gdocs_add_tabs`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, `gdocs_edit_range`, `gdocs_insert_table`/`insert_markdown_table`, `gdocs_format_range`/`format_paragraph`, `gdocs_rename_tab`/`delete_tab`/`set_tab_icons`, etc.). Read-only is insufficient — the core feature **creates and restructures** documents (native Tabs, ranged edits, tables).

- **`…/auth/spreadsheets`** — Create and mutate Google Sheets at the user's request via `spreadsheets.batchUpdate` / `values.*` (13 sheets tools: `gsheets_create_spreadsheet`, `gsheets_write_range`, `gsheets_append_rows`, `gsheets_add_sheet`/`delete_sheet`/`rename_sheet`/`duplicate_sheet`, `gsheets_format_range`/`apply_conditional_format`/`freeze`/`protect_range`/`clear_range`, plus `gsheets_read_range`). The full scope (not `spreadsheets.readonly`) is required because the majority of tools write, format, protect, and restructure sheets.

- **`…/auth/presentations`** — Create and batch-edit Google Slides at the user's request via `presentations.batchUpdate` (8 slides tools: `gslides_create_presentation`, `gslides_add_slide`, `gslides_replace_all_text`, `gslides_create_image`/`create_table`/`create_shape`/`create_line`, plus `gslides_get_outline`). Read-only is insufficient — the feature creates decks and inserts shapes/lines/tables/images.

- **`…/auth/drive.file`** — Per-file Drive scope (only files the app created or the user explicitly opened) backing file organization and sharing (10 drive tools: `gdocs_move_to_folder`, `gdocs_create_folder`, `gdocs_trash_file`/`untrash_file`, `gdocs_share_file`/`list_permissions`/`revoke_permission`, `gdocs_find_doc_by_title`/`find_file`, `gdocs_export_doc`) plus video-pipeline frame staging. **Deliberately chosen over broad Drive** to minimize access — appscriptly cannot see or search the rest of the user's Drive.

- **`…/auth/script.projects`** — The headline feature: create and update Google Apps Script projects bound to the user's Doc/Sheet so persistent automations (custom menus, custom `=FUNCTION()`s, scheduled dashboards, onEdit/onFormSubmit triggers, web-app endpoints) can be installed (`gdocs_install_automation`, `as_generate_bound_script`, `as_install_doc_menu`/`custom_function`/`sheet_dashboard`, `as_install_edit_trigger`/`form_handler`, `gdocs_setup_apps_script`). No narrower scope exists for managing Apps Script project content.

- **`…/auth/script.deployments`** — Deploy the Apps Script project the user asked us to create — as a Web App / installable trigger — so the automation actually runs (`as_deploy_web_app`, the deploy step of `gdocs_install_automation`, and the installable-trigger tools `as_install_edit_trigger`/`as_install_form_handler`). The `script.projects` scope alone cannot create deployments.

---

## 5. Demo-video script (shot-by-shot, against the LIVE 66-tool app)

Record unlisted on YouTube, English narration, against prod `https://mcp.appscriptly.com/mcp` (the custom-domain serving URL since the 2026-06-14 cutover, backed by the Fly app). Target 3–5 min. **Every sensitive scope must be shown being used by a real tool**, plus the consent screen with the app name + client_id.

> **CURRENT demo: `https://youtu.be/r7ZB1YeT3SE`** (unlisted, on `sundeepg8@` / channel "Sundeep G"), re-recorded to exercise the deployed scopes end-to-end. **It supersedes the prior `https://youtu.be/hBuuDemD8Js`, which Google REJECTED as chat-only / insufficient — do not resubmit that one.** The T&S verification reply carrying the new link was **sent 2026-06-14 and is awaiting Google re-review.** The script below is the canonical shotlist if a further re-record is ever needed.

| # | Duration | Action | Scope demonstrated |
|---|---|---|---|
| 0 | 5s | Title card; say "appscriptly". | — |
| 1 | 15s | Trigger connect → land on the Google consent screen; pause. | OAuth grant flow |
| 2 | 15s | Zoom in: show "appscriptly wants access to your Google Account" + the scope list; show the address bar with `client_id=…apps.googleusercontent.com`. | **App name + client_id (required)** |
| 3 | 5s | Click **Allow** → callback success page. | — |
| 4 | 35s | Run `gdocs_make_tabbed_doc` (3 tabs); open the doc; show native Tabs. Optionally `gdocs_edit_range` to show a ranged edit. | **documents** |
| 5 | 35s | Run `gsheets_create_spreadsheet` + `gsheets_write_range`; then `gsheets_freeze` (freeze header) and/or `gsheets_protect_range`; show the values + frozen/protected result. | **spreadsheets** |
| 6 | 30s | Run `gslides_create_presentation` + `gslides_replace_all_text`; optionally `gslides_create_shape`/`create_line`; open the deck. | **presentations** |
| 7 | 50s | Run `as_install_doc_menu` on the doc (or `as_install_edit_trigger` for an onEdit automation); refresh the doc; show the new custom menu / the created script project + deployment. | **script.projects + script.deployments** |
| 8 | 20s | Run `gdocs_move_to_folder` / `gdocs_share_file` (or `as_generate_video_deck` frame staging); show the file organized/shared. | **drive.file** |
| 9 | 10s | Show revoke: Google Account → Security → Third-party apps → appscriptly → Remove access (or run `gdocs_reset_authorization`). | — (good-faith data-control demo) |

---

## 6. Per-scope → tool map (full 66-tool surface, authoritative)

Built from each service's `_expected_tools.py` on `origin/main` (62) + PR #191's 4 sheets tools = **66**. Counts verified exhaustive (every golden tool classified; none extra).

### `documents` — Docs service (17 tools)
`gdocs_make_tabbed_doc`, `gdocs_tab_existing_doc`, `gdocs_add_tabs`, `gdocs_delete_tab`, `gdocs_rename_tab`, `gdocs_get_tab_url`, `gdocs_set_tab_icons`, `gdocs_preview_tab_split`, `gdocs_get_doc_outline`, `gdocs_read_doc`, `gdocs_append_to_tab`, `gdocs_replace_all_text`, `gdocs_edit_range`, `gdocs_insert_table`, `gdocs_insert_markdown_table`, `gdocs_format_range`, `gdocs_format_paragraph`

### `spreadsheets` — Sheets service (13 tools)
`gsheets_create_spreadsheet`, `gsheets_read_range`, `gsheets_write_range`, `gsheets_append_rows`, `gsheets_clear_range`, `gsheets_add_sheet`, `gsheets_delete_sheet`, `gsheets_rename_sheet`, `gsheets_duplicate_sheet`, `gsheets_freeze`, `gsheets_protect_range`, `gsheets_format_range`, `gsheets_apply_conditional_format`

### `presentations` — Slides service (8 tools)
`gslides_create_presentation`, `gslides_add_slide`, `gslides_get_outline`, `gslides_replace_all_text`, `gslides_create_image`, `gslides_create_table`, `gslides_create_shape`, `gslides_create_line`

### `drive.file` — Drive service (10 tools)
`gdocs_find_doc_by_title`, `gdocs_find_file`, `gdocs_create_folder`, `gdocs_move_to_folder`, `gdocs_trash_file`, `gdocs_untrash_file`, `gdocs_export_doc`, `gdocs_share_file`, `gdocs_list_permissions`, `gdocs_revoke_permission`

### `script.projects` + `script.deployments` — Apps Script + GAS-deploy services (11 tools)
**apps_script (8):** `as_generate_bound_script`, `as_install_doc_menu`, `as_install_custom_function`, `as_install_sheet_dashboard`, `as_install_edit_trigger`, `as_install_form_handler`, `as_generate_video_deck`, `as_encode_video`
**gas_deploy (3):** `gdocs_install_automation`, `gdocs_setup_apps_script`, `as_deploy_web_app`
(Project creation/update needs `script.projects`; deploying web apps + installing triggers needs `script.deployments`.)

### `openid` + `userinfo.email` — identity (no dedicated tools)
Consumed by every authenticated tool call for per-user token keying (`sub`) and routing (`email`). Not surfaced as standalone tools.

### Admin / meta — no Google API scope (7 tools)
`gdocs_server_info`, `gdocs_help`, `gdocs_guide`, `gdocs_test_manifest`, `gdocs_admin_audit` (reads the local user-state DB), `gdocs_get_signed_upload_url` (server-side staging endpoint, no Drive read), `gdocs_reset_authorization` (revokes/clears local creds). These do not call Google Workspace APIs, so they don't drive any consent scope.

**Totals:** 17 + 13 + 8 + 10 + 11 = **59 scope-bearing tools** + **7 admin/meta** (+ identity scopes used by all) = **66**.

---

## 7. Operator-identity-gated vs orchestrator-drivable steps

Honest classification of the remaining Console/Verification-Center work (per the decide-drive-batch guardrail — only true identity/capability gates go to the operator).

### 🔒 OPERATOR-only (Google login / human click / monitored inbox — cannot be delegated)
- **Google account login + 2FA** on `sundeepg8@gmail.com` (the account owning the GCP project + verified domain).
- **The final "Submit for verification" click** in the Verification Center.
- **Responding to Google's review emails** in the `sundeepg8@` inbox — *the single most time-sensitive item while under review* (a slow reply stalls the whole review).
- **Selecting the user-support email + developer-contact email** (Console only offers addresses the logged-in account controls).
- **Uploading the app logo** (if it needs (re)uploading — see §1 FLAG).
- **Publishing to Production** (flips Testing → In production) — done already per START_HERE, but it's an operator click if it ever needs redoing.

### 🤖 Orchestrator-drivable (via Playwright, once the operator is logged in OR on a read-only pass)
- **Reading current Console/Verification-Center state** (what's already entered, scope classification, review status) — read-and-screenshot only.
- **Typing the §1 branding values, §2 redirect URIs, and §3 scope list** into the forms (filling fields is automatable; the human still does login + final submit).
- **Pasting the §4 per-scope justifications** into the Verification Center text boxes.
- **Pasting the demo-video URL** into the submission form.

> Per the browser-agent state-change guardrail: a Playwright agent should **read-and-screenshot only** unless the operator explicitly authorizes the form-filling pass; never navigate to a `…/submit`/`…/publish` URL on its own, and **hard-stop at the Google login/2FA wall** (do not attempt to enter credentials).

---

## 8. PLACEHOLDER / UNKNOWN values needing the operator (consolidated)

| Item | Status | Action needed |
|---|---|---|
| **App logo** | ⚠️ **UNKNOWN — likely BLOCKER** | No logo file in repo. Confirm a 120×120 PNG was uploaded to the Console; if not, create + upload one — **verification requires a logo.** |
| **User-support email** | ⚠️ Needs confirmation | Confirm the exact address selected in the Console (account-bound; kit's `support@appscriptly.com` is for the privacy-policy text, not necessarily the Console dropdown value). |
| **Developer-contact email** | ⚠️ Needs confirmation | Confirm it's a monitored address (likely `sundeepg8@gmail.com`); Google contacts you here. |
| **Privacy-policy contact email** | ⚠️ Minor | The live `appscriptly.com/privacy` should show a real support email, not the kit draft's `support@appscriptly.com *(replace)*` placeholder. Spot-check the live page. |
| **Demo video** | ✅ Done (redo submitted) | Current demo `https://youtu.be/r7ZB1YeT3SE` (supersedes the REJECTED `hBuuDemD8Js`); T&S reply with the new link sent 2026-06-14, awaiting Google re-review (§5). |
| **`fly.dev` as authorized domain** | ℹ️ Decision | Include `fly.dev` only if keeping the Fly URL during transition; drop after Migration #4 cutover. |

Everything else (app name, home/privacy/terms URLs, the 8 scopes, both redirect URIs, per-scope justifications) is **known and verified** above.
