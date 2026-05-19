"""Tests for scripts/mutation_check.py — the mutation gate itself.

These tests exist because the gate that proves regression guards
catch their bugs can ALSO silently rot: if a mutation's `find` text
moves or its semantics drift, the gate would report "caught" for a
bug that was never actually injected. v1.2.2 added stale_patch /
imprecise_patch detection; these tests verify that detection works.

We test pure functions in isolation. The slow integration test —
"deliberately rot a real patch, watch CI report stale_patch" — is
implicitly run any time the source moves under the existing
mutation list, which is exactly when this gate needs to be sharp.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ isn't on the package path; add it explicitly so the test
# can import mutation_check without being co-located.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mutation_check as mc  # noqa: E402  # pyright: ignore[reportMissingImports]


# ---------- apply_mutation: the apply-cleanly check ---------------

def test_apply_mutation_returns_false_when_find_text_absent(tmp_path):
    """Acceptance: a patch whose `find` text isn't in the file must
    not silently no-op. apply_mutation returns False so the caller
    can classify it as stale_patch."""
    src = tmp_path / "src.py"
    src.write_text("def foo(): return 1\n", encoding="utf-8")
    m = mc.Mutation(
        guard="test_irrelevant",
        test_path="tests/unit/test_irrelevant.py::test_irrelevant",
        description="rotted patch",
        file=str(src),
        find="THIS TEXT DEFINITELY DOES NOT EXIST IN THE FILE xyz123",
        replace="something",
    )
    assert mc.apply_mutation(m) is False
    # File untouched.
    assert src.read_text(encoding="utf-8") == "def foo(): return 1\n"


def test_apply_mutation_returns_false_when_find_text_ambiguous(tmp_path):
    """A `find` that matches multiple times is also rotted: we can't
    trust which occurrence got patched. Treat as stale_patch."""
    src = tmp_path / "src.py"
    src.write_text("x = 1\nx = 1\n", encoding="utf-8")
    m = mc.Mutation(
        guard="test_irrelevant",
        test_path="tests/unit/test_irrelevant.py::test_irrelevant",
        description="ambiguous patch",
        file=str(src),
        find="x = 1",
        replace="x = 2",
    )
    assert mc.apply_mutation(m) is False


def test_apply_mutation_succeeds_when_find_uniquely_present(tmp_path):
    src = tmp_path / "src.py"
    src.write_text("def foo(): return 1\n", encoding="utf-8")
    m = mc.Mutation(
        guard="test_irrelevant",
        test_path="tests/unit/test_irrelevant.py::test_irrelevant",
        description="ok patch",
        file=str(src),
        find="return 1",
        replace="return 2",
    )
    assert mc.apply_mutation(m) is True
    assert src.read_text(encoding="utf-8") == "def foo(): return 2\n"


# ---------- classify_outcome: the four-way decision ---------------

def _m(test_path: str = "tests/unit/test_x.py::test_target",
       expected_collateral: list[str] | None = None) -> "mc.Mutation":
    return mc.Mutation(
        guard="test_target",
        test_path=test_path,
        description="any",
        file="any.py", find="any", replace="any",
        expected_collateral=expected_collateral or [],
    )


def test_matches_nodeid_handles_exact_and_parametrized():
    """Parametrized failures (test_X[case1]) must match a base
    declared test_path (test_X)."""
    target = "tests/unit/test_x.py::test_target"
    assert mc._matches_nodeid(target, target)
    assert mc._matches_nodeid(f"{target}[case1]", target)
    assert mc._matches_nodeid(f"{target}[gdocs_make_tabbed_doc]", target)
    assert not mc._matches_nodeid("tests/unit/test_x.py::other_test", target)
    assert not mc._matches_nodeid(f"{target}_suffix", target)  # not a param


def test_classify_caught_when_only_parametrized_target_failed():
    """The historical bug: target declared as base, pytest reports
    parametrized id — must still classify as caught."""
    target = "tests/unit/test_x.py::test_target"
    out = mc.classify_outcome(
        _m(target),
        applied=True,
        failed_tests=[f"{target}[case1]"],
    )
    assert out["outcome"] == "caught"


def test_classify_caught_when_expected_collateral_failed():
    """Declared siblings (defense in depth) don't count as
    unexpected failures."""
    target = "tests/unit/test_x.py::test_target"
    sibling = "tests/unit/test_x.py::test_sibling"
    out = mc.classify_outcome(
        _m(target, expected_collateral=[sibling]),
        applied=True,
        failed_tests=[target, sibling],
    )
    assert out["outcome"] == "caught"


def test_classify_imprecise_when_undeclared_collateral_failed():
    """Only DECLARED siblings are forgiven; surprises still flag."""
    target = "tests/unit/test_x.py::test_target"
    declared = "tests/unit/test_x.py::test_known_sibling"
    surprise = "tests/unit/test_y.py::test_surprise"
    out = mc.classify_outcome(
        _m(target, expected_collateral=[declared]),
        applied=True,
        failed_tests=[target, declared, surprise],
    )
    assert out["outcome"] == "imprecise_patch"
    assert surprise in out["unexpected_failures"]
    assert declared not in out["unexpected_failures"]


def test_classify_stale_when_patch_did_not_apply():
    """`applied=False` → stale_patch with the apply-cleanly reason."""
    out = mc.classify_outcome(_m(), applied=False, failed_tests=[])
    assert out["outcome"] == "stale_patch"
    assert "not present" in out["reason"]


def test_classify_stale_when_patch_applied_but_zero_failures():
    """Patch applied but suite stayed green → mutation doesn't model
    its bug anymore (semantic rot)."""
    out = mc.classify_outcome(_m(), applied=True, failed_tests=[])
    assert out["outcome"] == "stale_patch"
    assert "zero" in out["reason"]


def test_classify_caught_when_exactly_target_failed():
    """Exactly-one-named-failure is the pass condition."""
    target = "tests/unit/test_x.py::test_target"
    out = mc.classify_outcome(_m(target), applied=True, failed_tests=[target])
    assert out["outcome"] == "caught"


def test_classify_imprecise_when_target_and_others_failed():
    """Patch broke target AND unrelated tests → too broad."""
    target = "tests/unit/test_x.py::test_target"
    failures = [target, "tests/unit/test_other.py::test_unrelated"]
    out = mc.classify_outcome(_m(target), applied=True, failed_tests=failures)
    assert out["outcome"] == "imprecise_patch"
    assert "tests/unit/test_other.py::test_unrelated" in out["unexpected_failures"]


def test_classify_asleep_when_target_did_not_fail_but_others_did():
    """Patch broke unrelated tests but not the targeted guard →
    guard is asleep (whatever the collateral)."""
    target = "tests/unit/test_x.py::test_target"
    failures = ["tests/unit/test_other.py::test_unrelated"]
    out = mc.classify_outcome(_m(target), applied=True, failed_tests=failures)
    assert out["outcome"] == "asleep_guard"


# ---------- aggregate: the status-priority logic ------------------

def _r(guard: str, outcome: str) -> dict:
    return {"guard": guard, "outcome": outcome, "caught": outcome == "caught",
            "duration_ms": 1}


def test_aggregate_passed_when_all_caught():
    payload = mc.aggregate([
        _r("g1", "caught"), _r("g2", "caught"), _r("g3", "caught"),
    ])
    assert payload["status"] == "passed"
    assert payload["caught"] == 3
    assert payload["ran"] == 3
    assert payload["stale_patches"] == []
    assert payload["imprecise_patches"] == []
    assert payload["asleep_guards"] == []


def test_aggregate_status_stale_patch_takes_priority():
    """When any patch rotted, the gate itself is suspect — that
    diagnostic outranks imprecise / asleep."""
    payload = mc.aggregate([
        _r("g1", "caught"),
        _r("g2", "stale_patch"),
        _r("g3", "imprecise_patch"),
        _r("g4", "asleep_guard"),
    ])
    assert payload["status"] == "stale_patch"
    assert payload["stale_patches"] == ["g2"]
    assert payload["imprecise_patches"] == ["g3"]
    assert payload["asleep_guards"] == ["g4"]
    assert payload["caught"] == 1


def test_aggregate_status_imprecise_when_no_stale():
    payload = mc.aggregate([
        _r("g1", "caught"),
        _r("g2", "imprecise_patch"),
        _r("g3", "asleep_guard"),
    ])
    assert payload["status"] == "imprecise_patch"


def test_aggregate_status_asleep_when_only_asleep():
    payload = mc.aggregate([
        _r("g1", "caught"),
        _r("g2", "asleep_guard"),
    ])
    assert payload["status"] == "asleep_guard"


def test_aggregate_failed_when_no_mutations_ran():
    """Empty list is a degenerate signal — the gate ran nothing."""
    payload = mc.aggregate([])
    assert payload["status"] == "failed"
    assert payload["ran"] == 0
