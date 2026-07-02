"""/health payload contract (deploy-standard hardening, 2026-07-02).

The public, unauthenticated ``/health`` endpoint now carries a
``git_commit`` field: the deployed short SHA, baked by deploy.yml's
``GIT_COMMIT`` build-arg into the Dockerfile's ``ENV GIT_COMMIT`` and
read from the environment at request time (identical sourcing to the
bearer-authed ``/info`` endpoint). This gives the prod-drift monitor
and the deploy smoke check an unauthenticated "what commit is prod
serving" read; before this the only public drift signal was the scope
count.

These tests exercise the REAL handler from
``appscriptly.http_server.routes.observability`` (the middleware tests
in test_http_server_middleware.py mount the same handler behind the
TrustedHost exemption; this file pins the payload contract itself).
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _build_health_app() -> Starlette:
    """Minimal Starlette app with just the real /health route mounted,
    mirroring the production wiring in ``http_server.app`` for this
    endpoint (no middleware: /health is public and host-exempt)."""
    from appscriptly.http_server.routes.observability import health

    return Starlette(routes=[Route("/health", health, methods=["GET"])])


def test_health_returns_200_json():
    """Fly's probe contract: HTTP 200 + JSON-parseable body."""
    client = TestClient(_build_health_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


def test_health_payload_keeps_ok_and_service_fields():
    """Operators grep the ``service`` field (log-aggregation rules);
    ``ok`` is the liveness bit. Both survive the git_commit addition -
    the new field is strictly additive."""
    client = TestClient(_build_health_app())
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["service"] == "appscriptly"


def test_health_git_commit_sourced_from_env(monkeypatch):
    """Provenance chain: deploy.yml passes GIT_COMMIT as a --build-arg,
    the Dockerfile bakes it via ``ARG GIT_COMMIT=unknown`` + ``ENV
    GIT_COMMIT``, and the handler reads the env at request time. When
    the env is set (a CI-built image), /health must surface it."""
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    client = TestClient(_build_health_app())
    body = client.get("/health").json()
    assert body["git_commit"] == "abc1234"


def test_health_git_commit_falls_back_to_unknown_without_env(monkeypatch):
    """A local run (vanilla ``docker build`` or bare ``appscriptly``)
    has no GIT_COMMIT env; the field must degrade to "unknown", never
    crash or disappear (the drift monitor treats "unknown" as
    "stamp not available", a warning rather than a failure)."""
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    client = TestClient(_build_health_app())
    body = client.get("/health").json()
    assert body["git_commit"] == "unknown"


def test_health_payload_has_exactly_the_documented_fields(monkeypatch):
    """Pin the full key set so an accidental field drop OR an
    accidental leak of something new into the PUBLIC unauthenticated
    payload is a deliberate, test-visible act (a short SHA of a public
    repo leaks nothing; anything more needs review)."""
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    client = TestClient(_build_health_app())
    body = client.get("/health").json()
    assert body == {
        "ok": True,
        "service": "appscriptly",
        "git_commit": "abc1234",
    }
