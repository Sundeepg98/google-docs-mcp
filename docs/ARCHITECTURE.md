# Architecture — Hex Foundation for the Google Workspace MCP

**Version:** v2.0.6 (foundation in flight; M1a POC on `ship/key-provider-port-poc`)
**Last updated:** 2026-05-20
**Audience:** maintainers, reviewers of refactor PRs, contributors adding a new Google service

## 1. Context

`google-docs-mcp` is being expanded into a multi-service Google Workspace MCP. Current scope (v2.0.x): Docs + Drive + Apps Script. Target scope (v2.x → v3.x): Sheets, Slides, Gmail, Calendar, and Drive sharing as first-class peers of Docs.

The pre-expansion codebase has a single-service shape — most modules live flat under `src/google_docs_mcp/` and Google API access is open-coded at the call site (`build("docs", "v1", ...)`). That shape was fine while there was one service. With five services landing over the next several releases, it becomes the wrong shape: every new service either duplicates wiring or threads through the Docs-shaped path.

The operator's intent (recorded in session: 2026-05-20): apply Hex/Ports/Adapters at the infrastructure seam where multiple implementations already exist or are imminent. **Do not** apply it at the service surface, where each service is naturally service-specific and abstraction would be cargo-culted.

This doc captures the rationale so future contributors don't have to re-derive it from session logs.

## 2. Decision

> **Hex at the core infrastructure layer. Pragmatic per-service folders at the services layer.**

Two layers, two design philosophies, one codebase.

| Layer | Philosophy | Why |
|---|---|---|
| Core infrastructure (`src/google_docs_mcp/`) | Ports + Adapters | Multiple implementations already exist (or are imminent) for storage, credentials, key derivation, Google API access. Hex isolates the swap. |
| Service layer (`src/google_docs_mcp/services/<service>/`) | Per-service folders, no shared abstraction | Each Google service has a distinct API surface (Docs vs Gmail vs Drive). Sharing structure across them is premature; mirroring service boundaries in code aids navigability. |

The split is deliberate. Hex earns its keep only when there's a real swap to defend; folder layout earns its keep when there's real navigation pressure. Different problems, different tools.

## 3. Infrastructure ports promoted (priority order)

The 4 ports below are the candidate set. Promotion order is by **risk × evidence-of-need**, not alphabet.

### 3.1 `StorageBackend` — ALREADY PROVEN (v2.1)

