"""PR-Δ5 — Multi-tenant hardening: tenant-stamp + assert_tenant_match.

The defensive check belt-and-suspenders the storage layer. Today, no
cross-tenant bug exists; these tests pin the contract so a future bug
(caching, SQL, race condition) gets caught BEFORE wrong-tenant data
flows downstream rather than after.

Three test concerns:

  1. ``_stamp_tenant`` writes the user_id attribute reliably (round-
     trip via getattr returns the stamped value).
  2. ``assert_tenant_match`` accepts matching stamps, raises
     ``TenantIsolationError`` on mismatch, warns silently when the
     stamp is absent, and no-ops correctly for stdio mode
     (expected_user_id is None).
  3. The audit log fires with structured fields on each
     dispatch-relevant event (dispatched / needs_reauth / revoked).
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------
# _stamp_tenant — round-trip contract
# ---------------------------------------------------------------------


def test_stamp_tenant_writes_attribute_readable_via_getattr():
    """The stamp must round-trip — what _stamp_tenant writes is what
    assert_tenant_match reads. Pin the attribute name + behavior here
    so a refactor that renames the attribute (or moves it to a wrapper)
    fires this test instead of silently breaking the assertion chain.
    """
    from google_docs_mcp.credentials import _stamp_tenant

    fake_creds = MagicMock()
    returned = _stamp_tenant(fake_creds, "user-abc-123")
    assert returned is fake_creds, "stamp should return the same object"
    # Direct attribute access uses the exact name assert_tenant_match
    # reads via getattr.
    assert fake_creds._google_docs_mcp_user_id == "user-abc-123"


# ---------------------------------------------------------------------
# assert_tenant_match — the load-bearing check
# ---------------------------------------------------------------------


def test_assert_tenant_match_passes_on_matching_stamp():
    """The common happy-path: creds correctly stamped for the expected
    user. Returns silently (no exception)."""
    from google_docs_mcp._tool_helpers import assert_tenant_match
    from google_docs_mcp.credentials import _stamp_tenant

    creds = _stamp_tenant(MagicMock(), "alice")
    # No exception means the assertion passed.
    assert_tenant_match(creds, "alice")


def test_assert_tenant_match_raises_on_mismatched_stamp():
    """The load-bearing assertion: when the storage layer hands back
    Bob's creds while Alice was requested, raise immediately. This is
    the cross-tenant-leak prevention contract."""
    from google_docs_mcp._tool_helpers import (
        TenantIsolationError,
        assert_tenant_match,
    )
    from google_docs_mcp.credentials import _stamp_tenant

    bobs_creds = _stamp_tenant(MagicMock(), "bob")
    with pytest.raises(TenantIsolationError, match="tenant isolation breach"):
        assert_tenant_match(bobs_creds, "alice")


def test_TenantIsolationError_is_subclass_of_AssertionError():
    """Pinned: ``TenantIsolationError`` must subclass ``AssertionError``
    (not generic ``Exception``) so the standard @workspace_tool envelope
    — which catches HttpError and lets everything else propagate —
    doesn't accidentally translate it into a user-facing 400. Cross-
    tenant leaks fail loud, not soft."""
    from google_docs_mcp._tool_helpers import TenantIsolationError

    assert issubclass(TenantIsolationError, AssertionError)


def test_assert_tenant_match_no_op_when_expected_user_id_is_None():
    """Stdio mode: no per-tenant binding to check. The function must
    return silently rather than fire on the absent stamp (which would
    break every stdio tool call)."""
    from google_docs_mcp._tool_helpers import assert_tenant_match

    # Creds with NO stamp, expected_user_id=None — stdio mode.
    bare_creds = MagicMock(spec=[])  # spec=[] = no auto-mock attributes
    # spec=[] gives us a MagicMock where attribute access raises
    # AttributeError, simulating "really has no _google_docs_mcp_user_id".
    # No exception → assertion passed.
    assert_tenant_match(bare_creds, None)


def test_assert_tenant_match_warns_when_stamp_absent_but_user_id_set(caplog):
    """Stamp absent in HTTP mode = monitoring gap, not an incident.
    Log a WARNING for visibility but don't raise — the absence
    doesn't prove a cross-tenant bug, just that the defensive check
    couldn't run.
    """
    from google_docs_mcp._tool_helpers import assert_tenant_match

    # spec=[] makes _google_docs_mcp_user_id attribute access raise
    # AttributeError, which getattr(..., None) catches and returns
    # None — same as the "stamp absent" production case where some
    # creds resolution path bypassed _stamp_tenant.
    bare_creds = MagicMock(spec=[])
    with caplog.at_level(
        logging.WARNING, logger="google_docs_mcp.audit.tenant_isolation",
    ):
        # No exception — warning only.
        assert_tenant_match(bare_creds, "alice-sub")
    # Log line names the warning + the truncated user_id for
    # operator log review.
    assert any(
        "monitoring gap" in r.message.lower() for r in caplog.records
    ), f"expected 'monitoring gap' in log records; got {caplog.records!r}"


def test_assert_tenant_match_mismatch_emits_error_log(caplog):
    """Before raising, the mismatch path must emit a clear ERROR-level
    log with both user_ids (truncated) so the operator's incident
    response can identify which two tenants were involved without
    fishing through the full traceback."""
    from google_docs_mcp._tool_helpers import (
        TenantIsolationError,
        assert_tenant_match,
    )
    from google_docs_mcp.credentials import _stamp_tenant

    bobs_creds = _stamp_tenant(MagicMock(), "bob-very-long-sub-claim-12345")
    with caplog.at_level(
        logging.ERROR, logger="google_docs_mcp.audit.tenant_isolation",
    ), pytest.raises(TenantIsolationError):
        assert_tenant_match(bobs_creds, "alice-very-long-sub-67890")
    # Log carries the truncated form of BOTH ids so the operator can
    # cross-reference without trusting the exception message.
    error_lines = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_lines, "expected an ERROR-level log line on mismatch"
    error_text = " ".join(error_lines)
    assert "MISMATCH" in error_text
    # Truncated forms (first 8 chars).
    assert "bob-very" in error_text
    assert "alice-ve" in error_text


# ---------------------------------------------------------------------
# Audit log — credentials.py:_emit_tenant_audit_log
# ---------------------------------------------------------------------


def test_emit_tenant_audit_log_records_dispatched_outcome(caplog):
    """The audit log must include the structured ``audit_event`` field
    so a downstream JSON formatter / log shipper can route it. Pinned
    so a refactor doesn't accidentally drop the structured fields."""
    from google_docs_mcp.credentials import _emit_tenant_audit_log

    with caplog.at_level(
        logging.INFO, logger="google_docs_mcp.audit.tenant",
    ):
        _emit_tenant_audit_log(
            "user-alice-sub-12345",
            required_scopes=["scope.a", "scope.b"],
            granted_scopes=["scope.a", "scope.b", "scope.c"],
            outcome="dispatched",
        )

    records = [
        r for r in caplog.records
        if r.name == "google_docs_mcp.audit.tenant"
    ]
    assert records, "no audit log emitted"
    rec = records[-1]
    # Structured fields ride on the ``extra`` dict.
    assert rec.audit_event == "tenant_dispatch"
    assert rec.audit_user_id == "user-alice-sub-12345"
    assert rec.audit_outcome == "dispatched"
    assert rec.audit_required_scopes == ["scope.a", "scope.b"]
    assert rec.audit_granted_scopes == ["scope.a", "scope.b", "scope.c"]


