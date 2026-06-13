"""Doc-count drift guard: README.md / ROADMAP.md cite the TRUE tool count.

The fourth count-of-record (after the golden snapshot, the boot-floor
``_MIN_EXPECTED_TOOL_COUNT``, and the per-service ``_expected_tools.py``
witnesses) is the *documented* tool count — the "N tools" figures in
README.md and the ``_MIN_EXPECTED_TOOL_COUNT=N`` mention in ROADMAP.md.
Historically these drifted silently: the code grew to 73 tools while the
README still advertised 57 (the audit that motivated
``scripts/refreeze_onto_main.py``).

This guard makes that drift a hard CI failure. It asserts every
documented tool-count mention equals the REAL derived registered count
(``current_tool_surface()`` — the same single source the golden and the
boot floor derive from). So a new service that bumps the count can never
again leave the docs stale: either the author re-runs
``python scripts/refreeze_onto_main.py`` (which re-syncs docs + golden +
floor together) or this test goes red.

Why import the patterns from ``refreeze_onto_main`` rather than re-list
them here: there must be exactly ONE definition of "where the documented
counts live". The refreeze writer, its ``--check`` verifier, and this
test all consume ``_DOC_COUNT_PATTERNS`` — they cannot drift apart, and
a future doc-count mention is covered everywhere the moment it's added to
that one list.

This is a NEW test file (not an edit to an existing witness) by design —
it must not collide with in-flight service PRs that touch the existing
``tests/unit/services/test_tool_registration.py`` /
``test_discovery_safety.py`` witness files.

NOTE on execution context: like the other surface witnesses, the true
count is derived via the real ``appscriptly.server`` import under FILE
execution (pytest), NOT ``python -c`` (which under-registers under an
editable/src-layout install — a packaging artifact documented in
``scripts/freeze_tool_surface.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't on the package path; add it explicitly so we can import
# the refreeze tooling (single source of the doc-count pattern spec +
# the surface derivation). Mirrors the pattern in
# tests/unit/test_mutation_check.py / test_migrate_existing_users.py.
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import refreeze_onto_main as rf  # noqa: E402  # pyright: ignore[reportMissingImports]


def _derived_count() -> int:
    """The true registered tool count from the live surface (single
    source — same as the golden + the boot floor)."""
    _names, count = rf.derive_surface()
    return count


def test_doc_count_patterns_each_match_exactly_once():
    """Every documented-count pattern must locate exactly one mention in
    its file.

    ``read_doc_counts()`` raises if any pattern matches 0 or >1 times —
    i.e. if the doc prose drifted away from the pattern spec (the count
    mention was reworded/removed, or a second one appeared). That's a
    distinct failure from a *stale number*: it means the tooling can no
    longer find the count to check it, so it must fail loudly and the
    ``_DOC_COUNT_PATTERNS`` spec needs updating.
    """
    # Raises RuntimeError with a precise message on a match-count problem.
    results = rf.read_doc_counts()
    assert len(results) == len(rf._DOC_COUNT_PATTERNS), (
        "read_doc_counts() did not return one result per documented "
        "pattern — the spec and the reader are out of step."
    )


def test_all_documented_counts_equal_true_registered_count():
    """Every "N tools" / ``_MIN_EXPECTED_TOOL_COUNT=N`` figure in
    README.md and ROADMAP.md MUST equal the real derived registered tool
    count.

    If this fails, the docs drifted from the live surface. Re-sync with
    ``python scripts/refreeze_onto_main.py`` (default --sync), which
    rewrites the documented counts (and the golden + the boot floor) to
    the derived count in one shot, then commit the diff.
    """
    true_count = _derived_count()
    stale: list[str] = []
    for pat, doc_count in rf.read_doc_counts():
        if doc_count != true_count:
            stale.append(
                f"[{pat.label}] in {pat.file.name} cites {doc_count} "
                f"(true registered count = {true_count})"
            )
    assert not stale, (
        "Documented tool count(s) drifted from the live registered "
        "surface:\n  " + "\n  ".join(stale)
        + "\nRun `python scripts/refreeze_onto_main.py` to re-sync docs + "
        "golden + _MIN_EXPECTED_TOOL_COUNT to the derived count, then commit."
    )


def test_doc_counts_consistent_with_golden_and_boot_floor():
    """Cross-check: the documented counts, the golden surface size, and
    the boot floor (``_MIN_EXPECTED_TOOL_COUNT``) all agree.

    This is the lockstep invariant the refreeze tool maintains — the
    four count-of-record sources (golden / boot floor / docs / live
    surface) must be one number. Pinning it here means a partial manual
    edit (e.g. someone bumps the golden + floor but forgets the docs)
    fails CI even if, hypothetically, the live count happened to match
    the docs.
    """
    true_count = _derived_count()
    golden_count = len(rf.read_golden())
    floor = rf.read_min_count()

    assert golden_count == true_count, (
        f"golden size {golden_count} != derived registered count "
        f"{true_count} (re-run scripts/refreeze_onto_main.py)."
    )
    assert floor == true_count, (
        f"_MIN_EXPECTED_TOOL_COUNT {floor} != derived registered count "
        f"{true_count} (re-run scripts/refreeze_onto_main.py)."
    )
    for pat, doc_count in rf.read_doc_counts():
        assert doc_count == true_count, (
            f"[{pat.label}] cites {doc_count} != {true_count}."
        )
