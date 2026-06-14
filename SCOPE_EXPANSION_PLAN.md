# appscriptly Scope Expansion: Deploy-Staging Package

The Calendar, Tasks, Forms, and Contacts REST services and their OAuth scopes are
BUILT and merged to main. What remains is the operator-gated, verify-LAST DEPLOY,
not a build. This document is the staging package for that deploy.

- Repo: appscriptly (remote Sundeepg98/google-docs-mcp), branch main.
- Pinned commit read for this analysis: `ccbce87db134956d056df2ee5fcf0854f2f5b496`
- Deploy state: un-deployed. The app is UNDER live Google OAuth verification for the
  scopes CURRENTLY on the consent screen (the under-review 8, see Section 2).
- This is read-only design. Nothing here is deployed, no GCP or OAuth config is
  changed, no API is enabled. The only file written is this artifact.
- All outward-facing draft text in this document is em-dash-free by rule.

---

## 0. SHARP-EDGE WARNING (read before anything else)

DEPLOY MUST STAY OFF until Google approves the expanded scope set.

The CODE on main now requests 13 OAuth scopes. The consent set CURRENTLY under
Google verification is 8. The code is AHEAD of what is under review by 5 scopes
(forms.body, forms.responses.readonly, tasks, calendar, contacts).

If the app is deployed NOW:
- The live OAuth consent screen would present all 13 scopes to users and to
  Google's reviewers, WHILE the active verification is scoped to 8.
- That pushes a 13-scope consent screen against an 8-scope review. Google's
  reviewer sees scopes that are not part of the submission, the demo video does not
  cover them, and the privacy policy (currently stale, Section 8) does not list
  them.
- Likely outcome: the in-flight approval is damaged or rejected, and the project
  loses the sensitive-only, no-CASA verification it has been protecting.

Therefore: keep the deploy gate OFF (un-deployed) until the expanded 13-scope set
is submitted and APPROVED. The build being done does NOT mean ship now. The deploy
is the last step, after verification lands. Full sequence in Section 7.

(Note on the gate: `DEPLOY_ENABLED` is an operator/deploy-side concept, referenced
in code comments and the tasks service docstrings; it is not a runtime feature flag
in `config.py`/`server.py`/`fly.toml`/`Dockerfile`. The operational control is
"do not run the deploy" until approval. Treat that as the gate.)

---

## 1. Reality check: what is BUILT vs what REMAINS

### 1.1 BUILT and merged to main (ccbce87)

All 8 Workspace services now ship REST tools, and all their scopes are in the
single-source `auth.WORKSPACE_SCOPES`:

- Service directories present:
  `admin`, `apps_script`, `calendar`, `contacts`, `docs`, `drive`, `forms`,
  `gas_deploy`, `sheets`, `slides`, `tasks`.
- The four newer services and their declared tool surfaces
  (from each `services/<svc>/_expected_tools.py`):
  - calendar (7): `gcal_list_events`, `gcal_get_event`, `gcal_create_event`,
    `gcal_update_event`, `gcal_delete_event`, `gcal_list_calendars`,
    `gcal_freebusy`.
  - tasks (7): `gtasks_list_tasklists`, `gtasks_create_tasklist`,
    `gtasks_list_tasks`, `gtasks_create_task`, `gtasks_update_task`,
    `gtasks_complete_task`, `gtasks_delete_task`.
  - forms (7): `gforms_create_form`, `gforms_get_form`, `gforms_add_question`,
    `gforms_update_item`, `gforms_delete_item`, `gforms_list_responses`,
    `gforms_get_response`.
  - contacts (6): `gcontacts_list`, `gcontacts_search`, `gcontacts_get`,
    `gcontacts_create`, `gcontacts_update`, `gcontacts_delete`.
- The pre-existing services remain: docs (16), sheets (9), slides (6), drive (10,
  all within `drive.file` reach), gas_deploy (3), apps_script (6), admin (7).
- The GAS bridge (`as_generate_bound_script`, `as_deploy_web_app`,
  `as_install_doc_menu`, `as_install_custom_function`,
  `as_install_sheet_dashboard`, plus the slides-to-video pair) is shipped. It uses
  ONLY `script.projects` + `script.deployments` (already in the under-review set),
  so it needs no new consent. Detail in Section 5.

### 1.2 Scope plumbing is single-source and CI-guarded

