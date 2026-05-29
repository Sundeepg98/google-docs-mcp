# ADR 2026-05-28 — Generic bound-Apps-Script generator (`as_generate_bound_script`)

**Status**: Accepted
**Date**: 2026-05-28
**PR**: PR-Δ7
**Predecessors**: PR-α (`gdocs_install_automation` reframe), PR-Δ5.5 (rename to `appscriptly`)
**Follows**: PR-Δ6 (Vercel pilot)

## Context

The strategic direction (confirmed across the rename ADR and the multi-service feasibility research) is **"Workspace Automation MCP": Claude generates persistent Apps Script automations that live in the user's Workspace and run on Google's infrastructure without Claude in the loop.** That is the differentiator — competitive Workspace REST coverage already exists from other MCP servers; generating persistent, container-bound automation does not.

Every concrete near-term feature that realizes this direction is a *bound script*:

- **slides-for-video** — a Slides deck with a bound script that drives slide-advance timing / export.
- **sheets dashboards** — a Sheet with a bound script: a daily time-driven refresh + a custom menu to re-run on demand.
- **docs menus** — a Doc with a bound `onOpen` menu that runs document-specific actions (e.g. "refresh from linked Sheet").

All three need the *same primitive*: create an Apps Script project **bound to a specific container** (Doc / Sheet / Slides), push a `.gs` body + manifest, and deploy it. Before this PR, that primitive did not exist. The existing `services/gas_deploy` creates a **standalone** Web App (the runtime installer for the lossless-retrofit path) — a different thing (see "Distinct from gas_deploy" below).

Building each feature's bound-script logic independently would duplicate the create/push/deploy plumbing three-plus times and re-derive the container-detection + manifest-translation each time. The disciplined move is to **ship the primitive first** and let the use-case PRs compose it.

### Apps Script API reality (verified against the official reference)

Two findings shaped the design; both were verified against the Apps Script API v1 + manifest references rather than assumed:

