"""Service Account + DWD auth path.

Guards the opt-in headless setup mode for Workspace users / CI.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_load_sa_raises_clear_error_when_key_missing(tmp_path):
    from google_docs_mcp.auth import load_service_account_credentials

    missing = tmp_path / "no_such_key.json"
    with pytest.raises(FileNotFoundError, match="Service account key file"):
        load_service_account_credentials(missing, "user@example.com", ["scope"])


def test_load_sa_invokes_with_subject_for_impersonation(tmp_path):
    """The whole point of DWD: SA impersonates a specific user via with_subject()."""
    from google_docs_mcp import auth

    fake_key = tmp_path / "key.json"
    fake_key.write_text('{"type":"service_account"}')

    fake_creds = MagicMock()
    fake_impersonated = MagicMock()
    fake_creds.with_subject.return_value = fake_impersonated

    with patch.object(
        auth.service_account.Credentials, "from_service_account_file",
        return_value=fake_creds,
    ) as mock_from_file:
        result = auth.load_service_account_credentials(
            fake_key, "operator@workspace.com", ["scope.a", "scope.b"],
        )

    mock_from_file.assert_called_once_with(
        str(fake_key), scopes=["scope.a", "scope.b"],
    )
    fake_creds.with_subject.assert_called_once_with("operator@workspace.com")
    assert result is fake_impersonated


def test_setup_auto_requires_impersonate_user_in_sa_mode(tmp_path):
    """sa_key without impersonate_user is a misuse — must fail loudly."""
    from google_docs_mcp.setup_apps_script import setup_apps_script_auto

    fake_key = tmp_path / "key.json"
    fake_key.write_text("{}")

    with pytest.raises(ValueError, match="impersonate_user"):
        setup_apps_script_auto(
            service_account_key=fake_key,
            impersonate_user=None,
        )
