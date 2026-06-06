"""Unit tests for the Google API transport socket-timeout hardening.

ROADMAP Hardening-P1: ``build()`` previously used a bare ``httplib2.Http()``
with no deadline, so a stalled connection hung ``.execute()`` forever and
never raised — silently defeating the retry layer (tenacity can only retry
an attempt that *finishes* by raising).

These tests pin two properties:

1. ``GoogleApiClientAdapter.get_service`` constructs the SDK client with a
   transport that carries a socket timeout (the default, and the
   env-overridden value), and passes it via ``http=`` (NOT ``credentials=``).
2. A simulated transport timeout on an *idempotent* call triggers a RETRY
   through ``RetryingGoogleApiClientAdapter.execute_with_retry`` rather than
   crashing — and is NOT retried when the call is non-idempotent.
"""
from __future__ import annotations

import errno
import socket
from unittest.mock import MagicMock, patch

import pytest

from appscriptly import google_api_client as gac
from appscriptly.google_api_client import (
    GoogleApiClientAdapter,
    InMemoryGoogleAPIClient,
    RetryingGoogleApiClientAdapter,
    _DEFAULT_HTTP_TIMEOUT_SECONDS,
    _is_retryable,
    _is_retryable_transport_error,
    _resolve_http_timeout_seconds,
)


# ---------------------------------------------------------------------
# (1) The client is constructed WITH a timeout-bearing transport
# ---------------------------------------------------------------------


