# appscriptly — Migration Readiness Report

> 2026-05-30. Verified against `Sundeepg98/google-docs-mcp` origin/main `e7222e5` (post-#148/#149/#150); ADR `docs/adr/2026-05-27-rename-to-appscriptly.md` (shipped as PR #135); pypi.org; github.com; local workdir.

## Headline: the rename already largely happened
PR #135 (merged 2026-05-27) shipped the user-facing rename — PyPI dist name, `FastMCP("appscriptly")`, README, health `service` field, CLI binary (`appscriptly` + `google-docs-mcp` alias). So this is about confirming irreversibles + flagging the real risk (a stale local tree), not planning a rename from scratch.

## Rated table

| # | Item | Current → Target | One-shot? | Ready? | When |
|---|------|------------------|-----------|--------|------|
| 1 | **Local folder** | `mcp-servers\google-docs` → `appscriptly` | **No — fully reversible** (git identity is in `.git/config`, not the dir name) | ✅ | **Now**, via fresh clone (§1) |
| 2 | Claude memory scope (derived from folder path) | new bucket `D--Sundeep-projects-appscriptly` | sticky-ish, **non-destructive** (old bucket preserved; this repo has none yet) | ✅ | falls out of #1 |
| 3 | Python module `appscriptly` → `appscriptly` | unchanged (ADR-deferred) | No — internal refactor | ✅ ready | dedicated PR, **NEXT** on a fresh tree (#148/#149/#150 merged) |
| 4 | **PyPI `appscriptly`** | stub v0.0.1 **already published, yours** | YES — **DONE ✅** | ✅ | name locked |
| 4b | old PyPI `google-docs-mcp` | **squatter's** ("Jag_k") — never ours | lost, irrelevant | n/a | confirms rename was right |
| 5 | **GitHub org `appscriptly`** | **reserved 2026-05-27 ✅** | DONE | ✅ | — |
| 6 | GitHub repo transfer → `appscriptly/appscriptly` | not yet | No — GitHub auto-redirects | ✅ low-risk | after branch fleet drains |
| 7 | Tool prefixes `gdocs_*`→`as_*` | 35 legacy + 6 `as_*` | No — but ADR says **keep `gdocs_*` forever**; only new tools get `as_*` | n/a | **no action** |
| 8 | Fly app rename | `sundeepg98-docs-mcp` → `appscriptly` | No — 90-day 308 redirect | ⚠️ | after OAuth client swap |
| 9 | Domain + OAuth client | `appscriptly.com` reserved ✅; client = Phase-1 kit | domain DONE; client reversible | ✅ | per Phase-1 kit |
| 10 | **Code-restructuring readiness** | #148/#149/#150 merged + deployed; only local tree stale | n/a | ✅ **ready on fresh tree** | clone fresh, then module rename (§10) |

## §1 — Folder: the one-shot that isn't
Fully reversible (rename anytime; repo identity lives in `.git/config`). What depends on the path: the Claude memory bucket (new path = new, **empty but non-destructive** bucket; old preserved), any `.mcp.json` cwd, and **60+ git worktrees** hard-coding the old path (this is why you must NOT rename in place).
**Recommendation: fresh clone, do NOT move the old tree:**
```
git clone https://github.com/Sundeepg98/google-docs-mcp.git D:\Sundeep\projects\appscriptly
```
Name `appscriptly`, top-level (standalone product, clean memory bucket, away from worktree pollution). Start the new session there. **Commit this now.**

## §10 — The real caution flag 🚩
- **PR #148** (drive.readonly drop, ship-d1): **MERGED + deployed + live-verified** on `sundeepg98-docs-mcp` (base now requests the zero-restricted 8-scope set). **Phase-1 verification submission no longer blocked on it.**
- **PR #149** (tool-DX `gdocs_guide`): **MERGED + deployed.**
- **PR #150** (fix the `/upload/frames` route #148 left unrouted → now wired + integration-tested; live POST probe 403 not 404; `/health` 200): **MERGED + deployed.** origin/main HEAD = `e7222e5`.
- Local working tree: stale — many commits behind origin/main `e7222e5`, uncommitted changes, stashes, **pre-`services/` architecture** (predates #144), 60+ worktrees. **Do not build on it or rename it — clone fresh.**

## Bottom line
**Commit NOW (safe / already done):** create `D:\Sundeep\projects\appscriptly` via fresh clone + start new session there; PyPI/GitHub-org/domain all already reserved in your favor.
**Already landed (no longer gating):** #148 (drive.readonly drop) + #149 (`gdocs_guide`) + #150 (`/upload/frames` route fix) all MERGED + deployed + live-verified; origin/main HEAD `e7222e5`. **Module rename is now NEXT** — its own PR on a fresh tree (clone fresh; do not build on the stale local tree).
**Sequence after:** OAuth client swap (current Fly app) → module rename → GitHub repo transfer → Fly app cutover (90-day redirect). All reversible/gradual; none blocks the folder.
**Non-issues the brief worried about:** folder reversibility (it IS reversible), memory loss (non-destructive), name squatting (all reserved).
