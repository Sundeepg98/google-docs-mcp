# appscriptly — OAuth Demo Video RE-RECORD Script (addresses T&S rejection)

**Why we're re-recording.** Google Trust & Safety (project **923279484962**) rejected the prior cut
(`youtu.be/hBuuDemD8Js`): *"The video you submitted does not sufficiently demonstrate the functionality
of your application."* Per Google's demo-video requirements (support.google.com/cloud/answer/13804565),
the video must show **the full OAuth consent screen + workflow** AND **each requested scope actually being
USED by a real app feature, with the result visible** — not just the scopes requested. The prior cut
under-demonstrated: it didn't visibly exercise every sensitive scope end-to-end.

**This script fixes that by exercising ONE real, visible operation per sensitive scope.** Record it in
ONE screen-capture take, narrate (voice or on-screen text) calling out each scope as it's used, upload
unlisted to YouTube, send T&S the new link.

---

## Hard requirements this script satisfies (checklist for the reviewer)
- [ ] **Consent screen in ENGLISH**, showing the **complete** scope list (toggle language to English if needed).
- [ ] The **complete OAuth consent workflow** (click through to "Allow").
- [ ] The **exact app under verification**: name **appscriptly**, the dedicated **`appscriptly-server`** OAuth client (project 923279484962). Branding must match.
- [ ] **Each requested scope visibly exercised** with the resulting change shown in the actual Google Workspace file.
- [ ] Narration (voice or text overlay) naming each scope at the moment it's used.

## UPDATE (2026-06-14): this is now the 13-SCOPE go-live demo

The operator decided to expand to ALL 13 code scopes NOW (the review reset today, so the cost is low).
This script is extended below from 8 scopes to 13: the original 8 (scenes 0-7) PLUS the 5 new SENSITIVE
scopes (scenes 8-11) for Calendar, Tasks, Forms (+ form responses), and Contacts. ALL 13 are SENSITIVE,
zero RESTRICTED, no CASA. The recorder at `D:/Sundeep/projects/_demo_rec/recorder.js` already has the
matching new scenes (scene7_calendar, scene8_tasks, scene9_forms, scene10_contacts).

## The EXACT scope set to demonstrate (13 scopes = original 8 + 5 new)
| # | Scope | Class | Must be SEEN doing |
|---|-------|-------|--------------------|
| 1 | `…/auth/userinfo.email` | non-sensitive | shown by consent + signed-in email |
| 2 | `openid` | non-sensitive | shown by consent (sign-in) |
| 3 | `…/auth/drive.file` | per-file | app creating/managing files it owns, visible in Drive |
| 4 | `…/auth/documents` | **sensitive** | create + edit a Google **Doc** |
| 5 | `…/auth/spreadsheets` | **sensitive** | create + write + format a **Sheet** |
| 6 | `…/auth/presentations` | **sensitive** | create + populate **Slides** |
| 7 | `…/auth/script.projects` | **sensitive** | create a bound **Apps Script** project |
| 8 | `…/auth/script.deployments` | **sensitive** | deploy that script (custom menu / `=FUNCTION()` appears) |
| 9 | `…/auth/calendar` | **sensitive (NEW)** | create + list a **Calendar** event, opened in Google Calendar |
| 10 | `…/auth/tasks` | **sensitive (NEW)** | create + list a **Task**, shown in tasks.google.com |
| 11 | `…/auth/forms.body` | **sensitive (NEW)** | create a **Form** + a question, opened |
| 12 | `…/auth/forms.responses.readonly` | **sensitive (NEW)** | read the form's **responses** (read-only) |
| 13 | `…/auth/contacts` | **sensitive (NEW)** | create + search a **Contact**, shown in contacts.google.com |

> The consent screen will now show ALL 13 (Docs, Sheets, Slides, Drive per-file, Apps Script projects +
> deployments, email + sign-in, Calendar, Tasks, Forms, Form responses, Contacts). Scroll through ALL of
> them in Scene 1. Do NOT show or mention `script.container.ui` / `script.external` / `script.scriptapp`
> — those are the bound script's OWN manifest scopes (a separate end-user consent), NOT in the
> `appscriptly-server` client. Gmail and full Drive are NOT requested.

---

