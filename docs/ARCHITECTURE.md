# Architecture — Hex Foundation for the Google Workspace MCP

**Version:** v2.1.3 (M1a landed v2.1.0/v2.1.1; M1b skipped; M2 landed v2.1.2; M3 §5.1 spec added; M3 Phase A in flight on `ship-d1`)
**Last updated:** 2026-05-20 (v2.1.3 §5.1 server.py decomposition spec)
**Audience:** maintainers, reviewers of refactor PRs, contributors adding a new Google service

## 1. Context

`google-docs-mcp` is being expanded into a multi-service Google Workspace MCP. Current scope (v2.0.x): Docs + Drive + Apps Script. Target scope (v2.x → v3.x): Sheets, Slides, Gmail, Calendar, and Drive sharing as first-class peers of Docs.

The pre-expansion codebase has a single-service shape — most modules live flat under `src/appscriptly/` and Google API access is open-coded at the call site (`build("docs", "v1", ...)`). That shape was fine while there was one service. With five services landing over the next several releases, it becomes the wrong shape: every new service either duplicates wiring or threads through the Docs-shaped path.

The operator's intent (recorded in session: 2026-05-20): apply Hex/Ports/Adapters at the infrastructure seam where multiple implementations already exist or are imminent. **Do not** apply it at the service surface, where each service is naturally service-specific and abstraction would be cargo-culted.

This doc captures the rationale so future contributors don't have to re-derive it from session logs.

## 2. Decision

> **Hex at the core infrastructure layer. Pragmatic per-service folders at the services layer.**

Two layers, two design philosophies, one codebase.

| Layer | Philosophy | Why |
|---|---|---|
| Core infrastructure (`src/appscriptly/`) | Ports + Adapters | Multiple implementations already exist (or are imminent) for storage, credentials, key derivation, Google API access. Hex isolates the swap. |
| Service layer (`src/appscriptly/services/<service>/`) | Per-service folders, no shared abstraction | Each Google service has a distinct API surface (Docs vs Gmail vs Drive). Sharing structure across them is premature; mirroring service boundaries in code aids navigability. |

The split is deliberate. Hex earns its keep only when there's a real swap to defend; folder layout earns its keep when there's real navigation pressure. Different problems, different tools.

## 3. Infrastructure ports promoted (priority order)

The 4 ports below are the candidate set. Promotion order is by **risk × evidence-of-need**, not alphabet.

### 3.1 `StorageBackend` — ALREADY PROVEN (v2.1)

