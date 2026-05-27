# Sentry — Error Tracking Setup

**Audience:** the operator deploying `google-docs-mcp` to Fly.
**Reading time:** ~5 min.

## What this gets you

- **Post-deploy 5xx visibility.** Any unhandled exception in a
  request handler emits a Sentry event with the stack trace, the
  request URL/method, and breadcrumb log lines leading up to the
  error. Pre-Sentry, these were silent until a user complained.
- **Release tagging.** Each event is tagged with `release=<git_commit>`
  so you can pin "this regression appeared in `<sha>`" without
  spelunking through logs.
- **No PII leakage.** A `before_send` scrubber (see
  `src/google_docs_mcp/observability.py`) walks every event before
  transmit and replaces values for keys matching ~20 sensitive
  patterns (Authorization, bearer, sig, token, refresh_token,
  client_secret, sub, email, …) with `[REDACTED]`. Regression-pinned
  by `tests/unit/test_observability_sentry.py`.

## Why this matters

Without error tracking, every server-side 500 is invisible until a
user files an issue. By then the trace is gone (Fly's log retention
is short), the user is annoyed, and you debug from memory. With
Sentry, you see the exception WHEN it fires, with the full Python
stack trace + the last ~50 log lines as breadcrumbs.

## One-time activation (~5 minutes)

### 1. Create a free Sentry account

Sign up at <https://sentry.io/signup/>. The free "Developer" plan
includes:
- **5,000 errors per month** (more than enough for personal scale)
- **30-day error retention**
- Unlimited team members, single project

No credit card required.

### 2. Create a Sentry project

1. After signup, you're prompted to create a project.
2. **Platform:** select **Python**.
3. **Project name:** `google-docs-mcp` (or whatever you like).
4. **Alert me on every new issue** is sensible for personal scale.
5. Click **Create Project**.

### 3. Copy the DSN

Sentry shows you a setup snippet immediately after project creation
with a `dsn=` URL. Copy that URL. It looks like:

```
https://<key>@<organization-id>.ingest.sentry.io/<project-id>
```

If you skipped past the setup screen, find it again at:
**Settings → Projects → google-docs-mcp → Client Keys (DSN)**.

### 4. Set the Fly secret

```bash
fly secrets set SENTRY_DSN="https://<key>@<org-id>.ingest.sentry.io/<project-id>"
```

Fly automatically restarts the app. On the next boot, `init_sentry()`
in `server.py` detects the DSN and activates Sentry. Look for this
line in `fly logs`:

```
INFO ... google_docs_mcp.observability Sentry initialized: release=<sha> env=<region>
```

If you don't see that line, double-check `fly secrets list` shows
`SENTRY_DSN`.

### 5. Verify with a synthetic test event

The cleanest verification is to trigger a real exception. SSH in
and force one:

```bash
fly ssh console -C 'python -c "import sentry_sdk; sentry_sdk.init(\"$SENTRY_DSN\"); raise RuntimeError(\"sentry-setup verification\")"'
```

The exit code is non-zero (the script raises by design). Within
~30 seconds, the Sentry dashboard's "Issues" view should show a
new event titled `RuntimeError: sentry-setup verification`. If it
appears, redaction is working too (no DSN bleed into the event).

After verifying, delete the issue from Sentry's UI so it doesn't
clutter the dashboard.

## What gets captured

- **Unhandled exceptions** in HTTP request handlers (FastMCP +
  `/api/convert` + OAuth callback). Stack traces include filename,
  line, function — but NOT local variables (we opted out at SDK
  init time; primary leak vector for tokens).
- **Log records at ERROR level and above**, captured as Sentry
  events via `LoggingIntegration`.
- **Log records at INFO level and above**, captured as breadcrumbs
  attached to the next captured event — gives you context for what
  happened just before the error.

## What does NOT get captured

- **Performance traces.** `traces_sample_rate=0.0` — would burn
  through the 5k-event/mo budget trivially on an active server.
  Errors only.
- **OAuth tokens, signing keys, signatures, PII.** The
  `_before_send` scrubber redacts values for any key whose name
  matches `authorization`, `bearer`, `token`, `refresh_token`,
  `access_token`, `client_secret`, `signing_key`, `hmac_key`,
  `sig`, `signature`, `nonce`, `uid`, `sub`, `email`,
  `google_creds_json`, and ~5 others. Test pinning at
  `tests/unit/test_observability_sentry.py`.
- **Default PII.** `send_default_pii=False` — Sentry's SDK won't
  attach the requester's IP, cookies, or headers we haven't
  explicitly opted into.

## When the free tier gets tight

5,000 errors/month = ~166/day. If you hit the limit, Sentry stops
ingesting events for the rest of the month (won't break your app —
just stops capturing new ones).

Three responses:
1. **Investigate the noisy errors.** A single error type firing
   100x/day is usually a regression worth fixing, not a budget
   problem.
2. **Sample at the SDK.** Set `traces_sample_rate=0.0` (already
   done) and add `sample_rate=0.1` to `sentry_sdk.init()` to send
   only 10% of error events. Edit `init_sentry()` in
   `src/google_docs_mcp/observability.py`.
3. **Upgrade Sentry.** Their next tier ("Team") is $26/mo for
   50k events. If you're at this point, you have real users and
   the math is fine.

## Rotating the DSN

DSNs are bearer credentials — anyone with the DSN can write events
to your Sentry project. If a DSN leaks (e.g. to a fork, a paste,
git history before redaction landed), rotate at
**Settings → Projects → google-docs-mcp → Client Keys (DSN) → Generate New Key**.

Then `fly secrets set SENTRY_DSN=<new-dsn>` and revoke the old key
from the same UI.

## Disabling Sentry

```bash
fly secrets unset SENTRY_DSN
```

The next boot runs `init_sentry()`, finds no DSN, and returns
False without loading the SDK. No code change needed.
