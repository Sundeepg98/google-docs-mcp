# Runbook: GCP project linking for Apps Script (Cloud Logging audit trail)

**Status**: Opt-in (PR-Δ5+). Default behavior is unlinked — personal users see no change.
**When to enable**: commercial customers, enterprise users, SOC 2 prep, any deployment that needs an enterprise-grade audit trail of Apps Script executions.
**Cost**: $0 / month under Google Cloud Logging's free tier (50 GiB ingestion/month) for typical Apps Script workloads. Larger projects pay standard Cloud Logging rates.
**Reversibility**: fully reversible — `fly secrets unset GCP_PROJECT_NUMBER && fly deploy` restores the unlinked behavior on the next Apps Script re-deploy.

## What this gives you

By default, every Apps Script project this app provisions for a user is a **standalone script** — execution logs live inside the Apps Script editor's "Executions" panel, scoped to the deploying user. Sufficient for personal use.

When you link the Apps Script projects to a **GCP project** via `GCP_PROJECT_NUMBER`:

- Every `restructure.gs` execution writes to Cloud Logging under the linked GCP project.
- Stack traces, `console.log` output, and execution metadata are queryable via the Cloud Logging UI / `gcloud logging read`.
- The audit trail is retained per Cloud Logging's retention policy (30 days default, configurable up to 10 years).
- The logs can be exported to BigQuery, Cloud Storage, or Pub/Sub for downstream compliance / SIEM integration.

This is the SOC 2-compliant audit trail path for Apps Script executions.

## Prerequisites

- A Google Cloud account (free tier works).
- Permission to create / use a GCP project.
- ~5 minutes for the one-time setup.

## Step-by-step setup

### 1. Create or pick a GCP project

If you already have a GCP project for this deployment, skip to step 2.

```bash
# Option A: via gcloud CLI (recommended for automation)
gcloud projects create your-project-id-here --name="google-docs-mcp logs"

# Option B: via Cloud Console
# Visit https://console.cloud.google.com/projectcreate
# Enter a project name + organization (optional).
```

### 2. Get the project NUMBER (not the project ID)

Apps Script's manifest field is named `projectId` but expects the **numeric project number**, not the human-readable string ID. Despite the confusing naming, this is per Google's documented schema:
<https://developers.google.com/apps-script/manifest#cloudplatform>

```bash
# Get the project number for the project you just created (or are reusing):
gcloud projects describe your-project-id-here --format="value(projectNumber)"
# Example output: 123456789012
```

Or via Cloud Console: Home dashboard → "Project info" card → "Project number" row.

### 3. Enable the Apps Script API in the GCP project

```bash
gcloud services enable script.googleapis.com --project=your-project-id-here
```

Or via Cloud Console: APIs & Services → Library → search "Apps Script API" → Enable.

### 4. Set the Fly secret

```bash
fly secrets set GCP_PROJECT_NUMBER=123456789012
# Deploys are automatic on secret change; or trigger manually:
fly deploy
```

For self-hosted (non-Fly) deployments, set the env var via your usual mechanism (systemd unit, Docker env, k8s ConfigMap, etc.). The variable name is `GCP_PROJECT_NUMBER`.

### 5. Trigger a re-deploy of the Apps Script runtime

The manifest change (adding `cloudPlatform.projectId`) modifies the content hash, which triggers the setup-state ledger's "manifest changed → re-deploy" path on the next `gdocs_install_automation` call. **Existing users need to re-run** `gdocs_install_automation` for their per-user Apps Script project to pick up the new manifest:

```
Claude: gdocs_install_automation()
```

The first call returns `{status: "needs_authorization", ...}` if their token doesn't yet cover the (existing) Apps Script scopes — they consent once, then the second call re-deploys with the linked manifest.

## Verifying the link

### Check the Apps Script side

Open the user's Apps Script project (URL from `gdocs_install_automation`'s `url` field), then:
**Project Settings → Google Cloud Platform (GCP) Project** — the linked project number appears here.

### Check the Cloud Logging side

```bash
gcloud logging read 'resource.type="app_script_function"' \
  --project=your-project-id-here \
  --limit=10 \
  --format=json
```

Wait ~30s after the user's next `restructure.gs` invocation; entries should appear. Each entry includes the user's Google account email (the script's `executeAs: USER_DEPLOYING` runs under the user's identity).

If no entries appear after 5 min + a triggering invocation:
1. Confirm `GCP_PROJECT_NUMBER` matches the project number (not project ID).
2. Confirm Apps Script API is enabled in that project (`gcloud services list --enabled --project=...`).
3. Confirm the user re-ran `gdocs_install_automation` after the env var was set.
4. Check `flyctl logs` for any structured-log line tagged `google_docs_mcp.setup_apps_script` — manifest pushes are visible there.

## Disabling

```bash
fly secrets unset GCP_PROJECT_NUMBER
fly deploy
# Users re-run gdocs_install_automation to push the unlinked manifest.
```

Existing logs in Cloud Logging are retained per the project's retention policy; the link is removed prospectively, not retroactively.

## Compliance notes

- **SOC 2**: the audit-log trail from Cloud Logging satisfies CC7.2 (system monitoring) and CC9.1 (incident response — searchable logs). Pair with log export to a SIEM (Splunk, Sumo, Datadog) for full SOC 2 coverage.
- **HIPAA**: Cloud Logging is HIPAA-eligible under a signed BAA with Google Cloud. The Apps Script project itself runs as the user (not as a covered-entity service), so customer health-data flows through user-owned infrastructure — the audit trail is the operator's accountability surface.
- **GDPR**: Cloud Logging entries include user email (from `executeAs: USER_DEPLOYING`). Configure log-retention + DSR (data subject request) procedures accordingly.

## Reference: what changes in the Apps Script manifest

**Without** `GCP_PROJECT_NUMBER`:

```json
{
  "timeZone": "Etc/GMT",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "webapp": {
    "executeAs": "USER_DEPLOYING",
    "access": "ANYONE_ANONYMOUS"
  }
}
```

**With** `GCP_PROJECT_NUMBER=123456789012`:

```json
{
  "timeZone": "Etc/GMT",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "webapp": {
    "executeAs": "USER_DEPLOYING",
    "access": "ANYONE_ANONYMOUS"
  },
  "cloudPlatform": {
    "projectId": "123456789012"
  }
}
```

The only change is the new `cloudPlatform` block. `exceptionLogging: "STACKDRIVER"` was already present pre-PR-Δ5; the GCP link is what gives that Stackdriver setting a destination project.

## Related

- ADR: `docs/adr/2026-05-27-commercial-ready-engineering.md`
- Code seam: `src/google_docs_mcp/setup_apps_script.py::_build_manifest`
- Tests: `tests/unit/test_gcp_project_linking.py`
- Companion runbooks: `docs/runbooks/backup-restore.md`, `docs/runbooks/key-rotation.md`, `docs/runbooks/sentry-setup.md`
