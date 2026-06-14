"""Catches HMAC-fiction-class regressions — claims must match code reality.

Rationale
---------
v2.0a's ``apps_script_hmac_key`` schema column landed WITHOUT the runtime
verify-path. While that gap was open, security docs had to HEDGE every HMAC
claim ("NOT YET WIRED" / "deferred to v2.0c"). **v2.0c wired the verify-path
end-to-end** (``restructure.gs::doPost`` verifies the signature;
``_call_webapp`` signs), so the situation INVERTED: those deferral hedges are
now FALSE statements, and the affirmative claim ("HMAC authenticates /exec")
is the truth. This test stops the fiction from regrowing in either
direction:

1. ``test_no_stale_hmac_deferral_hedges`` — the security/migration docs must
   NOT carry the now-false "HMAC verify-path is deferred / schema-only / not
   consumed at runtime" hedges next to an Apps Script mention. (This is the
   INVERSE of the pre-v2.0c rule, which REQUIRED such a hedge.) Same
   atomic-unit granularity so a stale hedge can't hide in a paragraph
   adjacent to an affirmative one.

2. ``test_restructure_gs_has_hmac_validation`` — reality-check that
   ``restructure.gs`` NOW HAS the HMAC verify-path (landed v2.0c): the real
   ``computeHmacSha256Signature`` call + the doPost ``stage:'auth'``
   rejection + the ``MCP_HMAC_REQUIRED`` fail-closed gate are present.

3. ``test_call_webapp_signs_requests`` — the server side of the pair:
   ``docx_import`` attaches ``X-MCP-Signature`` and computes the signature.

If HMAC is ever ripped out, (2)/(3) flip red on purpose — and the docs would
then need their hedges back, which (1) would (correctly) start allowing
again only after the code regressed.

Design notes
------------
- ``_atomic_units`` splits markdown into table rows (one ``|...|`` line
  each) and prose paragraphs (``\\n\\n``-separated). Headings are their
  own units. This is a deliberate over-split.
- A "stale hedge" is detected as a unit that mentions Apps Script (or
  ``restructure.gs`` / ``_call_webapp`` / ``apps_script_hmac_key``) AND a
  now-false deferral phrase. Rollback-context lines (v1.5.x "ignores the
  column") are NOT flagged — they're true statements about OLD code.
- ``docs/PRIVACY.md`` is scanned if present; ``_read`` returns ``""`` for an
  absent file so the test is robust to doc set changes.
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


# Now-FALSE deferral hedges (case-insensitive). Post-v2.0c, an HMAC unit
# carrying any of these is a stale claim that contradicts the wired code.
# ``forward-compatible`` / ``ignores .* column`` are intentionally NOT here:
# they describe a v1.5.x ROLLBACK reading the column (a true statement about
# OLD code), not the current runtime.
# These match only FORWARD-DEFERRAL / not-yet phrasings — the claims that
# became false when v2.0c shipped. They deliberately do NOT match affirmative
# mentions that happen to name v2.0c ("verify-path is WIRED as of v2.0c",
# "WAS schema-only; now wired"): those describe the closed state and must be
# allowed. Past-tense ("was ... only") and "now wired" are not flagged.
_STALE_HEDGE_PATTERNS: tuple[str, ...] = (
    r"\bNOT YET WIRED\b",
    r"runtime DEFERRED",
    r"verify[- ]path is planned",
    r"verify[- ]path .{0,30}\b(?:deferred|pending|planned)\b",
    r"\bdeferred to v2\.0c\b",
    r"\bplanned for v2\.0c\b",
    r"\bpending v2\.0c\b",
    r"verify[- ]path lands? in v2\.0c",
    r"stored but unused",
    r"stored,? (?:but )?not (?:yet )?consumed",
    r"\bnot yet consumed\b",
    # NB: bare "schema-only" is intentionally NOT a stale signal — a
    # historical "was schema-only; now wired" description is legitimate. The
    # forward-deferral phrases above are what flag a genuinely false claim.
)

# A unit is "about the Apps Script HMAC surface" if it mentions HMAC near an
# Apps-Script-surface token. ``apps[_ ]?script`` covers ``Apps Script`` and
# the ``apps_script`` identifier; restructure.gs / _call_webapp are the two
# code sites the hedges named.
_HMAC_RE = re.compile(r"\bhmac\b", re.IGNORECASE)
_APPSSCRIPT_RE = re.compile(
    r"apps[_ ]?script|restructure\.gs|_call_webapp", re.IGNORECASE
)
_CO_OCCURRENCE_WINDOW = 160  # chars between HMAC and the Apps-Script token


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


def _unit_is_about_hmac_surface(unit_text: str) -> tuple[bool, str | None]:
    """Return (is_about_surface, matched_token) for a unit.

    True if the unit mentions ``HMAC`` within ``_CO_OCCURRENCE_WINDOW`` chars
    of an Apps-Script-surface token (Apps Script / restructure.gs /
    _call_webapp). That's the population a stale deferral hedge could falsely
    describe.
    """
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


def _unit_has_stale_hedge(unit_text: str) -> str | None:
    """Return the matched stale-deferral phrase, or None."""
    for p in _STALE_HEDGE_PATTERNS:
        m = re.search(p, unit_text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def test_no_stale_hmac_deferral_hedges():
    """Post-v2.0c, security/migration docs must NOT carry the now-false
    "HMAC verify-path deferred / schema-only / not consumed" hedges next to
    an Apps Script HMAC mention. The verify-path IS wired (see
    test_restructure_gs_has_hmac_validation), so such a hedge is a false
    claim. This is the INVERSE of the pre-v2.0c rule.
    """
    docs_to_scan = [
        "docs/THREAT_MODEL.md",
        "docs/MIGRATION_v1_to_v2.md",
        "docs/TOOL_CONTRACT.md",
        "docs/PRIVACY.md",
    ]
    violations: list[str] = []
    for doc_path in docs_to_scan:
        text = _read(doc_path)
        if not text:
            continue
        for line, unit in _atomic_units(text):
            is_surface, _signal = _unit_is_about_hmac_surface(unit)
            if not is_surface:
                continue
            stale = _unit_has_stale_hedge(unit)
            if stale:
                preview = unit if len(unit) < 220 else unit[:217] + "..."
                violations.append(
                    f"{doc_path}:{line} STALE HMAC deferral hedge "
                    f"(matched: {stale!r}) — the verify-path is wired as of "
                    f"v2.0c; update the claim. Unit: {preview!r}"
                )

    assert not violations, (
        "Found now-FALSE 'HMAC not wired / deferred to v2.0c' hedges. The "
        "verify-path shipped; these must be rewritten as affirmative:\n  "
        + "\n  ".join(violations)
    )


def test_restructure_gs_has_hmac_validation():
    """Reality-check (v2.0c): restructure.gs MUST verify a per-request HMAC.

    This is the FLIPPED form of the old ``..._has_no_hmac_validation`` test.
    The verify-path is now wired, so we assert the real crypto call + the
    fail-closed auth rejection are present. If this starts failing, the HMAC
    verify-path was removed and the ``/exec`` surface is unauthenticated
    again — a security regression, NOT a docs-hedge problem.
    """
    gs_text = _read("src/appscriptly/restructure.gs")
    assert "computeHmacSha256Signature" in gs_text, (
        "restructure.gs lost its HMAC verify call (Utilities."
        "computeHmacSha256Signature) — the ANYONE_ANONYMOUS /exec Web App "
        "would be unauthenticated again (THREAT_MODEL §4 row 5 regressed)."
    )
    # The doPost path must reject failed auth with stage:'auth' before acting.
    assert "stage: 'auth'" in gs_text or 'stage: "auth"' in gs_text, (
        "restructure.gs no longer returns stage:'auth' on a bad signature — "
        "the fail-closed rejection path was removed."
    )
    # The script must enforce (require) the signature, not just define a
    # helper. The MCP_HMAC_REQUIRED gate is the fail-closed switch.
    assert "MCP_HMAC_REQUIRED" in gs_text, (
        "restructure.gs dropped the MCP_HMAC_REQUIRED fail-closed gate."
    )


def test_call_webapp_signs_requests():
    """The server side of the HMAC pair: docx_import._call_webapp must SIGN
    every request (X-MCP-Signature + X-MCP-Timestamp) so restructure.gs's
    verify-path actually receives a signature to check."""
    src = _read("src/appscriptly/docx_import.py")
    assert "X-MCP-Signature" in src or "SIGNATURE_HEADER" in src, (
        "docx_import._call_webapp no longer attaches the HMAC signature "
        "header — signed-request path regressed."
    )
    assert "compute_signature" in src, (
        "docx_import no longer computes the HMAC signature."
    )
