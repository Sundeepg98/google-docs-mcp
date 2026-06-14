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

## 1. Identity model: sensitive-scope-only OAuth (no restricted scopes)

google-docs-mcp implements Google's standard OAuth 2.0 authorization
code flow via `google-auth-oauthlib`. The single source of truth for the
consent set is `auth.py:WORKSPACE_SCOPES` (11 Workspace scopes) plus the
two OIDC identity scopes in `oauth_google.py:IDENTITY_SCOPES`, unioned as
`oauth_google.py:GOOGLE_API_SCOPES` (13 scopes total for the HTTP/cloud
connector; stdio mode is the same minus the two identity scopes).

**Every requested scope is a Google SENSITIVE scope; NONE is RESTRICTED.**
This is the load-bearing posture property: only restricted scopes (full
Gmail, full Drive, `drive.readonly`, Fit, Chat, etc.) trigger a CASA
security assessment, so a sensitive-only set qualifies for OAuth
verification without CASA.

The Drive access the app holds is the per-file scope only:

- **Per-file access (`drive.file`)**: the canonical Drive scope. Grants
  read/write access to the specific files this app creates OR to files
  the user explicitly hands the app via the Drive picker. This is
  Google's most-narrow Drive scope, and it is SENSITIVE, not restricted.

The remaining scopes are the per-service sensitive scopes for the
features the app exposes: `documents`, `spreadsheets`, `presentations`,
`forms.body`, `forms.responses.readonly`, `tasks`, `calendar`,
`contacts`, plus the Apps Script management scopes (`script.projects`,
`script.deployments`, baseline as of v2.3.4 / PR-Δ1 so the user hits ONE
consent screen on first connection; see PR-Δ1's CHANGELOG entry for the
UX rationale).

`drive.readonly` is **NOT** requested. It is the only RESTRICTED scope
this app ever carried, and it was deliberately removed from the base
tier to keep the no-CASA posture; its two former consumers were
re-plumbed onto Drive-read-free paths (signed-URL upload for `.docx`
ingestion; signed staging endpoint for the slides-to-video frame
handoff). See `auth.py:WORKSPACE_SCOPES` for the full removal rationale.

> **Verification posture.** The OAuth verification round currently under
> review covers the base set (Docs / Drive.file / Sheets / Slides / Apps
> Script + identity). The additional sensitive services (Calendar, Tasks,
> Forms, Contacts) reach existing users through Google's incremental-
> consent flow and their live rollout is held by the CI deploy gate
> (`DEPLOY_ENABLED=false`) until their own verification round (verify
> LAST). `WORKSPACE_SCOPES` therefore lists the full target set in code
> while the consent screen under review shows the already-submitted
> subset. All of the additional scopes are sensitive, none restricted,
> so they add no CASA requirement.

**What this means in practice:**
- The server cannot read arbitrary user docs. Every Drive file it
  touches is either (a) one it created, (b) one the user explicitly
  named, or (c) one the user opened in the Drive picker. There is no
  whole-Drive read scope in the set.
- The app does request per-service scopes for Calendar, Tasks, Forms,
  and Contacts (all sensitive); these authorize only those specific
  Google APIs and only for the signed-in user. It does NOT request any
  Gmail scope.

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
see [`src/appscriptly/key_provider.py`](../src/appscriptly/key_provider.py)).

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

(Note: an earlier revision of this section listed `drive.readonly` as a
retained restricted scope. It has since been DROPPED from the base tier
to preserve the sensitive-scope-only / no-CASA posture; the cloud-chat
ingestion path was re-plumbed onto the signed-URL upload flow. See §1 and
`auth.py:WORKSPACE_SCOPES`. It is no longer an open item.)

If your threat model requires closure of any of these BEFORE
adoption, please flag it via a private GitHub Security Advisory
(see SECURITY.md) so we can prioritize.

## 8. Reporting issues

See [`SECURITY.md`](../SECURITY.md) for the full vulnerability
disclosure policy. The short version: **don't file a public issue
for security bugs.** Open a private advisory at
<https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new>;
acknowledged within 7 days.
