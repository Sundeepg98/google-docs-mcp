# Security posture — google-docs-mcp

**Version:** v2.3.5 (PR-Δ2)
**Audience:** prospective users, downstream consumers, security reviewers

This document is the human-readable narrative companion to the
machine-readable artifacts in this repo:

| Artifact | What it is | Where it lives |
|---|---|---|
| Vulnerability disclosure policy | who to contact, what's in scope, how we handle reports | [`SECURITY.md`](../SECURITY.md) at repo root |
| Machine-readable disclosure contact | RFC 9116 `security.txt` served by the running server | `GET /.well-known/security.txt` on every deployment |
| Threat model | trust boundaries, attack surfaces, STRIDE per component, what we don't defend against | [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) |
| OWASP ASVS Level 1 self-attestation | control-by-control evaluation against ASVS L1 (V1-V14) | [`docs/asvs-level-1-checklist.md`](asvs-level-1-checklist.md) |
| OAuth Protected Resource Metadata | RFC 9728 discovery for OAuth-protected MCP resources | `GET /.well-known/oauth-protected-resource` on every deployment |
| OAuth Authorization Server Metadata | RFC 8414 discovery — auto-wired by FastMCP's `GoogleProvider` | `GET /.well-known/oauth-authorization-server` |
| Continuous posture monitoring | OpenSSF Scorecard runs in CI on every merge to `main` | [Scorecard badge](https://api.securityscorecards.dev/projects/github.com/Sundeepg98/google-docs-mcp) on README |
| Signed release artifacts | Sigstore-attested build provenance on every tag | GitHub Releases page, per-tag `.intoto.jsonl` attestation |

The rest of this doc is the narrative version of the above. If you
already trust the artifacts, skim the **Honest gaps** section and
stop.

## 1. Identity model — minimal-scope OAuth

google-docs-mcp implements Google's standard OAuth 2.0 authorization
code flow via `google-auth-oauthlib`. The deployment requests two
classes of scopes from the user:

- **Per-file access (`drive.file`)** — the canonical primary scope.
  Grants read/write access to the specific files this app creates OR
  to files the user explicitly hands the app via the Drive picker.
  This is Google's most-narrow Drive scope.

- **Read-only ingestion (`drive.readonly`)** — needed for one
  specific cloud-chat workflow: `gdocs_tab_existing_doc(drive_file_id=…)`,
  where the user passes an arbitrary Drive ID of a doc the app
  didn't create and asks for tab conversion. Without `drive.readonly`
  this call returns "404 not found" because `drive.file` doesn't see
  files this app didn't author. Mutation is impossible under this
  scope.

Apps Script management scopes (`script.projects`, `script.deployments`)
are baseline as of v2.3.4 (PR-Δ1) so the user hits ONE consent screen
on first connection. See PR-Δ1's CHANGELOG entry for the UX rationale.

**What this means in practice:**
- The server cannot read arbitrary user docs. Every doc it touches is
  either (a) one it created, (b) one the user explicitly named, or
  (c) one the user opened in the Drive picker.
- The server cannot impersonate the user against other Google services
  (Gmail, Calendar, etc.) — scopes don't authorize those APIs.

## 2. Token storage and key derivation

**Per-user OAuth tokens** are stored in SQLite at
`/data/user_state.db`, keyed by Google's `sub` claim. The volume is
private to the Fly machine — operator SSH is the only path to read
it (inside-trust threat, called out in
[THREAT_MODEL.md §3](THREAT_MODEL.md#3-attacker-model)). The token
storage path stripping operator-level OAuth `client_secret` /
`client_id` from persisted blobs is regression-pinned by
`test_oauth_callback_endpoint_strips_operator_secrets_in_production`.

**Cryptographic key derivation** uses HKDF-SHA256 (post v2.0b
strict-flip) over one operator-supplied master (`MCP_BEARER_TOKEN`),
yielding three logically-isolated derived keys:

| Purpose | Info string | Used for |
|---|---|---|
| `api_bearer` | `b"google-docs-mcp v1 api_bearer"` | Bearer header equality on `/api/*` |
| `oauth_state` | `b"google-docs-mcp v1 oauth_state"` | HMAC over OAuth callback state token |
| `signed_url` | `b"google-docs-mcp v1 signed_url"` | HMAC over signed upload URL query params |

Per-purpose isolation: leaking the derived bytes for one purpose does
NOT enable forging tokens for another. Operators who want stronger
isolation can override any single purpose's key via the matching
`MCP_API_BEARER_KEY` / `OAUTH_STATE_SIGNING_KEY` /
`SIGNED_URL_SIGNING_KEY` env var (the `KeyProvider` rotation pattern;
see [`src/google_docs_mcp/key_provider.py`](../src/google_docs_mcp/key_provider.py)).

Rotation procedures live in [`docs/RUNBOOK.md`](RUNBOOK.md) § 3.4.

## 3. Standards-compliant OAuth discovery

The server publishes the OAuth metadata endpoints the MCP
Authorization spec and the broader OAuth 2.0 ecosystem expect:

- **RFC 8414** — `/.well-known/oauth-authorization-server`. Auto-wired
  by FastMCP's `GoogleProvider`. Describes the authorization server
  this resource trusts (a thin proxy in front of Google).
- **RFC 9728** — `/.well-known/oauth-protected-resource`. Added in
  PR-Δ1 (v2.3.4). Describes THIS resource server: surfaced
  `scopes_supported`, `bearer_methods_supported: ["header"]` (we
  deliberately don't implement RFC 6750 §2.2/§2.3 query-string or
  POST-body bearer presentation), and an `authorization_servers`
  pointer.
- **RFC 9116** — `/.well-known/security.txt`. Added in PR-Δ2
  (v2.3.5). Machine-readable vulnerability disclosure contact.

The `bearer_methods_supported: ["header"]` posture is pinned by
`test_oauth_protected_resource_response_carries_rfc9728_required_fields`
so a future "let me add query-string bearer for convenience" change
fails CI and forces a security review.

## 4. Architectural bounded blast radius

A central property documented in detail at
[THREAT_MODEL.md §8](THREAT_MODEL.md#8-bounded-blast-radius--architectural-callout-pr-2):

> **No single token, key, or credential held by the server grants
> cross-user access.**

Per-user OAuth tokens, per-user signed-URL HMAC binding (the `uid`
embedded in the signing tuple), per-user Apps Script Web App
deployments, and per-purpose key derivation together mean that an
attacker who compromises any one credential cannot pivot to harvest
all users' docs. The compromise blast radius is bounded to that
single user's state.

The architectural property doesn't replace per-control mitigations;
it makes "what if X is leaked" reasoning concrete and bounded.

## 5. Continuous posture monitoring

- **OpenSSF Scorecard** runs in CI on every merge to `main` and
  publishes a badge on the README. Scorecard evaluates dependency
  pinning, branch protection, CI hygiene, security policy presence,
  signed releases, and more.

- **CodeQL** runs on every PR via `.github/workflows/codeql.yml`. Any
  CWE-tagged finding fails the build.

- **pip-audit** runs against the locked dependency graph
  (`uv export --format requirements-txt | pip-audit --strict`) via
  the e2e workflow. HIGH/CRITICAL CVEs fail the build; the one current
  documented non-applicability (PYSEC-2025-183 / pyjwt) is captured
  in `SECURITY.md` with the canonical re-audit trigger.

- **Sigstore-signed releases** (PR-Δ2) — every tagged release ships
  with an `actions/attest-build-provenance@v2` attestation, letting
  downstream consumers verify provenance with `gh attestation verify`.

## 6. Self-attestation, not third-party audit

This codebase is self-attested against
[OWASP ASVS Level 1](https://owasp.org/www-project-application-security-verification-standard/)
(see [`docs/asvs-level-1-checklist.md`](asvs-level-1-checklist.md)).
ASVS L1 is the **minimum baseline** the OWASP ASVS framework defines —
appropriate for "low-assurance" applications, which is our honest
self-classification given the operational scope (small open-source MCP
server with bounded per-user blast radius). It is NOT a substitute for
a paid third-party audit or a CASA assessment.

We do not claim:
- SOC 2, ISO 27001, or other formal certifications
- CASA Tier 2 or Tier 3 compliance (we run in Google's OAuth Testing
  Mode bypass; not a present concern for our user base — see
  [THREAT_MODEL.md §9](THREAT_MODEL.md#9-what-we-currently-dont-defend-against-honest-section-pr-2) row 8)
- Penetration test attestation (no budget for paid pen-test)
- ASVS Level 2 or Level 3 (would require multi-factor auth flows,
  defense-in-depth controls we don't implement)

If/when these are commissioned, results will be published here.

## 7. Honest gaps

[THREAT_MODEL.md §9](THREAT_MODEL.md#9-what-we-currently-dont-defend-against-honest-section-pr-2)
enumerates 8 currently-open gaps with rationale + planned closure.
The most operationally-significant:

- **Rate limiting** (PR-Δ3 closes) — `/api/convert`, `/info`, and
  `/oauth/google/api/callback` have no in-process rate limit today.
  Fly's edge proxy provides per-IP limits at the platform layer.
- **Apps Script HMAC verify-path** (v2.0c closes) — Per-user
  `apps_script_hmac_key` column exists but is unused. URL secrecy
  is the access control today.
- **`drive.readonly` scope** (not planned to remove) — required for
  the cloud-chat ingestion UX; mutation impossible under this scope.

If your threat model requires closure of any of these BEFORE
adoption, please flag it via a private GitHub Security Advisory
(see SECURITY.md) so we can prioritize.

## 8. Reporting issues

See [`SECURITY.md`](../SECURITY.md) for the full vulnerability
disclosure policy. The short version: **don't file a public issue
for security bugs.** Open a private advisory at
<https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new>;
acknowledged within 7 days.
