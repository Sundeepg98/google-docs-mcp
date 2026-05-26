"""Shared resolution helpers for routes/oauth.py and routes/convert.py.

Both endpoints need:
  - the operator's Google OAuth client config (``_resolve_client_config``)
  - the public-facing base URL for redirect / consent URLs
    (``_resolve_base_url``)

Co-located here to avoid duplicating the env-var resolution + fallback
logic between the two route modules.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.requests import Request

from ..auth import default_data_dir
from ..oauth_google import load_client_config


def _resolve_client_config() -> dict:
    """Load the Google OAuth client_secrets JSON.

    Resolution order (first match wins):
      1. ``GOOGLE_OAUTH_CLIENT_SECRETS_JSON`` env var — full JSON inline.
         Right for Fly secrets where we don't want files on disk.
      2. ``GOOGLE_OAUTH_CLIENT_SECRETS_PATH`` env var — path to JSON file.
      3. Fall back to the existing stdio-mode discovery in auth.py.
    """
    inline = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_JSON")
    if inline:
        data = json.loads(inline)
        if not any(k in data for k in ("web", "installed")):
            raise RuntimeError(
                "GOOGLE_OAUTH_CLIENT_SECRETS_JSON must contain a 'web' "
                "or 'installed' top-level key"
            )
        return data

    path_str = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_PATH")
    if path_str:
        return load_client_config(Path(path_str))

    from ..auth import find_client_config
    return load_client_config(find_client_config(default_data_dir()))


def _resolve_base_url(request: Request) -> str:
    """Determine the public-facing base URL for OAuth redirects.

    Prefers ``GOOGLE_OAUTH_BASE_URL`` env var (most reliable in prod
    behind Fly's edge proxy). Falls back to reconstructing from the
    request's scheme + host headers — fine for local dev, fragile if
    the deployment is behind a non-standard reverse proxy that
    doesn't set X-Forwarded-*.
    """
    override = os.environ.get("GOOGLE_OAUTH_BASE_URL")
    if override:
        return override.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{scheme}://{host}"