1. **Binding is a `parentId` on `projects.create`.** `projects.create` takes a request body of `{title, parentId}`; supplying `parentId` (the container's Drive ID) makes the new project *container-bound* rather than standalone. This matches what the task brief described.

2. **Menus / sidebars / triggers are NOT manifest fields — they are code.** The `appsscript.json` manifest carries `timeZone`, `runtimeVersion`, `oauthScopes`, `dependencies`, `addOns`, `webapp`. It has **no** field for custom menus (`Ui.createMenu` from `onOpen`), sidebars (`HtmlService`), time-driven triggers, or `onEdit` handlers — those are all implemented in the `.gs` source and (for installable triggers) installed via `ScriptApp.newTrigger(...)`. This contradicts a literal reading of the brief's `build_manifest` spec ("translate `menu`/`triggers`/`sidebar_html` into appsscript.json format"). We followed the real API.

## Decision

**Add a new `services/apps_script/` service folder housing one generic tool, `as_generate_bound_script`** — the primitive every later feature PR composes. Ship the primitive, not the use cases.

### Shape

- `scopes.py` — `GAS_BOUND_SCOPES` (`script.projects` + `script.deployments`). Already in baseline `auth.SCOPES` (PR #125), so **no second consent**; the decorator's `scopes=` declaration is the honest annotation, not a new grant.
- `api.py` — pure logic + Apps Script REST calls, no decorators:
  - `auto_detect_container_kind(creds, container_id)` — Drive `files.get` → mimeType → `"docs"`/`"sheets"`/`"slides"`; clear `ValueError` for unsupported types.
  - `build_manifest(manifest_dict, timezone)` — **pure** function. Always emits `runtimeVersion: "V8"` + `timeZone`. Reconciles the manifest-reality finding (see below).
  - `create_bound_project` — `projects.create` with `parentId` (idempotent=False).
  - `set_project_content` — `projects.updateContent` with manifest + `.gs` (idempotent=True).
  - `create_deployment` — `versions.create` then `deployments.create` (idempotent=False).
- `tools.py` — `as_generate_bound_script` (`@workspace_tool(service="apps_script", scopes=GAS_BOUND_SCOPES, creds=True, idempotent=False, ...)`) orchestrating detect → create → push → deploy → `{script_id, deployment_id, container_id, container_kind, project_url}`. First `as_*`-prefixed tool (appscriptly-native naming per PR-Δ5.5).

### How `build_manifest` reconciles the manifest-reality finding

`build_manifest` accepts all four operator-friendly keys (`menu`, `triggers`, `sidebar_html`, `oauth_scopes`) as the brief specified, but maps them to what the manifest *can actually express*:

- It does the one manifest-relevant thing for menu/sidebar/trigger capabilities: **derives the required `oauthScopes`** (a menu or sidebar implies `script.container.ui`; a time-driven trigger implies `script.scriptapp`) and unions them with caller-supplied `oauth_scopes` (de-duplicated, order-stable).
- It **validates** the `menu`/`triggers` entry shapes (cheap client-side rejection of malformed input) and **echoes** the normalized intent under a private `__plan__` key — an internal hand-off so the orchestration layer / generated body / tests can see what was understood. `set_project_content` strips `__plan__` before serializing (it's not a real manifest field; Apps Script would reject an unknown top-level key).
- The actual menu/trigger/sidebar **wiring lives in the `.gs` `script_body`** the caller supplies — which is precisely why `script_body` is a required argument. Claude authors the `onOpen`/handler code; the manifest's job is runtime + scopes.

This keeps `build_manifest` pure and property-testable (any valid-shaped input → a manifest that always has `runtimeVersion` + `timeZone` + a well-formed scope list) while staying honest about the API.

## Distinct from `services/gas_deploy`

`apps_script/` is deliberately a **new, separate service** from `gas_deploy/`, not an extension of it. They speak the same Apps Script REST API through the same `get_service("script", "v1")` chokepoint, but their purposes do not overlap:

| | `gas_deploy` | `apps_script` (this PR) |
|---|---|---|
| Project kind | **standalone** (no `parentId`) | **container-bound** (`parentId`) |
| Purpose | runtime *bootstrap* — ONE Web App per user for the lossless-retrofit backend | per-container automation — a NEW bound script per Doc/Sheet/Slides |
| Entry point | `/exec` Web App URL | container menus / sidebars / triggers |
| Cardinality | one per user | one per automation target |

Folding bound-script generation into `gas_deploy` would conflate "the runtime installer" with "the per-feature generator" and overload that service's single tool. Separate folders keep each service's responsibility crisp.

## Alternatives rejected

1. **Extend `gas_deploy` instead of a new service.** Rejected — different purpose (standalone runtime bootstrap vs per-container generation), different cardinality, different entry point. Overloading `gas_deploy`'s installer tool with a generic generator would muddy both. The two are siblings, not parent/child.

2. **Paste-the-script UX (return generated `.gs` for the user to manually paste into the Apps Script editor).** Rejected — it defeats the entire automation thesis. The differentiator is that the automation gets *installed and deployed* so it runs without Claude in the loop. A copy-paste step puts a human back in the loop on every install, reintroduces transcription errors, and surrenders the "it just works in your Workspace" experience that is the whole point.

3. **Ship a use-case tool (e.g. `as_install_sheet_dashboard`) directly, skipping the generic primitive.** Rejected — premature. The three known consumers (slides-for-video, sheets dashboards, docs menus) share the create/push/deploy + detection + manifest plumbing; building the primitive once and composing it is the M3-Phase-C extraction discipline applied up-front where the shared shape is already known. Use-case convenience tools layer on later.

4. **Put menu/trigger/sidebar config literally into `appsscript.json`.** Rejected — not possible. The manifest has no such fields (verified); they are code-level. `build_manifest` instead derives the right scopes and echoes the intent for the generated body to implement.

## Consequences

- The feature foundation exists: slides-for-video, sheets dashboards, and docs menus can each be a thin PR that calls `as_generate_bound_script` with the right `script_body` + `manifest`, rather than re-implementing the deploy plumbing.
- No breaking changes. New service folder, one new `as_*` tool, additive scope annotation (scopes already in baseline). Existing `gdocs_*` tools are untouched.
- `build_manifest` being pure + property-tested gives high confidence the manifest is always deployable regardless of the capability mix — the load-bearing invariant for every downstream feature.
- The retry stance follows the established annotation-driven policy: detection + content-push are idempotent (wrapped); create + deploy are not (single-shot, never replayed into duplicate projects/deployments).
