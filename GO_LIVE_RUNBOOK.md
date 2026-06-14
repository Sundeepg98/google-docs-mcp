# appscriptly 13-Scope GO-LIVE RUNBOOK

Coordinated go-live for the full 13-scope consent set (the original 8 plus Calendar,
Tasks, Forms + form responses, and Contacts). The operator decided to expand to ALL
13 now rather than wait for the 8-scope approval, because the verification review
reset today (2026-06-14), so the cost of widening the submission now is low.

- Repo state read for this runbook: appscriptly main at
  `334656bb53fffcd416eaf7f272e6c9d0c3a2a21c` (post-#201, GAS automation for
  Calendar/Tasks/Contacts merged; 100 tools).
- GCP APIs (Calendar, Tasks, Forms, People): the operator reports these are ALREADY
  ENABLED (done via gcloud today). Step 1 below is therefore a verify, not an action.
- This document is prep only. Nothing here is deployed, no consent screen is touched,
  no Google login is performed by the agent. The deploy, the consent-screen change,
  and the demo login are the coordinated go-live, gated on the operator.
- All outward-facing draft text here is em-dash-free by rule.

---

## 0. What is ALREADY prepped (ready to fire)

| Artifact | State | Location |
|---|---|---|
| Privacy policy, served HTML | UPDATED locally (held, not deployed) | `D:/Sundeep/projects/appscriptly-site/privacy.html` |
| Privacy policy, source-of-truth mirror | UPDATED, in the held PR | `PHASE1_VERIFICATION_KIT.md` section 3 (repo) |
| Per-scope justifications (5 new) | UPDATED, in the held PR | `PHASE1_VERIFICATION_KIT.md` section 2 (repo) |
| Held PR (no merge) | branch `chore/privacy-13-scopes-go-live` | appscriptly repo |
| Demo recorder (4 new scenes) | UPDATED + syntax-checked | `D:/Sundeep/projects/_demo_rec/recorder.js` |
| Demo shot-list (4 new scenes, 13-scope framing) | UPDATED | `DEMO_VIDEO_RERECORD_SCRIPT.md` (repo) |
| Consent-screen steps + justifications | this doc, sections 3 + 4 | here |
| T&S reply (8 -> 13 scopes) | drafted, this doc section 6 | here |

