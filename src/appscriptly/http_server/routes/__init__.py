"""HTTP route handlers (one module per concern).

- observability.py — /health, /info
- oauth.py         — /oauth/google/api/callback
- convert.py       — /api/convert

Each module exports its handler function(s); the parent ``http_server.app``
module wires them into routes.
"""
