"""Process-wide mutable state shared across http_server submodules.

Currently just the single-use nonce tracker used by:

- ``middleware.BearerTokenMiddleware.dispatch`` for signed-URL replay
  protection (v0.9.0).
- ``routes.oauth.oauth_google_api_callback`` for OAuth state-param
  replay protection (v1.1+).

Single store is fine: nonce strings are unique-per-mint regardless of
which surface minted them.

The concrete store is ``DurableNonceStore`` — SQLite-backed on the
``/data`` volume — so a consumed nonce stays consumed across a Fly
deploy/restart. This gives both surfaces strict single-use replay
protection even across the restart window (the in-process ``NonceStore``
would forget the consumed set and let a nonce be replayed once within
its ≤10-min TTL after a restart). ``DurableNonceStore`` is a drop-in
``NonceStore`` subclass, so nothing downstream changes.

Lives in its own module rather than ``__init__.py`` so:

  - There is no import-cycle risk between submodules that need it.
  - Tests can rebind via ``http_server._state._NONCE_STORE = NonceStore()``
    to reset between tests. Reassigning ``http_server._NONCE_STORE``
    on the package would NOT propagate to the modules that already
    captured a reference at import time — late binding through a
    module attribute is the canonical fix.
"""
from __future__ import annotations

from appscriptly.crypto import NonceStore
from appscriptly.durable_nonce import DurableNonceStore

_NONCE_STORE: NonceStore = DurableNonceStore()
