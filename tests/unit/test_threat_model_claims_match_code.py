"""Catches HMAC-fiction-class regressions — claims must match code reality."""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_hmac_claims_paired_with_status_qualifier():
    """Every doc claim about Apps Script HMAC must be near a status qualifier."""
    # Patterns indicating an aspirational HMAC claim:
    claim_patterns = [
        r"v2\.0 ships HMAC",
        r"Authenticates POSTs from",
        r"baked into deployed `restructure\.gs`",
        r"closing THREAT_MODEL .* row 5",
    ]
    # Patterns indicating an honest status hedge nearby:
    hedge_patterns = [
        r"schema only",
        r"NOT YET WIRED",
        r"runtime DEFERRED",
        r"verify-path .* v2\.0c",
        r"stored but unused",
        r"not yet consumed",
        r"pending v2\.0c",
    ]
    hedge_window = 500  # chars before/after the claim

    docs_to_scan = [
        "docs/THREAT_MODEL.md",
        "docs/MIGRATION_v1_to_v2.md",
        "docs/TOOL_CONTRACT.md",
    ]
    violations: list[str] = []
    for doc_path in docs_to_scan:
        text = _read(doc_path)
        for pat in claim_patterns:
            for m in re.finditer(pat, text):
                start = max(0, m.start() - hedge_window)
                end = min(len(text), m.end() + hedge_window)
                neighborhood = text[start:end]
                if not any(re.search(h, neighborhood) for h in hedge_patterns):
                    line = text[:m.start()].count("\n") + 1
                    violations.append(
                        f"{doc_path}:{line} claim '{m.group(0)}' without "
                        f"status hedge nearby (window {hedge_window} chars)"
                    )

    assert not violations, (
        "Aspirational HMAC claims must include a status qualifier:\n  "
        + "\n  ".join(violations)
    )


def test_restructure_gs_has_no_hmac_validation():
    """Reality-check: if this test starts failing, restructure.gs gained
    HMAC validation — update the docs AND remove the hedges."""
    gs_text = _read("src/google_docs_mcp/restructure.gs")
    assert "computeHmacSha256Signature" not in gs_text, (
        "restructure.gs now has HMAC validation! Time to remove the "
        "'NOT YET WIRED' / 'planned for v2.0c' hedges in THREAT_MODEL.md, "
        "MIGRATION_v1_to_v2.md, TOOL_CONTRACT.md, and "
        "scripts/migrate_existing_users.py."
    )
