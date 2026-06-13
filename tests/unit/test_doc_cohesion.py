"""Mechanically prevent N1-class doc/code drift."""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_readme_access_level_matches_manifest():
    """README's stated Apps Script access level must equal _MANIFEST."""
    from appscriptly.setup_apps_script import _MANIFEST
    actual = _MANIFEST["webapp"]["access"]

    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    m = re.search(r'access:\s*([A-Z_]+)', readme)
    assert m, "README does not state the Apps Script access level"
    claimed = m.group(1)
    assert claimed == actual, (
        f"README claims access:{claimed} but _MANIFEST sets access:{actual}. "
        f"Security-relevant doc bug — users believe deploy is more restricted "
        f"than reality. Update README.md or change _MANIFEST."
    )
