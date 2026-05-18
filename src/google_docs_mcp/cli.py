"""Command-line subcommands for one-off setup tasks.

``google-docs-mcp`` with no args runs the MCP server (default mode).
Subcommands handle the Apps Script Web App deployment and config:

  google-docs-mcp setup-apps-script-auto   - automated: API-driven deploy
                                              (recommended; one OAuth consent
                                              replaces the manual UI dance)
  google-docs-mcp setup-apps-script        - manual: print deployment recipe
                                              for users who can't run a
                                              browser OAuth flow locally
  google-docs-mcp configure-webapp <URL>   - save a manually-deployed URL
  google-docs-mcp status                   - show current config state
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from . import config

SCRIPT_FILE = Path(__file__).parent / "restructure.gs"


def cli_main(argv: list[str]) -> int:
    if not argv:
        _print_help()
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "setup-apps-script-auto":
        return _cmd_setup_auto(rest)
    if cmd == "setup-apps-script":
        return _cmd_setup(rest)
    if cmd == "configure-webapp":
        return _cmd_configure(rest)
    if cmd == "status":
        return _cmd_status(rest)
    if cmd in ("-h", "--help", "help"):
        _print_help()
        return 0
    print(f"Unknown command: {cmd}\n", file=sys.stderr)
    _print_help()
    return 2


def _cmd_setup_auto(rest: list[str]) -> int:
    """Automated path: create + push + deploy in one shot via Apps Script API.

    Default: OAuth flow (opens browser once for consent).
    With --auth-mode=service-account --sa-key=PATH --impersonate-user=EMAIL:
    use Service Account + Domain-Wide Delegation. Truly headless. Requires
    Google Workspace + admin who's enabled DWD for the SA.
    """
    from .setup_apps_script import setup_apps_script_auto

    sa_key: Path | None = None
    impersonate: str | None = None
    auth_mode = "oauth"
    args = list(rest)
    while args:
        a = args.pop(0)
        if a.startswith("--auth-mode="):
            auth_mode = a.split("=", 1)[1]
        elif a == "--auth-mode" and args:
            auth_mode = args.pop(0)
        elif a.startswith("--sa-key="):
            sa_key = Path(a.split("=", 1)[1]).expanduser()
        elif a == "--sa-key" and args:
            sa_key = Path(args.pop(0)).expanduser()
        elif a.startswith("--impersonate-user="):
            impersonate = a.split("=", 1)[1]
        elif a == "--impersonate-user" and args:
            impersonate = args.pop(0)
        else:
            print(f"Unknown flag for setup-apps-script-auto: {a}", file=sys.stderr)
            return 2

    if auth_mode not in ("oauth", "service-account"):
        print(
            f"Invalid --auth-mode: {auth_mode!r}. Use 'oauth' or 'service-account'.",
            file=sys.stderr,
        )
        return 2
    if auth_mode == "service-account":
        if not sa_key or not impersonate:
            print(
                "service-account mode requires both --sa-key=PATH and "
                "--impersonate-user=EMAIL. See README 'Apps Script setup → "
                "Advanced: headless via Service Account + DWD'.",
                file=sys.stderr,
            )
            return 2
        print(
            f"Setting up Apps Script Web App via Service Account.\n"
            f"  SA key:        {sa_key}\n"
            f"  Impersonating: {impersonate}\n"
        )
    else:
        print(
            "Setting up Apps Script Web App automatically.\n"
            "If this is your first run, a browser window will open for\n"
            "OAuth consent — grant the Apps Script + Drive scopes shown.\n"
        )

    try:
        deployment = setup_apps_script_auto(
            service_account_key=sa_key if auth_mode == "service-account" else None,
            impersonate_user=impersonate if auth_mode == "service-account" else None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"\nSetup failed: {e}", file=sys.stderr)
        print(
            "\nIf the API path doesn't work for you (firewall, scope\n"
            "consent issues, etc.), fall back to the manual recipe with:\n"
            "  google-docs-mcp setup-apps-script",
            file=sys.stderr,
        )
        return 1
    print(f"Web App deployed successfully.")
    print(f"  scriptId:     {deployment.script_id}")
    print(f"  deploymentId: {deployment.deployment_id}")
    print(f"  version:      {deployment.version}")
    print(f"  /exec URL:    {deployment.url}")
    print(f"\nSaved to {config.config_path()}.")
    print("Test it with: google-docs-mcp status")
    return 0


def _print_help() -> None:
    print(__doc__ or "")


def _cmd_setup(_rest: list[str]) -> int:
    gs = SCRIPT_FILE.read_text(encoding="utf-8")
    cfg_path = config.config_path()
    saved_script = cfg_path.parent / "restructure.gs"
    saved_script.parent.mkdir(parents=True, exist_ok=True)
    saved_script.write_text(gs, encoding="utf-8")

    print(_RECIPE.replace("__SCRIPT_PATH__", str(saved_script)))
    return 0


def _cmd_configure(rest: list[str]) -> int:
    if not rest:
        print(
            "Usage: google-docs-mcp configure-webapp <URL>\n"
            "Paste the deployed Web App URL you copied in step 7.",
            file=sys.stderr,
        )
        return 2
    url = rest[0].strip()
    if not url.startswith("https://script.google.com/macros/s/"):
        print(
            f"Refusing to save URL that doesn't look like an Apps Script "
            f"Web App URL: {url}\nExpected: "
            "https://script.google.com/macros/s/.../exec",
            file=sys.stderr,
        )
        return 2
    if url.endswith("/dev"):
        print(
            "Refusing /dev URL — use the /exec URL from a saved "
            "deployment (the /dev one only works while you are logged "
            "into Google in the same browser).",
            file=sys.stderr,
        )
        return 2

    config.save({"apps_script_webapp_url": url})
    print(f"Saved webapp URL to {config.config_path()}")
    _ping(url)
    return 0


def _cmd_status(_rest: list[str]) -> int:
    cfg = config.load()
    url = cfg.get("apps_script_webapp_url")
    print(f"Config file: {config.config_path()}")
    if url:
        print(f"Apps Script webapp URL: {url}")
        _ping(url)
    else:
        print("Apps Script webapp URL: (not configured)")
        print("Run `google-docs-mcp setup-apps-script` to get the deployment recipe.")
    return 0


def _ping(url: str) -> None:
    """Health-check the webapp via the GET endpoint and print result."""
    try:
        with urlrequest.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
        if '"ok":true' in body or '"ok": true' in body:
            print("Health check: OK (script responding).")
        else:
            print(f"Health check: unexpected response: {body[:200]}")
    except urlerror.HTTPError as e:
        print(f"Health check: HTTP {e.code} (URL may be wrong)")
    except urlerror.URLError as e:
        print(f"Health check: network error: {e.reason}")
    except (OSError, ValueError) as e:
        print(f"Health check: {e}")


_RECIPE = """\
google-docs-mcp: Apps Script Web App setup
==========================================

