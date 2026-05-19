#!/usr/bin/env bash
# preflight_strict_flip.sh — validate it's safe to ship v2.0b HKDF strict-flip.
#
# Gate semantics (v2.6, post-R8 BLOCKER fix):
#
# The strict-flip is "remove the back-compat shim from keys.py." It is
# safe to flip IFF every caller of keys.get_key() is currently served
# by the per-purpose OVERRIDE path (MCP_API_BEARER_KEY,
# OAUTH_STATE_SIGNING_KEY, SIGNED_URL_SIGNING_KEY) — because override
# bypasses both shim AND HKDF, so removing the shim is then a no-op for
# live traffic.
#
# Pre-R8 the gate read "shim_hits == 0" without the override prep
# context. That was unsatisfiable during the normal shim window — every
# get_key call hits the shim until the operator sets overrides, so the
# gate would return exit 4 forever. R8 caught this; the new gate
# semantics demand the operator has already set overrides (so
# shim_hits == 0 because nothing routes through the shim anymore, and
# the override_hits derived value proves the overrides ARE serving
# traffic).
#
# Asserts:
#   1. total_calls >= 100   — counter has enough signal to trust (no
#                             traffic = no evidence)
#   2. shim_hits == 0       — every call served by override or HKDF;
#                             nothing routes through the shim. Achieved
#                             by the operator setting per-purpose
#                             overrides BEFORE running this preflight.
#                             See docs/RUNBOOK.md §3.6 for the full prep.
#
# Run this BEFORE merging v2.0b (or any PR that removes entries from
# _BACK_COMPAT_RAW_MASTER in keys.py). See docs/RUNBOOK.md §3.6 for
# the full operator-side prep procedure; §3.5 for the rollback path
# if v2.0b ships with a regression.
#
# Usage:
#   ./preflight_strict_flip.sh <base-url> <bearer-token>
#
# Exit codes:
#   0 — safe to flip (all traffic on override path)
#   1 — usage error (bad args)
#   2 — unreachable endpoint / non-2xx response
#   3 — total call count below the 100-call floor (insufficient signal)
#   4 — shim hits > 0 (HOLD; operator has not set per-purpose overrides,
#                     OR overrides aren't being picked up by the running
#                     server — check redeploy completed). See RUNBOOK §3.6.
#   5 — required field missing from response (server too old / wrong build)

set -euo pipefail

BASE_URL="${1:?usage: $0 <base-url> <bearer-token>}"
TOKEN="${2:?bearer token required as second arg}"

# Strip a trailing slash so we don't double-up below.
BASE_URL="${BASE_URL%/}"

# v2.6 (#48): hit the bearer-authed /info endpoint (replaces a previous
# fragile `/mcp/v1/info` curl path that never existed on the real FastMCP
# HTTP transport; and an alternative `fastmcp client ... call` invocation
# that auth-auditor's R5 pre-mortem caught — fastmcp 3.3.1 has no `client`
# subcommand). /info mirrors the slice of gdocs_server_info the preflight
# needs (shim hits / call totals / first-call ages) and goes through the
# same BearerTokenMiddleware as /api/*, so the bearer enforcement is
# identical to the rest of the REST surface.
URL="$BASE_URL/info"
if ! INFO=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$URL" 2>&1); then
    echo "FAIL: could not GET $URL" >&2
    echo "      ($INFO)" >&2
    echo "      Check: bearer token correct? Server >= v2.6 deployed?" >&2
    exit 2
fi

# Sanity: required fields must be present.
if ! echo "$INFO" | jq -e 'has("key_back_compat_shim_active_hits") and has("key_call_totals")' >/dev/null; then
    echo "FAIL: response missing key_back_compat_shim_active_hits or key_call_totals" >&2
    echo "      (server may pre-date v1.5.1 — upgrade before preflight)" >&2
    exit 5
fi

SHIM_HITS=$(echo "$INFO" | jq -r '.key_back_compat_shim_active_hits | to_entries | map(.value) | add // 0')
TOTAL=$(echo "$INFO" | jq -r '.key_call_totals | to_entries | map(.value) | add // 0')
# Derived: calls served by NEITHER the shim NOR a future HKDF (since v2.6
# is still pre-strict-flip, this is identical to "override hits"). After
# v2.0b ships, this is "override hits + HKDF hits" combined; the gate
# still works because what we're testing is the absence of shim hits.
OVERRIDE_HITS=$((TOTAL - SHIM_HITS))

echo "shim hits:     $SHIM_HITS"
echo "override hits: $OVERRIDE_HITS"
echo "total calls:   $TOTAL"

if [ "$TOTAL" -lt 100 ]; then
    echo "FAIL: total get_key() calls < 100 — counter not sensitive enough yet." >&2
    echo "      Let the soak run longer OR drive synthetic traffic (see RUNBOOK §3.6)." >&2
    exit 3
fi

# Gate semantics (R8 BLOCKER fix): we are testing readiness to FLIP THE
# SHIM, which means proving callers survive without it. The proof is
# operator has set per-purpose override env vars AND every call now
# hits the override path (shim_hits == 0 because nothing routes through
# the shim anymore). Pre-R8 the gate read "shim_hits == 0" alone, which
# was unsatisfiable during a normal shim-active soak: every get_key
# call hit the shim until the operator set overrides. See file header
# + RUNBOOK §3.6 for the operator's prep procedure.
if [ "$SHIM_HITS" -gt 0 ]; then
    echo "FAIL: shim is still serving $SHIM_HITS calls (override serving $OVERRIDE_HITS)." >&2
    echo "      Before v2.0b strict-flip is safe, operator must set:" >&2
    echo "        MCP_API_BEARER_KEY, OAUTH_STATE_SIGNING_KEY, SIGNED_URL_SIGNING_KEY" >&2
    echo "      via 'flyctl secrets set' (32+ chars each). Then redeploy." >&2
    echo "      After the next soak window, shim_hits should drop to 0 (all" >&2
    echo "      calls served by overrides). See RUNBOOK §3.6 for the full" >&2
    echo "      strict-flip preparation procedure." >&2
    exit 4
fi

echo "OK: safe to merge v2.0b strict-flip."
echo "    All $TOTAL calls served by override path (or HKDF if post-flip);"
echo "    shim is unused — removing it from _BACK_COMPAT_RAW_MASTER is a no-op."
