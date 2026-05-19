"""HKDF key derivation from MCP_BEARER_TOKEN master.

Per-purpose key separation: deriving distinct keys for ``api_bearer``,
``oauth_state``, and ``signed_url`` purposes from one master means
rotating the master invalidates all three atomically without per-purpose
key management.

v1.3.1 ships with a back-compat shim: the 3 purposes above STILL return
the raw master so existing claude.ai connectors + in-flight signed URLs
continue working unchanged. Operators see ``key_back_compat_shim_active:
true`` in ``gdocs_server_info`` (added in v1.4.0). The shim removes in
v2.0+ alongside the tool-consolidation Option B shim window.

Threat-model notes (see THREAT_MODEL §5 once that doc lands):

- The 32-char master length check is DEFERRED to first ``get_key()``
  call rather than module import. Short legacy tokens (pre-v1.3.1,
  often 16 chars) must boot the server cleanly and only fail loudly
  when a derived-key purpose is requested. The back-compat shim
  returns the raw master without invoking the length check at all.

- v1.3.1 ships the shim path active for all 3 purposes; no caller in
  v1.3.1 actually exercises the HKDF derived path. The derived code
  is present-but-inactive so v2.0's strict-flip is a small,
  reviewable change rather than a new-module introduction under
  time pressure.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Literal

# Purposes that return raw master via the back-compat shim. Removal of
# any entry constitutes a breaking change requiring CHANGELOG note +
# major bump. See v2.0+ release plan.
_BACK_COMPAT_RAW_MASTER: frozenset[str] = frozenset(
    {"api_bearer", "oauth_state", "signed_url"}
)

# HKDF info-context per purpose. Changing any string here invalidates
# the derived key for that purpose — same blast radius as rotating
# the master. Never change these once they leave the shim.
_HKDF_INFO: dict[str, bytes] = {
    "api_bearer": b"google-docs-mcp v1 api_bearer",
    "oauth_state": b"google-docs-mcp v1 oauth_state",
    "signed_url": b"google-docs-mcp v1 signed_url",
}

Purpose = Literal["api_bearer", "oauth_state", "signed_url"]

_MIN_MASTER_LEN = 32


@dataclass(frozen=True)
class KeyProvenance:
    """How a key was sourced — for ``gdocs_server_info`` introspection."""
    purpose: str
    mechanism: Literal["raw_master_shim", "hkdf_derived"]
    master_len: int


def _master() -> str:
    val = os.environ.get("MCP_BEARER_TOKEN")
    if not val:
        raise RuntimeError(
            "MCP_BEARER_TOKEN env var is required for key derivation"
        )
    return val


def _validate_master_or_raise(master: str) -> None:
    """Enforce ≥32 chars. Called only when derivation is actually needed.

    NEVER call at module import — short legacy tokens must boot cleanly
    and only fail when a derived purpose is requested. The shim path
    skips this entirely.
    """
    if len(master) < _MIN_MASTER_LEN:
        raise RuntimeError(
            f"MCP_BEARER_TOKEN must be ≥{_MIN_MASTER_LEN} chars for "
            f"HKDF derivation (got {len(master)}). Either lengthen "
            f"the token or use a purpose currently in the back-compat "
            f"shim. See THREAT_MODEL.md §5 for rotation guidance."
        )


def _hkdf_sha256(master_bytes: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-Extract+Expand using HMAC-SHA256, salt='' for our use.

    Simple inline implementation — single dep on stdlib ``hmac`` +
    ``hashlib`` keeps ``keys.py`` import-free of cryptography for the
    shim path. The full ``cryptography`` lib is used elsewhere but
    avoiding the import here keeps boot fast and minimizes surface.

    Currently UNREACHED in v1.3.1 (all live purposes route through the
    shim) but verified via unit tests so v2.0's strict-flip is a
    pure-config change.
    """
    # Extract: PRK = HMAC-SHA256(salt=b"", IKM=master_bytes)
    prk = hmac.new(b"", master_bytes, hashlib.sha256).digest()
    # Expand: T(1) = HMAC-SHA256(PRK, info || 0x01)
    t = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return t[:length]


def get_key(purpose: Purpose) -> bytes:
    """Return key bytes for ``purpose``. Raw master (shim) or HKDF-derived.

    Length check on master happens HERE, not at import, and only when
    the derivation path is exercised. Shim path bypasses entirely.

    Raises ``ValueError`` for unknown purposes (defensive — purposes
    are a closed set; an unknown one is a typo, never a runtime case).
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")

    master = _master()

    if purpose in _BACK_COMPAT_RAW_MASTER:
        # Shim: return raw master bytes. No length check.
        return master.encode("utf-8")

    # Derived path: enforce master length first.
    _validate_master_or_raise(master)
    return _hkdf_sha256(master.encode("utf-8"), _HKDF_INFO[purpose])


def key_provenance(purpose: Purpose) -> KeyProvenance:
    """Report how a key WOULD be sourced for ``purpose`` — without deriving.

    Used by ``gdocs_server_info`` (v1.4.0+) to surface shim state
    without invoking the actual key material into a tool response.
    """
    if purpose not in _HKDF_INFO:
        raise ValueError(f"Unknown key purpose: {purpose!r}")
    master = _master()
    if purpose in _BACK_COMPAT_RAW_MASTER:
        return KeyProvenance(
            purpose=purpose, mechanism="raw_master_shim",
            master_len=len(master),
        )
    return KeyProvenance(
        purpose=purpose, mechanism="hkdf_derived",
        master_len=len(master),
    )


def is_shim_active() -> bool:
    """True if any purpose currently routes through the raw-master shim.

    Consumed by ``gdocs_server_info`` (v1.4.0+) so operators see the
    shim is active before v2.0's strict-flip deploy. Currently always
    True in v1.3.1; flips to False per-purpose as v2.0 removes
    entries from ``_BACK_COMPAT_RAW_MASTER``.
    """
    return bool(_BACK_COMPAT_RAW_MASTER)
