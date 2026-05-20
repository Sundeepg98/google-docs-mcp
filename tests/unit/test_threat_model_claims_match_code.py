"""Catches HMAC-fiction-class regressions — claims must match code reality.

Rationale
---------
v2.0a's ``apps_script_hmac_key`` schema column landed without the runtime
verify-path that some docs claimed for it. The R23/R26 audit and the C4
hedge PR (#53) corrected 8 doc sites; this test stops the fiction from
regrowing in either direction:

1. ``test_hmac_claims_paired_with_status_qualifier`` — any aspirational
   HMAC claim in a security/migration doc must include a status hedge in
   the **same atomic unit** (markdown table row OR paragraph). A
   paragraph-bounded check is intentionally tighter than the original
   500-char byte window from #53: a 500-char window let a future edit
   relocate the hedge >500 chars from the claim — or rely on a different
   nearby paragraph's hedge to cover an unrelated claim — without
   triggering the test. Atomic-unit coupling defeats both.

2. ``test_restructure_gs_has_no_hmac_validation`` — reality-check that
   ``restructure.gs`` still has no HMAC validation. When it does land
   (planned v2.0c) this test flips red on purpose, forcing the hedges
   to be removed at the same time.

Design notes
------------
- ``_atomic_units`` splits markdown into table rows (one ``|...|`` line
  each) and prose paragraphs (``\\n\\n``-separated). Headings are their
  own units. This is a deliberate over-split: false-positive risk is
  "a hedge in paragraph N doesn't cover a claim in paragraph N+1" which
  is exactly what we want to flag.
- Claim detection is permissive: explicit patterns from the C4 audit
  PLUS a HMAC+AppsScript co-occurrence catch-all (any unit mentioning
  both within 100 chars triggers the hedge requirement). The
  catch-all defeats future rewordings like "shipping HMAC", "baked-in
  secret", "per-request validation" that wouldn't match a literal
  pattern.
- ``docs/PRIVACY.md`` is in the scan list unconditionally. It does
  not exist today (lands via PR #44). ``_read`` returns ``""`` on
  ``FileNotFoundError`` so the test passes today AND auto-activates
  coverage the moment #44 merges.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    """Read a repo-relative file; empty string if absent.

    Returning ``""`` for missing files lets us list docs that have not
    yet landed (e.g. ``docs/PRIVACY.md`` from in-flight PR #44) in
    ``docs_to_scan`` so coverage auto-activates the moment those PRs
    merge.
    """
    try:
        return (_REPO / rel).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# Explicit aspirational-claim patterns (case-insensitive). Augmented per
# R28 with the future-reword variants design-external flagged.
_CLAIM_PATTERNS: tuple[str, ...] = (
    r"v2\.0 ships HMAC",
    r"Authenticates POSTs from",
    r"baked into deployed `restructure\.gs`",
    r"baked[- ]in secret",
    r"closing THREAT_MODEL .* row 5",
    r"shipping HMAC",
    r"HMAC per-request",
    r"per-request validation",
)

# Hedge keywords (case-insensitive). Any one of these in the same atomic
# unit as a claim signal satisfies the test.
_HEDGE_PATTERNS: tuple[str, ...] = (
    r"schema only",
    r"schema-only",
    r"NOT YET WIRED",
    r"runtime DEFERRED",
    r"verify-path .* v2\.0c",
    r"stored but unused",
    r"not yet consumed",
    r"pending v2\.0c",
    r"forward-compatible",  # line-118-style "schema landed, runtime ignores"
    r"ignores .* column",
    r"deferred to v2\.0c",
)

# Catch-all signal: any unit mentioning HMAC + Apps Script within 100
# chars of each other is treated as an HMAC claim and must be hedged.
# ``apps[_ ]?script`` covers both ``Apps Script`` (prose) and
# ``apps_script`` (identifier form) without forcing every test author to
# remember the underscore.
_HMAC_RE = re.compile(r"\bhmac\b", re.IGNORECASE)
_APPSSCRIPT_RE = re.compile(r"apps[_ ]?script", re.IGNORECASE)
_CO_OCCURRENCE_WINDOW = 100  # chars between HMAC and Apps Script mention


def _atomic_units(text: str) -> list[tuple[int, str]]:
    """Split a markdown doc into (line_number, unit_text) pairs.

    Each markdown table row (a single line starting with ``|`` and ending
    with ``|``) is one unit — including all of its cells. Prose blocks
    separated by blank lines are one unit each. This is intentionally
    finer-grained than the prior 500-char byte window in #53.
    """
    units: list[tuple[int, str]] = []
    lines = text.split("\n")
    buffer: list[str] = []
    buffer_start_line = 1  # 1-indexed
    current_line = 1

    def _flush_buffer() -> None:
        nonlocal buffer
        if buffer:
            unit_text = "\n".join(buffer).strip()
            if unit_text:
                units.append((buffer_start_line, unit_text))
            buffer = []

    for raw in lines:
        stripped = raw.strip()
        is_table_row = stripped.startswith("|") and stripped.endswith("|")
        if is_table_row:
            # Table row is its own atomic unit; flush any prose buffer first.
            _flush_buffer()
            units.append((current_line, stripped))
            buffer_start_line = current_line + 1
        elif stripped == "":
            # Blank line ends the current prose paragraph.
            _flush_buffer()
            buffer_start_line = current_line + 1
        else:
            if not buffer:
                buffer_start_line = current_line
            buffer.append(raw)
        current_line += 1

    _flush_buffer()
    return units


def _unit_has_hmac_claim(unit_text: str) -> tuple[bool, str | None]:
    """Return (is_claim, matched_signal) for a unit.

    A unit is a claim if it matches any explicit ``_CLAIM_PATTERNS``
    entry OR if it contains ``HMAC`` and ``Apps Script`` within
    ``_CO_OCCURRENCE_WINDOW`` chars of each other.
    """
    for pat in _CLAIM_PATTERNS:
        m = re.search(pat, unit_text, re.IGNORECASE)
        if m:
            return True, m.group(0)

    hmac_matches = [m.start() for m in _HMAC_RE.finditer(unit_text)]
    apps_matches = [m.start() for m in _APPSSCRIPT_RE.finditer(unit_text)]
    if not hmac_matches or not apps_matches:
        return False, None
    for h in hmac_matches:
        for a in apps_matches:
            if abs(h - a) <= _CO_OCCURRENCE_WINDOW:
                snippet = unit_text[max(0, min(h, a) - 10): max(h, a) + 20]
                return True, f"HMAC+AppsScript co-occurrence: ...{snippet}..."
    return False, None


def _unit_has_hedge(unit_text: str) -> bool:
    return any(re.search(p, unit_text, re.IGNORECASE) for p in _HEDGE_PATTERNS)


def test_hmac_claims_paired_with_status_qualifier():
    """Every doc claim about Apps Script HMAC must carry a status hedge
    in the SAME atomic unit (table row or prose paragraph).

    See module docstring for why same-unit coupling beats the 500-char
    byte window we shipped originally.
    """
    docs_to_scan = [
        "docs/THREAT_MODEL.md",
        "docs/MIGRATION_v1_to_v2.md",
        "docs/TOOL_CONTRACT.md",
        # PRIVACY.md lands via in-flight PR #44; _read returns "" until then.
        "docs/PRIVACY.md",
    ]
    violations: list[str] = []
    for doc_path in docs_to_scan:
        text = _read(doc_path)
        if not text:
            continue  # absent doc (e.g. PRIVACY.md pre-#44-merge)
        for line, unit in _atomic_units(text):
            is_claim, signal = _unit_has_hmac_claim(unit)
            if is_claim and not _unit_has_hedge(unit):
                preview = unit if len(unit) < 200 else unit[:197] + "..."
                violations.append(
                    f"{doc_path}:{line} unhedged HMAC claim "
                    f"(signal: {signal!r}) — unit: {preview!r}"
                )

    assert not violations, (
        "Aspirational HMAC claims must include a status qualifier in the "
        "SAME table row / prose paragraph:\n  " + "\n  ".join(violations)
    )


def test_restructure_gs_has_no_hmac_validation():
    """Reality-check: if this test starts failing, restructure.gs gained
    HMAC validation — update the docs AND remove the hedges."""
    gs_text = _read("src/google_docs_mcp/restructure.gs")
    assert "computeHmacSha256Signature" not in gs_text, (
        "restructure.gs now has HMAC validation! Time to remove the "
        "'NOT YET WIRED' / 'planned for v2.0c' hedges in THREAT_MODEL.md, "
        "MIGRATION_v1_to_v2.md, TOOL_CONTRACT.md, PRIVACY.md, and "
        "scripts/migrate_existing_users.py."
    )
