"""Next-wave polish (2026-07-10): transport-level GET retry.

``_GetRetryHttp`` wraps the AuthorizedHttp built by
``_build_authorized_http`` and gives every Resource ONE bounded retry
for GET requests answered with 429/5xx — the floor policy for the long
tail of read call sites that never adopted ``execute_with_retry``.
Non-GET methods must pass through with exactly one wire request.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import _GetRetryHttp


class _Resp(dict):
    """httplib2.Response stand-in: dict of lowercased headers + .status."""

    def __init__(self, status: int, headers: dict[str, str] | None = None):
        super().__init__()
        self.status = status
        for k, v in (headers or {}).items():
            self[k.lower()] = v


class _ScriptedHttp:
    """Fake inner http: returns scripted (resp, content) pairs in order
    and records every request made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.timeout = 30.0  # delegation probe target

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.calls.append((method, uri))
        resp, content = self._responses.pop(0)
        return resp, content


@pytest.fixture
def no_sleep(monkeypatch):
    """Capture (instead of perform) the backoff sleep."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


def test_get_500_retries_once_and_returns_second_response(no_sleep):
    inner = _ScriptedHttp([
        (_Resp(500), b"boom"),
        (_Resp(200), b"ok"),
    ])
    resp, content = _GetRetryHttp(inner).request("https://api.example/x", "GET")
    assert resp.status == 200
    assert content == b"ok"
    assert [m for m, _ in inner.calls] == ["GET", "GET"]
    assert len(no_sleep) == 1


def test_get_retry_is_bounded_to_one(no_sleep):
    """Two 5xx in a row: exactly two wire requests, second response
    returned as-is — the retry never loops."""
    inner = _ScriptedHttp([
        (_Resp(503), b"a"),
        (_Resp(503), b"b"),
    ])
    resp, content = _GetRetryHttp(inner).request("https://api.example/x", "GET")
    assert resp.status == 503
    assert content == b"b"
    assert len(inner.calls) == 2


def test_get_success_makes_single_request(no_sleep):
    inner = _ScriptedHttp([(_Resp(200), b"ok")])
    _GetRetryHttp(inner).request("https://api.example/x", "GET")
    assert len(inner.calls) == 1
    assert no_sleep == []


def test_get_4xx_is_not_retried(no_sleep):
    """404 is a caller bug, not a transient — single request."""
    inner = _ScriptedHttp([(_Resp(404), b"nope")])
    resp, _ = _GetRetryHttp(inner).request("https://api.example/x", "GET")
    assert resp.status == 404
    assert len(inner.calls) == 1


def test_post_5xx_is_never_retried(no_sleep):
    """Mutations keep their safety at the execute_with_retry layer —
    the transport must not blind-retry a POST."""
    inner = _ScriptedHttp([(_Resp(500), b"boom")])
    resp, _ = _GetRetryHttp(inner).request(
        "https://api.example/x", "POST", body=b"{}"
    )
    assert resp.status == 500
    assert len(inner.calls) == 1
    assert no_sleep == []


def test_retry_after_is_honored_as_floor(no_sleep):
    inner = _ScriptedHttp([
        (_Resp(429, {"Retry-After": "3"}), b""),
        (_Resp(200), b"ok"),
    ])
    _GetRetryHttp(inner).request("https://api.example/x", "GET")
    assert len(no_sleep) == 1
    assert no_sleep[0] >= 3.0


def test_retry_after_is_capped(no_sleep):
    """A pathological Retry-After must not wedge the call for minutes."""
    inner = _ScriptedHttp([
        (_Resp(429, {"Retry-After": "9999"}), b""),
        (_Resp(200), b"ok"),
    ])
    _GetRetryHttp(inner).request("https://api.example/x", "GET")
    # Cap (8s) + jitter fraction (<= 0.25).
    assert no_sleep[0] <= 8.25


def test_attribute_access_delegates_to_inner():
    """googleapiclient introspects transport attributes; the wrapper
    must be transparent for everything but request()."""
    inner = _ScriptedHttp([])
    wrapper = _GetRetryHttp(inner)
    assert wrapper.timeout == 30.0
    assert wrapper.calls is inner.calls


def test_build_authorized_http_returns_wrapped_transport(monkeypatch):
    """The production wiring: _build_authorized_http composes the
    wrapper around AuthorizedHttp so every Resource gets the policy."""
    import sys

    from appscriptly import google_api_client as gac

    fake_authorized = MagicMock(name="AuthorizedHttp()")
    fake_mod = MagicMock(
        AuthorizedHttp=MagicMock(return_value=fake_authorized)
    )
    monkeypatch.setitem(sys.modules, "google_auth_httplib2", fake_mod)
    monkeypatch.setitem(sys.modules, "httplib2", MagicMock())

    result = gac._build_authorized_http(MagicMock(name="creds"))
    assert isinstance(result, gac._GetRetryHttp)
    assert result._http is fake_authorized
