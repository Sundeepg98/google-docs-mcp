#!/usr/bin/env bash
# preflight_strict_flip.sh — validate it's safe to ship v2.0b HKDF strict-flip.
#
# Asserts both halves of the soak signal:
#   1. shim_hits == 0     — nobody actively using the back-compat shim
#   2. total_calls >= 100 — counter has enough signal to be trusted (no
#                           traffic = no evidence)
#
# Run this BEFORE merging v2.0b (or any PR that removes entries from
# _BACK_COMPAT_RAW_MASTER in keys.py). See docs/RUNBOOK.md §3.5/§3.6.
#
# Usage:
#   ./preflight_strict_flip.sh <base-url> <bearer-token>
#
# Exit codes:
#   0 — safe to flip
#   1 — usage error (bad args)
#   2 — unreachable endpoint / non-2xx response
#   3 — total call count below the 100-call floor (insufficient signal)
#   4 — shim hits > 0 (HOLD; in-flight tokens still present)
#   5 — required field missing from response (server too old / wrong build)

set -euo pipefail

BASE_URL="${1:?usage: $0 <base-url> <bearer-token>}"
TOKEN="${2:?bearer token required as second arg}"

# Strip a trailing slash so we don't double-up below.
BASE_URL="${BASE_URL%/}"

# Hit the introspection endpoint. The /mcp transport surfaces tools as
# JSON-RPC; here we assume an HTTP shim that exposes gdocs_server_info
# at /mcp/v1/info. Adjust path if the deployment uses a different
# surface (e.g. a direct fastmcp HTTP transport).
URL="$BASE_URL/mcp/v1/info"
if ! INFO=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$URL"); then
    echo "FAIL: could not reach $URL (network / auth / wrong endpoint)" >&2
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

echo "shim hits:   $SHIM_HITS"
echo "total calls: $TOTAL"

if [ "$TOTAL" -lt 100 ]; then
    echo "FAIL: total get_key() calls < 100 — counter not sensitive enough yet." >&2
    echo "      Let the soak run longer OR generate synthetic traffic before retrying." >&2
    exit 3
fi
if [ "$SHIM_HITS" -gt 0 ]; then
    echo "FAIL: shim still active ($SHIM_HITS hits) — DO NOT flip." >&2
    echo "      Wait for in-flight tokens to expire (signed-URL TTL is 24h" >&2
    echo "      by default; OAuth state TTL is 10min). See RUNBOOK §3.5." >&2
    exit 4
fi

echo "OK: safe to merge v2.0b strict-flip."