- **Status:** Shipped in v2.1 (PR #39). Production-proven.
- **File:** `src/appscriptly/user_store.py`
- **Shape:** `Protocol` with 4 methods (`get_state`, `save_state`, `delete_state`, `iter_users`).
- **Adapters:** `SqliteBackend` (default), `InMemoryBackend` (test ergonomics).
- **Why it earned its keep:** the test suite uses `InMemoryBackend` and the production deploy uses `SqliteBackend`, with zero test scaffolding leaking into prod code. A future `PostgresBackend` is a single new class, not a rewrite.
- **Reference for other ports:** the StorageBackend shape (small surface, runtime-checkable Protocol, module-level `_backend` swappable via `with_backend()` context manager) is the template. New ports should match this shape unless there's a concrete reason to diverge.

### 3.2 `GoogleAPIClient` — CHOKEPOINT EXISTS

- **Status:** Wrapper module shipped in v2.6a (PR #48); call-site migration in PR #70/#71/#75 (v2.6b). Promotion to Protocol pending.
- **File:** `src/appscriptly/google_clients.py`
- **Today:** thin passthrough wrapper around `googleapiclient.discovery.build`. Enforced as the sole import surface via TID251 lint.
- **Why promote:** the chokepoint already exists; promoting to a Protocol enables:
  - swapping in `aiogoogle` for async (concrete future requirement — see in-flight Gmail integration, which needs streaming + concurrent message fetches).
  - per-call retry wrappers, per-call logging, per-call test doubles without touching consumers.
  - centralizing the per-user `creds` injection that's currently re-derived at every call site.
- **Risk:** LOW. The chokepoint is already enforced; promoting to Protocol is a refinement, not a restructure.

### 3.3 `KeyProvider` — LANDED v2.1.0 (M1a complete)

- **Status:** Shipped in v2.1.0 (PR #88). Consumer test migration + HKDF golden-value regression landed in v2.1.1 (PR #90).
- **File:** `src/appscriptly/key_provider.py` — Protocol + 3 adapters + `InMemoryKeyProvider` for tests + `with_key_provider()` context manager (mirrors the `with_backend()` shape from v2.1).
- **Three mechanisms already coexist:**
  1. Raw master via `MCP_BEARER_TOKEN` env (legacy shim, removed in v2.0b strict-flip).
  2. Per-purpose env override (`MCP_API_BEARER_KEY`, `OAUTH_STATE_SIGNING_KEY`, `SIGNED_URL_SIGNING_KEY` — v1.5.1).
  3. HKDF-SHA256 derivation from the master (default post-v2.0b).
- **Why promote:** three implementations already coexist in `keys.py` as branching `if` chains. The Protocol replaces the branching with adapter selection at boot; new mechanisms (e.g. KMS-backed keys for the SaaS deploy variant) become new adapters instead of new `if` branches.
- **Risk (retrospective):** MEDIUM at design time — keys path is security-critical. Landed clean: HKDF golden-value regression test + 3-mechanism behavioral-equivalence parity confirmed across PR #88 + #90.

### 3.4 `CredentialStore` — DECIDED: M1b SKIPPED (v2.1.2)

- **Status:** RESOLVED — leave `credentials.py` as plain functions. See §3.4.1 below.
- **Today:** `credentials.py` exposes `get_credentials_for_user(user_id, ...)` — a single function that does lookup → validate → refresh → check-scopes against `user_store` + Google's OAuth2 `Credentials` class. The earlier framing in this section ("three call patterns, one logical concept") overcounted: HTTP-mode per-user resolution and stdio-mode single-user resolution are the SAME function with different `user_id` resolution at the call site, and `setup_state.py` is a separate deploy-time concern that doesn't touch the credential refresh path.
- **The decision criterion from the prior version of this section** — "does the next service need a non-default credential adapter?" — was answered NO once we actually examined the function. See §3.4.1 for the full reasoning.

#### 3.4.1 Why we did NOT promote `CredentialStore` to a port

The original 4-port roadmap considered `CredentialStore` (with a possible split into `CredentialStore` + `CredentialRefresher` + `AuthorizationUrlMinter`). After M1a (KeyProvider) shipped and we reviewed `credentials.get_credentials_for_user` against the Hex-applicability criteria, the verdict was **leave it as plain functions**.

**Reasoning.** The 4 concerns inside `get_credentials_for_user` (per-user threading lock, `invalid_grant` revocation handling, `NeedsReauthError` minting, incremental-scope check) are NOT 4 cross-cutting concerns to abstract. They are **4 steps of one coherent protocol**:

1. Lookup credentials via `user_store.get_state`
2. Validate / refresh (may raise `RefreshError` → revocation → `clear_state` side-effect → re-raise as `NeedsReauthError`)
3. Check granted scopes against required scopes (may raise `NeedsReauthError`)
4. Return usable `Credentials`

The steps share a user-scoped lock. The failure modes interleave (revocation discovered DURING refresh causes a `clear_state` side-effect BEFORE raise). This is a **transaction script**, not a collection of services.

**The split-into-3-ports option (call it option B) was evaluated and rejected:**

| Proposed port | Why the port leaks |
|---|---|
| `CredentialStore.load` | Needs `client_config` to reconstruct `Credentials` — the port would either carry operator config (leaks the abstraction) or hand back a half-built object that's still the caller's problem to finish. |
| `CredentialRefresher.refresh` | Google's `Credentials.refresh()` mutates in-place per its contract. A `Protocol` claiming "returns refreshed credentials" would be lying about the side-effect; a `Protocol` claiming "mutates in-place" leaks Google's implementation detail. |
| `AuthorizationUrlMinter.mint` | Needs `signing_key + base_url + client_config + scopes` — 4 of the original function's 5 params relocated. The "port" is the function in a class hat. |
| Per-user lock | Belongs to NONE of the three ports — it guards the sequence, not any single step. A coordinating `CredentialOrchestrator` re-introduces the fat object that IS the original function, with extra indirection. |

**Hex earns its keep when there's a swap candidate.** There is no credible "alternative refresh flow" implementation — only Google's OAuth2 + `google.oauth2.credentials.Credentials` class. KMS-backed key storage is a `KeyProvider` adapter concern (M1a) and a `StorageBackend` adapter concern (v2.1), not a `CredentialStore` concern. Adding ports without swap candidates is over-decomposition.

**Test architecture is already adequate.** `tests/unit/test_credentials.py` uses `with_backend(InMemoryBackend())` (from v2.1's StorageBackend port) for persistence fakes + `patch.object(Credentials, "refresh", ...)` for Google's refresh — a single-line patch at the actual integration seam. An `InMemoryCredentialStore` adapter wouldn't add coverage; worse, it would **lose** coverage of the interleaved failure modes (revocation-during-refresh → `clear_state` side-effect → re-raise sequence) because a fake `Protocol` returning `Credentials` doesn't model the side-effect chain.

**If this decision is re-litigated in a future session, the trigger should be a CONCRETE alternative implementation** — for example, session-based auth instead of stored refresh tokens, or a SaaS-deploy variant where credentials live in a vendor's identity service rather than `user_state.db`. At that point the right refactor is a new module (e.g. `session_credentials.py` with its own function), not new adapters under speculative ports promoted today. The criterion remains: real swap candidate first, port second.

## 4. Infrastructure NOT promoted (YAGNI)

The 2 candidates below were considered and rejected. Documenting them so future contributors don't waste a round re-evaluating.

### 4.1 `HTTPServer` — NOT PROMOTED

- **Today:** the `http_server/` package (`app.py` + `middleware.py` + `routes/`) wires Starlette routes + middleware directly.
- **Why not:** Starlette IS FastAPI's foundation. There is no credible swap target — moving to FastAPI would inherit Starlette anyway; moving to aiohttp / Quart / litestar requires a full rewrite that no current requirement motivates. Abstracting now is abstraction without alternative.
- **Re-evaluate if:** a concrete reason to swap appears (e.g. a SaaS deployment variant that requires a non-Starlette framework). Until then, the current shape is correct.

### 4.2 `UrlSigner` — NOT PROMOTED

- **Today:** `crypto.py` ships a single HMAC-SHA256 signer with a fixed canonical-string format coupled to the `/api/convert` URL schema.
- **Why not:** the canonical-string format is tightly coupled to what we're signing (per-user signed-upload URLs). A Protocol over this would have one implementation forever, and the "swap" point (changing the canonical-string format) is a breaking change to the signed-URL contract — i.e. a versioning concern, not an adapter-selection concern.
- **Re-evaluate if:** we ever add a second class of signed URL (e.g. signed download URLs, signed share URLs) with a different canonical-string format. The natural shape at that point is one signer per URL class, not one Protocol over all of them.

## 5. Service-layer pattern

> **STATUS — SHIPPED.** The per-service restructure described in this
> section is complete on `main`. The live layout is
> `services/{docs,drive,gas_deploy,admin,sheets,slides,apps_script}/`
> (each with `api.py` + `tools.py` + `_expected_tools.py`), and the
> HTTP server is the `http_server/` package. The tree and phase
> narrative below are kept as the original design record; annotations
> like "`docs_api.py` moves here" and the Phase A/B/C "Status" column
> describe the migration as it was planned, not work still pending.
> The illustrative `gmail/` service is still hypothetical (no Gmail
> service ships yet).

Per-service folder shape, inspired by `taylorwilsdon/google_workspace_mcp`:

```
src/appscriptly/services/
├── docs/
│   ├── __init__.py
│   ├── api.py           # docs_api.py moves here (Google Docs REST calls)
│   ├── service.py       # high-level Docs operations (compose api + business rules)
│   └── tools.py         # @gdocs_tool wrappers exposed via MCP
├── drive/
│   ├── api.py           # drive_api.py moves here
│   ├── service.py
│   ├── sharing.py       # sub-module: permissions / ACL operations (large enough to split)
│   └── tools.py
└── gmail/                # NEW (v2.x)
    ├── api.py
    ├── message_builder.py  # sub-module: MIME assembly (large enough to split)
    ├── service.py
    └── tools.py
```

**Conventions:**
- Each service is a directory, not a flat module. The directory contains `api.py` + `service.py` + `tools.py` at minimum.
- Sub-modules (`message_builder.py`, `sharing.py`) split when a single file would cross ~400 LOC or fold two distinct concepts.
- Service-internal code never reaches across to another service's `api.py`. Cross-service composition happens at `tools.py` (which is allowed to import from multiple services' `service.py`).
- Common shape stays in core infrastructure (`google_clients.py`, `credentials.py`, etc.) — services depend on infrastructure, not on each other.

**Why not a `BaseService` ABC:** each service's `api.py` is naturally service-specific (Docs operates on docs; Gmail operates on messages). A shared base class would carry no useful methods and would invite hand-waving inheritance. Folder mirroring + naming convention gets the navigability win without the abstraction debt.

### 5.1 `server.py` decomposition (M3 mechanism)

Added v2.1.3 after Hex specialist's M2 review verdict (GO to M3) flagged this section as under-specified. M3 is a 2-day landmine without it; ship-d1 (M3 POC) uses §5.1 as their spec reference.

**What STAYS in `server.py` post-M3:**
- `mcp = FastMCP(...)` instance creation (MUST be first, before any `@mcp.tool` decoration runs anywhere)
- `main()` + CLI dispatch
- `configure_auth_for_http()` invocation (FastMCP-coupled — intentionally NOT touching)
- Shared helpers: `_get_credentials`, `_format_http_error` (extract to `common.py` IFF a second service consumer materializes — premature today)
- The end-of-file side-effect import (`from . import resources as _llm_recovery_resources`) — see import-order section below for why this must stay at the bottom
- Version metadata / build_info introspection (`gdocs_server_info`, `gdocs_test_manifest`, `gdocs_guide`, `gdocs_help` — the 4 local-only tools that don't fit any service)

Expected size: **~500–800 LOC** (down from current 2,452 — 70% reduction). The 1,600+ LOC that leaves is the 15 API-touching tools' bodies that migrate to `services/*/tools.py`.

**Tool registration mechanism (Option A — chosen by operator):**

`services/<service>/tools.py` imports the live `mcp` instance from `server.py` and uses `@mcp.tool` (via the `@gdocs_tool` composite from PR #83) directly:

```python
# services/docs/tools.py
from appscriptly.server import mcp  # the FastMCP instance
from appscriptly.decorators import gdocs_tool  # from PR #83
from appscriptly.server import _get_credentials, _format_http_error
from . import api as docs_api

@gdocs_tool(
    title="Create a new tabbed Google Doc",
    readonly=False, destructive=False, idempotent=False, external=True,
    creds=True,
)
def gdocs_make_tabbed_doc(creds, title: str, tabs: list[dict]) -> dict:
    return docs_api.make_tabbed_doc(creds, title, tabs)
```

Why Option A (vs. a registration-function pattern like `def register_tools(mcp): ...`): the decorator-at-import-time shape matches the existing 24 tool decorators verbatim. Registration-function would force every service to add boilerplate (`register_tools(mcp)` + a manual call site in `server.py`) that decorators do for free. The trade-off is the import-order guarantee below.

**Import-order guarantee (CRITICAL — Round 1 landmine):**

`server.py` MUST create `mcp = FastMCP(...)` BEFORE importing any service module:

```python
# server.py
mcp = FastMCP("google-docs", instructions=_SERVER_INSTRUCTIONS)  # 1. Create mcp first

# 2. THEN import services (each triggers @mcp.tool decoration at module-load)
from .services import docs            # triggers services/docs/tools.py decoration
# from .services import drive         # added in Phase B
# from .services import gas_deploy    # added in Phase C

# 3. Existing side-effect import for resources (KEEP AT BOTTOM — see below)
from . import resources as _llm_recovery_resources  # noqa: E402,F401
```

Two specific footguns:

1. **`mcp.auth` is `None` until `configure_auth_for_http()` runs in `main()`.** If a service module's TOP-LEVEL code (not tool-body code) touches `mcp.auth`, the read returns `None` at import time, gets baked into a closure, and the tool silently misbehaves in HTTP mode while working fine in stdio mode. **Fix: services touch `mcp.auth` only inside tool BODIES (runtime), never at import time.** The existing `_get_credentials` helper already obeys this — migrating services should not re-derive it.
2. **The `from . import resources` line must stay LAST in `server.py`** (currently line 2172, `noqa: E402,F401` annotated). `resources.py` calls `@mcp.resource` at module load; if it runs before the tool decorators register, FastMCP's internal ordering can shift resource-discovery semantics. The comment block above the import documents this; keep it through M3.

**Test layout (Mirror `src/`):**

```
tests/unit/services/
├── __init__.py
└── docs/
    ├── __init__.py
    ├── test_api.py       # was tests/unit/test_docs_api.py
    └── test_tools.py     # NEW per service folder — covers the @gdocs_tool wrappers
```

Existing test files that don't have a service home (`test_keys.py`, `test_credentials.py`, `test_google_clients.py`, etc.) stay flat in `tests/unit/`. The mirror only applies to per-service code.

**Fixture-discovery test (REQUIRED — Round 1 silent-drop guard):**

To catch "forgot to add `services/X` to `server.py`'s import chain" silent failures (tools register in stdio dev but vanish in HTTP prod):

```python
# tests/unit/test_tool_registration.py
import asyncio
import pytest

# Verified expected list — bump on every add/remove. Keep alphabetized.
EXPECTED_TOOLS = {
    "gdocs_add_tabs", "gdocs_admin_audit", "gdocs_append_to_tab",
    "gdocs_delete_tab", "gdocs_find_doc_by_title", "gdocs_get_doc_outline",
    "gdocs_get_signed_upload_url", "gdocs_get_tab_url", "gdocs_guide",
    "gdocs_help", "gdocs_make_tabbed_doc", "gdocs_move_to_folder",
    "gdocs_preview_tab_split", "gdocs_read_doc", "gdocs_rename_tab",
    "gdocs_replace_all_text", "gdocs_reset_authorization",
    "gdocs_server_info", "gdocs_set_tab_icons", "gdocs_setup_apps_script",
    "gdocs_tab_existing_doc", "gdocs_test_manifest", "gdocs_trash_file",
    "gdocs_untrash_file",
}

def test_all_24_tools_register_from_their_service_locations():
    """Guard against the M3 silent-registration-drop class.

    If a services/X/tools.py module is missing from server.py's import
    chain, its @gdocs_tool decorators never run, the tools silently
    fail to register, and the failure mode is "works in stdio (dev),
    401/not-found in HTTP (prod)" — the worst possible class of bug
    to ship.

    FastMCP's tool registry is async-accessed via mcp.list_tools()
    (NOT mcp._tools — that's not a public attribute and breaks across
    FastMCP versions). Wrap in asyncio.run() for a sync test.
    """
    from appscriptly.server import mcp
    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    extra = registered - EXPECTED_TOOLS
    assert not missing, f"Service module missing from server.py import chain: {missing}"
    assert not extra, f"Unexpected tools registered (update EXPECTED_TOOLS): {extra}"
```

The `asyncio.run(mcp.list_tools())` pattern matches the existing precedent in `tests/unit/test_gdocs_tool_decorator.py:108` + `test_server_info.py:23`. Do NOT use `mcp._tools` — it's a private attribute that has shifted across FastMCP versions (we currently pin `fastmcp >= 3.3.1`; the public access path is the one FastMCP itself guarantees).

**Sequencing within M3:**

| Phase | Scope | Status |
|---|---|---|
| **A** | `services/docs/` POC. Move `docs_api.py` → `services/docs/api.py`; carve docs tool bodies from `server.py` → `services/docs/tools.py`. Land the fixture-discovery test in the SAME PR. | Done |
| **PAUSE** | Operator review of the docs/ shape before propagating. Verifies the import-order guarantee, the test-layout mirror, and the fixture-discovery test all behave as spec'd. | Done |
| **B** | `services/drive/` migration. Same shape as A; `sharing.py` sub-module split per §5 if `drive_api.py` exceeds the ~400 LOC threshold (current: check at migration time). | Done |
| **C** | `gas_deploy/` is already a sub-package boundary (per README:165); move to `services/gas_deploy/` with the same shape. Mostly file moves; expect minimal call-site churn. | Done |
| **D** | Final `server.py` cleanup — remove now-empty helper blocks, verify the LOC target (~500–800), update the LOC reference at the top of this sub-section. | Done |

PAUSE between A and B was deliberate — same rationale as M1a → M1b (the pattern set in Phase A flows to B/C/D; better to discover shape problems once than three times). (The restructure has since extended past docs/drive/gas_deploy to admin/sheets/slides/apps_script — see the service tag canon table in §5.2.)

### 5.2 `@workspace_tool` — canonical decorator post-M4 (v2.2.0)

M4 renames the composite tool decorator from `@gdocs_tool` to `@workspace_tool(service=...)` and adds a required `service=` parameter that tags each tool with its owning service.

**Why the rename:** when this repo adds its first non-docs Workspace service (Sheets, Slides, Gmail, Calendar — see the long-term vision note), `@gdocs_tool` would be misleading on tools that have nothing to do with Google Docs. `@workspace_tool(service=...)` carries the per-service tag explicitly and survives the expansion without another rename.

**Service tag canon** (counts are authoritatively declared in each
`services/<svc>/_expected_tools.py::EXPECTED` and enforced by the
partition test below):

| `service=` value | Tools | File |
|---|---|---|
| `"docs"` | 16 | `services/docs/tools.py` |
| `"drive"` | 10 | `services/drive/tools.py` |
| `"gas_deploy"` | 3 | `services/gas_deploy/tools.py` |
| `"admin"` | 7 | `services/admin/tools.py` (admin / introspection / auth / signed URLs) |
| `"sheets"` | 9 | `services/sheets/tools.py` |
| `"slides"` | 6 | `services/slides/tools.py` |
| `"apps_script"` | 6 | `services/apps_script/tools.py` |
| | **57 total** | |

**Where the tag lives at runtime:** `ToolAnnotations` is pydantic-backed with `extra: "allow"`, so the `service` value rides as an extra attribute on every registered tool. Access via `tool.annotations.service` from `mcp.list_tools()`. Verified by `tests/unit/services/test_tool_registration.py::test_every_tool_carries_service_annotation` + `::test_service_annotation_matches_expected_per_file_partition`.

**Deprecation window for `@gdocs_tool`:** the old name is preserved as a thin shim that emits `DeprecationWarning` and delegates to `workspace_tool(service="docs", ...)`. Planned removal in v2.2.x per Hex specialist Round 2 ("one-release deprecation window, then remove"). New code MUST use `@workspace_tool(service=..., ...)` with an explicit tag.

**The `service="admin"` judgment call:** the 7 admin tools (`gdocs_admin_audit`, `gdocs_get_signed_upload_url`, `gdocs_guide`, `gdocs_help`, `gdocs_reset_authorization`, `gdocs_server_info`, `gdocs_test_manifest`) — now in `services/admin/tools.py` — all share `service="admin"` as a single bucket. The 3-way split (`introspection` / `admin` / `auth`) was considered and rejected because it added enum values without behavioral payoff at this scale — operations parlance already treats "operates on the MCP server itself or its meta-state" as admin. Re-visit if a new tool genuinely fits one of those splits and is awkward under `"admin"`.

## 6. Test architecture impact

| Today (v2.0.6) | After Hex foundation (v2.x+) |
|---|---|
| 461 unit tests | ~500 unit tests (+ test_key_provider.py landing M1a contributes 23 of those) |
| 7 integration tests | ~20 integration tests (per-port contract tests + per-service smoke tests) |
| No property-based tests | Property-based tests immediately enabled at the port boundary (e.g. HKDF round-trip invariants over `KeyProvider`) |
| ~5% of tests use fakes against a Protocol | ~40% of tests can use fakes against ports |

The shift is from "test by mocking specific modules" (fragile to refactor) to "test by swapping adapter" (refactor-tolerant). The `InMemoryBackend` precedent (v2.1) is the proof-of-concept; M1a generalizes that pattern to `InMemoryKeyProvider`, and M2 generalizes it to a fake `GoogleAPIClient` that serves canned responses.

Concretely, the `unittest.mock.patch("googleapiclient.discovery.build")` calls scattered through `tests/unit/` go away, replaced by `with_client(FakeGoogleAPIClient(canned_responses=...))`. Less brittle, more readable, and the fakes become a documented surface that contributors can extend.

## 7. Sequencing

Milestones below are independent ship units. M1b was evaluated and skipped (see §3.4.1); the revised sequence is 4 milestones, not 5.

```
M1a (done) → M1b (skipped) → M2 → M3 → M4
   ↓             ↓             ↓     ↓     ↓
KeyProvider   Credential-    Google  per-  @gdocs_tool
LANDED        Store NOT      API-    serv  → @workspace_
v2.1.0        promoted —     Client  ice   tool rename
              transaction    Proto   fold
              script, not    (aiogoogle
              ports          swap)
```

| Milestone | Scope | Status |
|---|---|---|
| **M1a** | `KeyProvider` Protocol + 3 adapters + `InMemoryKeyProvider` for tests | LANDED v2.1.0 (PR #88) + test migration v2.1.1 (PR #90) |
| ~~**M1b**~~ | ~~`CredentialStore` decision~~ | **SKIPPED v2.1.2.** `credentials.py` is a transaction script with no credible swap candidate. See §3.4.1. |
| **M2** | `GoogleAPIClient` promoted to Protocol. Migrate consumers from `get_service` calls to `client.docs()`, `client.drive()`, etc. Real swap candidate: `aiogoogle` for async (in-flight Gmail integration needs streaming + concurrent message fetches). | In flight on `ship-d1` parallel worktree |
| **M3** | Service-layer folder restructure. Move `docs_api.py` → `services/docs/api.py`, etc. Pure file moves + import updates; no semantic change. | Done — shipped on `main` (extended to all 7 services; see §5). |
| **M4** | Rename `@gdocs_tool` → `@workspace_tool(service=...)` to reflect the multi-service reality. Backward-compat alias kept for one release. | Done — `@workspace_tool(service=...)` is the live decorator (see §5.2). |

**Why this order:** M1a was the highest-leverage / lowest-risk port (3 mechanisms already existed, ports-earn-their-keep was most visible). It established the pattern and proved the Protocol shape works for a security-critical surface. M1b was the planned soak-and-decide checkpoint; the soak surfaced that `credentials.py` doesn't fit the Hex pattern (one coherent protocol, not 4 cross-cutting concerns) and the milestone collapsed to "documented why, no code change." M2 is now the next concrete ship — `aiogoogle` is a real alternative implementation, which makes `GoogleAPIClient` the next port that earns its keep.

## 8. Research provenance

Three research agents pressure-tested this plan over the 2026-05-20 session. Key corrections that survived:

- **Research agent #1** (Hex applicability review) — initially proposed promoting `HTTPServer` and `UrlSigner` as well. Rejected for the reasons in §4; the agent's revised recommendation matches the final 4-port set in §3.
- **Research agent #2** (sequencing review) — initially proposed M1a → M2 with no PAUSE. Corrected to add the PAUSE after surfacing the "if M1a's Protocol shape is wrong, M2/M3/M4 all inherit the bug" argument. The PAUSE is now the explicit sequencing decision in §7.
- **Research agent #3** (CredentialStore deep-dive, initial pass) — initially proposed (a) (split into 3 narrow ports) as the obvious answer. Walked back to "defer to M1b" after surfacing that we don't yet have a concrete second consumer to drive the shape. The walk-back was the M1b decision criterion that was active until v2.1.2.
- **Research agent #3 re-ping** (post-M1a, v2.1.2 M1b decision) — re-pinged after M1a landed to make the M1b call. Verdict: **C — leave `credentials.py` as plain functions; SKIP M1b entirely.** The key insight that survived the round: the 4 concerns inside `get_credentials_for_user` are 4 STEPS of one coherent protocol, not 4 cross-cutting concerns to abstract. The split-into-3-ports option (B) was evaluated explicitly and each proposed port was shown to leak (port shape doesn't match the actual surface — `CredentialStore.load` needs `client_config`, `CredentialRefresher.refresh` mutates in-place, the per-user lock belongs to none of the three). Test architect noted that an `InMemoryCredentialStore` adapter wouldn't catch the interleaved-failure-mode sequence bugs (revocation-during-refresh → `clear_state` → re-raise) — a fake Protocol returning `Credentials` doesn't model the side-effect chain. Full reasoning in §3.4.1. **This decision can flip only on a concrete alternative-refresh-flow trigger** (session auth, vendor identity service, etc.) — not on speculation.

Documenting these so the next reviewer doesn't re-litigate them from scratch. The §3.4.1 re-litigation criterion is concrete: a real alternative refresh-flow implementation, not a generic "second consumer." The earlier "second consumer materializes" framing in v2.0.6 was looser; v2.1.2 sharpened it.

## 9. What this doc is not

- **Not a roadmap.** Roadmap lives in GitHub Issues + release planning. This doc only covers the foundation refactor.
- **Not a tutorial.** New-contributor onboarding lives in `CONTRIBUTING.md` + `docs/USER_GUIDE.md`. This doc assumes you already know what an MCP tool is.
- **Not immutable.** Architecture decisions are reversible. The v2.1.2 revision of §3.4 (M1b skipped) is a concrete example: the v2.0.6 cut had `CredentialStore` as port #4 with deferral language; the v2.1.2 cut documents why the port doesn't earn its keep. Future revisions should follow the same pattern — flip the decision in place, preserve the prior reasoning so reviewers can audit the change.

## 10. References

- `src/appscriptly/user_store.py` — proven Hex pattern (StorageBackend, v2.1).
- `src/appscriptly/key_provider.py` — M1a port (v2.1.0); `with_key_provider()` helper mirrors `with_backend()`.
- `src/appscriptly/credentials.py` — the transaction script that §3.4.1 explains we deliberately did NOT promote to a port.
- `src/appscriptly/google_clients.py` — chokepoint wrapper; M2 promotion in flight.
- `src/appscriptly/decorators.py` — `@gdocs_tool` (target of M4 rename).
- `taylorwilsdon/google_workspace_mcp` (GitHub) — inspiration for the per-service folder pattern.
- `docs/THREAT_MODEL.md` — security model the KeyProvider port preserves; the M1b-skipped decision in §3.4.1 means the credential refresh path keeps its current threat-model coverage unchanged.
- `CHANGELOG.md` v2.0.6 — the original session that motivated this doc; v2.1.0/v2.1.1/v2.1.2 — M1a landing + test cash-in + M1b decision.