- `auth.WORKSPACE_SCOPES` is the SINGLE SOURCE OF TRUTH (11 Workspace scopes).
  `auth.SCOPES = WORKSPACE_SCOPES` (stdio set). `oauth_google.GOOGLE_API_SCOPES =
  [*IDENTITY_SCOPES, *WORKSPACE_SCOPES]` (HTTP/connector set = 13).
- `oauth_google` imports `WORKSPACE_SCOPES` from `auth` (leaf module, no cycle).
  Adding a service scope is a one-line edit in `auth.py`; both consent sets update.
- Two CI guards already protect this:
  - `tests/unit/test_scope_union_single_source.py`: asserts the derived sets equal
    the exact intended literals (frozenset equality), so drift fails CI.
  - `tests/unit/test_base_tier_scopes.py`: declares the intended 13-scope
    `_TARGET_CONNECTOR` and asserts NONE of them are in Google's `_RESTRICTED`
    set, so a restricted-scope creep fails CI. This is the in-repo authoritative
    no-CASA guard.

### 1.3 What REMAINS (the only open work)

1. Enable 4 GCP APIs in the project (Calendar, Tasks, Forms, People). Section 6.
2. Add the 5 new scopes to the LIVE OAuth consent screen and submit for
   verification. Section 3 + 7.
3. Re-record the demo to cover the 5 new scopes end-to-end. Section 9.
4. Update the docs that must ship with the deploy, principally `docs/PRIVACY.md`
   (currently stale, Section 8).
5. ONLY on approval: deploy (turn the deploy gate on). Section 7.

No code change to the services or scope lists is required. A change to
`auth.WORKSPACE_SCOPES` IS a change to the consent screen and is operator-gated; it
is already done and correct on main.

---

## 2. Scope sets: under-review vs current-code

### 2.1 The 8 scopes CURRENTLY under Google verification (consent set being reviewed)

| # | Scope | Purpose | Tier |
|---|-------|---------|------|
| 1 | `openid` | identity | n/a |
| 2 | `.../auth/userinfo.email` | identity (per-user routing key) | sensitive (identity) |
| 3 | `.../auth/documents` | Docs read/write | SENSITIVE |
| 4 | `.../auth/drive.file` | per-file Drive (app-created/opened only) | not restricted |
| 5 | `.../auth/spreadsheets` | Sheets read/write/create | SENSITIVE |
| 6 | `.../auth/presentations` | Slides read/write/create | SENSITIVE |
| 7 | `.../auth/script.projects` | create + push Apps Script projects | SENSITIVE |
| 8 | `.../auth/script.deployments` | cut version + deploy Apps Script | SENSITIVE |

### 2.2 The 13 scopes in the CURRENT CODE (the target consent set after this deploy)

The under-review 8 PLUS the 5 new ones below. All 13 are SENSITIVE or identity;
ZERO are RESTRICTED; no CASA.

| # | New scope (the 5 to add) | Service | Purpose | Tier |
|---|---|---|---|---|
| 9 | `.../auth/forms.body` | forms | create / edit forms (forms.create, batchUpdate) | SENSITIVE |
| 10 | `.../auth/forms.responses.readonly` | forms | read submitted responses | SENSITIVE |
| 11 | `.../auth/tasks` | tasks | tasklists + tasks read/write/delete | SENSITIVE |
| 12 | `.../auth/calendar` | calendar | events + calendar metadata read/write | SENSITIVE |
| 13 | `.../auth/contacts` | contacts | People API contacts read/write | SENSITIVE |

The full-vs-narrow choices are deliberate and documented in `auth.py`: `calendar`
(not `calendar.events`/`.readonly`) because the service creates, patches, deletes
events AND lists calendars; `tasks` (not `tasks.readonly`) because it mutates;
`contacts` (not `contacts.readonly`) because it creates/updates/deletes;
`forms.body` + `forms.responses.readonly` paired for build-and-read. Every one is
SENSITIVE, none restricted (Section 4 + the in-repo `_RESTRICTED` guard), so the
no-CASA posture holds. `drive.readonly` remains deliberately OUT (it is the only
restricted scope the project ever held; CI test asserts its absence).

---

## 3. The EXACT scopes to ADD to the live consent screen

These 5, and only these 5, are added on top of the under-review 8. They are already
in the code; this is the consent-screen submission, not a code edit.

