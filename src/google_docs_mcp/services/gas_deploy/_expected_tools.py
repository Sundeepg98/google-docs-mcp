"""Declared tool surface for the gas_deploy service.

See ``services/docs/_expected_tools.py`` for the decentralized-witness
rationale. PR-α (v2.3.4) reframed the canonical name from
``gdocs_setup_apps_script`` to ``gdocs_install_automation`` and kept
the old name registered as a deprecation alias — BOTH names register
(one underlying installer, two registrations), so BOTH are declared
here. Planned removal of the alias in v3.0.
"""
from __future__ import annotations

EXPECTED: frozenset[str] = frozenset({
    "gdocs_install_automation",   # PR-α canonical name
    "gdocs_setup_apps_script",    # deprecated alias (planned removal v3.0)
})
