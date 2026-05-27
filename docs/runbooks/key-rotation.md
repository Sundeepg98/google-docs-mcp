# Key Rotation Runbook

**Owner:** operator (Sundeep)
**Audience:** future-operator (you, 6 months from now)
**Last reviewed:** 2026-05-27 (PR-Δ3)
**Status:** authoritative — supersedes any earlier rotation notes in `RUNBOOK.md`.

This document covers rotation of every long-lived secret the server depends on. The keys live in three places (Fly secrets, the per-user state DB, Google Cloud Console); each section below is self-contained — start at the section for the secret you're rotating.

## TL;DR — which key am I rotating?

| Secret | Lives in | Rotate when | Section |
|---|---|---|---|
| `MCP_BEARER_TOKEN` | Fly secret | Quarterly, or on suspected leak. **THIS IS THE MASTER** — bearer key + every HKDF-derived per-purpose key changes when it rotates. | [1. MCP_BEARER_TOKEN (HKDF master)](#1-mcp_bearer_token-hkdf-master) |
| `GOOGLE_CLIENT_CONFIG` | Fly secret | Annually, or on OAuth client compromise (Google Cloud Console). | [2. OAuth client secret](#2-oauth-client-secret) |
| `FLY_API_TOKEN` | GitHub secret | Per-deploy-actor rotation; annual cycle. Not user-data adjacent. | [3. FLY_API_TOKEN](#3-fly_api_token) |
| Per-purpose override (`OAUTH_STATE_SIGNING_KEY`, `SIGNED_URL_SIGNING_KEY`, `MCP_API_BEARER_KEY`) | Fly secret (rare) | Only during a controlled cutover of `MCP_BEARER_TOKEN` to avoid invalidating in-flight tokens. | [4. Per-purpose overrides (during master rotation)](#4-per-purpose-overrides-during-master-rotation) |
| Per-user OAuth tokens | SQLite (`/data/google-docs-mcp/user_state.db`) | Never manually — they refresh on Google's clock or invalidate on user-side revocation. | [5. Per-user OAuth tokens (FYI)](#5-per-user-oauth-tokens-fyi) |

## 1. `MCP_BEARER_TOKEN` (HKDF master)

### What it does

`MCP_BEARER_TOKEN` is the **single master input** to `src/google_docs_mcp/keys.py`'s HKDF derivation. Per purpose (`api_bearer`, `oauth_state`, `signed_url`), `keys.get_key(purpose)` derives 32 bytes deterministically:

```
derived = HKDF-SHA256(
    salt   = static per-purpose 32-byte constant,
    ikm    = MCP_BEARER_TOKEN (UTF-8 bytes),
    info   = b"google-docs-mcp/" + purpose,
    length = 32,
)
```

This means:

- **Rotating `MCP_BEARER_TOKEN` rotates every derived key simultaneously.** Any in-flight signed URL, OAuth state token, or bearer-authenticated request signed with the old master immediately fails verification when the new master takes effect.
- The bearer token used by the operator (in the `Authorization: Bearer ...` header for direct/admin calls) is **also** `MCP_BEARER_TOKEN` — the same value serves dual duty as both HKDF master and the operator's API password.

### When to rotate

| Trigger | Urgency |
|---|---|
| **Suspected leak** (token committed to git, posted in a logfile, screenshare leak) | Immediate. Skip the [graceful cutover](#graceful-cutover-zero-downtime-for-in-flight-tokens); revoke now, accept the in-flight-token disruption. |
| Quarterly cadence (no incident) | Schedule a maintenance window; use the graceful cutover. |
| Cryptographic floor change (e.g. bumping `_MIN_MASTER_LEN` in `keys.py`) | Same as quarterly. |

### Pre-rotation checklist

- [ ] You have shell access to a host with `flyctl` authenticated to the `sundeepg98-docs-mcp` app.
- [ ] You have a fresh entropy source for the new token (system `openssl rand -base64 48`, NOT a wordlist).
- [ ] You have the `docs/runbooks/key-rotation.md` open (this file).
- [ ] You have a way to notify any **operator-side clients** (curl scripts, Bruno collections) that the bearer token is changing — they need the new value to keep authenticating.
- [ ] User-side cloud-chat sessions DO NOT need notification; their signed URLs invalidate, they hit `/api/convert`, get a 401 + auth_url, and the connector re-mints. UX-wise this looks like a transient hiccup, not a logout.

### Graceful cutover (zero downtime for in-flight tokens)

> **TL;DR**: pin every derived key to its current value via per-purpose env-var overrides, swap the master, then unset the overrides one purpose at a time on a cadence that lets the longest-lived tokens age out.

The trick: `keys.get_key(purpose)` looks at the per-purpose env-var override FIRST (`MCP_API_BEARER_KEY`, `OAUTH_STATE_SIGNING_KEY`, `SIGNED_URL_SIGNING_KEY`) and only falls back to HKDF derivation when the override is absent. Pinning each purpose to its current derived value lets you swap `MCP_BEARER_TOKEN` without changing what the verifier sees for in-flight tokens.

1. **Capture the current derived keys.** SSH into a Fly machine (or run locally with the current master in env):
   ```sh
   python -c "from google_docs_mcp import keys; \
     print('api_bearer:', keys.get_key('api_bearer').hex()); \
     print('oauth_state:', keys.get_key('oauth_state').hex()); \
     print('signed_url:', keys.get_key('signed_url').hex())"
   ```
   Write these three values down securely. **They are bearer-equivalent secrets.**

2. **Set the per-purpose overrides** (Fly secrets). The override env vars accept hex strings via `bytes.fromhex(os.environ['...'])` — see `keys.py:_purpose_override_bytes`.
   ```sh
   flyctl secrets set \
     MCP_API_BEARER_KEY=<hex-from-step-1> \
     OAUTH_STATE_SIGNING_KEY=<hex-from-step-1> \
     SIGNED_URL_SIGNING_KEY=<hex-from-step-1> \
     -a sundeepg98-docs-mcp
   ```
   This triggers a deploy. After the rolling restart, derivation is bypassed; `get_key()` returns the override bytes. **Functionally a no-op for verifiers** because the override values ARE the previous derived values.

3. **Swap `MCP_BEARER_TOKEN`.** Generate the new master and set it:
   ```sh
   NEW_MASTER=$(openssl rand -base64 48)
   flyctl secrets set MCP_BEARER_TOKEN="${NEW_MASTER}" -a sundeepg98-docs-mcp
   ```
   Another rolling restart. The new master is the API-bearer for operator clients NOW. Distribute it to operator-side scripts/clients. Derived keys still come from the overrides — no in-flight token invalidation.

4. **Wait for in-flight tokens to age out.** TTLs:
   - **Signed URLs** (`signed_url` purpose): per-URL TTL is `crypto.DEFAULT_TTL_SECONDS = 600` (10 min), max 1 hour. Wait **at least 1 hour** to guarantee no v1 signed URL is in flight.
   - **OAuth state tokens** (`oauth_state` purpose): TTL is 10 minutes. Wait at least 10 minutes.
   - **Bearer tokens** (`api_bearer` purpose): no TTL — they're long-lived API passwords. Override-removal here invalidates any client still using the previous bearer.

   Run the preflight script to confirm zero in-flight usage of the shim path:
   ```sh
   ./scripts/preflight_strict_flip.sh
   ```
   This reads `gdocs_server_info().key_back_compat_shim_active_hits` and refuses to advise the flip if any purpose is non-zero in the last soak window.

5. **Remove overrides one purpose at a time.** Order: longest-TTL first (signed_url) so the bulk of the cutover happens behind the hour-long signed-URL aging window, then oauth_state (10 min), then api_bearer (operator-coordinated):
   ```sh
   flyctl secrets unset SIGNED_URL_SIGNING_KEY -a sundeepg98-docs-mcp
   # wait for deploy + a few minutes of soak time, monitor logs
   flyctl secrets unset OAUTH_STATE_SIGNING_KEY -a sundeepg98-docs-mcp
   # wait again
   flyctl secrets unset MCP_API_BEARER_KEY -a sundeepg98-docs-mcp
   ```
   After each unset, `keys.get_key(purpose)` falls back to HKDF derivation from the **new** master. From this point on, every newly-minted token of this purpose is signed with the new derived key.

6. **Verify.** Run a sanity check from a fresh shell:
   ```sh
   # New bearer: should return 200
   curl -fsS -H "Authorization: Bearer ${NEW_MASTER}" \
     https://sundeepg98-docs-mcp.fly.dev/api/info
   # Old bearer: should return 401
   curl -sS -H "Authorization: Bearer ${OLD_MASTER}" \
     https://sundeepg98-docs-mcp.fly.dev/api/info | jq .
   ```

### Emergency rotation (suspected leak)

Skip the graceful cutover. In-flight tokens are collateral damage — accepting a few minutes of "users see 401, hit re-auth" is the price of stopping ongoing exploitation.

```sh
NEW_MASTER=$(openssl rand -base64 48)
flyctl secrets set MCP_BEARER_TOKEN="${NEW_MASTER}" -a sundeepg98-docs-mcp
# DO NOT set the per-purpose overrides. Let derivation switch immediately.
```

After deploy, every in-flight token (signed URL, OAuth state) fails HMAC verify and the caller gets 401 + auth_url to re-mint. Per-user OAuth tokens themselves are **unaffected** — they're stored encrypted by a per-user key, not by the HKDF master, and refresh against Google normally.

Communicate the breach + the disruption window to any operator-side client.

## 2. OAuth client secret

### What it does

`GOOGLE_CLIENT_CONFIG` is a JSON blob with the OAuth `client_id` + `client_secret` for our Google Cloud Console OAuth client. It's used in:

- The OAuth authorization-code flow (`oauth_google.py`).
- Refresh-token exchanges (`credentials.py`).

It does **not** sign user tokens or signed URLs — that's `MCP_BEARER_TOKEN`'s job. A leaked `GOOGLE_CLIENT_CONFIG` enables an attacker to impersonate **our app** in OAuth flows, but each victim still has to consent — phishing-amplification risk, not silent account takeover.

### When to rotate

- Annually, by convention.
- Immediately if Google flags the client secret as exposed (they scan public GitHub).
- After any incident where we suspect the secret was disclosed.

### Procedure

1. **Generate a new client secret in Google Cloud Console.** Project `[your-google-cloud-project]`, → APIs & Services → Credentials → click the OAuth 2.0 Client ID → "Add Secret". This creates the new secret alongside the old one; both are valid until you delete the old.

2. **Build the new client-config JSON** locally, swapping the new `client_secret` in:
   ```json
   {
     "web": {
       "client_id": "<unchanged>",
       "client_secret": "<NEW-VALUE>",
       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
       "token_uri": "https://oauth2.googleapis.com/token",
       "redirect_uris": ["https://sundeepg98-docs-mcp.fly.dev/oauth/callback"]
     }
   }
   ```

3. **Set the Fly secret.** The blob is single-quoted to survive shell escaping:
   ```sh
   flyctl secrets set GOOGLE_CLIENT_CONFIG="$(cat new-client-config.json)" \
     -a sundeepg98-docs-mcp
   ```
   Rolling deploy. New OAuth code-exchanges and refresh calls use the new secret.

4. **Verify**: trigger a fresh OAuth login flow from a test browser. Watch the deploy logs for `oauth callback success` and confirm `gdocs_server_info` still reports a healthy user count.

5. **Delete the old secret in Google Cloud Console.** Wait at least 1 hour after step 3 so any in-flight refresh calls have completed against the old value. Then in the same credentials page, hover the old secret row → trash icon.

### What about user refresh tokens?

User refresh tokens are minted server-side by Google and stored per-user in `user_state.db`. They're **not** tied to a specific client_secret — they're tied to the `client_id`. Rotating `client_secret` does NOT invalidate refresh tokens. User sessions survive.

If you ever rotate the `client_id` (different procedure — requires re-publishing the OAuth consent screen), every user has to re-authorize. That's a much larger user-disruption event; track it as a separate incident.

## 3. `FLY_API_TOKEN`

### What it does

GitHub Actions uses `FLY_API_TOKEN` to (a) push the built image to `registry.fly.io` and (b) trigger the `flyctl deploy --image ...` call. It's an org/app-scoped Fly token, NOT a personal token.

### When to rotate

- Annually.
- When the actor who created the token leaves the project.
- When GitHub flags it as exposed.

### Procedure

1. Generate a new deploy token:
   ```sh
   flyctl tokens create deploy -a sundeepg98-docs-mcp -j | jq -r .token
   ```
2. Pipe directly into `gh secret set`:
   ```sh
   flyctl tokens create deploy -a sundeepg98-docs-mcp -j \
     | jq -r .token \
     | gh secret set FLY_API_TOKEN
   ```
   Direct pipe so the token never appears in shell history.
3. Test by pushing an empty commit to a PR branch:
   ```sh
   git commit --allow-empty -m "ci: smoke-test new FLY_API_TOKEN" && git push
   ```
   The deploy job triggers on push to main only, so for a true end-to-end check, merge a no-op PR; otherwise inspect that the build+push step succeeds in the PR's `deploy.yml` workflow run.
4. Revoke the old token:
   ```sh
   flyctl tokens list -a sundeepg98-docs-mcp
   # find the old token ID
   flyctl tokens revoke <token-id>
   ```

## 4. Per-purpose overrides (during master rotation)

These three env vars (`MCP_API_BEARER_KEY`, `OAUTH_STATE_SIGNING_KEY`, `SIGNED_URL_SIGNING_KEY`) exist **only** to enable the graceful cutover described in [Section 1](#graceful-cutover-zero-downtime-for-in-flight-tokens). Outside of an active rotation, they should be unset.

If you find one of these set in production secrets but no rotation is in flight, something is wrong — they should not linger. Check the deploy log for the most recent `flyctl secrets unset <KEY>` call; if you can't find one, an in-flight rotation was never completed. Resume from step 5 of the master-rotation procedure.

## 5. Per-user OAuth tokens (FYI)

You **don't** rotate these manually. Per-user refresh tokens stored in `user_state.db`:

- Refresh themselves against Google on every API call that has an expired access token.
- Invalidate automatically when the user revokes our app's access at https://myaccount.google.com/permissions.
- Carry no operator-side rotation primitive.

If a per-user token misbehaves (rare; usually a Google-side oddity), the surgical fix is to delete that single user's row from `user_state.db` and let the next call hit the `NeedsReauthError` → re-auth flow. **Do not** truncate the whole table; that logs every user out.

## Related modules

- `src/google_docs_mcp/keys.py` — HKDF derivation + per-purpose lookup.
- `src/google_docs_mcp/key_provider.py` — Hex Port + Adapters for the key system (M1a refactor; `keys.py` is the facade).
- `src/google_docs_mcp/crypto.py` — signed-URL TTL constants (`DEFAULT_TTL_SECONDS`, `MAX_TTL_SECONDS`) used in the wait-out step above.
- `src/google_docs_mcp/credentials.py` — `NeedsReauthError`, per-user OAuth refresh.
- `scripts/preflight_strict_flip.sh` — telemetry-gated readiness check used in the graceful cutover.
- `RUNBOOK.md` §3.4 — original v1.5.1 documentation of the per-purpose env-var overrides; this runbook supersedes it for rotation procedure.

## Change log for this document

- **2026-05-27 (PR-Δ3)**: initial version, written during the operational gap audit. Covers MCP_BEARER_TOKEN, GOOGLE_CLIENT_CONFIG, FLY_API_TOKEN, per-purpose overrides, per-user refresh tokens. Includes both graceful and emergency rotation procedures for the HKDF master.