```
https://www.googleapis.com/auth/forms.body
https://www.googleapis.com/auth/forms.responses.readonly
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/contacts
```

All SENSITIVE. None RESTRICTED. No CASA assessment triggered.

---

## 4. Authoritative scope-tier classification (verified live against Google docs)

CASA gate: RESTRICTED = CASA annual third-party security assessment = hard NO-GO.
SENSITIVE = Google brand/sensitive review, NO CASA. The authoritative current
source is the App-Verification Help Center "Restricted Scopes" list
(support.google.com/cloud/answer/13464325), which enumerates the COMPLETE restricted
set; anything absent from it is not restricted. The master scope page
(/identity/protocols/oauth2/scopes) no longer carries tier badges. This matches the
in-repo `tests/unit/test_base_tier_scopes.py::_RESTRICTED` guard exactly.

The 5 scopes being added (all confirmed SENSITIVE, no CASA):

| Scope | Tier | CASA |
|---|---|---|
| forms.body | SENSITIVE | No |
| forms.responses.readonly | SENSITIVE | No |
| tasks | SENSITIVE | No |
| calendar | SENSITIVE | No |
| contacts | SENSITIVE | No |

Wider reference table (kept for the GAS-vs-REST analysis and to bound any future
expansion):

| Scope | Tier | CASA |
|---|---|---|
| calendar / calendar.events / calendar.readonly / calendar.events.readonly | SENSITIVE | No |
| tasks / tasks.readonly | SENSITIVE | No |
| forms.body / forms.body.readonly / forms.responses.readonly | SENSITIVE | No |
| contacts / contacts.readonly / contacts.other.readonly / directory.readonly | SENSITIVE | No |
| gmail.send | SENSITIVE | No |
| gmail.labels | SENSITIVE | No |
| gmail.readonly | RESTRICTED | YES (NO-GO) |
| gmail.modify | RESTRICTED | YES (NO-GO) |
| mail.google.com/ | RESTRICTED | YES (NO-GO) |
| drive (full) | RESTRICTED | YES (NO-GO) |
| drive.readonly | RESTRICTED | YES (NO-GO) |
| drive.file | not restricted | No |

The classification people most often get wrong: `gmail.send` and `gmail.labels`
are SENSITIVE, not restricted; full Gmail read/modify is restricted. `drive.file`
is the CASA-free Drive scope (already deployed). These are NOT part of this
expansion; they are documented only to bound any future request.

Sources confirmed (live, 2026-06-14):
- Restricted list (read verbatim): https://support.google.com/cloud/answer/13464325
- Sensitive vs restricted framework, no-CASA-for-sensitive:
  https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- Restricted = security assessment (CASA):
  https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- Scope descriptions: https://developers.google.com/identity/protocols/oauth2/scopes

---

## 5. The GAS bridge (already shipped; complements REST, no new scope)

The GAS lever is not future work. It exists today and rides only the already
under-review `script.*` scopes, so it is unaffected by this expansion. It matters
here for two reasons: it explains why the project never needs a RESTRICTED scope,
and it is the no-CASA path to capabilities REST cannot express.

- `as_generate_bound_script` (`services/apps_script/api.py` + `tools.py`):
  `projects.create(parentId=container_id)` -> `updateContent` (manifest + `.gs`) ->
  `versions.create` -> `deployments.create`. Generates a CONTAINER-BOUND script
  with caller-authored `.gs`. The script's OWN manifest declares its `oauthScopes`,
  so the target-service authorization lives on the USER's script, not on
  appscriptly's consent screen.
- `as_deploy_web_app` (`services/gas_deploy/tools.py`): deploys a STANDALONE Web App
  from caller `doGet`/`doPost`, `execute_as="USER_DEPLOYING"`,
  `access="ANYONE_ANONYMOUS"`, returns the live `/exec` URL. The docstring already
  prescribes a shared-secret / HMAC check in the handler because Apps Script cannot
  put auth in front of an anonymous Web App.