def test_emit_tenant_audit_log_truncates_user_id_in_human_message(caplog):
    """The human-readable message truncates user_id to 8 chars so
    shoulder-surfable terminal log output doesn't leak the full
    ``sub`` claim. The structured field carries the untruncated
    value for downstream correlation."""
    from google_docs_mcp.credentials import _emit_tenant_audit_log

    full_id = "user-extremely-long-sub-claim-from-google-12345"
    with caplog.at_level(
        logging.INFO, logger="google_docs_mcp.audit.tenant",
    ):
        _emit_tenant_audit_log(
            full_id,
            required_scopes=None,
            granted_scopes=None,
            outcome="dispatched",
        )

    rec = caplog.records[-1]
    # Human message has the truncated form (first 8 chars).
    assert "user-ext" in rec.message
    # But never the full id in the human-readable text.
    assert full_id not in rec.message, (
        f"Full user_id leaked into human-readable log message: {rec.message!r}"
    )
    # Structured field DOES carry the full id (for downstream
    # correlation / compliance audit trail).
    assert rec.audit_user_id == full_id


# ---------------------------------------------------------------------
# End-to-end: _stamp_tenant + assert_tenant_match round-trip via
# get_credentials_for_user
# ---------------------------------------------------------------------


def test_get_credentials_for_user_stamps_returned_creds(monkeypatch):
    """The production contract: every credentials object that flows
    out of ``get_credentials_for_user`` MUST be stamped with the
    requesting user_id, so downstream ``assert_tenant_match`` checks
    can verify the binding. This is the regression guard for "someone
    accidentally drops the stamp in a future refactor.\""""
    from google.oauth2.credentials import Credentials

    from google_docs_mcp import credentials as creds_mod
    from google_docs_mcp import user_store

    # Stub user_store to return a valid creds-json payload.
    fake_creds = MagicMock(spec=Credentials)
    fake_creds.valid = True
    fake_creds.scopes = ["scope.a"]

    monkeypatch.setattr(
        user_store, "get_state",
        lambda _user_id: {"google_creds_json": '{"token": "x"}'},
    )
    monkeypatch.setattr(
        creds_mod, "_credentials_from_state",
        lambda _state, _client_config: fake_creds,
    )
    # _check_scopes_or_raise returns its first arg unchanged in the
    # happy path; stub to that shape so we can observe the stamping.
    monkeypatch.setattr(
        creds_mod, "_check_scopes_or_raise",
        lambda creds, *_a, **_kw: creds,
    )

    returned = creds_mod.get_credentials_for_user(
        "alice-sub",
        client_config={"web": {"client_id": "X", "client_secret": "Y"}},
        signing_key=b"k" * 32,
        base_url="https://example.fly.dev",
    )
    # The stamp is the production-contract pin.
    assert getattr(returned, "_google_docs_mcp_user_id", None) == "alice-sub"