The code itself needs NO change: all 13 scopes are already in
`auth.WORKSPACE_SCOPES` on main, and the Calendar/Tasks/Forms/Contacts REST tools
(plus their GAS automation tools from #201) are merged.

---

## 1. The 13-scope set (what the consent screen will request)

The original 8 (already under review before today's reset):

```
openid
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/presentations
https://www.googleapis.com/auth/script.projects
https://www.googleapis.com/auth/script.deployments
```

The 5 NEW scopes to add (all SENSITIVE, none RESTRICTED, no CASA):

```
https://www.googleapis.com/auth/forms.body
https://www.googleapis.com/auth/forms.responses.readonly
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/contacts
```

CASA confirmation: verified against Google's authoritative restricted-scope list
(support.google.com/cloud/answer/13464325) and the in-repo guard
`tests/unit/test_base_tier_scopes.py` (`_TARGET_CONNECTOR` lists exactly these 13;
the test asserts none are in `_RESTRICTED`). Gmail full read/modify and full Drive
are the CASA scopes and are NOT requested.

---

## 2. The coordinated GO-LIVE order of operations

Run in this exact order. The earlier worry about consent-ahead-of-review does not
apply now because the review reset today, so 8-and-13 are no longer in conflict; the
constraint that DOES remain is to keep the demo, the privacy policy, the consent
screen, and the deploy MUTUALLY CONSISTENT at the moment of submission.

1. VERIFY the 4 GCP APIs are enabled (operator already enabled them). Section 7.
2. PUBLISH the privacy update. Two surfaces, both must go live before the consent
   screen is submitted (the consent screen links to the privacy URL and a reviewer
   will open it):
   a. Deploy the updated `appscriptly-site/privacy.html` to Cloudflare Pages
      (project `appscriptly-site`, domain `appscriptly.com`). Section 5.
   b. Merge the held PR `chore/privacy-13-scopes-go-live` so the repo
      source-of-truth (`PHASE1_VERIFICATION_KIT.md`) matches what is served.
3. RECORD the 13-scope demo in ONE login session (the recorder drives it). Upload
   unlisted to YouTube. Section 8 + the shot-list. This is an operator step
   (password and 2FA, and the one manual form-response submission in Scene 10).
4. ADD the 5 new scopes to the live OAuth consent screen and SUBMIT for
   verification with the new demo link + the (already live) privacy URL. Section 3.
5. DEPLOY the app so the live server requests all 13 scopes (the code already does;
   this is the normal Fly deploy / DEPLOY_ENABLED flip). Section 9.
6. UPDATE the existing T&S verification thread (`19e979748391b6c4`) with the new
   13-scope demo link so the open submission reflects the wider set. Section 6.

Ordering notes:
- Privacy (step 2) before consent submission (step 4): a reviewer opens the privacy
  URL during review; it must already disclose Calendar/Tasks/Forms/Contacts.
- Demo (step 3) before or with consent submission (step 4): the submission references
  the demo video; record it first so the link exists.
- Deploy (step 5) can run right after the consent screen is updated. Because the
  review reset, there is no "8-scope review to protect" anymore; the live server
  requesting 13 while verification is pending is the normal pending-verification
  state (existing users see the unverified-app interstitial until approval, same as
  the 8-scope flow was).

---

## 3. Consent-screen change + resubmit steps (GCP console / OAuth)

Operator action. Project: appscriptly OAuth (the `appscriptly-server` client,
GCP project 923279484962). Account: the project owner (sundeepg8@gmail.com).

Add the 5 scopes:
1. Go to Google Cloud Console, select project 923279484962.
2. Navigate to APIs and Services, then OAuth consent screen (or the newer Google
   Auth Platform, then Branding / Data Access, depending on console version).
3. Open the Data Access (Scopes) section, then Add or Remove Scopes.
4. The 4 new APIs being enabled (Calendar, Tasks, Forms, People) make their scopes
   selectable in the picker. Add these 5:
   - `https://www.googleapis.com/auth/calendar`
   - `https://www.googleapis.com/auth/tasks`
   - `https://www.googleapis.com/auth/forms.body`
   - `https://www.googleapis.com/auth/forms.responses.readonly`
   - `https://www.googleapis.com/auth/contacts`
   If a scope is not in the picker, paste it into the "manually add scopes" box and
   click Add to Table, then Update.
5. Confirm the consent screen scope table now shows all 13 (the original 8 plus
   these 5). Save.
6. Confirm the App home page is `https://appscriptly.com/` and the Privacy policy
   URL is `https://appscriptly.com/privacy` (both must be live and reachable; the
   privacy URL must already be the updated 13-scope version from step 2).

Resubmit for verification:
7. In the OAuth consent screen / verification center, there should be a "Submit for
   verification" (or "Prepare for verification" / "Resubmit") control now that the
   scope set changed. Click it.
8. In the justification fields, paste the per-scope justification text from
   section 4 below (one per added sensitive scope). The original 8 are already
   justified in the existing submission; you are adding justifications for the 5 new
   ones.
9. Provide the new demo video URL (from step 3 / section 8) as the demonstration.
10. Submit. This is a sensitive-scope verification (no CASA), so expect a
    brand / sensitive review on the order of days, not a third-party security audit.

---

## 4. Per-scope justification text (em-dash-free, paste into the console)

These are the 5 NEW sensitive scopes. (The original 8 keep their existing
justifications; full set is in `PHASE1_VERIFICATION_KIT.md` section 2.)

Calendar (`https://www.googleapis.com/auth/calendar`):
"appscriptly uses Calendar access to let users manage their schedule through natural
language: listing upcoming events, creating and editing events, checking free and
busy times, and removing events. The full calendar scope is requested because the
app both reads the calendar list and writes events on the user's behalf. The app
acts only on the calendars the user asks about and does not change calendar sharing."

Tasks (`https://www.googleapis.com/auth/tasks`):
"appscriptly uses Tasks access to let users capture and manage to do items by voice
or chat: creating tasks, updating and completing them, and organizing task lists.
The read and write scope is requested because the app creates, updates, completes,
and deletes the user's own tasks. Access is limited to the user's task data so the
app can keep their list in sync with the actions they request."

Forms (`https://www.googleapis.com/auth/forms.body`):
"appscriptly uses Forms access to let users build and manage forms: creating a form
and adding, editing, and removing questions. The forms.body scope is requested
because the app creates and edits form structure on the user's behalf. The app does
not access forms the user did not ask it to work with."

Form responses (`https://www.googleapis.com/auth/forms.responses.readonly`):
"appscriptly uses read-only access to form responses so users can review and
summarize the submissions to their own forms. This scope is read only by design: the
app reads and reports responses but cannot alter or delete them. It is paired with
forms.body so a user can both build a form and review its results in one place."

Contacts (`https://www.googleapis.com/auth/contacts`):
"appscriptly uses Contacts access to help users find and organize the people they
work with: searching and viewing contacts and, where the user asks, creating,
updating, or removing contact entries. The full contacts scope is requested because
the app both reads and modifies the user's own contacts. Access is limited to the
user's address book so the app acts only on their behalf."

---

## 5. Privacy policy publish (two surfaces, both required)

The live privacy policy at `https://appscriptly.com/privacy` is NOT served by the
appscriptly Python app. It is a separate static site:

- Live host: Cloudflare Pages, project `appscriptly-site`, domain `appscriptly.com`.
- Local source: `D:/Sundeep/projects/appscriptly-site/privacy.html` (a standalone,
  UNCOMMITTED folder, not a git repo, with no deploy automation).
- Repo source-of-truth mirror (tracked): `PHASE1_VERIFICATION_KIT.md` section 3.

Both have been UPDATED in this prep to disclose the 4 new data categories (Calendar
events, Tasks, Forms + form responses, Contacts), with what / how-used / stored /
shared / retained per service, and the now-false "does NOT access contacts or
calendar" line was removed. Last-updated date set to 14 June 2026.

DRIFT WARNING (verified): the deployed site has drifted from the local folder. The
live `https://appscriptly.com/terms` returns 200 but there is NO `terms.html` on
disk, which proves the live surface has been edited directly and not saved back
locally. Therefore, BEFORE publishing:
1. Open the CURRENT live `https://appscriptly.com/privacy` and diff it against the
   updated local `appscriptly-site/privacy.html`. Make sure you are not overwriting
   newer live content (e.g. a support email or wording changed on the live site).
2. Reconcile any live-only changes into the local file first.

Publish (operator, needs a Cloudflare login or API token, so it is a go-live step,
not agent-doable):
- Option A (CLI): from `D:/Sundeep/projects/appscriptly-site/`, run
  `npx wrangler pages deploy . --project-name=appscriptly-site` (requires a CF API
  token in the environment).
- Option B (dashboard): Cloudflare dashboard, Workers and Pages, the
  `appscriptly-site` project, then upload / drag-drop the folder (or connect to a
  fresh upload). 
- After publish, hard-refresh `https://appscriptly.com/privacy` and confirm it shows
  the 14 June 2026 version with the Calendar / Tasks / Forms / Contacts section.

Then merge the held PR (section 10) so the repo mirror matches what is served.

Recommended one-time fix while you are here: SAVE the live `/terms` content back into
`appscriptly-site/terms.html` so the drift is closed and the folder is a true mirror.
(Not required for this go-live; it just prevents the next person hitting the same
"live has content the folder lacks" surprise.)

---

## 6. T&S thread reply (8 -> 13 scopes), em-dash-free

We replied to the T&S verification thread (threadId `19e979748391b6c4`) TODAY with
the 8-scope demo. Moving to 13 means a fresh demo and updating that submission. After
the new 13-scope demo is uploaded (section 8), reply on the SAME thread with this
(swap in the real new YouTube link):

"Update on this verification. We have expanded the requested scope set from 8 to 13
to cover four additional Google Workspace services the app now supports: Calendar,
Tasks, Forms (with read-only access to form responses), and Contacts. All five added
scopes are sensitive, not restricted, so no security assessment is required. The new
scopes are: calendar, tasks, forms.body, forms.responses.readonly, and contacts.

We have recorded a new demo video that shows the full English consent screen with all
13 scopes and then exercises each one end-to-end with the result visible: Docs,
Sheets, and Slides created and edited; per-file Drive management; an Apps Script
project created and deployed with the automation running in the Sheet; a Calendar
event created and shown in Google Calendar; a Task created and shown in Google Tasks;
a Form created with a question and its submitted response read back (read-only); and
a Contact created and shown in Google Contacts.

New demo video (unlisted): <PASTE NEW YOUTUBE LINK>

Our privacy policy at https://appscriptly.com/privacy has been updated to disclose
the Calendar, Tasks, Forms, form responses, and Contacts data the app accesses, how
it is used, and that it is not stored or shared. Please let us know if anything
further is needed."

Notes:
- Keep it on the SAME thread so the reviewer sees the supersession, not a new case.
- The prior 8-scope demo link (in the earlier reply) is now superseded; the new link
  is the one to review. You can say so explicitly if the thread format invites it.
- No em dashes or en dashes in the sent text (operator standing rule).

---

## 7. GCP APIs to enable (VERIFY, operator already enabled)

These must be enabled in project 923279484962 for the new REST tools to work live.
Operator reports all 4 are already enabled via gcloud today. Verify each shows
"API enabled" in APIs and Services, then Enabled APIs and services:

| Service | GCP API |
|---|---|
| Calendar | Google Calendar API |
| Tasks | Google Tasks API |
| Forms | Google Forms API |
| Contacts | People API |

(Docs, Sheets, Slides, Drive, and Apps Script APIs were already enabled for the
original 8-scope build.) Quick CLI verify, if wanted:
`gcloud services list --enabled --project=923279484962` and confirm
`calendar-json.googleapis.com`, `tasks.googleapis.com`, `forms.googleapis.com`, and
`people.googleapis.com` appear.

---

## 8. Demo recording (operator runs it; what is automated vs manual)

The recorder at `D:/Sundeep/projects/_demo_rec/recorder.js` has been extended with
4 new scenes for the 5 new scopes (it was syntax-checked):

- `scene7_calendar`: creates an event, lists tomorrow, opens the event htmlLink so
  it renders in Google Calendar.
- `scene8_tasks`: creates a task, lists tasks, opens tasks.google.com to show it
  (Tasks has no per-item public URL, so the result is shown in the app surface).
- `scene9_forms`: creates a form plus a question, opens the form, then reads
  responses (forms.body + forms.responses.readonly in one scene).
- `scene10_contacts`: creates a contact, searches, opens contacts.google.com to
  show it (Contacts has no per-item public URL).

How to run:
1. `cd D:/Sundeep/projects/_demo_rec` then `node recorder.js`. It uses the logged-in
   `state.json` (claude.ai = karthick, Google = sundeepg8@). It records ONE webm of
   the claude.ai page with the rendered Google artifacts opened in the same tab.
2. The recorder PAUSES and waits (up to 25 min) whenever Google shows a password or
   2FA challenge. The operator types the sundeepg8@ password and any 2FA into the
   visible window; the recorder resumes automatically.

What ONLY the operator can do (the irreducible manual steps):
- Enter the sundeepg8@ Google password and 2FA when the recorder pauses (Scene 1
  consent, and any mid-run re-auth).
- Scene 10 (forms): SUBMIT one test response to the created form while it is open on
  screen, so the read-only response read has data to show. The recorder dwells ~8s
  on the open form; submit during that dwell, or pause and resume the recording.
  Without this, the response read shows zero responses (the scope is still exercised,
  just less convincingly).
- After the run, review the produced webm in `runs/<timestamp>/appscriptly_demo.webm`
  and confirm all 13 scopes are visibly exercised (the original 8 plus the 4 new
  scenes). Then upload it unlisted to YouTube from sundeepg8@.

Recording quality reminders (from the existing script):
- Consent screen in ENGLISH; scroll through ALL 13 scopes in Scene 1.
- Title and any on-screen text must be em-dash-free.
- Suggested title: "appscriptly OAuth scopes demo (Google verification, 13 scopes,
  project 923279484962)".

If a scene's REST tool needs a one-time per-user Google consent for the new service
(Calendar/Tasks/Forms/Contacts), the recorder's `handlePerUserAuthIfPresent` drives
that in-chat consent automatically (operator still clears any password/2FA).

---

## 9. Deploy (operator; the final flip)

The app code already requests 13 scopes on main, so deploy is the normal Fly deploy
of current main to `sundeepg98-docs-mcp` (and the DEPLOY_ENABLED flip if the operator
gates on it). Sequence with the rest:
- Do this AFTER the consent screen is updated (section 3) and the privacy is live
  (section 5).
- After deploy, confirm `https://sundeepg98-docs-mcp.fly.dev/health` returns
  `{"ok":true,"service":"appscriptly"}` and that a fresh connector consent shows all
  13 scopes.
- Pending verification, users see Google's unverified-app interstitial for the
  sensitive scopes until the verification (section 3) is approved. This is expected
  and is the same state the 8-scope flow was in.

---

## 10. The held PR (do NOT merge until go-live)

Branch: `chore/privacy-13-scopes-go-live` (appscriptly repo). Contents (tracked
files only):
- `PHASE1_VERIFICATION_KIT.md`: section 3 privacy mirror updated to 13 scopes; 
  section 2 gains the 5 new per-scope justifications.
- `GO_LIVE_RUNBOOK.md`: this file.
- `DEMO_VIDEO_RERECORD_SCRIPT.md`: extended to the 13-scope shot-list (scenes 8-11).

NOT in the PR (intentionally, because they live outside the repo):
- `D:/Sundeep/projects/appscriptly-site/privacy.html`: the served page; updated in
  place, published via Cloudflare Pages at go-live (section 5).
- `D:/Sundeep/projects/_demo_rec/recorder.js`: the recorder; updated in place,
  outside the repo.

Open the PR but DO NOT merge. It ships at go-live, together with the privacy publish
and the deploy, so the repo source-of-truth flips to "13 scopes" at the same moment
the live privacy page and the live server do. Merging early is harmless to users (it
is docs only) but keeping it held keeps the whole change set landing as one
coordinated go-live.

---

## 11. EXACTLY what the operator must do (the human-only steps)

Everything else is prepped. The operator must:

1. VERIFY the 4 GCP APIs are enabled (section 7): likely already done.
2. DIFF live `appscriptly.com/privacy` vs the updated local `privacy.html`, reconcile
   any live-only edits, then PUBLISH the updated privacy to Cloudflare Pages
   (section 5). Needs a Cloudflare login or API token.
3. RUN `node recorder.js` and, during the run, ENTER the sundeepg8@ password / 2FA
   when it pauses, and SUBMIT one test form response in Scene 10 (section 8). Then
   REVIEW the webm and UPLOAD it unlisted to YouTube.
4. ADD the 5 scopes to the OAuth consent screen and SUBMIT for verification with the
   new demo link, pasting the section 4 justifications (section 3). Needs Google
   console login.
5. DEPLOY current main and confirm /health (section 9).
6. REPLY on the T&S thread `19e979748391b6c4` with the new demo link using the
   section 6 text (swap in the real YouTube URL).
7. MERGE the held PR `chore/privacy-13-scopes-go-live` (section 10).

Agent-side prep that is DONE: privacy.html + the repo mirror + the 5 justifications +
the recorder scenes + the shot-list + this runbook + the T&S reply draft + the
consent-screen steps. No code change was needed (the 13 scopes and all tools are
already on main).
