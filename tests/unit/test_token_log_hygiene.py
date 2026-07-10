"""N4 (2026-07-10 retest, SECURITY): bearer tokens must never reach logs.

The incident: fastmcp's ``GoogleTokenVerifier`` validates every MCP call
with ``GET oauth2.googleapis.com/tokeninfo?access_token=<live token>``,
and httpx INFO-logs the full request URL - cleartext, replayable OAuth
access tokens in the Fly log stream.

Two layers under test:

1. ``oauth_google._TokenInfoBodyClient`` (primary): the httpx client we
   inject into ``GoogleProvider`` rewrites exactly that call into a
   POST with the token in the form body, so the logged URL is
   token-free at the source.
2. ``http_server.middleware.SensitiveQueryScrubFilter`` (defense in
   depth): every root log handler redacts credential-shaped
   ``name=value`` query pairs from every record, whatever emits it.

The acceptance criterion from the retest report: a log capture of the
validation path contains no token substring.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from appscriptly.http_server.middleware import SensitiveQueryScrubFilter
from appscriptly.oauth_google import _TOKENINFO_URL, _TokenInfoBodyClient

_LIVE_TOKEN = "ya29.a0AfB_SECRET-LIVE-TOKEN-VALUE"


def _google_endpoints_handler(seen: list[httpx.Request]):
    """MockTransport handler speaking tokeninfo + userinfo."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url).split("?")[0] == _TOKENINFO_URL:
            return httpx.Response(200, json={
                "aud": "client-id-123",
                "sub": "user-sub-1",
                "scope": "openid https://www.googleapis.com/auth/userinfo.email",
                "expires_in": "3600",
                "email": "u@example.com",
            })
        return httpx.Response(200, json={
            "id": "user-sub-1", "email": "u@example.com", "name": "U",
        })

    return handler


# ---------------------------------------------------------------------
# Layer 1: the tokeninfo call itself carries no token in the URL
# ---------------------------------------------------------------------


def test_tokeninfo_get_is_rewritten_to_post_with_body():
    seen: list[httpx.Request] = []
    client = _TokenInfoBodyClient(
        transport=httpx.MockTransport(_google_endpoints_handler(seen))
    )

    async def scenario():
        async with client:
            return await client.get(
                _TOKENINFO_URL,
                params={"access_token": _LIVE_TOKEN},
                headers={"User-Agent": "FastMCP-Google-OAuth"},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["sub"] == "user-sub-1"

    (request,) = seen
    assert request.method == "POST"
    assert _LIVE_TOKEN not in str(request.url)
    assert "access_token" not in str(request.url)
    # The token rides in the form body (never logged by httpx)...
    assert f"access_token={_LIVE_TOKEN}" in request.content.decode()
    # ...and the verifier's headers survive the rewrite.
    assert request.headers["User-Agent"] == "FastMCP-Google-OAuth"


def test_non_tokeninfo_requests_pass_through_as_get():
    seen: list[httpx.Request] = []
    client = _TokenInfoBodyClient(
        transport=httpx.MockTransport(_google_endpoints_handler(seen))
    )

    async def scenario():
        async with client:
            return await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": "Bearer whatever"},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 200
    (request,) = seen
    assert request.method == "GET"


def test_fastmcp_verifier_path_logs_no_token(caplog):
    """End to end through fastmcp's real GoogleTokenVerifier with our
    client injected: the full validation path (tokeninfo + userinfo)
    succeeds AND the captured log stream never contains the token.

    This test is the N4 CANARY named at the fastmcp pin in
    pyproject.toml: a fastmcp bump that changes the verifier's call
    shape must FAIL here, never skip - hence the plain import (fastmcp
    is a hard runtime dependency; an import failure is a real failure).
    """
    from fastmcp.server.auth.providers.google import GoogleTokenVerifier

    seen: list[httpx.Request] = []
    verifier = GoogleTokenVerifier(
        http_client=_TokenInfoBodyClient(
            transport=httpx.MockTransport(_google_endpoints_handler(seen))
        ),
    )

    with caplog.at_level(logging.INFO):
        token_obj = asyncio.run(verifier.verify_token(_LIVE_TOKEN))

    assert token_obj is not None, "verification must still succeed"
    assert _LIVE_TOKEN not in caplog.text
    # And the tokeninfo request really was the POST shape.
    tokeninfo_requests = [
        r for r in seen if str(r.url).split("?")[0] == _TOKENINFO_URL
    ]
    assert tokeninfo_requests and all(
        r.method == "POST" and _LIVE_TOKEN not in str(r.url)
        for r in tokeninfo_requests
    )


# ---------------------------------------------------------------------
# Layer 2: the scrub filter (defense in depth)
# ---------------------------------------------------------------------


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="x", lineno=1,
        msg=msg, args=(), exc_info=None,
    )