## Pre-roll setup (before you hit record)
1. Sign into the browser as **`sundeepg8@gmail.com`** (the account that owns the GCP project + submission).
2. Have the appscriptly client reachable: the connector at `https://sundeepg98-docs-mcp.fly.dev`
   (`/health` returns `{"ok":true,"service":"appscriptly"}` — confirmed live). Use whatever entry point
   triggers the appscriptly OAuth consent for the `appscriptly-server` client (the claude.ai connector
   "Connect" flow, or the app's `/oauth/google/api/*` start URL).
3. Set the OS/browser display language to **English** so the consent screen renders in English.
4. Open a clean Google Drive tab (to show created files appearing).
5. Screen recorder ready (1080p, capture the whole browser window). Plan ~3–5 min.

---

## SHOT LIST (record in this order, one take)

### Scene 0 — Identify the app (~15s)
- On screen: the appscriptly home page `https://appscriptly.com/` (name + logo) so branding matches the submission.
- Narrate: *"This is appscriptly, a Google Workspace automation MCP server. I'll grant the OAuth scopes it
  requests, then demonstrate each one being used."*

### Scene 1 — OAuth consent workflow (~30s)  → covers `openid`, `userinfo.email`, and DISPLAYS all 8 scopes
- Trigger the appscriptly sign-in. Show the Google account chooser → pick `sundeepg8@gmail.com`.
- **Land on the consent screen. PAUSE here 3–4 seconds.** Make sure the frame clearly shows:
  - the app name **appscriptly**,
  - the language is **English**,
  - the **full scope list** (scroll slowly through all of them so every scope is on screen at least once:
    Docs, Sheets, Slides, Drive (per-file), Apps Script projects, Apps Script deployments, email, profile).
- Narrate each as you scroll: *"appscriptly is requesting: see/edit your Google Docs, Sheets, Slides;
  per-file Drive access; create and deploy Apps Script projects; and your email for sign-in."*
- Click **Allow**. Show the redirect back to the app ("connected"/success).

### Scene 2 — `…/auth/documents` (~40s)  → create + edit a Google Doc, change VISIBLE
- In the app, ask appscriptly to **create a Google Doc** titled e.g. "appscriptly demo — Doc" and insert content.
  (Tools: `gdocs_make_tabbed_doc`, then `gdocs_replace_all_text` or `gdocs_insert_table`.)
- **Open the created Doc in Drive/Docs on screen** and show the inserted text/table actually present.
- Narrate: *"Using the documents scope, appscriptly created this Doc and wrote its contents — here it is in my Drive."*

### Scene 3 — `…/auth/spreadsheets` (~40s)  → create + write + format a Sheet, VISIBLE
- Ask appscriptly to **create a Spreadsheet**, **write a table of values**, and **apply formatting**
  (bold header / a conditional-format highlight). (Tools: `gsheets_create_spreadsheet` → `gsheets_write_range`
  → `gsheets_format_range` or `gsheets_apply_conditional_format`.)
- **Open the Sheet on screen**; show the values AND the formatting applied.
- Narrate: *"Using the spreadsheets scope, appscriptly created this Sheet, wrote the data, and formatted it."*

### Scene 4 — `…/auth/presentations` (~30s)  → create + populate Slides, VISIBLE
- Ask appscriptly to **create a Presentation** and **add a slide with a title + body** (and/or a table/image).
  (Tools: `gslides_create_presentation` → `gslides_add_slide`.)
- **Open the deck on screen**; show the populated slide.
- Narrate: *"Using the presentations scope, appscriptly created this Slides deck and populated a slide."*

### Scene 5 — `…/auth/drive.file` (~20s)  → app managing its own Drive files, VISIBLE
- Show the **Drive folder/list** with the three files appscriptly just created (Doc, Sheet, Slides), OR have
  appscriptly **move one into a folder** (`gdocs_move_to_folder`) / **share it** (`gdocs_share_file`) and show
  the change.
- Narrate: *"appscriptly uses the per-file Drive scope — it only touches files it created, like these three,
  and can organize or share them."*

### Scene 6 — `…/auth/script.projects` + `…/auth/script.deployments` (~50s)  → THE differentiator
- Ask appscriptly to **install a persistent automation** into one of the files — e.g. a custom `=FUNCTION()`
  into the Sheet, or a custom menu / web-app, via `as_install_custom_function` / `as_generate_bound_script` /
  `as_deploy_web_app`. This **creates an Apps Script project (script.projects)** and **deploys it
  (script.deployments)**.
- **Show the result in the Workspace file**: the new custom function working in a cell (type `=YOURFUNC(...)`
  and show the result), OR the custom menu appearing in the file's menu bar, OR the deployed web-app `/exec`
  URL responding.
- Narrate: *"Using the Apps Script projects and deployments scopes, appscriptly generated and deployed a bound
  automation — here's the custom function it installed, running live in the Sheet. This persistent automation
  is appscriptly's core differentiator."*

### Scene 8 — `…/auth/calendar` (~40s)  → create + list a Calendar event, VISIBLE  [NEW]
- Ask appscriptly to **create a Google Calendar event** ("appscriptly demo - Calendar event", tomorrow
  10:00 to 10:30, with a description) and then **list tomorrow's events** to confirm it. (Tools:
  `gcal_create_event` then `gcal_list_events`.)
