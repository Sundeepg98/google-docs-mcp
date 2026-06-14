# Privacy — google-docs-mcp

**Last updated:** 2026-05-19
**Audience:** end-users connecting via claude.ai, operators deploying the server, and anyone asking "what do you store and what are my rights?"

This document is a factual self-attestation grounded in the actual code. It is not legal advice. The open-source maintainer is not the data controller — the **operator who deploys this server** is. See § 7 for what that means in practice.

## 1. What we store

When you authorize the server via the Google OAuth flow, the following are persisted to a SQLite file (`/data/google-docs-mcp/user_state.db` on the Fly volume, or `~/.google-docs-mcp/user_state.db` for the stdio deployment). Every row is keyed by your Google account's `sub` claim (an opaque per-account identifier — see § 4).

The schema (per `src/appscriptly/user_store.py`):

| Column | Type | Sensitivity | What it is |
|---|---|---|---|
| `user_id` | Google `sub` (string) | Identifier — opaque, not your email | Stable per-Google-account identifier from the OAuth `id_token`. Falls back to email if `sub` is absent (rare). |
| `google_creds_json` | JSON blob | **Sensitive** | Output of `google.oauth2.credentials.Credentials.to_json()`. Contains `token` (short-lived access token), `refresh_token` (long-lived; lets us call Google APIs on your behalf), `scopes`, `expiry`, `token_uri`. See § 1.1 for the operator-secret stripping caveat. |
| `apps_script_url` | URL string | Functional | The `/exec` endpoint of the per-user Apps Script Web App. Validated to `https://script.google.com/macros/s/<id>/(exec\|dev)` only — see `_valid_gas_url` in user_store. |
| `apps_script_script_id` | string | Functional | Google Apps Script project id (lets us update vs re-create on retry). |
| `apps_script_deployment_id` | string | Functional | Versioned deployment id of the Web App. |
| `apps_script_version_number` | int | Functional | Deployment version. |
| `apps_script_content_hash` | string | Functional | sha256 of the deployed script contents; used for setup idempotency. Reveals nothing about you. |
| `apps_script_hmac_key` | 64-char hex (v2.0+) | **Sensitive** | Per-user HMAC-SHA256 key. As of v2.0c this key authenticates every POST to your Apps Script Web App: `_call_webapp` signs each request with it and your deployed `restructure.gs` verifies the signature before acting (see THREAT_MODEL.md §4 row 5). It is never logged or echoed. Compromise would let an attacker invoke restructure operations on docs you own, so it is treated as a secret. |
| `created_at` / `updated_at` | unix epoch seconds | Telemetry | First-write and last-write timestamps. No per-tool-call audit log. |

**On-disk encryption**: the SQLite file is stored in **plaintext**. Anyone with shell access to the running container (operator, Fly platform staff in an incident scenario, or anyone who compromises a credential authorized for `fly ssh console`) can read every row. If you require encryption at rest, deploy on infrastructure that provides volume-level encryption and treat that as your trust boundary.

### 1.1 Operator-secret stripping (known gap — see issue tracker)

`src/appscriptly/user_store.py::save_credentials_json` exists specifically to strip the operator's OAuth `client_id` and `client_secret` from the persisted JSON before writing. **Closed in v2.0.3** (PR #47, commit `dadb699`) — the production OAuth callback in `src/appscriptly/http_server.py` now correctly invokes `save_credentials_json(...)` rather than the un-stripping `save_state(...)`. Operators on v2.0.3 or later: a `user_state.db` leak no longer reveals the deployment's OAuth `client_secret`. Operators still running v2.0.2 or earlier: a leak today does include the operator OAuth secrets; upgrade.

## 2. What we do NOT store

