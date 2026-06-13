"""Base-tier scope invariants — the free-publish guardrail.

The base tier must request ZERO of Google's RESTRICTED scopes so it
qualifies for the free "sensitive scopes only" verification (no CASA, no
Testing-mode 7-day refresh-token cap). The base-tier redesign dropped
``drive.readonly`` (the only restricted scope we held) by re-plumbing its
two consumers:
  * slides→video frame handoff → bound script POSTs frames to the
    server's signed staging endpoint (no Drive read);
  * legacy .docx ingest (drive_file_id) → deprecated in favor of the
    signed-URL upload path.

These tests lock that in so a future change can't silently re-add a
restricted scope and break the free-publish eligibility.
"""
from __future__ import annotations

# Google's RESTRICTED scopes (the ones that trigger CASA). drive.file and
# the per-product scopes we use are SENSITIVE, not restricted.
_RESTRICTED = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.metadata",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.metadata",
}

# The exact intended connector (HTTP) scope set after the base-tier redesign.
# v2.4.0: + ``calendar`` (read/write) for the calendar service, and
# + ``contacts`` (People API read/write) for the contacts service. BOTH
# are Google-SENSITIVE scopes, NOT in ``_RESTRICTED`` below — so neither
# adds a CASA obligation and the free "sensitive scopes only" verification
# eligibility is preserved (the no-restricted-scope guard below still
# holds for both).
_TARGET_CONNECTOR = {
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
}
# The stdio set is the connector set minus the identity-only scopes.
_TARGET_STDIO = _TARGET_CONNECTOR - {
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
}

_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def test_auth_scopes_has_no_drive_readonly():
    from appscriptly.auth import SCOPES
    assert _READONLY not in SCOPES, (
        "drive.readonly is back in auth.SCOPES — that re-restricts the base "
        "tier (CASA + 7-day refresh cap). It belongs on a future restricted "
        "tier, not the free base."
    )


def test_connector_scopes_has_no_drive_readonly():
    from appscriptly.oauth_google import GOOGLE_API_SCOPES
    assert _READONLY not in GOOGLE_API_SCOPES, (
        "drive.readonly is back in oauth_google.GOOGLE_API_SCOPES (HTTP "
        "connector mode) — same problem; keep the two scope sets in sync."
    )


def test_encode_video_scopes_has_no_drive_readonly():
    from appscriptly.services.apps_script.encode_video import (
        AS_ENCODE_VIDEO_SCOPES,
    )
    assert _READONLY not in AS_ENCODE_VIDEO_SCOPES
    # The encode tool's only Drive op is the MP4 upload → drive.file.
    assert AS_ENCODE_VIDEO_SCOPES == [
        "https://www.googleapis.com/auth/drive.file"
    ]


def test_video_render_scopes_has_no_drive_at_all():
    """The bound renderer POSTs frames to the server, so it needs NO Drive
    scope — only Slides read + UrlFetch."""
    from appscriptly.services.apps_script.video_deck import _RENDER_SCOPES
    assert _READONLY not in _RENDER_SCOPES
    assert "https://www.googleapis.com/auth/drive.file" not in _RENDER_SCOPES
    assert set(_RENDER_SCOPES) == {
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/script.external_request",
    }


def test_no_restricted_scope_in_either_base_set():
    """Belt-and-suspenders: NONE of Google's restricted scopes may appear
    in the base sets (any one would trigger CASA)."""
    from appscriptly.auth import SCOPES
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    for name, scope_set in (
        ("auth.SCOPES", SCOPES),
        ("oauth_google.GOOGLE_API_SCOPES", GOOGLE_API_SCOPES),
    ):
        leaked = _RESTRICTED.intersection(scope_set)
        assert not leaked, f"{name} contains RESTRICTED scope(s): {leaked}"


def test_base_scope_sets_match_intended_exactly():
    """Pin the exact intended end-state (catches an accidental add OR a
    drop of a needed sensitive scope)."""
    from appscriptly.auth import SCOPES
    from appscriptly.oauth_google import GOOGLE_API_SCOPES

    assert set(GOOGLE_API_SCOPES) == _TARGET_CONNECTOR, (
        f"connector scope drift: extra={set(GOOGLE_API_SCOPES) - _TARGET_CONNECTOR}, "
        f"missing={_TARGET_CONNECTOR - set(GOOGLE_API_SCOPES)}"
    )
    assert set(SCOPES) == _TARGET_STDIO, (
        f"stdio scope drift: extra={set(SCOPES) - _TARGET_STDIO}, "
        f"missing={_TARGET_STDIO - set(SCOPES)}"
    )


def test_identity_scopes_unchanged():
    """IDENTITY_SCOPES (the connector required_scopes floor) is still just
    openid + email — we deliberately did NOT add userinfo.profile (nothing
    reads profile claims)."""
    from appscriptly.oauth_google import IDENTITY_SCOPES
    assert set(IDENTITY_SCOPES) == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    }
    assert "https://www.googleapis.com/auth/userinfo.profile" not in IDENTITY_SCOPES
