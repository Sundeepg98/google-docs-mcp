"""Process-wide mutable state shared across http_server submodules.

Currently just the single-use nonce tracker used by:

- ``middleware.BearerTokenMiddleware.dispatch`` for signed-URL replay
  protection (v0.9.0).
- ``routes.oauth.oauth_google_api_callback`` for OAuth state-param
  replay protection (v1.1+).

Single store is fine: nonce strings are unique-per-mint regardless of
which surface minted them.

Lives in its own module rather than ``__init__.py`` so:

  - There is no import-cycle risk between submodules that need it.
  - Tests can rebind via ``http_server._state._NONCE_STORE = NonceStore()``
    to reset between tests. Reassigning ``http_server._NONCE_STORE``
    on the package would NOT propagate to the modules that already
    captured a reference at import time — late binding through a
    module attribute is the canonical fix.
"""
from __future__ import annotations

from ..crypto import NonceStore

_NONCE_STORE = NonceStore()