@pytest.mark.parametrize("line", [
    f'HTTP Request: GET {_TOKENINFO_URL}?access_token={_LIVE_TOKEN} "HTTP/1.1 200 OK"',
    f"retrying https://x.example/upload?exp=1&mcp_sig={_LIVE_TOKEN}&i=2",
    f"POST /cb?client_secret={_LIVE_TOKEN}&code=abc failed",
    f"weird url https://y.example/a?refresh_token={_LIVE_TOKEN}",
    f"api call with key={_LIVE_TOKEN} rejected",
    f"state nonce={_LIVE_TOKEN} replayed",
])
def test_scrub_filter_redacts_credential_query_params(line):
    record = _record(line)
    scrub = SensitiveQueryScrubFilter()
    assert scrub.filter(record) is True
    scrubbed = record.getMessage()
    assert _LIVE_TOKEN not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scrub_filter_leaves_benign_lines_alone():
    line = (
        "upload_session session_id=abc user_id=sub:12345678 "
        "file_size_bytes=100 file_sha256=deadbeef split_by=heading_1 ts=1"
    )
    record = _record(line)
    SensitiveQueryScrubFilter().filter(record)
    assert record.getMessage() == line


def test_scrub_filter_redacts_lazy_percent_formatted_records():
    """httpx logs with %-style lazy args; scrubbing must apply to the
    RENDERED message, not just record.msg."""
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="x", lineno=1,
        msg='HTTP Request: %s %s "%s"',
        args=("GET", f"{_TOKENINFO_URL}?access_token={_LIVE_TOKEN}", "200 OK"),
        exc_info=None,
    )
    SensitiveQueryScrubFilter().filter(record)
    assert _LIVE_TOKEN not in record.getMessage()


def test_scrub_filter_redacts_credential_in_non_str_arg():
    """httpx logs ``request.url`` as an httpx.URL OBJECT (not a str);
    a credential in that object's string form must still be redacted.
    A str-only scrub (``isinstance(a, str)``) would let it through - the
    filter's contract is "no credential from httpx/urllib3/... reaches a
    log line", and those libraries pass URL objects, not strings. Arity
    and the numeric %d arg's type are preserved so the record still
    formats."""
    url = httpx.URL(f"{_TOKENINFO_URL}?access_token={_LIVE_TOKEN}")
    assert not isinstance(url, str)  # guards the premise this test exists for
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname="x", lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", url, "1.1", 200, "OK"),
        exc_info=None,
    )
    SensitiveQueryScrubFilter().filter(record)
    # getMessage also proves %d still formats -> the 200 arg kept its int type.
    rendered = record.getMessage()
    assert _LIVE_TOKEN not in rendered
    assert "access_token=[REDACTED]" in rendered
    assert len(record.args) == 5
    assert record.args[3] == 200 and isinstance(record.args[3], int)


def test_signed_url_query_params_scrubbed_from_access_log_lines():
    """uvicorn.access logs the full request target; a signed upload URL
    carries nonce + sig (credentials) and uid (the Google sub, PII).
    All three redact; the harmless exp/max survive."""
    line = (
        '169.155.1.1:0 - "POST /api/convert?exp=1799999999&nonce=N-SECRET'
        '&max=52428800&uid=110169484474386276334&sig=S-SECRET HTTP/1.1" 200'
    )
    record = _record(line)
    SensitiveQueryScrubFilter().filter(record)
    scrubbed = record.getMessage()
    assert "N-SECRET" not in scrubbed
    assert "S-SECRET" not in scrubbed
    assert "110169484474386276334" not in scrubbed
    assert "exp=1799999999" in scrubbed
    assert "max=52428800" in scrubbed