- **Status:** Shipped in v2.1 (PR #39). Production-proven.
- **File:** `src/google_docs_mcp/user_store.py`
- **Shape:** `Protocol` with 4 methods (`get_state`, `save_state`, `delete_state`, `iter_users`).
- **Adapters:** `SqliteBackend` (default), `InMemoryBackend` (test ergonomics).
- **Why it earned its keep:** the test suite uses `InMemoryBackend` and the production deploy uses `SqliteBackend`, with zero test scaffolding leaking into prod code. A future `PostgresBackend` is a single new class, not a rewrite.
- **Reference for other ports:** the StorageBackend shape (small surface, runtime-checkable Protocol, module-level `_backend` swappable via `with_backend()` context manager) is the template. New ports should match this shape unless there's a concrete reason to diverge.

### 3.2 `GoogleAPIClient` — CHOKEPOINT EXISTS

- **Status:** Wrapper module shipped in v2.6a (PR #48); call-site migration in PR #70/#71/#75 (v2.6b). Promotion to Protocol pending.
- **File:** `src/google_docs_mcp/google_clients.py`
- **Today:** thin passthrough wrapper around `googleapiclient.discovery.build`. Enforced as the sole import surface via TID251 lint.
- **Why promote:** the chokepoint already exists; promoting to a Protocol enables:
  - swapping in `aiogoogle` for async (concrete future requirement — see in-flight Gmail integration, which needs streaming + concurrent message fetches).
  - per-call retry wrappers, per-call logging, per-call test doubles without touching consumers.
  - centralizing the per-user `creds` injection that's currently re-derived at every call site.
- **Risk:** LOW. The chokepoint is already enforced; promoting to Protocol is a refinement, not a restructure.

### 3.3 `KeyProvider` — 3 MECHANISMS ALREADY, M1a POC IN FLIGHT

- **Status:** POC on branch `ship/key-provider-port-poc` (M1a milestone). 493 LOC in `key_provider.py`, 331 LOC of unit tests, `keys.py` rewired against the Protocol.
- **File (POC):** `src/google_docs_mcp/key_provider.py`
- **Three mechanisms already coexist:**
  1. Raw master via `MCP_BEARER_TOKEN` env (legacy shim, removed in v2.0b strict-flip).
  2. Per-purpose env override (`MCP_API_BEARER_KEY`, `OAUTH_STATE_SIGNING_KEY`, `SIGNED_URL_SIGNING_KEY` — v1.5.1).
  3. HKDF-SHA256 derivation from the master (default post-v2.0b).
- **Why promote:** three implementations already coexist in `keys.py` as branching `if` chains. The Protocol replaces the branching with adapter selection at boot; new mechanisms (e.g. KMS-backed keys for the SaaS deploy variant) become new adapters instead of new `if` branches.
- **Risk:** MEDIUM. The keys path is security-critical; the POC must demonstrate behavioral equivalence before promotion past M1a.

### 3.4 `CredentialStore` — RISKIEST, DECISION DEFERRED TO M1b

- **Status:** UNDECIDED. Will be re-evaluated after M1a (KeyProvider POC) ships and we've absorbed the lessons.
- **Today:** `credentials.py` (stdio mode, single-user) + per-user credential resolution in `http_server.py` (multi-tenant mode) + `setup_state.py` (operator's deploy-time scratchpad). Three call patterns, one logical concept.
- **Two candidate shapes:**
  - **(a)** Split into 3 narrow ports — `OperatorCredentialStore`, `UserCredentialStore`, `SetupCredentialStore` — each modeling one of the three call patterns above. Higher upfront cost, clear separation.
  - **(b)** Leave as plain functions, do nothing. The current shape is messy but works; promoting it to a Protocol may be premature abstraction if the next service doesn't actually need the swap.
- **Decision criterion (set at M1b, NOT before):** does the Gmail or Sheets integration need a non-default credential adapter? If yes → (a). If no → (b). Defer until we have one real second consumer; until then we're guessing.
- **Why this is the RISKIEST port:** credential handling is where bugs become security incidents. Premature Protocol promotion that ships with a subtly wrong default is worse than the current open-coded approach.

## 4. Infrastructure NOT promoted (YAGNI)

The 2 candidates below were considered and rejected. Documenting them so future contributors don't waste a round re-evaluating.

### 4.1 `HTTPServer` — NOT PROMOTED

- **Today:** `http_server.py` wires Starlette routes + middleware directly.
- **Why not:** Starlette IS FastAPI's foundation. There is no credible swap target — moving to FastAPI would inherit Starlette anyway; moving to aiohttp / Quart / litestar requires a full rewrite that no current requirement motivates. Abstracting now is abstraction without alternative.
- **Re-evaluate if:** a concrete reason to swap appears (e.g. a SaaS deployment variant that requires a non-Starlette framework). Until then, the current shape is correct.

### 4.2 `UrlSigner` — NOT PROMOTED

- **Today:** `crypto.py` ships a single HMAC-SHA256 signer with a fixed canonical-string format coupled to the `/api/convert` URL schema.
- **Why not:** the canonical-string format is tightly coupled to what we're signing (per-user signed-upload URLs). A Protocol over this would have one implementation forever, and the "swap" point (changing the canonical-string format) is a breaking change to the signed-URL contract — i.e. a versioning concern, not an adapter-selection concern.
- **Re-evaluate if:** we ever add a second class of signed URL (e.g. signed download URLs, signed share URLs) with a different canonical-string format. The natural shape at that point is one signer per URL class, not one Protocol over all of them.

## 5. Service-layer pattern

Per-service folder shape, inspired by `taylorwilsdon/google_workspace_mcp`:

```
src/google_docs_mcp/services/
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

Milestones below are independent ship units. PAUSE between M1a and M1b is deliberate.

```
M1a (in flight) → PAUSE → M1b → M2 → M3 → M4
   ↓                ↓        ↓     ↓     ↓     ↓
KeyProvider     decide    Cred-   Google  per-  @work-
POC             on Cred-  Store   API-    serv  space_
                Store     adopt   Client  ice   tool
                shape     (if a)  Proto   fold  rename
                                  promot  ers
```

| Milestone | Scope | Status |
|---|---|---|
| **M1a** | `KeyProvider` Protocol + 3 adapters + `InMemoryKeyProvider` for tests | POC on `ship/key-provider-port-poc` — landing as v2.1.0 |
| **PAUSE** | Soak M1a in production. Validate the Hex pattern works for a security-critical port before committing to more. | Triggered post-M1a merge |
| **M1b** | `CredentialStore` decision (split-into-3 vs leave-as-functions). Driven by what the next service actually needs. | Pending M1a + first second-service POC |
| **M2** | `GoogleAPIClient` promoted to Protocol. Migrate consumers from `get_service` calls to `client.docs()`, `client.drive()`, etc. | After M1a/M1b lessons absorbed |
| **M3** | Service-layer folder restructure. Move `docs_api.py` → `services/docs/api.py`, etc. Pure file moves + import updates; no semantic change. | After M2 |
| **M4** | Rename `@gdocs_tool` → `@workspace_tool` to reflect the multi-service reality. Backward-compat alias kept for one release. | After M3 — cosmetic capstone |

**Why this order:** M1a is the highest-leverage / lowest-risk port (3 mechanisms already exist, ports earn-their-keep is most visible). It establishes the pattern. Everything after M1a is conditional on M1a actually working in production — if the POC reveals a problem with the Protocol shape, that problem flows to all later ports, and we'd rather discover it once.

## 8. Research provenance

Three research agents pressure-tested this plan over the 2026-05-20 session. Key corrections that survived:

- **Research agent #1** (Hex applicability review) — initially proposed promoting `HTTPServer` and `UrlSigner` as well. Rejected for the reasons in §4; the agent's revised recommendation matches the final 4-port set in §3.
- **Research agent #2** (sequencing review) — initially proposed M1a → M2 with no PAUSE. Corrected to add the PAUSE after surfacing the "if M1a's Protocol shape is wrong, M2/M3/M4 all inherit the bug" argument. The PAUSE is now the explicit sequencing decision in §7.
- **Research agent #3** (CredentialStore deep-dive) — initially proposed (a) (split into 3 narrow ports) as the obvious answer. Walked back to "defer to M1b" after surfacing that we don't yet have a concrete second consumer to drive the shape. The walk-back is the M1b decision criterion in §3.4.

Documenting these so the next reviewer doesn't re-litigate them from scratch. If the criteria in §3.4 (concrete second consumer) materialize, the M1b decision can flip; the rest of this plan stays.

## 9. What this doc is not

- **Not a roadmap.** Roadmap lives in GitHub Issues + release planning. This doc only covers the foundation refactor.
- **Not a tutorial.** New-contributor onboarding lives in `CONTRIBUTING.md` + `docs/USER_GUIDE.md`. This doc assumes you already know what an MCP tool is.
- **Not immutable.** Architecture decisions are reversible. If M1a's POC reveals the Protocol shape is wrong, this doc gets revised — preferably with a §X "what changed and why" section so the prior thinking is preserved.

## 10. References

- `src/google_docs_mcp/user_store.py` — proven Hex pattern (StorageBackend).
- `src/google_docs_mcp/key_provider.py` (on branch `ship/key-provider-port-poc`) — M1a POC.
- `src/google_docs_mcp/google_clients.py` — chokepoint wrapper (pre-M2).
- `src/google_docs_mcp/decorators.py` — `@gdocs_tool` (target of M4 rename).
- `taylorwilsdon/google_workspace_mcp` (GitHub) — inspiration for the per-service folder pattern.
- `docs/THREAT_MODEL.md` — security model the KeyProvider + CredentialStore ports must preserve.
- `CHANGELOG.md` v2.0.6 — the consolidated session that motivated this doc.
