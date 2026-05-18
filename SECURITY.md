# Security Policy

## Reporting a vulnerability

If you find a security issue in this project — OAuth flow, signed-URL handling, Apps Script deployment, or anything else with user-data implications — **please do not file a public issue.**

Instead, open a private security advisory at:
https://github.com/Sundeepg98/google-docs-mcp/security/advisories/new

We'll acknowledge within 7 days. Fixes will be coordinated and released alongside disclosure.

## Scope

In scope:
- Authentication and authorization (OAuth token handling, signed-URL HMAC, service-account impersonation)
- Data handling (anything that touches the user's Google Drive contents)
- Server-side issues in the Fly.io HTTP transport (e.g., the `/api/convert` endpoint)
- Apps Script deployment flow (the `setup-apps-script-auto` CLI and `gas_deploy` sub-package)

Out of scope:
- Vulnerabilities in upstream dependencies (report those to the dependency directly)
- Vulnerabilities in Google's services themselves (report via Google's VRP)
- Misconfigurations that require user action to exploit (e.g., setting `access: ANYONE` on a Web App deployment)

## Supported versions

The latest tagged release on `main` is supported. Earlier versions are not patched.