This is a one-time setup that takes about 2 minutes. It deploys a small
helper script in your own Google account so the MCP can do things the REST
API cannot (preserving full .docx fidelity when restructuring into tabs).

The script was just saved to:
    __SCRIPT_PATH__

Step 1.  Open https://script.google.com/ in your browser.

Step 2.  Click "New project" (top-left). A blank script opens with a
         file called Code.gs containing a stub function — ignore the stub.

Step 3.  Click the project title at the top ("Untitled project") and
         rename it to "google-docs-mcp restructure" so you can find
         it later.

Step 4.  Select ALL the text in Code.gs (Ctrl+A / Cmd+A) and replace
         it with the contents of the .gs file we saved above. Open
         that file, copy its contents, paste over Code.gs. Save with
         Ctrl+S / Cmd+S. You should see "Saved" near the title.

Step 5.  Click "Deploy" (top-right) -> "New deployment".
         Click the gear icon next to "Select type" and choose "Web app".
         Fill in:
             Description:    google-docs-mcp v1
             Execute as:     Me (your-email@gmail.com)        <- important
             Who has access: Anyone with the link             <- important
         Click "Deploy".

         GOTCHA: "Anyone with the link" sounds scary but means anyone
         who knows the URL can INVOKE the script - they cannot read or
         edit your script's source, and the script can only act on
         documents YOU have access to (because "Execute as: Me" runs
         as you). The URL is a secret; treat it like a password.

Step 6.  Google will ask you to authorize the script. Click "Authorize
         access" -> pick your Google account -> on the "Google hasn't
         verified this app" screen click "Advanced" -> "Go to
         google-docs-mcp restructure (unsafe)" -> "Allow".

         GOTCHA: The "unsafe" wording is standard for unverified Apps
         Script projects you deploy yourself. It's safe - it's literally
         your own code running as you.

Step 7.  After deployment finishes, copy the "Web app URL". It looks
         like:
             https://script.google.com/macros/s/AKfyc.../exec
         Make sure it ends in /exec and NOT /dev. The /dev URL only
         works for you while logged in; /exec is the deployed endpoint.

Step 8.  Tell google-docs-mcp where to find it:
             google-docs-mcp configure-webapp <paste URL here>

Verify:  curl <URL> should return
             {"ok":true,"service":"google-docs-mcp restructure","version":"1"}
         If you see HTML instead, the URL is wrong (probably /dev not
         /exec, or "Who has access" wasn't set to "Anyone with the link").

Updating: If a new restructure.gs ships, repeat steps 4 and 5, but on
         step 5 choose the gear icon -> "Manage deployments" -> pick
         the existing one -> pencil-edit -> set Version to "New
         version" -> Deploy. The URL stays the same; do NOT create a
         second deployment or you'll have two URLs.
"""
