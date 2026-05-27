# User Guide — appscriptly

**Audience:** you got a connector URL (or installed Claude Desktop with this server wired up) and want to actually *do* something with it. No engineering background assumed.

> **Project was renamed (2026-05-27).** What used to be `google-docs-mcp` is now `appscriptly` — same software, broader scope (Apps Script-backed Workspace Automation is now the headline). If you set up Claude Desktop before the rename with the `google-docs` server label or the `google-docs-mcp` CLI binary, **nothing changes for you** — both names continue to work as backward-compat aliases through v3.0.

This is the human-facing guide. The LLM also has its own orientation (`gdocs_guide()` and `gdocs://error-recovery`) — but you don't need either; you just talk to Claude in plain English and Claude calls the right tool.

## Table of contents

1. [What this lets you do](#what-this-lets-you-do)
2. [Workflow A — Create a new tabbed doc from chat](#workflow-a--create-a-new-tabbed-doc-from-chat)
3. [Workflow B — Convert an existing `.docx` into a tabbed doc](#workflow-b--convert-an-existing-docx-into-a-tabbed-doc)
4. [Workflow C — Retrofit an already-uploaded Google Doc into tabs](#workflow-c--retrofit-an-already-uploaded-google-doc-into-tabs)
5. [Common errors — what they mean and what to do](#common-errors--what-they-mean-and-what-to-do)
6. [Switching Google accounts — the multi-account gotcha](#switching-google-accounts--the-multi-account-gotcha)

---

## What this lets you do

Google Docs has a feature called **Tabs** (launched October 2024): a sidebar where each "tab" is a separately-navigable section of the same document. It's like having chapters that the reader can click between, instead of one long scrolling page.

Google's own apps (Word import, the `.docx` exporter) DON'T support Tabs — anything you round-trip through `.docx` collapses to a single tab. The only way to create or preserve Tabs programmatically is via the Google Docs API.

This server lets you do that through Claude. Three things you can do today:

1. **Create a new tabbed doc** from content you describe in chat ("make me a doc with tabs for Intro, Findings, and Next Steps...")
2. **Convert an existing `.docx`** into a tabbed Google Doc, preserving tables, drawings, equations, and formatting that text-only round-trips would destroy
3. **Retrofit a Google Doc that's already on your Drive** — add Tabs around its existing sections without rebuilding the file from scratch

[Screenshot: a Google Doc open in browser, showing the Tabs sidebar on the left with 4 named tabs, body content visible on the right]

The rest of this guide walks you through each workflow with the actual phrasing to use with Claude.

---

## Workflow A — Create a new tabbed doc from chat

**Best when:** the content lives in your conversation with Claude (you pasted notes, brainstormed an outline, asked Claude to draft sections, etc.). You want the result to land as a Google Doc with one tab per section.

### What you say

Just describe what you want. Claude figures out the tool call.

> "Make me a Google Doc titled 'Q4 Planning' with tabs for Introduction, Findings, Recommendations, and Next Steps. Use the bullet points we just discussed for each section."

Or even shorter:

> "Turn that outline into a tabbed Google Doc."

Or:

> "Create a Google Doc with four tabs, one for each region we covered."

### What Claude does

Claude calls `gdocs_make_tabbed_doc` — a single tool call with the title and the list of tabs (each tab is `{title, content}`). One round-trip, no file upload, no preview step.

### What you'll see

Claude replies with a link to the new doc (something like `https://docs.google.com/document/d/.../edit?tab=t.0`). Open it: the Tabs sidebar shows up on the left, one entry per tab you asked for.

[Screenshot: Claude's response showing "Created your tabbed doc: [Q4 Planning](https://docs.google.com/...)" with the URL highlighted]

### Things to know

- **First time only:** Claude will hand you a Google authorization link before doing anything. Click it, sign in, allow access. After that, your authorization is remembered for ~1 hour at a time and auto-refreshes silently.
- **Title rules:** Drive doesn't allow titles with control characters or longer than ~1024 characters. If your title gets rejected, simplify it.
- **Empty tabs:** if you ask for a tab with no content, Claude will create it with a placeholder. The default is to leave the placeholder visible; ask "remove empty tabs" if you don't want them.
- **Editing afterwards:** once the doc exists, you can keep talking to Claude about it. "Add a fifth tab called Risks," "rename the Findings tab to Insights," "add this paragraph to the Recommendations tab" — these all work, Claude just calls the appropriate edit tool (`gdocs_add_tabs`, `gdocs_rename_tab`, `gdocs_append_to_tab`, etc.).

### Limitations

- Tabs created this way only support **text + lists + headings + tables you describe in chat**. Drawings, equations, embedded images — for those, write the doc in Google Docs first and use Workflow C (retrofit) instead, OR upload a `.docx` and use Workflow B.

---

## Workflow B — Convert an existing `.docx` into a tabbed doc

**Best when:** you have a `.docx` file — exported from Word, downloaded from somewhere, or built by some other tool — and you want it to become a Google Doc with native Tabs (rather than the single-tab blob you'd get from Google's normal `.docx` import).

This is the differentiator: Google's own `.docx` import collapses everything into one tab. This server's conversion preserves the structure as Tabs.

### What you say

How you provide the file depends on where you're running Claude.

**From Claude Desktop (local install):** the file lives on your computer.

> "Convert `~/Documents/team-handbook.docx` to a tabbed Google Doc. Split it by Heading 1."

**From claude.ai (web, custom connector):** Claude's sandbox sees its own bytes, not your computer. You either upload the file in chat, or — if Claude wrote the `.docx` for you in this same conversation — Claude has it in its sandbox already.

> "Upload that handbook .docx I gave you to my Google Drive as a tabbed doc, split by Heading 1."

**Already on Drive:** if the `.docx` (or already-converted Google Doc) is already on your Drive:

> "Convert the Google Doc 'Team Handbook' into tabbed format. Use Heading 1 as the section break."

### What Claude does

Two-step choreography:

1. **`gdocs_preview_tab_split`** — Claude asks the server what sections it would create *before* changing anything. You can see the proposed tab titles and confirm before commit. Destructive conversion is one-way; the preview is your safety net.
2. **`gdocs_tab_existing_doc`** — once you confirm, this does the actual conversion. The original file is replaced in-place (Google's `.docx`-to-Doc converter creates a new Doc; this tool then restructures it).
3. **`gdocs_get_doc_outline`** — Claude verifies the result and gives you the link.

### What you'll see

Something like:

> *"I'll preview the split first. Your document has 6 Heading 1 sections: Mission, Roles, Onboarding, Day-to-Day Tooling, Performance Reviews, Departures. Should I proceed?"*

You say yes. Then:

> *"Done — your tabbed handbook is at [https://docs.google.com/document/d/.../edit?tab=t.0]. Tables and embedded screenshots were preserved."*

[Screenshot: a Claude.ai conversation showing the preview list, user confirmation, and final link with a Drive-style file card embedded]

### Things to know

- **Preview first.** Always. The conversion is one-way; if the split is wrong, you'd be unwinding manually.
- **`split_by` options:** `"heading_1"` is the default. If your doc uses Heading 2 as the section break, ask for `"heading_2"`. If sections are separated by page breaks rather than headings, ask for `"page_break"`. If you're not sure, ask for `"auto"` — Claude/the server tries to guess.
- **Tables, drawings, equations are preserved.** This is the whole point of the workflow; if these weren't preserved you'd just use Google's normal `.docx` import.
- **Sandbox upload path:** under the hood, claude.ai uploads via a short-lived signed URL (`gdocs_get_signed_upload_url`). You don't need to know this — Claude handles it. But if Claude says "the upload URL expired," it just means the conversation took longer than the URL's lifetime. Re-ask and Claude will mint a fresh one.

### Limitations

- The server can only convert `.docx` (or `.gdoc`) files it can read. Password-protected files fail. Files larger than ~100MB are likely to hit either the upload size limit or Google's own conversion timeout.
- The **Workspace automation runtime** that retrofit (and future workflow tools) need is a **per-user, one-time install**. The first time you ask Claude to use Workflow B or C, it will tell you to run `gdocs_install_automation` first (you'll see a message asking Claude to call that tool — say "yes, install it" and Claude will run it, then re-do your original ask). Install takes 10-30 seconds. After install, the runtime lives in your Google account and runs on Google's infrastructure; every Workflow B or C call after that runs without further setup, and Claude can also build persistent automations (scheduled jobs, custom menus, reactive workflows) on top of it. The older name `gdocs_setup_apps_script` is still accepted but deprecated and will be removed in v3.0.

---

## Workflow C — Retrofit an already-uploaded Google Doc into tabs

**Best when:** you have a styled Google Doc on your Drive (or imported from `.docx` already) and you want to add Tabs around its existing sections **without losing any formatting**.

This is the "I already have the doc, I just want it tabbified" workflow. It's also the answer when Workflow B's `split_by` heading detection doesn't work because your section breaks aren't H1s — they're styled banners, shaded paragraphs, table rows, etc.

### What you say

If your doc already has clear Heading 1 / Heading 2 paragraphs:

> "Add Tabs to my Google Doc 'Project X Report'. Use Heading 1 as the section break."

If your section breaks are NOT headings — they're styled blocks like banners or shaded callouts — you give Claude the exact text of those markers:

> "Add Tabs to my doc 'Annual Review'. The sections start at 'Q1 RESULTS', 'Q2 RESULTS', 'Q3 RESULTS', 'Q4 RESULTS'. Use those as the tab names too."

### What Claude does

Calls `gdocs_tab_existing_doc` directly (no preview step needed for retrofit — the marker list IS the preview). Internally:

- If you specified `split_by="heading_1"` (or 2, or page_break): the server splits at those structural break points.
- If you specified explicit markers: the server uses **retrofit mode** — it inserts synthetic Heading 1 paragraphs just before each marker block, then converts. Your original formatting (tables, drawings, fonts, colors) is preserved exactly.

### What you'll see

> *"Done — your Annual Review is now at [https://docs.google.com/document/d/.../edit?tab=t.0] with 4 tabs: Q1 Results, Q2 Results, Q3 Results, Q4 Results. The original tables and charts are unchanged."*

[Screenshot: a Google Doc with the Tabs sidebar visible, showing 4 tabs; the body content shows a complex table with charts that remain intact]

### Things to know

- **Use retrofit, not rebuild.** If your doc is styled (has tables, drawings, equations, specific fonts), DO NOT ask Claude to "make a new doc with the same content." That round-trips through text and loses formatting. Always retrofit instead.
- **Markers must be exact.** The marker text you give Claude must match the document literally — same casing, same punctuation. If your banners say "Q1 Results" with a capital R, don't ask Claude to use "Q1 RESULTS" — it won't find them.
- **Placeholder behavior:** retrofit creates a leading "placeholder" tab for any content that appears *before* the first marker. By default Claude removes it; if your doc has a meaningful cover page or table-of-contents you want to keep, ask "leave the cover page as a tab" — Claude will pass `placeholder_behavior="rename"`.
- **One-way operation.** Once retrofitted, you cannot easily go back to the un-tabbed version. Copy the doc first if you want a backup ("make a copy of this doc first" works).

### Limitations

- Files this server can't write to (e.g. someone else's Google Doc you have view-only access to) will fail with a "not authorized" message. Make a copy first or ask the owner to share write access.
- The server can only modify files **it created** — including files YOU uploaded through it. Pre-existing Drive files that were never touched by this server may be read-only for write/trash operations (see the [error guide](#common-errors--what-they-mean-and-what-to-do)).

---

## Common errors — what they mean and what to do

Claude usually handles errors gracefully and tells you what to do. But here are the most common ones explained for you directly, in case Claude's translation isn't clear.

| What you see / what Claude says | What it means | What to do |
|---|---|---|
| **"I could not detect any natural section breaks"** (`no_splits` warning) | Your `.docx` has no Heading 1 paragraphs, so the auto-split failed. | Either add Heading 1s in Word/Docs first, OR tell Claude the exact text of your section markers (see Workflow C). |
| **"That file was not created through this connection — Google's permissions stop me from editing it"** (`"owned_by_app": false` / `"reason": "app_not_authorized"`) | The Drive file exists, but it wasn't uploaded or created through this server. Google's "app scope" policy means each app can only modify files it created. | If you only want to **read** it, ask Claude to "just read the doc, don't edit it." If you want to **edit** it, ask Claude to "make me a copy of that doc through you" — the copy will be owned-by-app and editable. |
| **"Your Google authorization needs a refresh — please open the 'Click here to authorize' link"** (`Click here to authorize`) | Your OAuth token expired, was revoked, or this is your first use. | Click the link Claude gave you. Sign in to Google. Allow access. Then tell Claude to retry. |
| **"Google rate-limited me. I will wait about a minute and retry"** (`Google API error: 429`) | Google's per-user quota hit. Usually means you ran many operations in a short burst. | Wait. Claude will retry automatically. If it happens repeatedly, batch your requests ("create all five docs in one call" instead of asking for each individually). |
| **"I could not find that document"** (`"reason": "not_found"`) | The doc was trashed, you lost access to it, or the link/ID is wrong. | If you remember the title, tell Claude — "find the doc called X" calls the search tool. If the doc is trashed, restore it from Drive's trash first. |
| **"One of the tabs had no content — should I leave it, rename it to 'Overview', or remove it?"** (`placeholder_behavior` prompt) | After retrofit, the content before your first marker became its own (possibly empty) tab. | Choose: **delete** removes it (cleanest), **rename** keeps it (renamed to whatever you say, e.g. "Overview" or "Cover"), **keep** leaves it as-is. |
| **"Something unexpected went wrong on the server side. This looks like a bug"** (`KeyError`, `AttributeError`, etc.) | Real server-side bug, not your fault. Claude will not auto-retry because the same call will fail the same way. | Ask Claude to share `gdocs_server_info()` output. File an issue on the GitHub repo with that info and the error text. As a workaround, ask Claude to try a simpler approach (e.g. "just create one tab instead of all four"). |

### When Claude does the right thing automatically

Claude has access to the full error-recovery table at `gdocs://error-recovery` and the `gdocs_help` tool. For most errors, Claude will:

1. Read the error response
2. Look up the recovery guidance
3. Either retry automatically (rate-limit, recoverable auth) or tell you in plain English what to do

If Claude does something that seems wrong, you can always ask: "what error did you get? what does it say to do?" — Claude will surface the structured guidance from the recovery table.

---

## Switching Google accounts — the multi-account gotcha

**The trap:** if you have multiple Google accounts (work + personal, multiple work accounts, etc.) and you switch which one is "active" in your browser mid-flow, the document can land in the wrong Drive.

### Why it happens

When you click an authorization link Claude gives you, Google's OAuth flow uses **whichever account is currently the "default" in your browser** for the consent screen. If you have:

- `you@work.com` logged in at position 1 (the default)
- `you@personal.com` logged in at position 2

…and you sign in at position 2 partway through, the authorization gets bound to `you@personal.com` instead of `you@work.com`, even though you started in your work session.

The doc gets created in `you@personal.com`'s Drive. Now it's "missing" from your work Drive.

### How to prevent it

**Best practice:** before clicking the authorization link, **explicitly select the account** you want to use. Either:

1. Open the link in an **incognito / private window** and sign in fresh with the target account. Claude/the server doesn't care which window the URL opens in; only the account that completes the consent matters.
2. OR: in your normal browser, click your Google profile icon (top right of any Google page), pick the account you want, then click the authorization link.

**Once authorized, your account choice sticks.** Claude reuses the same OAuth grant until you explicitly revoke it. So this is a one-time setup gotcha, not a per-conversation one.

### How to recover if you got it wrong

Symptom: you ran a workflow, Claude said "done," and the doc isn't in your Drive — it's in a different Google account's Drive.

1. **Find the doc.** Sign in to the OTHER Google account, go to Drive. The doc is there, in your "Recent" view (created within the last few minutes).
2. **Decide what to do with it:**
   - **Share it back** — right-click → Share → add your other account → set them as Owner (or Editor, then transfer ownership). The doc stays in the wrong-account Drive but you can access it from your main account.
   - **Move it** — download as `.docx`, upload to the right account, re-run the workflow. (You'll lose Tabs in the round-trip; better to use the next option.)
   - **Make a copy** in the right account: from the wrong account, share the doc with the right account, then in the right account "Make a copy" — the copy lives in the right Drive. The original can be deleted from the wrong account.
3. **Reset the authorization** so future workflows use the right account: ask Claude to `gdocs_reset_authorization(full=true)`. This clears the cached token and the Apps Script setup. On your next workflow, Claude will hand you a fresh authorization link — open it in the correct account this time.

### Things to know

- The server stores credentials **per user identity** (your `sub` claim from Google's ID token), not per browser session. Two people can use the same connector URL safely — their workflows are isolated by Google account, not by URL.
- If you genuinely want to use multiple Google accounts (one for work docs, one for personal), you need **two separate connector setups** OR you need to `gdocs_reset_authorization` between sessions. There's no "switch active account" command — Claude can only hold one authorization at a time.
- **First-time setup will ask for permissions.** Google's consent screen will name whatever OAuth Client the operator registered in Google Cloud Console (often the pre-rename name `google-docs-fly` or the post-rename `appscriptly` — the Google Cloud Console display name is operator-controlled and updates on its own schedule). The screen will list permissions like "access your Google Drive and Google Docs" plus "manage your Apps Script projects" — the latter is for the per-user Apps Script Web App setup, required for the retrofit workflows (B and C) and for the persistent-automation runtime the rename brings to the forefront.

---

## Where to go next

- **You're stuck:** ask Claude directly. The server is self-documenting; Claude can call `gdocs_guide()` for the workflow catalogue, `gdocs_help("error text")` for error-specific guidance, or `gdocs_server_info()` to show you which version is running.
- **You found a bug:** open an issue at https://github.com/Sundeepg98/google-docs-mcp/issues. Include the output of `gdocs_server_info()` (Claude will run it if you ask) so the maintainer knows exactly which build you hit.
- **You're a developer / operator:** see the project [README](../README.md) for install / config / deployment, [docs/RUNBOOK.md](RUNBOOK.md) for ops, [docs/TOOL_CONTRACT.md](TOOL_CONTRACT.md) for tool stability guarantees, and [docs/THREAT_MODEL.md](THREAT_MODEL.md) for the security posture.
