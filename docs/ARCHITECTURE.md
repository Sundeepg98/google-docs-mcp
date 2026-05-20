# Architecture — Hex Foundation for the Google Workspace MCP

**Version:** v2.1.2 (M1a landed v2.1.0/v2.1.1; M1b skipped; M2 in flight on `ship-d1`)
**Last updated:** 2026-05-20 (v2.1.2 M1b-skipped revision)
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

### 3.3 `KeyProvider` — LANDED v2.1.0 (M1a complete)

- **Status:** Shipped in v2.1.0 (PR #88). Consumer test migration + HKDF golden-value regression landed in v2.1.1 (PR #90).
- **File:** `src/google_docs_mcp/key_provider.py` — Protocol + 3 adapters + `InMemoryKeyProvider` for tests + `with_key_provider()` context manager (mirrors the `with_backend()` shape from v2.1).
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
| **M3** | Service-layer folder restructure. Move `docs_api.py` → `services/docs/api.py`, etc. Pure file moves + import updates; no semantic change. | After M2 |
| **M4** | Rename `@gdocs_tool` → `@workspace_tool(service=...)` to reflect the multi-service reality. Backward-compat alias kept for one release. | After M3 — cosmetic capstone |

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

- `src/google_docs_mcp/user_store.py` — proven Hex pattern (StorageBackend, v2.1).
- `src/google_docs_mcp/key_provider.py` — M1a port (v2.1.0); `with_key_provider()` helper mirrors `with_backend()`.
- `src/google_docs_mcp/credentials.py` — the transaction script that §3.4.1 explains we deliberately did NOT promote to a port.
- `src/google_docs_mcp/google_clients.py` — chokepoint wrapper; M2 promotion in flight.
- `src/google_docs_mcp/decorators.py` — `@gdocs_tool` (target of M4 rename).
- `taylorwilsdon/google_workspace_mcp` (GitHub) — inspiration for the per-service folder pattern.
- `docs/THREAT_MODEL.md` — security model the KeyProvider port preserves; the M1b-skipped decision in §3.4.1 means the credential refresh path keeps its current threat-model coverage unchanged.
- `CHANGELOG.md` v2.0.6 — the original session that motivated this doc; v2.1.0/v2.1.1/v2.1.2 — M1a landing + test cash-in + M1b decision.