def test_scrub_filter_on_uvicorn_loggers_survives_uvicorns_dictconfig():
    """uvicorn wires NON-propagating handlers via dictConfig inside
    uvicorn.run - after configure_http_logging has run. Root-handler
    filters never see those records; the logger-LEVEL attachment must
    (a) exist and (b) survive dictConfig, which clears a logger's
    handlers but not its filters. Simulated with uvicorn's config shape."""
    import io
    import logging.config

    from appscriptly.http_server.app import configure_http_logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    access_logger = logging.getLogger("uvicorn.access")
    saved_access_filters = access_logger.filters[:]
    saved_access_propagate = access_logger.propagate
    root.handlers = []
    try:
        configure_http_logging()
        assert any(
            isinstance(f, SensitiveQueryScrubFilter)
            for f in access_logger.filters
        )

        # uvicorn's own logging setup: fresh non-propagating handler on
        # uvicorn.access (mirrors uvicorn.config.LOGGING_CONFIG shape).
        stream = io.StringIO()
        logging.config.dictConfig({
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "access": {"class": "logging.StreamHandler",
                           "stream": stream},
            },
            "loggers": {
                "uvicorn.access": {
                    "handlers": ["access"], "level": "INFO",
                    "propagate": False,
                },
            },
        })
        # The filter attachment survived...
        assert any(
            isinstance(f, SensitiveQueryScrubFilter)
            for f in access_logger.filters
        )
        # ...and scrubs a record served by uvicorn's OWN handler.
        access_logger.info(
            '"POST /api/convert?nonce=LIVE-NONCE&sig=LIVE-SIG HTTP/1.1" 200'
        )
        written = stream.getvalue()
        assert "LIVE-NONCE" not in written
        assert "LIVE-SIG" not in written
        assert "[REDACTED]" in written
    finally:
        access_logger.handlers = []
        access_logger.filters = saved_access_filters
        access_logger.propagate = saved_access_propagate
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_access_log_5arg_record_renders_scrubbed_without_logging_error(capsys):
    """REGRESSION (post-#232): uvicorn's AccessFormatter unpacks
    ``record.args`` into exactly 5 positional values. The first scrub
    implementation rendered the message and set ``record.args = ()``,
    which made that unpack raise ``ValueError`` and print a
    "--- Logging error ---" traceback INSTEAD of the access line on
    every signed request - the BUG-4 noise class, reintroduced on the
    access log. The fix scrubs the string args in place, preserving
    arity, so AccessFormatter still renders a (redacted) access line.

    The other Layer-2 tests missed this because they feed pre-rendered
    single-string records (args=()) through a plain formatter - never
    the real 5-arg + AccessFormatter shape uvicorn's h11 protocol emits.
    """
    import io

    from uvicorn.logging import AccessFormatter

    logger = logging.getLogger("test.regression.uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    # use_colors=False -> plain text (no ANSI), so substring asserts hold.
    handler.setFormatter(
        AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    logger.addHandler(handler)
    # Attach the filter at the LOGGER level, exactly where production
    # (configure_http_logging) attaches it to uvicorn.access.
    logger.addFilter(SensitiveQueryScrubFilter())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        # The precise record uvicorn emits: 5 positional args, the full
        # request target (with signed-URL credentials) as the third.
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "169.155.1.1:0",
            "POST",
            "/api/convert?exp=1799999999&nonce=N-SECRET"
            "&max=52428800&uid=110169484474386276334&sig=S-SECRET",
            "1.1",
            200,
        )
    finally:
        logger.removeHandler(handler)
        logger.filters = []

    written = stream.getvalue()
    # (a) the access line actually rendered - AccessFormatter did NOT blow up.
    assert "POST /api/convert" in written
    assert "200" in written
    # (b) credentials + PII redacted; harmless params survive.
    assert "N-SECRET" not in written
    assert "S-SECRET" not in written
    assert "110169484474386276334" not in written
    assert "exp=1799999999" in written
    assert "[REDACTED]" in written
    # (c) NO logging-error block on stderr - the regression's symptom.
    err = capsys.readouterr().err
    assert "Logging error" not in err
    assert "ValueError" not in err


def test_configure_http_logging_installs_the_scrub_on_root_handlers(capsys):
    """The wiring test: after configure_http_logging, a record emitted
    by a CHILD logger (httpx's propagation path) reaches the root
    handler token-free."""
    from appscriptly.http_server.app import configure_http_logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    try:
        configure_http_logging()
        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "200 OK"',
            f"{_TOKENINFO_URL}?access_token={_LIVE_TOKEN}",
        )
        err = capsys.readouterr().err
        assert _LIVE_TOKEN not in err
        assert "access_token=[REDACTED]" in err
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_provider_wiring_injects_the_body_client(monkeypatch, tmp_path):
    """configure_auth_for_http must hand GoogleProvider our rewriting
    client - the wiring, not just the class, is what fixes prod."""
    import appscriptly.oauth_google as og

    captured: dict = {}

    class _FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.client_registration_options = None

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRETS_JSON", json.dumps({
        "web": {"client_id": "cid", "client_secret": "cs"},
    }))
    monkeypatch.setenv("GOOGLE_OAUTH_BASE_URL", "https://example.fly.dev")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "test-master-token-32-chars-minimum!!")
    monkeypatch.delenv("FLY_APP_NAME", raising=False)

    import fastmcp.server.auth.providers.google as fastmcp_google
    monkeypatch.setattr(fastmcp_google, "GoogleProvider", _FakeProvider)

    class _FakeMCP:
        auth = None

    og.configure_auth_for_http(_FakeMCP())
    assert isinstance(captured.get("http_client"), _TokenInfoBodyClient)
