# START HERE — appscriptly (fresh-session resume note)

**If you're a fresh Claude session: read this first, then `CLAUDE.md`.**

This is the cross-session handoff for the appscriptly work (and a pointer to the two sibling projects). Stable architecture/conventions live in `CLAUDE.md`; volatile status lives here in **Live state** (frozen 2026-06-01) — re-confirm against `git`/Fly/the Google Console before trusting it.

> **⚠️ POST-FREEZE UPDATE (2026-06-14) — read before the frozen Live-state below, which it overrides on these points:**
> - **Tool surface is now 110 tools across 11 services** (the frozen body says 41→57). Calendar/Tasks/Forms/Contacts REST services + the GAS automation layer + a REST-enrichment pass all shipped after the freeze (PR refs in `ROADMAP.md`). The **code requests 13 OAuth scopes** now (all SENSITIVE or identity, zero restricted, still no CASA); the 4 new services' live rollout is held by the verify-LAST deploy gate. Full deploy-staging detail: **`SCOPE_EXPANSION_PLAN.md`**.
> - **Demo redo:** the original demo `hBuuDemD8Js` was **REJECTED** (chat-only / insufficient) and replaced by **`https://youtu.be/r7ZB1YeT3SE`**; the T&S verification reply with the new link was **sent 2026-06-14 and is awaiting Google re-review.**
> - **Serving-URL cutover (2026-06-14):** the live serving URL is now the custom domain **`https://mcp.appscriptly.com/mcp`** (TLS cert + Cloudflare DNS + redirect URIs + 13 scopes on the consent screen + `TRUSTED_HOSTS` secret), still backed by the same Fly app `sundeepg98-docs-mcp`. The apex `appscriptly.com` remains the Cloudflare Pages landing/branding site only.

