"""Catches floating-ref supply-chain regressions in .github/workflows/."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def test_no_floating_third_party_action_refs():
    """Third-party actions must be SHA-pinned, not @master/@main."""
    workflows_dir = _REPO / ".github" / "workflows"
    floating_pattern = re.compile(r'uses:\s*(?!actions/)(\S+)@(master|main)\b')

    bad: list[str] = []
    for yml in sorted(workflows_dir.glob("*.yml")):
        text = yml.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            m = floating_pattern.search(line)
            if m:
                bad.append(f"{yml.name}:{line_no} uses {m.group(1)}@{m.group(2)}")

    assert not bad, (
        "Third-party actions must be SHA-pinned (security floor):\n  "
        + "\n  ".join(bad)
        + "\n\nUse: gh api repos/<owner>/<repo>/branches/<branch> --jq .commit.sha"
    )
