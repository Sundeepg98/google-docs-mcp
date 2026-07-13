"""Stream 6 / PR-G pins for generated-manifest scopes + consent-visible names.

N-S3V-1: ``container_data_scope`` maps each bound container kind to its
NON-restricted ``.currentonly`` data scope. The per-installer wiring (each
generated manifest actually carries its container's data scope) is pinned in
the installers' own test files; this file pins the shared helper.

N-S3V-2 (HARD RULE): a generated project NAME is consent-visible (it renders
in the OAuth consent screen). An em-dash / en-dash there violates the no-dash
rule. Pin it on ALL generated project-name builders so a future default name
cannot regress a dash back into the consent flow.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from appscriptly.services.apps_script import api

# The four container kinds that carry a bound-container data scope.
_KINDS = ["sheets", "docs", "slides", "forms"]

# Every module that builds a generated project name via ``name or f"..."``.
_NAME_BUILDER_MODULES = [
    "doc_menu",
    "sheet_menu",
    "slides_menu",
    "sheet_dashboard",
    "edit_trigger",
    "form_handler",
    "contact_sync",
    "calendar_sync",
    "task_rollover",
    "grade_form_responses",
    "refresh_linked_slides",
    "video_deck",
    "custom_function",
    "tools",
]

_EM_DASH = "—"
_EN_DASH = "–"


# ---------------------------------------------------------------------
# N-S3V-1 — container-data scope helper
# ---------------------------------------------------------------------


def test_container_data_scopes_cover_the_four_bound_kinds():
    assert set(api.CONTAINER_DATA_SCOPES) == set(_KINDS)


@pytest.mark.parametrize("kind", _KINDS)
def test_container_data_scope_is_currentonly_and_non_restricted(kind):
    """Each container-data scope is a ``.currentonly`` scope (least-privilege,
    the bound file only) and NON-restricted (so the automation stays no-CASA
    and the scope raises no consent warning on its own)."""
    scope = api.container_data_scope(kind)
    assert scope == api.CONTAINER_DATA_SCOPES[kind]
    assert scope.endswith(".currentonly"), scope
    assert scope.startswith("https://www.googleapis.com/auth/"), scope
    assert not api.is_restricted_scope(scope), (
        f"{scope} must be NON-restricted (N-S3V-1 stays no-CASA)"
    )


def test_container_data_scope_exact_strings():
    assert api.container_data_scope("sheets") == (
        "https://www.googleapis.com/auth/spreadsheets.currentonly"
    )
    assert api.container_data_scope("docs") == (
        "https://www.googleapis.com/auth/documents.currentonly"
    )
    assert api.container_data_scope("slides") == (
        "https://www.googleapis.com/auth/presentations.currentonly"
    )
    assert api.container_data_scope("forms") == (
        "https://www.googleapis.com/auth/forms.currentonly"
    )


def test_container_data_scope_unknown_kind_raises():
    with pytest.raises(ValueError, match="no container-data scope"):
        api.container_data_scope("calendar")


# ---------------------------------------------------------------------
# N-S3V-2 — no em/en dash in any consent-visible generated project name
# ---------------------------------------------------------------------


@pytest.mark.parametrize("mod_name", _NAME_BUILDER_MODULES)
def test_default_project_name_builder_has_no_em_or_en_dash(mod_name):
    """The default project-name builder (``name or f"appscriptly ..."``) is
    consent-visible; it MUST NOT emit an em/en dash. Scans the builder line so
    a regression on ANY installer's default name is caught (N-S3V-2)."""
    mod = importlib.import_module(f"appscriptly.services.apps_script.{mod_name}")
    offenders = [
        ln.strip()
        for ln in inspect.getsource(mod).splitlines()
        if "name or" in ln and (_EM_DASH in ln or _EN_DASH in ln)
    ]
    assert not offenders, (
        f"{mod_name}: em/en dash in a consent-visible default project-name "
        f"builder (hard rule - it renders in the OAuth consent screen): "
        f"{offenders}"
    )