## One-line identity
**appscriptly** is a Google Workspace-automation MCP server (FastMCP, Python); its differentiator is generating **persistent Apps Script automations** (bound scripts, custom menus, custom `=FUNCTION()`s, scheduled sheet dashboards, slides-to-video decks, web-app/webhook endpoints) plus Docs/Sheets/Slides/Drive create-edit-manage. Deployed on Fly as `sundeepg98-docs-mcp`. Module path is `src/appscriptly/` (renamed from `src/google_docs_mcp/` in #151). Git remote is still literally `Sundeepg98/google-docs-mcp` (repo transfer deferred — see Live state).

## Read these, in order
1. **`CLAUDE.md`** (repo root) — stable orientation: architecture, exact CI commands, tool-surface witnesses, conventions/guardrails. Intentionally carries NO volatile state.
2. **`ROADMAP.md`** — feature/hardening/architecture roadmap; the **source of truth for what's shipped** (done-markers carry the PR # and land in the same PR as the code — the "Keep ROADMAP self-current" convention).
3. **`MIGRATION_READINESS.md`** — the rename/identity surface and sequencing (the deferred migration #4).
4. **`PHASE1_VERIFICATION_KIT.md`** — the Google OAuth free-verification + dedicated-client plan and operator punch-list (now executed — see Live state).
5. **`docs/adr/`** — ADRs (rename-to-appscriptly, vercel-pilot, **shared-engine-convention** with interview-prep).

## Memory scope
Claude memory scope: `D--Sundeep-projects-appscriptly` → start from its `MEMORY.md`. Key captured facts: name FINAL (`appscriptly`; appScriptify rejected); module rename DONE+deploy-verified (#151); free-verification-eligible scope set; Testing-mode 7-day refresh-token cap (now escaped — Published to Production); hatchling ships only git-tracked files; non-root + Fly-volume ownership trap; the ultracode-max binary-patch note (machine-global, separate concern).

## How to verify the running surface (don't trust `python -c`)
Use the REAL entry path (console script / pytest / in-container), never `python -c` — the src layout under-registers the `services` namespace under `-c` and gives false tool-count readings. Live checks: `/health`, `/.well-known/oauth-protected-resource`.

---

## Live state as of 2026-06-01 (FROZEN)

### appscriptly — THE headline: OAuth verification is UNDER REVIEW
- **Google OAuth verification SUBMITTED → "under review."** Expect the **first email in ~3–5 days**, full review up to **~6 weeks**. **⚠️ WATCH the `sundeepg8@` inbox for Google and respond FAST** — a slow reply stalls the whole review. This is the single most time-sensitive open item.
- Submission package (all done): demo video **https://youtu.be/r7ZB1YeT3SE** (CURRENT; unlisted, on `sundeepg8@` / channel "Sundeep G") — this SUPERSEDES the original **https://youtu.be/hBuuDemD8Js**, which Google REJECTED as chat-only / insufficient (the T&S reply carrying the new link was sent 2026-06-14, awaiting re-review — see the post-freeze banner at top); verified domain **appscriptly.com** (site + `/privacy` live on Cloudflare Pages); dedicated **`appscriptly-server`** OAuth client; **8 scopes — 5 sensitive, 0 restricted** ⇒ the **FREE verification path, no CASA** needed; the Testing-mode **7-day refresh-token cap is escaped** (app Published to Production).
- **Tool surface matured 41 → 57** across 3 worktree-isolated cascade rounds, ALL within the existing 8 scopes (**zero verification impact** — this is why maturation could proceed while verification is under review):
  - slides: `gslides_add_slide`, `gslides_create_image`, `gslides_create_table`
  - sheets: `gsheets_append_rows` + tab lifecycle (`add_sheet`/`delete_sheet`/`rename_sheet`) + `gsheets_apply_conditional_format`
  - drive: `gdocs_create_folder`, `gdocs_revoke_permission`, `gdocs_export_doc`, generalized `find_file`
  - docs: table insertion, range formatting, paragraph ops, markdown-table
  - apps_script: `as_deploy_web_app` (deploy a doGet/doPost project as a web app/webhook)
  - **⚠️ CONFIRM the live count once round-3's cascade finishes** — at freeze time round-3 was mid-cascade (origin/main at `7ab9145` = 53 tools; my sheets PR **#161** `gsheets_apply_conditional_format` was green + OPEN, awaiting ship-d1's serial merge). 57 is the post-round-3 target. Verify with: `git show origin/main:tests/golden/tool_surface.json | python -c "import sys,json;print(len(json.load(sys.stdin)))"`.

### appscriptly — DEFERRED to POST-approval (do NOT do before Google approves)
- **Migration #4** — GitHub repo transfer (`Sundeepg98/google-docs-mcp` → `appscriptly/appscriptly`) + Fly app cutover (`sundeepg98-docs-mcp` → `appscriptly`). Safe to defer because the public identity is **anchored to appscriptly.com**, which decouples the brand from the repo/Fly names — so the transfer is cosmetic and best done after approval to avoid disturbing the under-review client. See `MIGRATION_READINESS.md`.
- **Trash the 6 "appscriptly demo —" files** in the `sundeepg8@` Drive (left over from the demo-video recording). Trivial cleanup; do anytime, no rush.
- **Whole-Drive generalized search** — needs a **RESTRICTED** scope, which would pull the app into the CASA/restricted tier. Out of scope for the free path; revisit as a post-approval tier decision.

### Sentinel-OS (the "mailin" folder; `Sundeepg98/Sentinel-OS`)
- **`development` is now 5/5 CI GREEN** (was 5/5 red). Greening landed via **PR #4**: both **critical** vulns + 27 advisories cleared **with the npm-audit gate UNTOUCHED** (no gate-loosening), plus three real bug fixes — a **server-can't-boot bug** (`adminOnly` was unexported), a **dossier-404**, and a **WarRoom auth-crash**. e2e went green via a **CI-only placeholder-`GEMINI` env relaxation** (production behavior unchanged).
- Residual: `GEMINI_API_KEY` is a **non-secret by design** in this project (don't flag it as a leak).
- Orientation: its own repo-root `CLAUDE.md` is authoritative; memory scope `D--Sundeep-projects-job-hunting-mailin` (see `project_sentinel_os.md`). The folder name `mailin` and the parent `job-hunting/.claude/CLAUDE.md` are misleading — this is Sentinel-OS, not email/job-search.

### interview-prep (`Sundeepg98/interview-prep`)
- Anthropic **`LlmAdapter` wired** (stub keyless default) + **minimal CI added** (PR **#1** scaffold + **#2** CI).
- **Still a SCAFFOLD** — the real engine (mirror Sentinel-OS's module shapes per the **shared-engine-convention ADR** present in both repos) is the **next big build arc**, not yet started.

### Six-items status (this multi-session program)
1. **Sentinel-OS** ✅ (+ greening: `development` 5/5 green)
2. **interview-prep** ✅ (+ CI; engine still to build)
3. **Parent `job-hunting/.claude/CLAUDE.md` de-misleading** ✅
4. **Migration #4** ⏸ **DEFERRED** to post-approval (operator-gated)
5. **appscriptly verify + maturation** ✅ (verification submitted/under-review; 41→57 tools)
6. **This freeze** ✅

### Open / next (in order)
1. **Watch the `sundeepg8@` inbox for Google** → respond fast (the gating item).
2. **After approval:** execute **migration #4** (repo transfer + Fly cutover).
3. **interview-prep engine** — the next big build arc (mirror Sentinel's module shapes per the ADR).
4. **Trash the 6 demo files** in `sundeepg8@` Drive (trivial; anytime).

---

## Patterns that worked (reuse these)
- **Standing warm-agent roster** reused across all rounds (coding + research agents), not spawn-and-discard — kept context warm and parallelism cheap.
- **Worktree-isolated parallel builds + serial verified-merge.** Each PR is built in its own `git worktree` (off current `origin/main`, with its own `uv sync` venv) so parallel agents never share a working tree (this avoided the #152/#153 commit-tangle). Merges are **serial + verified**: ship-d1 runs the cascade one PR at a time, and **whichever PR merges 2nd+ rebases onto the new main and re-freezes the golden tool surface** (the golden surface + `_MIN_EXPECTED_TOOL_COUNT` are the predictable conflict points every round).
- **Within-the-8-scopes discipline.** Every maturation tool stayed inside the already-submitted scope set → **zero verification impact**, which is what let the surface grow 41→57 while the OAuth review is in flight. Do NOT add a tool that needs a new (esp. RESTRICTED) scope until after approval.
- **Judge CI by per-job conclusions, not the watch/`gh pr checks` rollup** — and don't silently touch security gates (the npm-audit gate stayed untouched in Sentinel's greening). `CodeQL` sometimes sits in a perpetual `pending` on these PRs; `FAIL_COUNT=0` is the real green signal.
- **Verify on disk, not on success messages** — re-read/grep after edits; the Edit/Write "success" line is not proof.
