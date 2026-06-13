#!/usr/bin/env python3
"""Trap-proof "re-freeze onto main" for the tool-surface count anchors.

WHAT THIS REPLACES — the manual re-freeze dance + the "count-merge trap".
======================================================================
Every time a new Workspace service lands, the PR must be re-frozen onto
current ``main``. Historically that meant a hand sequence:

  git merge origin/main
    → resolve conflicts on auth.py (scope union), the witness tests,
      tests/golden/tool_surface.json, and server.py's
      _MIN_EXPECTED_TOOL_COUNT
    → re-run scripts/freeze_tool_surface.py
    → MANUALLY set the true _MIN_EXPECTED_TOOL_COUNT
    → run tests

The landmine — the **count-merge trap**: a plain ``git merge`` can
*silently* auto-merge the ``_MIN_EXPECTED_TOOL_COUNT = N`` line to a
WRONG integer with NO conflict marker. Both branches edited the SAME
single line to different integers (e.g. main says 73, your branch said
66), and git's line-merge just picks one side — no <<<<<<< marker,
because from git's view there's no textual conflict, just two edits to
one line that it "resolves". You end up booting with a floor that is
silently wrong (too low → the partial-surface guard never fires; too
high → boot crashes). The three count-of-record sources
(golden / boot-floor / docs) drift apart with nothing flagging it.

HOW THIS TOOL IS TRAP-PROOF
===========================
It NEVER trusts the inherited / on-disk value of the count. It DERIVES
the true registered tool count from the live server surface (the single
source: ``freeze_tool_surface.current_tool_surface()`` — the real
``appscriptly.server`` import path prod/CI use), then UNCONDITIONALLY
OVERWRITES every count-of-record to that derived number:

  1. tests/golden/tool_surface.json   — regenerated from the live surface
  2. server.py _MIN_EXPECTED_TOOL_COUNT — overwritten to the derived count
  3. README.md + ROADMAP.md tool-count mentions — set to the derived count

Because step (2) always re-writes the line from the derived truth, a
merge that mangled it is corrected here regardless of what git picked.
The number is DERIVED, never hard-coded — when a sibling service PR
bumps the real count, re-running this re-syncs everything to the new
truth with zero hand edits.

MODES
=====
  python scripts/refreeze_onto_main.py
      (default = --sync) Derive the truth and overwrite golden +
      _MIN_EXPECTED_TOOL_COUNT + docs to match, then run the witness +
      server test suite. Fails loudly if tests are red.

  python scripts/refreeze_onto_main.py --check
      Derive the truth and COMPARE against what's on disk (golden,
      _MIN_EXPECTED_TOOL_COUNT, doc counts). Exit non-zero with a clear
      diff if anything is out of sync. Changes NOTHING. (CI use.)

  python scripts/refreeze_onto_main.py --sync --no-tests
      Sync but skip the test run (fast inner-loop; CI/full run should
      NOT pass --no-tests).

SCOPE GUARANTEE — this is pure infra + docs. It touches ONLY the golden
count, the boot-floor constant, and documented counts. It does NOT
change any OAuth scope, the tool SET (only the count integer in the
golden can change; the name list is whatever the live surface is), the
tool surface, consent behaviour, or any service code.

CRITICAL — run as a FILE (``python scripts/refreeze_onto_main.py``),
NEVER ``python -c``. Same packaging reason as freeze_tool_surface.py: an
editable/src-layout ``-c`` import under-registers the services
subpackage and would derive a WRONG (partial) count. File execution
(prod console-script + CI + this script) registers the full surface.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse the freeze logic — do NOT duplicate surface derivation or golden
# I/O. ``freeze_tool_surface`` is the single source for "the live tool
# surface" and "where/how the golden is written". This script imports it
# so there is exactly one definition of each.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from freeze_tool_surface import (  # noqa: E402
    GOLDEN_PATH,
    current_tool_surface,
    read_golden,
    write_golden,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_PY = _REPO_ROOT / "src" / "appscriptly" / "server.py"
_README = _REPO_ROOT / "README.md"
_ROADMAP = _REPO_ROOT / "ROADMAP.md"

# ---------------------------------------------------------------------
# server.py _MIN_EXPECTED_TOOL_COUNT
# ---------------------------------------------------------------------
# Matches the assignment line; group(1) is everything up to and
# including ``= `` (preserving spacing), group(2) is the integer we
# overwrite. Anchored to start-of-line (MULTILINE) so it can't match a
# mention inside a comment/docstring mid-line.
_MIN_COUNT_RE = re.compile(
    r"^(_MIN_EXPECTED_TOOL_COUNT\s*=\s*)(\d+)", re.MULTILINE
)


# ---------------------------------------------------------------------
# Documented tool-count mentions (README.md + ROADMAP.md)
# ---------------------------------------------------------------------
# Each spec pins ONE phrasing where a CURRENT tool count is cited. The
# regex captures the count digits as group ``n`` (named) inside an
# unambiguous current-count context, so we replace ONLY that number and
# never touch historical narrative deltas like "golden re-frozen 39→40"
# or "66 → 73" (those are change-story numbers, not the live count).
#
# This list is the SINGLE definition of "where documented counts live".
# refreeze (writer), --check (verifier) AND the doc-count guard test
# (tests/test_doc_tool_count.py) all consume it, so they cannot drift.
#
# Patterns are number-agnostic (they match whatever integer is there
# now), which is what makes re-sync idempotent across count bumps.


@dataclass(frozen=True)
class DocCountPattern:
    """One documented tool-count mention.

    ``regex`` MUST have a named group ``n`` capturing the count digits;
    matching uses that group's span to splice in the derived count while
    leaving the surrounding prose byte-identical.
    """

    file: Path
    label: str
    regex: re.Pattern[str]


_DOC_COUNT_PATTERNS: tuple[DocCountPattern, ...] = (
    # README: "...operations into 57 tools (primarily `gdocs_*` ...)"
    DocCountPattern(
        _README,
        "README: 'into N tools' (overview paragraph)",
        re.compile(r"(?P<pre>\binto )(?P<n>\d+)(?P<post> tools\b)"),
    ),
    # README: "...authoritative list (all 57 tools) with descriptions."
    DocCountPattern(
        _README,
        "README: '(all N tools)' (tool index)",
        re.compile(r"(?P<pre>\(all )(?P<n>\d+)(?P<post> tools\))"),
    ),
    # README: "All 57 tools appear (`gdocs_*` plus ...)"
    DocCountPattern(
        _README,
        "README: 'All N tools appear' (connector walkthrough)",
        re.compile(r"(?P<pre>\bAll )(?P<n>\d+)(?P<post> tools appear\b)"),
    ),
    # ROADMAP: "...a `_MIN_EXPECTED_TOOL_COUNT=57` floor crash boot..."
    DocCountPattern(
        _ROADMAP,
        "ROADMAP: '_MIN_EXPECTED_TOOL_COUNT=N' (fail-loud guards bullet)",
        re.compile(r"(?P<pre>_MIN_EXPECTED_TOOL_COUNT=)(?P<n>\d+)(?P<post>)"),
    ),
)


# ---------------------------------------------------------------------
# Derivation (single source of truth)
# ---------------------------------------------------------------------


def derive_surface() -> tuple[list[str], int]:
    """Return ``(sorted_tool_names, count)`` from the LIVE registered
    surface — the single source every count-of-record is synced to."""
    names = current_tool_surface()
    return names, len(names)


# ---------------------------------------------------------------------
# server.py floor — read + rewrite (always overwrite; trap-proof core)
# ---------------------------------------------------------------------


def read_min_count(server_text: str | None = None) -> int:
    """Parse the current ``_MIN_EXPECTED_TOOL_COUNT`` from server.py."""
    text = server_text if server_text is not None else _SERVER_PY.read_text(
        encoding="utf-8"
    )
    m = _MIN_COUNT_RE.search(text)
    if not m:
        raise RuntimeError(
            f"could not locate _MIN_EXPECTED_TOOL_COUNT in {_SERVER_PY}; "
            "the floor-rewrite anchor is missing — has server.py changed?"
        )
    return int(m.group(2))


def set_min_count(count: int) -> bool:
    """Overwrite ``_MIN_EXPECTED_TOOL_COUNT`` to ``count``.

    ALWAYS rewrites from the derived truth (the trap-proofing): a
    mis-merged value is corrected here regardless of what git picked.
    Returns True if the file content changed.
    """
    text = _SERVER_PY.read_text(encoding="utf-8")
    if not _MIN_COUNT_RE.search(text):
        raise RuntimeError(
            f"could not locate _MIN_EXPECTED_TOOL_COUNT in {_SERVER_PY}; "
            "refusing to write (the floor anchor is missing)."
        )
    new_text = _MIN_COUNT_RE.sub(rf"\g<1>{count}", text, count=1)
    if new_text == text:
        return False
    _SERVER_PY.write_text(new_text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------
# Documented counts — read + rewrite
# ---------------------------------------------------------------------


def read_doc_counts() -> list[tuple[DocCountPattern, int]]:
    """Return ``(pattern, current_count)`` for every documented mention.

    Each pattern MUST match exactly once in its file — a missing or
    duplicated match means the doc text drifted from the pattern spec
    and the tooling can no longer locate the count (fail loud rather
    than silently skip).
    """
    out: list[tuple[DocCountPattern, int]] = []
    cache: dict[Path, str] = {}
    for pat in _DOC_COUNT_PATTERNS:
        text = cache.setdefault(pat.file, pat.file.read_text(encoding="utf-8"))
        matches = list(pat.regex.finditer(text))
        if len(matches) != 1:
            raise RuntimeError(
                f"doc-count pattern [{pat.label}] matched {len(matches)} "
                f"times in {pat.file.name} (expected exactly 1). The doc "
                "phrasing drifted from the pattern; update "
                "_DOC_COUNT_PATTERNS in scripts/refreeze_onto_main.py."
            )
        out.append((pat, int(matches[0].group("n"))))
    return out


def set_doc_counts(count: int) -> list[str]:
    """Rewrite every documented count mention to ``count``.

    Returns a human-readable list of the files actually changed.
    """
    changed: list[str] = []
    # Group patterns by file so we read/write each file once.
    by_file: dict[Path, list[DocCountPattern]] = {}
    for pat in _DOC_COUNT_PATTERNS:
        by_file.setdefault(pat.file, []).append(pat)

    for file, pats in by_file.items():
        text = file.read_text(encoding="utf-8")
        new_text = text
        for pat in pats:
            matches = list(pat.regex.finditer(new_text))
            if len(matches) != 1:
                raise RuntimeError(
                    f"doc-count pattern [{pat.label}] matched "
                    f"{len(matches)} times in {file.name} (expected 1); "
                    "refusing to write."
                )
            new_text = pat.regex.sub(
                rf"\g<pre>{count}\g<post>", new_text, count=1
            )
        if new_text != text:
            file.write_text(new_text, encoding="utf-8")
            changed.append(file.name)
    return changed


# ---------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------

# The witness + server suite. ``tests/unit`` is exactly what CI's
# tests.yml runs (the count anchors + scope-union + tool-annotation
# witnesses all live under tests/unit). ``--no-cov`` because we run a
# SUBSET-relevant suite for the re-freeze gate and the repo's
# coverage.report.fail_under=55 backstop (pyproject.toml) would trip on a
# partial selection; the full coverage gate stays the CI job's concern.
_TEST_ARGS = ["tests/unit", "-q", "--no-cov"]


def run_tests() -> int:
    """Run the witness + server test suite. Return the pytest exit code."""
    print(f"\n=== running tests: pytest {' '.join(_TEST_ARGS)} ===", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *_TEST_ARGS],
        cwd=str(_REPO_ROOT),
    )
    return proc.returncode


# ---------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------


def check() -> int:
    """--check: derive truth, compare to disk, change nothing.

    Exit 0 iff golden + _MIN_EXPECTED_TOOL_COUNT + every documented count
    all already equal the derived registered count. Otherwise print a
    precise diff and exit 1. This is the CI guard.
    """
    names, count = derive_surface()
    problems: list[str] = []

    # 1. Golden (name list + count).
    golden = read_golden()
    if golden != names:
        added = sorted(set(names) - set(golden))
        removed = sorted(set(golden) - set(names))
        problems.append(
            "golden tool_surface.json out of sync:\n"
            f"    added (registered, not in golden):   {added}\n"
            f"    removed (in golden, not registered): {removed}\n"
            f"    golden={len(golden)} registered={count}"
        )

    # 2. Boot floor.
    floor = read_min_count()
    if floor != count:
        problems.append(
            f"_MIN_EXPECTED_TOOL_COUNT={floor} in server.py != derived "
            f"registered count {count}"
        )

    # 3. Documented counts.
    for pat, doc_count in read_doc_counts():
        if doc_count != count:
            problems.append(
                f"[{pat.label}] cites {doc_count} != derived count {count}"
            )

    if problems:
        print(
            f"refreeze --check FAILED (derived registered count = {count}):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nRun `python scripts/refreeze_onto_main.py` (default --sync) "
            "to re-sync everything to the derived count, then commit.",
            file=sys.stderr,
        )
        return 1

    print(
        f"refreeze --check OK: golden, _MIN_EXPECTED_TOOL_COUNT, and all "
        f"documented counts agree on {count} tools."
    )
    return 0


def sync(run_test_suite: bool = True) -> int:
    """--sync (default): overwrite golden + floor + docs to the derived
    count, then (unless suppressed) run the test suite.

    Idempotent: re-running with nothing to change is a no-op write-wise
    and still re-verifies via the test run.
    """
    names, count = derive_surface()
    print(f"derived registered tool count: {count}")

    # 1. Golden — regenerate from the live surface.
    before_golden = read_golden() if GOLDEN_PATH.exists() else None
    write_golden(names)
    if before_golden != names:
        print(f"  golden tool_surface.json -> {count} tools (rewritten)")
    else:
        print(f"  golden tool_surface.json already current ({count} tools)")

    # 2. Boot floor — ALWAYS overwrite from derived truth (trap-proof).
    before_floor = read_min_count()
    changed_floor = set_min_count(count)
    if changed_floor:
        print(
            f"  server.py _MIN_EXPECTED_TOOL_COUNT {before_floor} -> {count} "
            "(overwritten)"
        )
    else:
        print(f"  server.py _MIN_EXPECTED_TOOL_COUNT already {count}")

    # 3. Documented counts.
    changed_docs = set_doc_counts(count)
    if changed_docs:
        print(f"  doc counts -> {count} in: {', '.join(changed_docs)}")
    else:
        print(f"  doc counts already {count} (README.md, ROADMAP.md)")

    if not run_test_suite:
        print("\n(--no-tests) skipping test suite.")
        return 0

    rc = run_tests()
    if rc != 0:
        print(
            f"\nrefreeze: TEST SUITE FAILED (pytest exit {rc}). The re-freeze "
            "wrote the count anchors but the suite is RED — fix before "
            "committing.",
            file=sys.stderr,
        )
        return rc
    print(f"\nrefreeze --sync OK: everything synced to {count} tools, tests green.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="refreeze_onto_main.py",
        description=(
            "Trap-proof re-freeze of the tool-surface count anchors "
            "(golden + _MIN_EXPECTED_TOOL_COUNT + docs) to the live "
            "registered tool count."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="derive + compare to disk; exit non-zero on drift; change "
        "nothing (CI use).",
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help="(default) overwrite golden + floor + docs to the derived "
        "count, then run tests.",
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="with --sync, skip the test run (fast inner loop).",
    )
    args = parser.parse_args(argv)

    if args.check:
        if args.no_tests:
            parser.error("--no-tests is only valid with --sync.")
        return check()
    # default = sync
    return sync(run_test_suite=not args.no_tests)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