- **Your document content.** Document bodies, images, and metadata transit through the server during tool calls and are sent to / received from Google's APIs. Nothing is persisted server-side after the call returns.
- **Email you send, your Gmail labels, your contacts, and Apps Script execution history.** The CASA-free scope-growth tools touch these data categories, but — exactly like document content — they only transit during a tool call and are **never persisted** server-side:
  - **Send email on your behalf** (`gmail.send`, via `gmail_send_message`): the recipient / subject / body you pass are assembled into a message and handed to Gmail's `users.messages.send`. Send-only — the server **cannot read your mailbox** (no `gmail.readonly` / `gmail.modify`); it never sees, stores, or indexes your inbox.
  - **Manage your Gmail labels** (`gmail.labels`, via `gmail_create_label` / `gmail_list_labels` / `gmail_delete_label`): label **objects** only (names + visibility). This scope cannot read message bodies and cannot relabel messages; label names transit during the call and are not persisted.
  - **Read your auto-saved "other" contacts** (`contacts.other.readonly`, via `gcontacts_list_other_contacts`): read-only access to the auto-saved contacts list; the returned names/emails/phones transit during the call and are not persisted.
  - **Read your Apps Script execution history** (`script.processes`, via `as_list_script_processes`): read-only metadata about which of your script projects' functions ran and when; not persisted.
- **Your Google profile** (name, profile picture, secondary emails, organizational data). The OAuth flow does request the `userinfo.email` scope (needed for the FastMCP JWT's `email` claim during routing), but the email is **not** persisted to `user_state.db` — only the `sub` claim is.
- **Tool-call history.** There is no per-invocation audit log. Only the row-level `updated_at` field tells you when a row last changed. The `gdocs_admin_audit` tool exposes row-level state to the operator on demand but does not record call traces.
- **Third-party analytics or telemetry.** No external requests beyond Google's APIs (Drive, Docs, Apps Script) and Anthropic's MCP transport (when running via the claude.ai connector).
- **IP addresses or request fingerprints**, beyond whatever Fly's edge proxy logs by default (out of our control; subject to Fly's own retention policy).

## 3. Data retention

There is no automatic expiry. Your row stays until one of:

- **You revoke**, via the `gdocs_reset_authorization` tool from your connector. Pass `full=True` to also clear Apps Script setup state. Backed by `user_store.clear_state(user_id)` — the row is `DELETE`d.
- **You revoke at Google's end** (Google account → Security → Third-party apps). Next tool call hits `invalid_grant` on refresh; the server then calls `user_store.clear_state(user_id)` automatically (see `credentials.py::get_credentials_for_user`).
- **You request deletion** by opening a GitHub issue against the operator's deployment. The operator can `DELETE FROM user_state WHERE user_id = ?` directly on the SQLite file.
- **The operator deletes the entire database** (e.g., during a server reset or volume rotation).

The default Fly volume persists indefinitely. There is no automated retention policy in code.

## 4. What `sub` actually is

The Google `sub` claim is an **opaque, stable, per-account-per-OAuth-client identifier**. It is **not** your email, name, or any human-readable handle. Google's OAuth docs describe it as a non-reassigned numeric string. It can be correlated with your Google identity by anyone with operator access to both this database and another database that links `sub` to email (Google itself, primarily). It cannot be reverse-engineered from outside without that join.

If you treat `sub` as personal data under your jurisdiction's definition (the GDPR's broad reading does), the items in § 1 are personal data. If you take a narrower view, only `google_creds_json` clearly is.

## 5. Data sharing and transmission

- **Google's APIs.** Every authorized tool call sends the relevant payload + your access token to `*.googleapis.com` and `script.google.com`. Limited to the OAuth scopes you consented to. With the CASA-free scope-growth tools this includes: an outbound email you compose (`gmail.send` → `gmail.googleapis.com`); Gmail label-object create/list/delete (`gmail.labels`); a read of your auto-saved "other" contacts (`contacts.other.readonly` → People API); and a read of your Apps Script execution history (`script.processes`). None of these payloads is persisted server-side (see § 2).
- **Your own Apps Script Web App.** The server POSTs to your per-user `apps_script_url`, signing each request with your `apps_script_hmac_key` (`X-MCP-Signature` + `X-MCP-Timestamp`). Your deployed `restructure.gs` verifies the signature in `doPost` before acting and rejects anything unsigned/stale/forged (v2.0c — THREAT_MODEL.md §4 row 5 CLOSED). The `/exec` endpoint is therefore authenticated, not protected by URL secrecy alone.
- **Anthropic's claude.ai infrastructure**, when running via the claude.ai connector. Your tool-call arguments and responses transit Anthropic's MCP transport. Anthropic's own privacy policy applies to that leg.
- **No other third parties.** No analytics, no error reporting SaaS, no LLM-based logging.