def test_get_credentials_for_user_emits_audit_log_on_dispatch(
    monkeypatch, caplog,
):
    """Every successful dispatch must emit a ``dispatched`` audit log
    record. Compliance audit trail anchor — without this the SOC 2
    "who got which creds when" question can't be answered."""
    from google.oauth2.credentials import Credentials

    from google_docs_mcp import credentials as creds_mod
    from google_docs_mcp import user_store

    fake_creds = MagicMock(spec=Credentials)
    fake_creds.valid = True
    fake_creds.scopes = ["scope.a"]

    monkeypatch.setattr(
        user_store, "get_state",
        lambda _user_id: {"google_creds_json": '{"token": "x"}'},
    )
    monkeypatch.setattr(
        creds_mod, "_credentials_from_state",
        lambda _state, _client_config: fake_creds,
    )
    monkeypatch.setattr(
        creds_mod, "_check_scopes_or_raise",
        lambda creds, *_a, **_kw: creds,
    )

    with caplog.at_level(
        logging.INFO, logger="google_docs_mcp.audit.tenant",
    ):
        creds_mod.get_credentials_for_user(
            "alice-sub",
            client_config={"web": {"client_id": "X", "client_secret": "Y"}},
            signing_key=b"k" * 32,
            base_url="https://example.fly.dev",
        )

    dispatched_records = [
        r for r in caplog.records
        if getattr(r, "audit_outcome", None) == "dispatched"
    ]
    assert dispatched_records, (
        "no ``dispatched`` audit record emitted on successful "
        "get_credentials_for_user dispatch"
    )
    assert dispatched_records[-1].audit_user_id == "alice-sub"


def test_get_credentials_for_user_emits_needs_reauth_on_missing_creds(
    monkeypatch, caplog,
):
    """The needs_reauth path must emit a ``needs_reauth`` audit record.
    Same compliance reasoning — re-auth events are part of the audit
    trail (especially for "did the user actually re-consent?")."""
    from google_docs_mcp import credentials as creds_mod
    from google_docs_mcp import user_store

    # No google_creds_json in state → NeedsReauthError path.
    monkeypatch.setattr(user_store, "get_state", lambda _user_id: {})
    # ``_auth_url`` would otherwise try to assemble a real Google
    # OAuth URL from the incomplete stub client_config and fail with
    # "Client secrets is not in the correct format." Stub it so the
    # test stays focused on the audit-log emission rather than the
    # OAuth-URL builder shape (which is exercised by separate tests).
    monkeypatch.setattr(
        creds_mod, "_auth_url",
        lambda *_args, **_kwargs: "https://accounts.google.com/auth?stub",
    )

    with caplog.at_level(
        logging.INFO, logger="google_docs_mcp.audit.tenant",
    ), pytest.raises(creds_mod.NeedsReauthError):
        creds_mod.get_credentials_for_user(
            "bob-sub",
            client_config={"web": {"client_id": "X", "client_secret": "Y"}},
            signing_key=b"k" * 32,
            base_url="https://example.fly.dev",
        )

    reauth_records = [
        r for r in caplog.records
        if getattr(r, "audit_outcome", None) == "needs_reauth"
    ]
    assert reauth_records, "no ``needs_reauth`` audit record emitted"
    assert reauth_records[-1].audit_user_id == "bob-sub"