`scripts.run` stays NO-GO (documented in
`services/apps_script/sheet_dashboard.py`: it requires an API-executable tied to a
standard GCP project plus an extra scope, "out of scope for, and unreliable from,
this tool"). The `/exec` Web App is the substitute and is already built.

Honest constraint already encoded: an INSTALLABLE trigger (time-driven, onEdit,
onFormSubmit) only exists once its `installTrigger()` actually runs; deploying does
not auto-run it. `as_install_sheet_dashboard` returns `trigger_active=False` +
`activation_required=True` with a one-step activation instruction. SIMPLE triggers
(`onOpen`, simple `onEdit`, custom functions, `doGet`/`doPost`) need no activation.

Consequence for this expansion: the GAS path is why even Gmail and full Drive can
be reached later WITHOUT CASA (the user authorizes their own script), so there is
never pressure to add a restricted scope to appscriptly's consent screen.

---

## 6. GCP APIs to enable (deploy prerequisite for the native-REST path)

The new REST tools call native Google APIs that must be ENABLED in the GCP project
before they work live. Enabling an API is free and does NOT by itself widen consent,
but it is a hard prerequisite, so it is the FIRST deploy step (and is explicitly not
done now).

| Service | GCP API to enable |
|---|---|
| Calendar | Google Calendar API |
| Tasks | Google Tasks API |
| Forms | Google Forms API |
| Contacts | People API |
| (already enabled) | Docs, Sheets, Slides, Drive, Apps Script APIs |

In-repo confirmation: `services/tasks/api.py` and `services/tasks/__init__.py`
explicitly note the Google Tasks API must be enabled before the `gtasks_*` tools
work live. The same applies to Calendar, Forms, and People for their services.
Procedure reference already in repo: `docs/runbooks/gcp-project-linking.md`.

---

## 7. Deploy sequence (verify-LAST; operator-gated)

Strict order. Do not reorder; do not skip the approval gate.

1. ENABLE the 4 GCP APIs (Section 6): Calendar, Tasks, Forms, People. Free,
   reversible, no consent change. Per `docs/runbooks/gcp-project-linking.md`.
2. ADD the 5 scopes (Section 3) to the LIVE OAuth consent screen configuration in
   the GCP OAuth consent screen, so the submitted set is the full 13.
3. UPDATE the docs that ship with the submission, principally `docs/PRIVACY.md`, to
   list the full 13-scope set and the four new data categories (Section 8). The
   privacy policy URL is part of the OAuth verification; a stale policy can fail
   review.
4. RE-RECORD the demo so it exercises each of the 5 new scopes end-to-end
   (Section 9). The prior demo covered only the under-review set and does not
   satisfy a 13-scope review.
5. SUBMIT for verification with the full 13-scope set, the updated privacy policy,
   and the new demo. This is a sensitive-scope verification (no CASA), so expect a
   brand/sensitive review on the order of days, not a third-party audit.
6. WAIT for Google approval of the expanded set. The app stays un-deployed
   throughout.
7. ONLY ON APPROVAL: deploy (turn the deploy gate on). Until this point the deploy
   gate stays OFF (Section 0).

Batching choice within step 2/5 (reversible operator call): submit all 5 scopes in
ONE verification cycle to minimize the number of review rounds (recommended, since
they are all sensitive and share one demo session), OR stage them service-by-service
to minimize per-submission risk. One cycle is the default recommendation here.

What must ship TOGETHER with the deploy (coupling):
- Code at 13 scopes (already on main) + GCP APIs enabled + consent screen at 13 +
  privacy policy at 13 + approved demo at 13. Deploying with any of these lagging
  reintroduces the Section 0 hazard (consent ahead of review/policy/demo).

---

## 8. Documentation that must ship WITH the deploy

The privacy policy and any scope-listing docs must reflect the full 13 scopes
BEFORE submission, because the privacy policy is part of OAuth verification.

Finding: `docs/PRIVACY.md` is STALE relative to the current code. It enumerates
only "Google's APIs (Drive, Docs, Apps Script)" and mentions the `userinfo.email`
scope; it does NOT mention Calendar, Tasks, Forms, or Contacts, nor the data those
services read and write. Specifically:
- The "What we access" section lists Drive/Docs/Apps Script only.
- There is no disclosure of calendar events, task lists/tasks, form definitions or
  form responses, or contact records as data the app reads or writes.

Required PRIVACY.md updates (em-dash-free, ready to drop in):

- In the data-access section, replace the Drive/Docs/Apps-Script-only list with the
  full surface: "appscriptly accesses your Google Docs, Sheets, Slides, Drive files
  it creates or opens, Apps Script projects it deploys, your Google Calendar events
  and calendar list, your Google Tasks lists and tasks, your Google Forms and the
  responses people submit to them, and your Google Contacts, limited to the OAuth
  scopes you consent to."
- Add a per-service data-category line for each new service:
  - "Calendar: the app reads and writes events and reads your calendar list to let
    you manage your schedule by request."
  - "Tasks: the app reads and writes your task lists and tasks to let you capture
    and manage to do items."
  - "Forms: the app creates and edits forms and reads the responses people submit,
    to let you build forms and review results. Response access is read only."
  - "Contacts: the app reads and, where you ask, creates or updates contacts to
    help you find and organize the people you work with."
- Confirm the "no third-party analytics" and "content stays in your Google account"
  statements still hold for the new services (they do; the new tools call only
  `*.googleapis.com`).

Other docs to sanity-check for scope counts before submission: `README.md`,
`docs/security-posture.md`, `docs/ARCHITECTURE.md` (any place that enumerates the
scope set or service count). These are lower-risk than the privacy policy but should
not contradict the 13-scope reality at deploy time.

---

## 9. Demo delta (additional scenes the re-verification demo must show)

Each of the 5 new scopes must be exercised end-to-end on camera. Draft scene list
plus one-paragraph, em-dash-free scope justification per scope (ready to paste into
the OAuth console scope-justification field). The under-review 8 are already
covered by the existing demo; these are the additions.

Calendar (`calendar`):
- Scene: a tool call lists upcoming events (`gcal_list_events`), creates a new event
  (`gcal_create_event`), updates it (`gcal_update_event`), runs a free/busy query
  (`gcal_freebusy`), then deletes the event (`gcal_delete_event`), with the Google
  Calendar UI shown reflecting each change.
- Justification: "appscriptly uses Calendar access to let users manage their
  schedule through natural language: listing upcoming events, creating and editing
  events, checking free and busy times, and removing events. The full calendar
  scope is requested because the app both reads the calendar list and writes events
  on the user's behalf. The app acts only on the calendars the user asks about."

Tasks (`tasks`):
- Scene: list task lists (`gtasks_list_tasklists`), create a task
  (`gtasks_create_task`), mark it complete (`gtasks_complete_task`), then delete it
  (`gtasks_delete_task`), with the Google Tasks UI shown.
- Justification: "appscriptly uses Tasks access to let users capture and manage to
  do items by voice or chat: creating tasks, updating and completing them, and
  organizing task lists. The read and write scope is requested because the app
  creates, updates, and deletes the user's own tasks. Access is limited to the
  user's task data so the app can keep their list in sync with the actions they
  request."

Forms (`forms.body` and `forms.responses.readonly`):
- Scene: create a form (`gforms_create_form`), add a question
  (`gforms_add_question`), open the live form and submit a test response, then read
  the responses back through the tool (`gforms_list_responses`,
  `gforms_get_response`).
- Justification: "appscriptly uses Forms access to let users build and manage forms
  and review submissions: creating a form, adding and editing questions, and reading
  the responses people submit. The forms.body scope covers creating and editing form
  structure; the forms.responses.readonly scope is read only, so the app reads
  responses but does not alter or delete them. The app summarizes and reports
  results at the user's request."

Contacts (`contacts`):
- Scene: list and search contacts (`gcontacts_list`, `gcontacts_search`), open a
  contact (`gcontacts_get`), create a contact (`gcontacts_create`), update it
  (`gcontacts_update`), then delete it (`gcontacts_delete`), with the Google Contacts
  UI shown.
- Justification: "appscriptly uses Contacts access to help users find and organize
  the people they work with: searching and viewing contacts and, where the user
  asks, creating, updating, or removing contact entries. The full contacts scope is
  requested because the app both reads and modifies the user's own contacts. Access
  is limited to the user's address book so the app acts only on their behalf."

Note: the GAS path needs NO demo delta against appscriptly's scopes, because any
target-service authorization for a generated script happens on the USER's own
script, not on appscriptly's consent screen. The existing demo already covers
`script.projects` / `script.deployments`.

---

## 10. PART 1 (retained) - Head-to-head value matrix: Native REST vs GAS /exec

All quota/limit figures verified against official Google docs (2026-06-14); sources
cited inline. "Consumer" = free @gmail.com; "Workspace" = paid Workspace. This
section is retained from the capability analysis; it explains why both levers are
kept and why no restricted scope is ever needed.

### 10.1 Capability class (what each can do at all)

| Capability | Native REST | GAS /exec or bound |
|---|---|---|
| Bulk CRUD over a service's data | Yes, full-fidelity | Yes, via the App service, sometimes narrower field coverage |
| Low-latency single call | Yes, direct HTTPS | No, extra hop into a script execution |
| Custom spreadsheet functions (`=FUNC()`) | Impossible | Yes, `@customfunction` in a bound script |
| Custom menus / sidebars in a file | Impossible | Yes, `onOpen` + `Ui.createMenu`, `HtmlService` |
| Event triggers (onEdit/onFormSubmit/onChange/time) | Impossible | Yes, `ScriptApp.newTrigger(...)` |
| Runs after the conversation ends | No | Yes, script lives in the user's account |
| Cross-service orchestration in one atomic unit | Only via N client calls | Yes, one `.gs` touches many services |
| Reaches RESTRICTED services with no CASA | No (taking the scope IS the trigger) | Yes (authorization on the user's script) |

### 10.2 Speed / latency / execution model

- Native REST: single HTTPS round-trip per call or batch; no documented cold-start;
  no 6-minute cap; concurrency bounded only by per-minute quota.
- GAS /exec: a call spins up a script execution (two hops minimum: client -> /exec
  -> Google service).
- Apps Script max execution: 6 min per execution (Consumer and Workspace). Custom
  function: 30 sec. Source:
  https://developers.google.com/apps-script/guides/services/quotas
- Cold-start / latency: Google publishes no official figure for Web Apps. Treat
  /exec latency as higher and less predictable than direct REST; no number quoted.
- Web-App-specific concurrency: not published separately; the only documented cap is
  30 simultaneous executions per user, 1,000 per script (same source).

### 10.3 Throughput / quotas (sourced)

Apps Script (https://developers.google.com/apps-script/guides/services/quotas):
- URL Fetch calls/day: Consumer 20,000; Workspace 100,000.
- URL Fetch max response size: 50 MB/call. Max URL length: 2 KB/call.
- Triggers total runtime/day: Consumer 90 min/day; Workspace 6 hr/day.
- Simultaneous executions: 30/user, 1,000/script.
- Email recipients/day (MailApp/GmailApp): Consumer 100/day; Workspace 1,500/day
  general (2,000/day same-domain recipients).

Native REST defaults (the four services in this expansion plus context):
- Calendar API: 10,000 req/min per project; 600 req/min per user per project;
  ~1,000,000 req/day per project (billing threshold, not a hard cap).
  Source: https://developers.google.com/calendar/api/guides/quota
- Tasks API: 50,000 queries/day (courtesy limit); no per-minute figure published.
  Source: https://developers.google.com/tasks/limits
- Forms API: read 975/min per project, 390/min per user; `forms.responses.list`
  (expensive read) 450/min per project, 180/min per user; write 375/min per
  project, 150/min per user; no per-day cap.
  Source: https://developers.google.com/forms/api/limits
- People API: rate-limit numbers are NOT published on any current official docs
  page (limits URLs 404; only data-merge behavior documented). Treat as
  unconfirmed. Batch endpoints ARE confirmed (Section 10.6).
- Sheets API: read/write 300/min per project, 60/min per user; no per-day cap.
  Source: https://developers.google.com/sheets/api/limits
- Docs API: read 3,000/min per project (300/min/user); write 600/min per project
  (60/min/user); no per-day cap.
  Source: https://developers.google.com/docs/api/limits
- Apps Script API (`projects.create`/`updateContent`/`deployments`, used to BUILD
  scripts): no quota figures in published docs; per-project only in Cloud console.
  Unconfirmed.

Read of the numbers: for high-volume bulk work, native REST per-minute quotas far
exceed routing the same work through one user's script (capped by 30 concurrent
executions and the daily trigger-runtime budget). For low-volume, event-driven, or
must-run-unattended work, GAS wins because REST cannot do it at any quota.

### 10.4 Per-user setup and state

- Native REST: NONE. Once the scope is on the consent screen and granted, every
  tool call just works. This is why the four new REST services need no per-user
  provisioning once deployed.
- GAS path: per user, one-time: create script -> push code -> deploy Web App for an
  `/exec` URL (or deploy bound + run `installTrigger` once for installable
  triggers) -> user authorizes their own script's scopes -> store `/exec` URL +
  shared secret. Failure modes to design for: trigger never activated; `/exec` URL
  or secret leaks (anonymous Web App is world-callable, handler must verify the
  secret); user revokes or deletes the script (stored URL 401s/404s, re-provision);
  re-deploy mints a new `/exec` URL (store and reuse, not re-create); 6-min cap on
  large jobs (chunk/continue); daily trigger-runtime budget exhausted.

### 10.5 Consent / verification surface (the CASA-decisive axis)

- Native REST scope: appears on appscriptly's consent screen and is part of its
  verification. SENSITIVE -> brand/sensitive review, no CASA. RESTRICTED -> CASA,
  NO-GO. Adding any scope re-opens verification and invalidates the approved demo.
  This is exactly why this expansion is verify-LAST and why the deploy is gated.
- GAS path: appscriptly's consent screen only ever needs `script.projects` +
  `script.deployments`. The target service's scope is authorized by the user on the
  user's own generated script. So GAS can reach even RESTRICTED services with ZERO
  CASA exposure for appscriptly.

This asymmetry is the strategic backbone: native REST gives full-fidelity,
no-setup, low-latency CRUD for the SENSITIVE services (what this deploy ships);
GAS covers the impossible-in-REST capabilities and the restricted services without
CASA.

### 10.6 Batching (confirmed)

- Sheets `spreadsheets.batchUpdate`: yes, atomic.
- Docs `documents.batchUpdate`: yes.
- Slides `presentations.batchUpdate`: yes.
- Forms `forms.batchUpdate`: yes (the forms service uses it for add/update/delete
  item).
- Calendar HTTP batch (`/batch/calendar/v3`, up to 1,000 calls, counts as N
  requests): yes; the Calendar batch guide carries no deprecation notice, though the
  cross-API `googleapis.com/batch` endpoint is deprecated in favor of per-API batch.
- People `people.getBatchGet` (max 200), `contactGroups.batchGet` (max 200), plus
  `batchCreateContacts` / `batchUpdateContacts` / `batchDeleteContacts`: yes.
- Tasks: no batchUpdate-style endpoint; per-item calls.

### 10.7 Security, observability, maintenance

- Native REST: errors surface as `HttpError` -> existing `_format_http_error` ->
  `ToolError` envelope; one code path; observable in the MCP's own logs.
- GAS: build/deploy errors are observable REST `HttpError`s, but errors INSIDE a
  running script are visible only in the user's Apps Script execution log, not in
  appscriptly's logs. Structural observability gap for GAS runtime failures;
  maintenance also covers the generated `.gs` templates and shared-secret rotation.

---

## 11. PART 2 (retained) - Per-service capability map (the 8 services)

For each: native REST lever (scope + tools + tier), GAS lever (Apps-Script-only
superpowers), current state at ccbce87, and where each lever wins.

### 11.1 Sheets (REST shipped + deployed-scope; GAS shipped)
- REST: `spreadsheets` (SENSITIVE, in under-review set). 9 tools.
- GAS: custom functions, custom menus, onEdit/onChange, time-driven dashboard.
  Shipped: `as_install_custom_function`, `as_install_sheet_dashboard` + primitive.
- Wins: REST for bulk cell I/O and throughput; GAS for `=FUNC()`, menus, automation.

### 11.2 Docs (REST shipped + deployed-scope; GAS shipped)
- REST: `documents` (SENSITIVE, in under-review set). 16 tools.
- GAS: doc menus (`as_install_doc_menu`), sidebars, onOpen automation.
- Wins: REST for structured batch edits; GAS for in-document UI and automation.

### 11.3 Slides (REST shipped + deployed-scope; GAS shipped)
- REST: `presentations` (SENSITIVE, in under-review set). 6 tools.
- GAS: bound automation + slides-to-video render (`as_generate_video_deck` ->
  `as_encode_video`).
- Wins: REST for deck construction; GAS for bound automation and render handoff.

### 11.4 Forms (REST shipped; NEW scope to add; GAS partial)
- REST: `forms.body` + `forms.responses.readonly` (both SENSITIVE; the NEW scopes 9
  and 10). 7 tools: create/get/add-question/update-item/delete-item/list-responses/
  get-response. Uses `forms.batchUpdate`.
- GAS: the impossible-in-REST piece is `onFormSubmit` (react to each submission).
  Note: the generic `as_generate_bound_script` deliberately REJECTS Forms
  containers (no menus/sidebars for Forms), so a real-time submit handler would need
  a dedicated GAS tool (e.g. `as_install_form_handler`); that is a FUTURE GAS add,
  not part of this deploy, and needs no new appscriptly scope.
- Wins: REST for form creation/editing and bulk response reads (shipping now); GAS
  for reacting to submissions (future, no-CASA).

### 11.5 Calendar (REST shipped; NEW scope to add)
- REST: `calendar` (SENSITIVE; NEW scope 12). 7 tools: list/get/create/update/
  delete events, list calendars, free/busy.
- GAS: `CalendarApp` scheduled/reactive automation (e.g. Sheet row -> event via
  onEdit); future, no new appscriptly scope.
- Wins: REST for bulk event CRUD and free/busy at high quota (shipping now); GAS for
  unattended scheduled calendar automation (future).

### 11.6 Tasks (REST shipped; NEW scope to add)
- REST: `tasks` (SENSITIVE; NEW scope 11). 7 tools: list/create tasklists, list/
  create/update/complete/delete tasks.
- GAS: Tasks is an Apps Script advanced service; scheduled task hygiene and
  cross-service flows; future, no new appscriptly scope.
- Wins: REST for direct task CRUD (shipping now); GAS for scheduled task automation.

### 11.7 Contacts / People (REST shipped; NEW scope to add)
- REST: `contacts` (SENSITIVE; NEW scope 13). 6 tools: list/search/get/create/
  update/delete. Batch via People `getBatchGet` / `batchCreate/Update/Delete`.
- GAS: People advanced service; scheduled enrichment/dedup/sync; future, no new
  appscriptly scope.
- Wins: REST for bulk contact CRUD and batch reads (shipping now); GAS for scheduled
  contact-sync (future).

### 11.8 Drive (REST `drive.file` shipped + deployed-scope; full Drive is GAS-only)
- REST: `drive.file` (NOT restricted, in under-review set). 10 tools, all within
  app-created/opened files. Full `drive`/`drive.readonly` are RESTRICTED -> CASA ->
  NO-GO as native scopes; NOT part of this expansion.
- GAS: `DriveApp` under the user's authorization reaches any file without
  appscriptly holding a restricted scope; the no-CASA way to offer broad-Drive
  automation; future.
- Wins: REST (`drive.file`) for simple app-scoped ops (shipping); GAS for any-file
  reach without CASA (future). Native full Drive stays off-limits.

(Gmail is intentionally NOT a service in this codebase. Full Gmail read/modify is
RESTRICTED. If a Gmail capability is ever wanted, route it through GAS, or take only
the SENSITIVE `gmail.send` / `gmail.labels` natively. Out of scope for this deploy.)

---

## 12. Taxonomy (retained)

- Class A (native SENSITIVE scope, no CASA): Sheets, Docs, Slides (already
  deployed-scope), Forms, Calendar, Tasks, Contacts. THIS deploy lights the last
  four by adding their sensitive scopes.
- Class B (GAS-only because the native scope is RESTRICTED; no-CASA reach via the
  user's own script): full Gmail, full Drive. Not in this deploy.
- Class C (REST-only by nature): high-volume, low-latency bulk CRUD (per-minute
  quota, direct round-trip, batchUpdate atomicity). The four new REST services serve
  this.
- Class D (GAS-only by nature): custom functions, menus/sidebars, all trigger types,
  unattended-after-conversation execution, single-unit cross-service orchestration.

---

## 13. Guardrails honored

- no-CASA: all 5 scopes being added are SENSITIVE; zero are RESTRICTED. Verified
  live against Google's restricted list AND against the in-repo
  `tests/unit/test_base_tier_scopes.py::_RESTRICTED` guard. Restricted scopes
  (full Gmail, full Drive) remain hard NO-GO and out of this deploy; narrowest
  posture preserved (`drive.file` over full Drive; `drive.readonly` stays out).
- verify-LAST: the entire package is staged. Build is done; the deploy is the final,
  operator-gated step AFTER Google approves the expanded set. Section 0 + Section 7
  make the consent-ahead-of-review hazard explicit.
- read-only: this analysis modified no code, no GCP/OAuth/Fly/console config, and
  deployed nothing. The only file written is this planning artifact.
- em-dash-free: all drafted outward-facing text (scope justifications, privacy
  policy copy) uses commas, periods, and parentheses only.