**Geographic location**: the reference Fly deployment lives in `bom` (Mumbai, India). Data resides on Fly's Mumbai datacenter. If you deploy this server elsewhere, your data resides wherever your Fly volume lives. There is no cross-region replication today.

## 6. Breach commitment

This is a single-author open-source project under the MIT license. There is no commercial entity, no SLA, no regulatory commitment.

What the maintainer commits to:

- Accept security disclosures via [GitHub Security Advisories](https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new).
- On confirmed breach of the reference deployment (`sundeepg98-docs-mcp.fly.dev`), post a public GitHub issue within 72 hours describing what was exposed and what users should do.
- Operators of other deployments are responsible for their own breach notification — see § 7.

What the maintainer cannot promise: regulatory compliance, certification, or any specific recovery timeline.

## 7. Operator obligations (and why the maintainer is not the data controller)

If you deploy this server for users other than yourself, **you are the data controller** for their data under GDPR / CCPA / equivalent frameworks. The open-source maintainer ships code; you decide who runs it and which users it serves. The maintainer cannot fulfill your data-controller obligations on your behalf.

You should at minimum:

- Tell your users that you operate the deployment (not the OSS maintainer).
- Honor data-subject access / deletion requests using the SQL access you have to `user_state.db`.
- Make a privacy notice available to your users — this document can serve as a starting template; adapt it for your jurisdiction.

## 8. EU GDPR and California CCPA notes

This section is **not legal advice**. Designed for GDPR / CCPA conformance, but conformance depends on the deployer's configuration and user-facing notices.

- **Lawful basis for processing** (GDPR Art. 6(1)(a)): your consent, captured in the Google OAuth flow. Withdrawing consent = revoke at Google or run `gdocs_reset_authorization`.
- **Right to access** (GDPR Art. 15 / CCPA "right to know"): your row is a single `SELECT * FROM user_state WHERE user_id = ?` away. Operator can provide a JSON dump on request.
- **Right to erasure / "right to be forgotten"** (GDPR Art. 17 / CCPA "right to delete"): `gdocs_reset_authorization` (self-serve) or operator-manual `DELETE` (request via GitHub issue).
- **Right to data portability** (GDPR Art. 20): a `user_state` row is a small JSON object; operator can export it on request. Document content itself stays in your Google Drive (you already have it).
- **Data minimization** (GDPR Art. 5(1)(c)): the schema in § 1 is the entire stored surface. We do not collect more than we need to operate.
- **Cross-border transfer**: data leaves your Google account's region only as far as the Fly deployment's region (default: Mumbai). The operator can pick a region matching their users' jurisdiction.

If you operate in the EU and your user base is EU-resident, consult a lawyer about whether you need a formal Data Processing Agreement with Google (the underlying data-processor for Drive/Docs).

## 9. Cross-references

- **`docs/THREAT_MODEL.md` § 1** — trust boundaries (outside / half-trusted / inside) and how this server's two deployment modes differ.
- **`docs/THREAT_MODEL.md` § 5** — cryptographic key inventory: which keys exist, what they protect, how they rotate.
- **`docs/RUNBOOK.md` § 3.4** — operator key-rotation procedures.
- **`SECURITY.md`** — how to report vulnerabilities (GitHub Security Advisories).
- **`src/appscriptly/user_store.py`** — authoritative schema definition (the table in § 1 is generated from this).
