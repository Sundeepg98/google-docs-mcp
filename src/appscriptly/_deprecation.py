"""Shared helper for tool-name deprecation aliases (namespace cleanup).

The tool-namespace cleanup (``chore/tool-namespace-cleanup``) renamed 18
tools off the historical ``gdocs_`` prefix to honest domain prefixes
(``gdrive_`` for Drive, ``server_`` / ``admin_`` / ``account_`` for the
admin/auth tools, ``as_`` for the Apps Script installer). Every old
``gdocs_`` name stays registered as a DEPRECATED ALIAS so existing
prompts, saved automations, and external integrations keep working —
exactly the dual-registration model already used for
``gdocs_setup_apps_script`` → ``gdocs_install_automation`` (PR-α).

This module holds the ONE shared piece of that pattern: the runtime
``DeprecationWarning`` emitter. The per-alias function bodies still live
next to their canonical tool (so the location witnesses — which require
every registered name to be a module-level attr of its service module —
keep passing), but they all route their warning through
``warn_deprecated_alias`` so the message wording is uniform and lives in
exactly one place.

The aliases are intentionally given a one-release+ window; planned
removal tracked alongside the existing ``gdocs_setup_apps_script`` alias
(v3.0). The canonical names are the ones documented in the server
instructions / ``gdocs_guide`` payload.
"""
from __future__ import annotations

import warnings


def warn_deprecated_alias(old_name: str, new_name: str) -> None:
    """Emit a uniform ``DeprecationWarning`` for a renamed tool.

    Args:
        old_name: the deprecated ``gdocs_``-era tool name the caller
            invoked (the alias).
        new_name: the canonical replacement tool name the caller should
            migrate to.

    The wording mirrors the existing ``gdocs_setup_apps_script``
    deprecation copy: identical behavior, name-only change, planned
    removal in v3.0. ``stacklevel=3`` so the warning points at the
    caller of the tool, not this helper or the alias wrapper.
    """
    warnings.warn(
        f"{old_name} is deprecated; use {new_name} instead. The tool was "
        f"renamed off the historical 'gdocs_' prefix to an honest domain "
        f"prefix (the behavior is identical — a name-only change). The old "
        f"name stays registered as an alias and is slated for removal in "
        f"v3.0.",
        DeprecationWarning,
        stacklevel=3,
    )
