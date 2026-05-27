# ADR 2026-05-27 — Rename project from `google-docs-mcp` to `appscriptly`

**Status**: Accepted
**Date**: 2026-05-27
**PR**: PR-Δ5.5
**Predecessors**: PR-α (reframe `gdocs_setup_apps_script` → `gdocs_install_automation`)
**Follows**: PR-Δ5 (commercial-ready engineering)

## Context

The project was bootstrapped in v1.0 with a narrow goal: an MCP server that creates Google Docs with native sidebar Tabs (Google's October 2024 feature). The name `google-docs-mcp` reflected that scope honestly.

Five things happened over the next ~30 PRs that made the name a liability rather than an asset:

1. **Service breadth grew.** Sheets (PR #119), Slides (PR #121), Drive sharing (PR #117), and broader Drive file management all landed. The server now covers SIX Google Workspace services; `google-docs-mcp` undersells the scope by 5×.
2. **Apps Script automation became the headline.** PR-α (this session) reframed `gdocs_setup_apps_script` as `gdocs_install_automation` — the user-facing positioning is now "install a Workspace Automation runtime that lets Claude build persistent workflows in your account." The Apps Script generation is what produces persistent value (workflows live in the user's Workspace and run on Google's infrastructure without Claude in the loop); the Docs / Sheets / Slides REST coverage is necessary infrastructure but not the moat. The name `google-docs-mcp` hides the actual moat.
3. **Multi-service feasibility research surfaced the naming gap.** The R33 multi-service specialist explicitly flagged "Apps Script generation is the differentiator — competitive REST coverage exists from Taylor Wilsdon's MCP and others; ours is the only one that generates persistent automation that lives IN the Workspace." The name should match the differentiator.
4. **Commercial activation needs a brand-led name.** PR-Δ5 wired the license-key middleware seam for commercial activation; the operator's strategic framing (2026-05-27) is "build commercial-ready engineering NOW; defer paid activities until later." A clean brand-led name lets the commercial-activation PR drop in without the user-facing surface still saying "google-docs-mcp" (which Google might object to as trademark-adjacent, and which sounds like a community tool rather than a product).
5. **Registry availability check came back clean** for `appscriptly` across PyPI, GitHub orgs, Fly app names, NPM, and `.com` (the operator deferred the `.com` per cost-rationality, but the four other registries are claimed). A name available across the entire commercial surface is a one-shot move; once any squat lands, the rename gets meaningfully harder.

## Decision

**Rename the project from `google-docs-mcp` to `appscriptly`** on the user-facing distribution and identity surfaces, while keeping the Python module path + cryptographic primitives + tool names + production deploy URL unchanged.

### What renames (this PR)

| Surface | Old | New | Why now |
|---|---|---|---|
| PyPI distribution name | `google-docs-mcp` | `appscriptly` | The brand-led name; what shows up in `pip install` instructions |
| README title + intro + tagline | "google-docs-mcp" / "google-docs-fly" | "appscriptly" + "Workspace Automation MCP" | The product positioning surface |
| FastMCP server identity | `FastMCP("google-docs", ...)` | `FastMCP("appscriptly", ...)` | What appears in MCP client UIs (claude.ai's connector picker, Claude Desktop's tool listing) |
| `_SERVER_INSTRUCTIONS` opening | "google-docs-fly — create, edit, read..." | "appscriptly — Workspace Automation MCP. Generates persistent workflows..." | The system prompt the LLM sees on every session |
| Health endpoint `service` field | `{"service": "google-docs-mcp"}` | `{"service": "appscriptly"}` | Operator monitoring + log-aggregation surface |
| CLI binary | `google-docs-mcp` | **both** `appscriptly` AND `google-docs-mcp` (alias) | New install path is `appscriptly`; legacy alias preserves muscle memory through v3.0 |
| Apps Script project title (new installs) | `"google-docs-mcp / restructure"` | `"appscriptly / restructure"` | What users see in their Drive after running `gdocs_install_automation` — only affects NEW installs (title isn't part of the content_hash; existing projects keep their original Drive title) |
| `gdocs_server_info`'s version lookup | `version("google-docs-mcp")` | fallback chain: `appscriptly` first, then `google-docs-mcp` (for legacy installs still pinned via uv.lock) | Backward compat during the transition |
| User-facing keyword set | `["mcp", "google-docs", "claude", "anthropic", "tabs"]` | adds `appscript`, `apps-script-generator`, `workspace-automation`, `google-workspace` | PyPI discoverability for the new positioning |

### What does NOT rename (intentionally, with rationale)

| Surface | Why it stays |
|---|---|
| Python module path `src/google_docs_mcp/` | Renaming would break ~hundreds of internal imports across src/ + tests/ + every consumer that pinned an import path. The cost is not justified by the user-facing benefit — PyPI distribution name and module name are conventionally allowed to differ (e.g. `PyYAML` distribution → `yaml` module). |
| All `gdocs_*` tool names | Renaming would break every existing claude.ai connector user's tool calls + every saved prompt that references the tool by name. The PR-α reframe (`gdocs_setup_apps_script` → `gdocs_install_automation`) used a deprecation-alias pattern; doing that for 30+ tools is a massive PR with high regression risk and minimal user-facing value. **New tools added in PR-Δ7+ will use the `as_*` prefix** (appscriptly-native); existing tools keep `gdocs_*` indefinitely. |
| Logger names `google_docs_mcp.*` | Operators have monitoring + log-aggregation rules that grep these strings. Renaming would silently break log routing across every Sentry / Datadog / Splunk / GCP Cloud Logging integration any operator has built. Logger names are observability infrastructure, not branding. |
| `_TENANT_ATTR = "_google_docs_mcp_user_id"` (PR-Δ5) | Just shipped in PR-Δ5. Process-local key with no external surface; renaming creates churn for zero benefit. |
| HKDF info bytes `b"google-docs-mcp v1 api_bearer"` etc. | **Cryptographic primitive.** Renaming would invalidate every derived key for every operator — every signed URL in flight, every bearer token cache, every OAuth state HMAC. Operators would need to flush every active session. The cost-benefit math is "infinite breakage for zero brand benefit." |
| `~/.google-docs-mcp/` user data directory paths | User data. Renaming would orphan every existing user's OAuth tokens — Claude Desktop / Code users would silently lose their session and re-consent. Catastrophic UX hit. |
| `app = "sundeepg98-docs-mcp"` in `fly.toml` | The production deploy. Changing this here would break the running app on the next `fly deploy`. The cutover plan to `appscriptly.fly.dev` is documented in the file's top comment block as a 7-step operator-scheduled migration. |
| GitHub repo URLs `Sundeepg98/google-docs-mcp/...` | Repo transfer to the `appscriptly` GitHub org is a separate scheduled operator decision. Until the repo actually moves, every URL in docs + the OpenSSF Scorecard badge + `SECURITY.md` advisory link must point at the live repo, which is still under `Sundeepg98/`. |
| Cryptographic / setup-state lookup keys, content hashes, ledger schemas | Renaming any of these triggers either a re-deploy on every existing install (annoying) or invalidates persisted state (catastrophic). Out of scope. |

### Tool prefix convention

- **Existing `gdocs_*` tools stay as-is** through v3.0+. No mass rename. The tools' titles + docstrings already correctly mention the specific Google service they target (Docs, Sheets, Slides, etc.) — the `gdocs_*` prefix is a historical artifact, not a misleading label.
- **New tools added in PR-Δ7 and later** use the `as_*` prefix (`as_create_trigger`, `as_install_menu`, `as_run_workflow`, etc.). The `as_*` prefix signals "this is appscriptly-native functionality that doesn't have a 1:1 Google API analogue" — typically tools that generate Apps Script code, install bound scripts, or wire up the persistent automation runtime.
- The PR-α deprecation-alias pattern (`gdocs_setup_apps_script` → `gdocs_install_automation`) stays as the model for any future explicit rename: keep the old name registered + emit `DeprecationWarning` + point at the new name in the docstring + planned removal in v3.0.

### Why staged (not a single atomic rename)

A complete rename — module path + Fly app + GitHub repo + OAuth client + cryptographic primitives + tool names + user-data path — is a multi-week ops project that would freeze feature work and require a coordinated maintenance window for every existing user. That's not the right shape for an operator on a personal-tier deploy with no support team.

The staged approach (this PR) ships the high-leverage user-facing surface change immediately + documents the deferred items with clear rationale. Each deferred item has a defined trigger condition:

- **Repo transfer**: when the operator decides the appscriptly GitHub org is the canonical project home.
- **Module path rename**: when there's a separate dedicated PR that absorbs the import churn + the consumer migration risk.
- **Fly app cutover**: when the operator schedules the 7-step migration window in `fly.toml`'s comment block.
- **PyPI stub publish**: when the operator runs `docs/runbooks/pypi-publish-stub.md`.
- **OAuth Client display name update**: when the operator opens Google Cloud Console (~30s manual change).

None of those are blocking for the rename benefits (PyPI discoverability + MCP client UI + product positioning) to land.

## Consequences

### What gets better immediately

- **PyPI install instructions** read `pip install appscriptly` — clean, brand-led, available globally.
- **MCP client UIs** (claude.ai's connector picker, Claude Desktop's tool list) show `appscriptly` — matches the positioning + the future commercial brand.
- **Operator monitoring** sees `{"service": "appscriptly"}` from the health endpoint — log-aggregation rules can shift to the new name on the next deploy without rushing.
- **New users** discovering the project via search find it under the canonical brand name + the upgraded `description` + extended keyword set.
- **`gdocs_server_info`** correctly resolves the package version after the rename (the fallback chain handles both old + new PyPI artifact names).

### What stays unchanged (positive: no breakage)

- Existing claude.ai connector users: every tool call still works.
- Existing Claude Desktop installs: `google-docs-mcp` CLI binary still launches the server.
- Existing OAuth tokens at `~/.google-docs-mcp/`: still read correctly; no re-consent.
- Existing Fly deploy: `sundeepg98-docs-mcp.fly.dev` keeps serving traffic until the operator schedules the cutover.
- Every existing import in operator-side wrappers / forks / integrations: still resolves (`from google_docs_mcp import ...`).
- Every signed URL in flight + every bearer token cached: still validates against the unchanged HKDF info bytes.

### What gets worse (the honest debt)

- **Two-name period.** Until the deferred items land, the project name has dual identity: distribution + UI + branding under `appscriptly`, but module path + URLs + CLI alias + production Fly app under `google-docs-mcp`. New contributors will see this and need to read this ADR to understand which is which. The README's "Note on the rename" block tries to surface it; the ADR is the canonical explanation.
- **Search engine duplication.** Search for "google-docs-mcp" finds the original; search for "appscriptly" finds the rename. Until the GitHub repo transfers + the canonical URL changes, both surface independently. The README's heading + tagline rewrites help future search-result clarity but the old name is sticky for ~6 months.
- **PyPI stub vs full publish.** The PyPI distribution rename per this PR is metadata-only — the operator still needs to actually `uv publish` the new wheel to claim the name. The runbook (`docs/runbooks/pypi-publish-stub.md`) covers the steps; until it runs, `pip install appscriptly` fails with `ERROR: Could not find a version`. That gap is operator-action-pending, not a code defect.

### Operator-action-pending checklist (post-merge)

```
[ ] Publish the PyPI stub (docs/runbooks/pypi-publish-stub.md, ~10 min)
[ ] Update Google OAuth Console: add appscriptly.fly.dev redirect URI (~30s manual)
[ ] Update Google OAuth Console: rename the OAuth Client display name to "appscriptly" (~30s manual, optional but recommended)
[ ] Decide cutover timing: when to migrate sundeepg98-docs-mcp.fly.dev → appscriptly.fly.dev (see fly.toml top comment for the 7-step procedure)
[ ] Decide repo-transfer timing: when to move github.com/Sundeepg98/google-docs-mcp → github.com/appscriptly/appscriptly (post-transfer: sweep docs for GitHub URL strings)
[ ] (Eventually) module-path rename PR: src/google_docs_mcp/ → src/appscriptly/ + import sweep across src/ + tests/ + scripts/
```

None of these blocks PR-Δ7+ feature work.

## Verification

- `pytest tests/` — 972 passed, 5 skipped (live-only). Identical to the PR-Δ5 baseline; no regressions from the rename.
- `ruff check src/ tests/` — clean.
- `pyright src/` — 0 errors, 0 warnings.
- `uv lock` regenerated to reflect the new distribution name (`Added appscriptly v1.5.1` / `Removed google-docs-mcp v1.5.1`).
- Backward-compat verifications baked into the existing test suite:
  - `tests/unit/test_cli_dispatch.py` still passes — the legacy `google-docs-mcp` CLI binary alias keeps `[project.scripts]` honest.
  - `tests/unit/test_security_txt.py` still passes — GitHub advisories URL unchanged until repo transfer.
  - `tests/unit/test_keys.py` + `test_key_provider.py` still pass — HKDF info bytes unchanged.
- Forward-compat: PR-Δ7+ feature tools can adopt the `as_*` prefix without any prerequisite work; the decorator + registration infrastructure is prefix-agnostic.

## References

- Multi-service feasibility audit (R33 agent `a2d2492bbebb200a6`) — surfaced the Apps Script differentiator that motivates the rename
- PR-α reframe (CHANGELOG `[Unreleased]`) — established `gdocs_setup_apps_script` → `gdocs_install_automation` as the canonical deprecation-alias pattern this rename follows for tool names
- PR-Δ5 ADR (`docs/adr/2026-05-27-commercial-ready-engineering.md`) — the stub-but-wired commercial-activation seams this rename supports
- `docs/runbooks/pypi-publish-stub.md` — operator action to claim the PyPI name
- `pyproject.toml` `[project]` block — the metadata surface that drives `pip install`
- `fly.toml` top comment block — the 7-step Fly migration plan
- [PEP 503](https://peps.python.org/pep-0503/#normalized-names) — PyPI name normalization (relevant to the case-insensitive `appscriptly` claim)
