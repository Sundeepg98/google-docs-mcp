"""Single-source-of-truth proof for the OAuth consent scope union.

Hardening-P1 (ROADMAP_SPECS #7). Historically ``auth.SCOPES`` (the
stdio/baseline Workspace consent set) and ``oauth_google.GOOGLE_API_SCOPES``
(the HTTP/connector set = those scopes + the OIDC identity scopes) were
TWO independently hand-edited lists, each carrying a "keep in sync BY
HAND" comment — a textbook drift trap. They are now both DERIVED from a
single source (``auth.WORKSPACE_SCOPES``) plus the OIDC identity scopes.

This file is the drift guard. It does two complementary things:

1. **Pins the exact consent scope sets against hard-coded literals**
   (frozenset equality). This is the verify-LAST safety net: the MCP is
   mid-OAuth-verification, so the consent scope SET must not change. If a
   future edit to ``WORKSPACE_SCOPES`` adds / removes / restricts a
   scope, the literal here won't match and CI fails — forcing an explicit
   operator decision rather than a silent consent-screen change.

2. **Proves the derivation wiring** — that ``auth.SCOPES`` really is
   ``WORKSPACE_SCOPES`` and ``GOOGLE_API_SCOPES`` really is
   ``IDENTITY_SCOPES + WORKSPACE_SCOPES`` — so the two lists *cannot*
   drift apart by construction. If someone "fixes a drift" by re-typing a
   literal into one of the two derived lists (reintroducing the twin),
   the relationship assertions below break.

If you intentionally change the consent scopes (operator-gated), update
the ``_EXPECTED_*`` literals here in the SAME commit and document the
scope change in the PR — that is the conscious gate this test exists to
enforce.
"""
from __future__ import annotations

# ---------------------------------------------------------------------
# The exact, current consent scope sets — the SOURCE OF TRUTH for this
# test. These mirror what Google's consent screen requests today.
#
#   * 9 Workspace scopes  → auth.SCOPES (stdio / baseline)
#   * +2 OIDC identity     → oauth_google.GOOGLE_API_SCOPES (HTTP) = 11
#
# Beyond the original 6, three SENSITIVE (NOT restricted → no CASA)
# Workspace scopes have been added by deliberate, operator-directed
# consent-set changes: ``calendar`` (read/write, services/calendar/),
# ``contacts`` (read/write, People API, services/contacts/), and
# ``tasks`` (read/write, Google Tasks, services/tasks/). Each literal
# below is updated in the SAME commit as its scope addition — the
# conscious verify-LAST gate this test enforces.
#
# Frozensets: scope SET identity is what matters for consent (Google
# ignores order on the screen). Order is checked separately below via the
# ordered-list assertions so a future reorder is still caught as a
# (benign) change rather than passing silently.
# ---------------------------------------------------------------------
_EXPECTED_WORKSPACE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
})

_EXPECTED_OIDC_SCOPES = frozenset({
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
})

_EXPECTED_GOOGLE_API_SCOPES = _EXPECTED_OIDC_SCOPES | _EXPECTED_WORKSPACE_SCOPES

# The exact ordered literals (byte-for-byte the pre-refactor hand lists).
# Used to prove the derivation preserved ordering, not just set identity.
_EXPECTED_SCOPES_ORDERED = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
]
_EXPECTED_GOOGLE_API_SCOPES_ORDERED = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
]


# ---------------------------------------------------------------------
# 1. The verify-LAST safety net: exact consent SET must not change.
# ---------------------------------------------------------------------


def test_stdio_consent_set_is_exactly_the_six_workspace_scopes():
    """``auth.SCOPES`` (stdio/baseline) == the exact Workspace scope set
    (9 after the calendar + contacts + tasks sensitive-scope additions).

    A mismatch means the stdio consent screen would request a different
    scope set. Under verify-LAST that is operator-gated — update the
    ``_EXPECTED_WORKSPACE_SCOPES`` literal here (same commit) only when a
    scope change is deliberate. (Count history: 6 → 7 calendar → 8
    contacts → 9 tasks, each a deliberate sensitive-scope addition.)
    """
    from appscriptly.auth import SCOPES

    assert frozenset(SCOPES) == _EXPECTED_WORKSPACE_SCOPES, (
        f"stdio consent scope drift: "
        f"extra={frozenset(SCOPES) - _EXPECTED_WORKSPACE_SCOPES}, "
        f"missing={_EXPECTED_WORKSPACE_SCOPES - frozenset(SCOPES)}"
    )


