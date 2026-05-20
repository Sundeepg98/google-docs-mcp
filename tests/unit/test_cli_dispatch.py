"""CLI subcommand dispatch — main()'s router (v2.0.4 P0 #1 fix).

``server.main()`` dispatches the first argv to ``cli.cli_main()`` only
if it matches ``_CLI_SUBCOMMANDS``. Anything else falls through to the
stdio MCP handler. Pre-v2.0.4, ``setup-apps-script-auto`` was missing
from the set even though ``cli.cli_main()`` handles it (cli.py:33-34)
and the README documents it (lines 156, 191) as the recommended setup
command. Following the README hit a silent fall-through into the MCP
handler — symptom was the command appearing to do nothing on a TTY.

These tests pin the dispatch surface against that regression class:
the README's documented commands MUST be in the dispatch set.
"""
from __future__ import annotations

import re
from pathlib import Path


# Every command surfaced in the README's setup section must dispatch
# through the CLI router. If you add a new `google-docs-mcp <cmd>`
# entry to the README, add it here too — and the corresponding
# cli.cli_main() branch.
README_DOCUMENTED_COMMANDS = [
    "setup-apps-script",
    "setup-apps-script-auto",
    "configure-webapp",
    "status",
]


def test_cli_subcommands_includes_setup_apps_script_auto():
    """v2.0.4 P0 #1 regression guard. The README at lines 156 and 191
    documents `google-docs-mcp setup-apps-script-auto` as THE
    recommended setup path. Pre-fix, that command fell through to the
    stdio MCP handler and silently died."""
    from google_docs_mcp.server import _CLI_SUBCOMMANDS
    assert "setup-apps-script-auto" in _CLI_SUBCOMMANDS, (
        "'setup-apps-script-auto' missing from _CLI_SUBCOMMANDS — every "
        "user following the README's recommended setup steps will hit a "
        "silent fall-through to the stdio MCP handler. See server.py "
        "near _CLI_SUBCOMMANDS."
    )


def test_all_readme_documented_commands_are_dispatched():
    """Every command the README tells users to run must be in the
    dispatch set. Catches the v2.0.4 bug class generally (not just the
    one symptom that surfaced)."""
    from google_docs_mcp.server import _CLI_SUBCOMMANDS
    missing = [c for c in README_DOCUMENTED_COMMANDS if c not in _CLI_SUBCOMMANDS]
    assert not missing, (
        f"README-documented commands missing from _CLI_SUBCOMMANDS: {missing}. "
        "Either add them to the set in server.py or remove them from the "
        "README — the two must agree or users hit silent fall-through."
    )


def test_every_dispatched_subcommand_has_a_cli_handler():
    """Bidirectional contract: every command we dispatch must have a
    branch in cli.cli_main(). Otherwise we route to cli.py and get
    'Unknown command' instead of either MCP startup or the intended
    CLI tool — equally broken, just a different failure shape."""
    from google_docs_mcp import cli, server

    # Drive cli_main with each subcommand and assert it does NOT take
    # the "Unknown command" branch (which returns 2). The handlers
    # themselves may exit non-zero if env/files are missing, but they
    # must not return 2 — that's the unhandled-command sentinel.
    # We skip help variants because those always print and return 0.
    cmds_to_check = [
        c for c in server._CLI_SUBCOMMANDS
        if c not in ("-h", "--help", "help")
    ]
    for cmd in cmds_to_check:
        # We just need to confirm the dispatch table HAS the command —
        # which we can do statically by reading cli_main's source.
        # Avoids triggering the actual setup tool's side effects.
        import inspect
        src = inspect.getsource(cli.cli_main)
        assert f'"{cmd}"' in src, (
            f"server._CLI_SUBCOMMANDS includes {cmd!r} but cli.cli_main "
            f"has no matching branch. Either add a handler in cli.py or "
            f"drop the entry from _CLI_SUBCOMMANDS — otherwise the "
            f"command routes into cli.py and hits the 'Unknown command' "
            f"branch."
        )


def test_error_recovery_references_exist_as_cli_subcommands():
    """errors.py guidance saying 'Run `google-docs-mcp X`' — X must be
    a real subcommand. Catches N2-class broken recovery instructions."""
    from google_docs_mcp.cli import cli_main  # noqa: F401
    errors_text = (
        Path(__file__).resolve().parents[2]
        / "src" / "google_docs_mcp" / "errors.py"
    ).read_text(encoding="utf-8")
    cli_text = (
        Path(__file__).resolve().parents[2]
        / "src" / "google_docs_mcp" / "cli.py"
    ).read_text(encoding="utf-8")

    known = set(re.findall(r'cmd\s*==\s*"([\w\-]+)"', cli_text))
    refs = set(re.findall(r'google-docs-mcp ([\w\-]+)', errors_text))
    missing = refs - known
    assert not missing, (
        f"errors.py references CLI subcommands that don't exist: {missing!r}. "
        f"Known subcommands: {sorted(known)}. "
        f"Either implement the subcommand OR fix the error message to "
        f"reference an existing one (e.g. tool name like "
        f"`gdocs_reset_authorization`)."
    )


def test_cli_setup_auto_prints_traceback_on_exception(capsys, monkeypatch):
    """F3 regression: operator must see full traceback on
    _cmd_setup_auto failure. Pre-fix, only ``str(e)`` was printed —
    chained exceptions (e.g. HttpError wrapped in RuntimeError) hid
    the root cause and made setup failures undebuggable from the
    operator console alone."""
    from google_docs_mcp import cli, setup_apps_script

    def boom(*a, **kw):
        raise RuntimeError("simulated chain") from ValueError("root cause")

    # _cmd_setup_auto does `from .setup_apps_script import
    # setup_apps_script_auto` at call time, so patch the source
    # module's attribute rather than a stale name in cli.
    monkeypatch.setattr(
        setup_apps_script, "setup_apps_script_auto", boom, raising=True
    )
    rc = cli.cli_main(["setup-apps-script-auto"])

    captured = capsys.readouterr()
    assert rc != 0
    assert "Setup failed" in captured.err
    assert "simulated chain" in captured.err
    assert "Traceback" in captured.err  # full traceback shown
    assert "root cause" in captured.err  # chained cause visible