class TestAdapterAppliesTimeout:
    def test_get_service_builds_http_with_default_timeout(self, monkeypatch):
        """Default path: httplib2.Http is built with the 30s default and
        wrapped in AuthorizedHttp, which is handed to build(http=...)."""
        monkeypatch.delenv("GOOGLE_API_HTTP_TIMEOUT_SECONDS", raising=False)

        fake_http_instance = MagicMock(name="httplib2.Http()")
        fake_http_cls = MagicMock(name="httplib2.Http", return_value=fake_http_instance)
        fake_authorized = MagicMock(name="AuthorizedHttp()")
        fake_authorized_cls = MagicMock(
            name="AuthorizedHttp", return_value=fake_authorized
        )
        fake_build = MagicMock(name="build", return_value=MagicMock(name="Resource"))

        creds = MagicMock(name="credentials")

        with patch.dict(
            "sys.modules",
            {
                "httplib2": MagicMock(Http=fake_http_cls),
                "google_auth_httplib2": MagicMock(AuthorizedHttp=fake_authorized_cls),
            },
        ), patch.object(gac, "build", fake_build):
            GoogleApiClientAdapter().get_service("docs", "v1", credentials=creds)

        # httplib2.Http was constructed WITH a timeout, equal to the default.
        fake_http_cls.assert_called_once()
        _, http_kwargs = fake_http_cls.call_args
        assert http_kwargs.get("timeout") == _DEFAULT_HTTP_TIMEOUT_SECONDS
        assert http_kwargs["timeout"] == pytest.approx(30.0)

        # The credentials were wrapped around that timeout-bearing transport.
        fake_authorized_cls.assert_called_once_with(creds, http=fake_http_instance)

        # build() received the authorized transport via http=, NOT credentials=.
        fake_build.assert_called_once()
        build_args, build_kwargs = fake_build.call_args
        assert build_args == ("docs", "v1")
        assert build_kwargs.get("http") is fake_authorized
        assert "credentials" not in build_kwargs

    def test_get_service_honors_env_timeout_override(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_HTTP_TIMEOUT_SECONDS", "5")

        fake_http_cls = MagicMock(return_value=MagicMock())
        fake_authorized_cls = MagicMock(return_value=MagicMock())
        fake_build = MagicMock(return_value=MagicMock())

        with patch.dict(
            "sys.modules",
            {
                "httplib2": MagicMock(Http=fake_http_cls),
                "google_auth_httplib2": MagicMock(AuthorizedHttp=fake_authorized_cls),
            },
        ), patch.object(gac, "build", fake_build):
            GoogleApiClientAdapter().get_service(
                "drive", "v3", credentials=MagicMock()
            )

        _, http_kwargs = fake_http_cls.call_args
        assert http_kwargs.get("timeout") == pytest.approx(5.0)


class TestTimeoutResolution:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_HTTP_TIMEOUT_SECONDS", raising=False)
        assert _resolve_http_timeout_seconds() == _DEFAULT_HTTP_TIMEOUT_SECONDS

    def test_positive_override(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_HTTP_TIMEOUT_SECONDS", "12.5")
        assert _resolve_http_timeout_seconds() == pytest.approx(12.5)

    @pytest.mark.parametrize("bad", ["abc", "", "  ", "0", "-3", "nan"])
    def test_malformed_or_nonpositive_falls_back_to_default(self, monkeypatch, bad):
        # A bad env var must NEVER silently disable the deadline.
        monkeypatch.setenv("GOOGLE_API_HTTP_TIMEOUT_SECONDS", bad)
        result = _resolve_http_timeout_seconds()
        # "nan" parses to float but is not > 0 in the (value <= 0) sense?
        # nan comparisons are False, so guard explicitly: result must be the
        # finite default for every malformed/non-positive input.
        assert result == _DEFAULT_HTTP_TIMEOUT_SECONDS


# ---------------------------------------------------------------------
# (2) A simulated timeout is classified retryable + actually retries
# ---------------------------------------------------------------------


class TestTransportErrorClassification:
    def test_socket_timeout_is_retryable(self):
        assert _is_retryable_transport_error(socket.timeout("timed out")) is True
        assert _is_retryable(socket.timeout("timed out")) is True

    def test_timeouterror_is_retryable(self):
        # socket.timeout is an alias for TimeoutError on 3.10+, but assert
        # the bare TimeoutError too for clarity.
        assert _is_retryable_transport_error(TimeoutError()) is True

    def test_connection_reset_is_retryable(self):
        assert _is_retryable_transport_error(ConnectionResetError()) is True

    def test_oserror_etimedout_is_retryable(self):
        assert _is_retryable_transport_error(OSError(errno.ETIMEDOUT, "timed out")) is True

    def test_unrelated_oserror_is_not_retryable(self):
        # ENOENT (bad cert path, etc.) is a config bug, not a transient blip.
        assert _is_retryable_transport_error(OSError(errno.ENOENT, "no file")) is False

    def test_value_error_is_not_retryable(self):
        assert _is_retryable_transport_error(ValueError("bug")) is False
        assert _is_retryable(ValueError("bug")) is False


class TestTimeoutTriggersRetry:
    def _fast_adapter(self):
        # Zero waits so the test doesn't actually sleep through backoff.
        return RetryingGoogleApiClientAdapter(
            InMemoryGoogleAPIClient(),
            max_attempts=3,
            base_wait_seconds=0.0,
            max_wait_seconds=0.0,
        )

    def test_idempotent_timeout_retries_then_succeeds(self):
        adapter = self._fast_adapter()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise socket.timeout("read timed out")
            return "ok"

        result = adapter.execute_with_retry(flaky, idempotent=True, op_name="probe")
        assert result == "ok"
        assert calls["n"] == 3  # retried twice, succeeded on the 3rd attempt

    def test_idempotent_timeout_exhausts_and_reraises_real_error(self):
        adapter = self._fast_adapter()
        calls = {"n": 0}

        def always_timeout():
            calls["n"] += 1
            raise socket.timeout("read timed out")

        # After max_attempts it re-raises the REAL socket.timeout (reraise=True),
        # not a tenacity RetryError, and not an unhandled hang.
        with pytest.raises(socket.timeout):
            adapter.execute_with_retry(
                always_timeout, idempotent=True, op_name="probe"
            )
        assert calls["n"] == 3  # all attempts consumed

    def test_non_idempotent_timeout_is_not_retried(self):
        adapter = self._fast_adapter()
        calls = {"n": 0}

        def mutating():
            calls["n"] += 1
            raise socket.timeout("read timed out")

        # Non-idempotent: exactly one attempt, exception propagates (a
        # partially-applied mutation must never be silently replayed).
        with pytest.raises(socket.timeout):
            adapter.execute_with_retry(
                mutating, idempotent=False, op_name="mutate"
            )
        assert calls["n"] == 1