def test_connector_consent_set_is_exactly_oidc_plus_workspace():
    """``oauth_google.GOOGLE_API_SCOPES`` (HTTP/connector) == the exact 11
    scopes (2 OIDC + 9 Workspace).

    Same verify-LAST gate as the stdio set: this is the consent screen
    claude.ai's connector flow renders. (Count history: 8 → 9 calendar →
    10 contacts → 11 tasks, each a deliberate sensitive-scope addition.)
    """
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    assert frozenset(GOOGLE_API_SCOPES) == _EXPECTED_GOOGLE_API_SCOPES, (
        f"connector consent scope drift: "
        f"extra={frozenset(GOOGLE_API_SCOPES) - _EXPECTED_GOOGLE_API_SCOPES}, "
        f"missing={_EXPECTED_GOOGLE_API_SCOPES - frozenset(GOOGLE_API_SCOPES)}"
    )


def test_no_duplicate_scopes_in_either_consent_list():
    """Neither derived list may carry a duplicate scope.

    A duplicate would mean a scope was both inherited from the single
    source AND re-typed locally (a half-reintroduced twin), or that the
    OIDC + Workspace partitions overlap. Either is a wiring smell.
    """
    from appscriptly.auth import SCOPES
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    assert len(SCOPES) == len(set(SCOPES)), (
        f"auth.SCOPES has duplicates: {SCOPES}"
    )
    assert len(GOOGLE_API_SCOPES) == len(set(GOOGLE_API_SCOPES)), (
        f"GOOGLE_API_SCOPES has duplicates: {GOOGLE_API_SCOPES}"
    )


# ---------------------------------------------------------------------
# 2. The single-source wiring: the two lists are DERIVED, not twins.
# ---------------------------------------------------------------------


def test_auth_scopes_is_the_single_source_workspace_list():
    """``auth.SCOPES`` is exactly ``auth.WORKSPACE_SCOPES``.

    If this breaks, someone re-typed a separate literal into ``SCOPES``
    instead of letting it derive from the single source — reintroducing
    the drift surface this refactor removed.
    """
    from appscriptly.auth import SCOPES, WORKSPACE_SCOPES

    assert SCOPES == WORKSPACE_SCOPES
    # Same object: SCOPES is a public alias of the single source, not a copy.
    assert SCOPES is WORKSPACE_SCOPES


def test_google_api_scopes_is_derived_from_the_single_source():
    """``GOOGLE_API_SCOPES`` == ``IDENTITY_SCOPES`` + ``WORKSPACE_SCOPES``.

    This is THE single-source guarantee: the connector list is mechanically
    the identity scopes followed by the same Workspace scopes the stdio
    list uses. Adding a Workspace scope to ``WORKSPACE_SCOPES`` therefore
    updates BOTH consent sets with no second edit; they cannot drift.
    """
    from appscriptly.auth import WORKSPACE_SCOPES
    from appscriptly.oauth_google import GOOGLE_API_SCOPES, IDENTITY_SCOPES

    assert GOOGLE_API_SCOPES == [*IDENTITY_SCOPES, *WORKSPACE_SCOPES], (
        "GOOGLE_API_SCOPES is no longer derived as IDENTITY_SCOPES + "
        "WORKSPACE_SCOPES — the twin-list drift surface may have returned."
    )


def test_connector_minus_identity_equals_stdio():
    """The connector set minus the OIDC identity scopes == the stdio set.

    Cross-check of the derivation from the consumer's angle: HTTP and
    stdio request the SAME Workspace scopes; they differ only by the two
    identity scopes the connector needs to identify the user.
    """
    from appscriptly.auth import SCOPES
    from appscriptly.oauth_google import GOOGLE_API_SCOPES, IDENTITY_SCOPES

    assert frozenset(GOOGLE_API_SCOPES) - frozenset(IDENTITY_SCOPES) == frozenset(SCOPES)


# ---------------------------------------------------------------------
# 3. Ordering preserved (proves the refactor was byte-identical, not just
#    set-identical — keeps diffs / any log or metadata snapshots stable).
# ---------------------------------------------------------------------


def test_stdio_scope_ordering_unchanged():
    """``auth.SCOPES`` order is byte-identical to the pre-refactor literal."""
    from appscriptly.auth import SCOPES

    assert list(SCOPES) == _EXPECTED_SCOPES_ORDERED


def test_connector_scope_ordering_unchanged():
    """``GOOGLE_API_SCOPES`` order is byte-identical to the pre-refactor
    literal: OIDC identity scopes first, then the Workspace scopes."""
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    assert list(GOOGLE_API_SCOPES) == _EXPECTED_GOOGLE_API_SCOPES_ORDERED
