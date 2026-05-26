"""HTML response helpers for the OAuth callback endpoint.

Split out so the (security-relevant) HTML template + CSP header live
in one place — easier to audit and easier to test in isolation
(``test_http_server_middleware.py`` imports ``_success_page`` and
``_error_page`` directly).
"""
from __future__ import annotations

import html as _html  # aliased — `html` is shadowed by local var in callers

from starlette.responses import Response

_OAUTH_SUCCESS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authorization complete</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 480px; margin: 96px auto; padding: 0 24px;
            color: #1f2328; line-height: 1.6; }}
    .check {{ font-size: 48px; }}
    .small {{ color: #656d76; font-size: 14px; margin-top: 32px; }}
  </style>
</head>
<body>
  <div class="check">{check}</div>
  <h1>{heading}</h1>
  <p>{body}</p>
  <p class="small">You can close this tab now.</p>
</body>
</html>"""


# v2.0.6 (R28 defense-in-depth on top of PR #50 D1 XSS fix): the OAuth
# callback HTML pages render at the Fly app's origin and contain a
# server-supplied ``body`` substitution. PR #50 escapes that substitution
# (``_html.escape(message)``), which is the actual fix. CSP is the
# fallback if a future edit forgets to escape — `default-src 'none'`
# blocks every resource type, so even an injected ``<script>`` cannot
# load. ``style-src 'unsafe-inline'`` is required because the template
# carries an inline ``<style>`` block; the template has NO ``<script>``
# tags so we deliberately do NOT permit any script source.
_CSP_HEADER = "default-src 'none'; style-src 'unsafe-inline'"


def _success_page() -> Response:
    body_html = _OAUTH_SUCCESS_HTML.format(
        check="✅",
        heading="Google access granted",
        body=(
            "google-docs-mcp can now act on your Drive, Docs, and Apps Script "
            "on your behalf. Return to your chat and retry the action."
        ),
    )
    return Response(
        body_html,
        status_code=200,
        media_type="text/html",
        headers={"Content-Security-Policy": _CSP_HEADER},
    )


def _error_page(message: str, status_code: int) -> Response:
    body_html = _OAUTH_SUCCESS_HTML.format(
        check="⚠️",
        heading="Authorization didn't complete",
        body=_html.escape(message),
    )
    return Response(
        body_html,
        status_code=status_code,
        media_type="text/html",
        headers={"Content-Security-Policy": _CSP_HEADER},
    )