- **Open the event in Google Calendar on screen** (the recorder opens the event `htmlLink`); show the
  event sitting on the calendar grid.
- Narrate: *"Using the calendar scope, appscriptly created this event and listed my calendar to confirm
  it. Here it is in Google Calendar."*

### Scene 9 — `…/auth/tasks` (~30s)  → create + list a Task, VISIBLE  [NEW]
- Ask appscriptly to **create a Google Task** ("appscriptly demo - Task", with notes, due tomorrow) and
  **list the tasks** in the default list to confirm. (Tools: `gtasks_create_task` then `gtasks_list_tasks`.)
- **Open `tasks.google.com` on screen** (the recorder navigates there) and show the new task in the list.
  (Tasks has no per-item public URL, so the result is shown in the Tasks app surface.)
- Narrate: *"Using the tasks scope, appscriptly created this task and listed my tasks. Here it is in
  Google Tasks."*

### Scene 10 — `…/auth/forms.body` + `…/auth/forms.responses.readonly` (~70s)  → create a Form + READ a response, VISIBLE  [NEW]
- Ask appscriptly to **create a Google Form** ("appscriptly demo - Form") and **add a short-answer
  question** ("What is your favorite Workspace app?"). (Tools: `gforms_create_form` then
  `gforms_add_question`.) The recorder opens the form (responder URL) so the question is visible.
- **OPERATOR ACTION (required for the read-only scope to show data):** while the form is open on screen,
  **submit one test response** to it (type an answer, click Submit) so there is a response to read.
  The recorder dwells ~8s on the open form — submit during that dwell, OR pause the recording, submit,
  resume. (If you skip this, the response read in the next step shows zero responses, which still
  exercises the scope but is less convincing.)
- Then ask appscriptly to **read and summarize the form responses**. (Tools: `gforms_list_responses`,
  `gforms_get_response`.) Show the submitted answer appearing in the chat / summary.
- Narrate: *"Using the forms scope, appscriptly built this form and added a question. Then, using the
  read-only form-responses scope, it read back the response I just submitted. It can read responses but
  cannot alter or delete them."*

### Scene 11 — `…/auth/contacts` (~30s)  → create + search a Contact, VISIBLE  [NEW]
- Ask appscriptly to **create a Google Contact** ("Appscriptly Demo", with email, organization, phone)
  and **search contacts** for it to confirm. (Tools: `gcontacts_create` then `gcontacts_search`.)
- **Open `contacts.google.com` on screen** (the recorder navigates there) and show the new contact.
  (Contacts has no per-item public URL, so the result is shown in the Contacts app surface.)
- Narrate: *"Using the contacts scope, appscriptly created this contact and searched my contacts to
  confirm it. Here it is in Google Contacts."*

### Scene 12 — Close (~10s)
- Recap on screen: *"That's appscriptly — every requested scope used end-to-end: Docs, Sheets, Slides,
  Drive (per-file), Apps Script project creation + deployment, plus Calendar, Tasks, Forms with form
  responses, and Contacts."*

---

## After recording
1. Upload to YouTube as **Unlisted** (same visibility as the prior video), from `sundeepg8@gmail.com`.
2. Title e.g. *"appscriptly — OAuth scopes demo (Google verification, project 923279484962)"*.
3. Copy the **watch URL**.
4. Reply on the T&S verification thread (threadId `19e979748391b6c4`) with the new link. Full
   em-dash-free reply text is in `GO_LIVE_RUNBOOK.md` section 6 (covers the move from 8 to 13 scopes).

## What changed vs the rejected cut (the one-liner for the operator)
**Breadth + visible results:** the new script exercises EVERY sensitive scope with the resulting change shown
in the actual Workspace file (Doc edit, Sheet write+format, Slides populate, Drive file management, and an
Apps Script project create→deploy with the automation running) — instead of showing consent + only a partial
slice of functionality, which is what "does not sufficiently demonstrate" flagged.
