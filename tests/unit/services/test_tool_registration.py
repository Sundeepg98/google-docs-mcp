"""Multi-service tool-registration guards (auto-discovery refactor).

These tests verify the central registration invariant: importing
``appscriptly.server`` registers every expected tool. Post-refactor,
registration happens via **auto-discovery** — ``server.py`` walks
``services/`` with ``pkgutil.walk_packages`` and imports each non-private,
non-``{api,scopes}`` leaf module; each module's ``@workspace_tool``
decorations register as a side effect. The prior 12 hand-maintained
``from .services.X import tools`` side-effect imports are GONE.

**The decentralized witness (resolves the per-PR merge-conflict tax).**
Each service declares its tool surface in
``services/<svc>/_expected_tools.py::EXPECTED`` (a frozenset). A new
tool updates ONLY that file + its own definition site — never a central
frozenset here, never a central ``server.py`` import. This test
aggregates every ``_expected_tools.py::EXPECTED`` into ``declared`` and
asserts ``declared == registered`` (the live ``mcp.list_tools()`` set).

Why this is NOT circular: ``declared`` comes from hand-written
frozensets (human source of truth, one per service); ``registered``
comes from auto-discovery imports (mechanism). They are independent
second-witnesses of each other — a wiring bug in discovery (drops a
tool) OR a stale declaration (forgets a tool) makes them diverge.

**The golden snapshot (independent count-anchor).**
``tests/golden/tool_surface.json`` pins the exact sorted name list (39),
regenerated only by ``scripts/freeze_tool_surface.py`` + PR-reviewed as
a diff (like uv.lock). This is the THIRD, fully-independent witness that
catches a whole-service SYMMETRIC miss — where a folder is dropped from
BOTH the discovery walk AND its ``_expected_tools.py`` is absent, so
``declared == registered`` would still hold (both miss it) but the
golden (frozen from the known-good surface) would not.

**Three independent witnesses, by construction:**
  1. ``declared`` (per-service ``_expected_tools.py``, human-maintained)
  2. ``registered`` (auto-discovery, mechanism)
  3. golden snapshot (frozen surface, reviewed-diff)
Plus a 4th, deliberately separate, in ``test_tool_schemas.py``
(``EXPECTED_TOOLS`` there is a hand-maintained set guarding schema/
description contracts — NOT folded into discovery on purpose).

**File location:** lives at ``tests/unit/services/test_tool_registration.py``;
per-service folders (``tests/unit/services/<svc>/``) hold consumer tests
that don't need a multi-service view.

CRITICAL — these tests run under pytest (FILE execution). The
auto-discovery walk + the ``server`` import register the full surface
correctly under file execution (prod console-script + CI both run as
files). They register a PARTIAL surface only under ``python -c`` in an
editable/src-layout install — a Python packaging artifact (the
``appscriptly.services`` subpackage namespace doesn't resolve under
``-c`` editable), entirely upstream of the registration mechanism and
irrelevant to prod/CI. See ``test_registration_context_independence.py``
for the subprocess test that pins the real (file) entry at 39.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
from pathlib import Path


# ---------------------------------------------------------------------
# Decentralized declaration aggregation
# ---------------------------------------------------------------------
#
# ``declared`` = union of every services/<svc>/_expected_tools.py::EXPECTED.
# This walk MUST mirror server.py's discovery walk's service enumeration
# so the witness covers exactly the services discovery covers. (The two
# walks are independent code, but enumerate the same services/ folders —
# that's the point: a folder dropped from one but not the other surfaces
# as a declared!=registered divergence OR a golden mismatch.)


def _declared_by_service() -> dict[str, frozenset[str]]:
    """Map service-folder name -> its declared EXPECTED frozenset.

    Walks ``appscriptly.services`` for sub-packages, importing each
    one's ``_expected_tools`` module. A service WITHOUT an
    ``_expected_tools.py`` is a hard error here (every service that
    registers tools must declare them) — caught by the assertion in
    ``test_every_service_package_declares_expected_tools``.
    """
    import appscriptly.services as services_pkg

    result: dict[str, frozenset[str]] = {}
    for modinfo in pkgutil.iter_modules(services_pkg.__path__):
        if not modinfo.ispkg:
            continue  # only service sub-packages
        svc = modinfo.name
        try:
            mod = importlib.import_module(
                f"appscriptly.services.{svc}._expected_tools"
            )
        except ModuleNotFoundError:
            # No declaration file — record empty so the dedicated test
            # below can flag it with a clear message rather than a
            # KeyError elsewhere.
            result[svc] = frozenset()
            continue
        result[svc] = frozenset(mod.EXPECTED)
    return result


def _declared_tools() -> frozenset[str]:
    """Union of all per-service declared tool names."""
    out: set[str] = set()
    for names in _declared_by_service().values():
        out |= names
    return frozenset(out)


def _expected_service_by_tool() -> dict[str, str]:
    """``{tool_name: service_folder}`` derived from the declarations.

    Replaces the old hand-maintained ``_EXPECTED_SERVICE_BY_TOOL`` (which
    was built from the now-deleted central frozensets). The service-folder
    name IS the expected ``service=`` annotation value for every tool the
    folder declares — that's the per-file-constant rule
    (``services/<svc>/`` ⟹ ``service="<svc>"``).
    """
    return {
        name: svc
        for svc, names in _declared_by_service().items()
        for name in names
    }


# Public module-level alias kept for any external readers / future test
# sweeps that imported ``EXPECTED_TOOLS`` from this module. Now derived
# from the decentralized declarations rather than a central union.
EXPECTED_TOOLS: frozenset[str] = _declared_tools()


def _registered_tool_names() -> set[str]:
    """Snapshot of currently-registered tool names from the live mcp."""
    from appscriptly.server import mcp
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def _registered_tools_by_name() -> dict:
    """Snapshot of every registered tool, keyed by name."""
    from appscriptly.server import mcp
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


# ---------------------------------------------------------------------
# Witness 1+2: declared (per-service _expected_tools.py) == registered
# ---------------------------------------------------------------------


def test_declared_equals_registered():
    """``declared`` (union of every services/<svc>/_expected_tools.py)
    MUST equal ``registered`` (the live auto-discovery surface).

    This is the primary witness that auto-discovery wired up every
    service correctly AND that every declaration is current. NOT
    circular: ``declared`` is hand-written human source (one frozenset
    per service folder); ``registered`` is the mechanism's output
    (discovery imports). A discovery wiring bug (drops a tool) or a
    stale declaration (forgets a tool) diverges them.

    Replaces the pre-refactor
    ``test_all_expected_tools_register_from_correct_locations`` (which
    compared a central hand-union ``EXPECTED_TOOLS`` against
    ``registered`` — the central union was the merge-conflict surface
    this refactor decentralizes).
    """
    declared = _declared_tools()
    registered = _registered_tool_names()

    missing = declared - registered  # declared but discovery didn't register
    unexpected = registered - declared  # registered but no service declares

    assert not missing, (
        f"Declared-but-NOT-registered: {sorted(missing)}. Auto-discovery "
        f"did not register a tool that a services/<svc>/_expected_tools.py "
        f"declares. Likely cause: the tool's module isn't being imported "
        f"by the discovery walk (name starts with '_', or is in the "
        f"{{api, scopes}} denylist, or the @workspace_tool decoration is "
        f"missing/failed). See server.py's discovery loop."
    )
    assert not unexpected, (
        f"Registered-but-NOT-declared: {sorted(unexpected)}. A tool "
        f"registered that no services/<svc>/_expected_tools.py declares. "
        f"Add it to the relevant service's _expected_tools.py EXPECTED "
        f"frozenset (the decentralized witness), and freeze the golden "
        f"(python scripts/freeze_tool_surface.py)."
    )
    # Redundant given the set-equality above, but pins the count in the
    # failure message for fast triage.
    assert len(registered) == len(declared), (
        f"Count drift: declared={len(declared)} registered={len(registered)}."
    )


def test_every_service_package_declares_expected_tools():
    """Every service sub-package that registers tools MUST ship an
    ``_expected_tools.py`` with a non-empty ``EXPECTED`` frozenset.

    Guards the decentralized-witness contract: a new service folder
    that forgets its ``_expected_tools.py`` would otherwise contribute
    nothing to ``declared`` while still registering tools via discovery
    → ``test_declared_equals_registered`` would flag the tools as
    "registered but not declared", but THIS test gives the clearer
    root-cause message ("service X has no _expected_tools.py").

    A service legitimately with zero tools (none exist today) would
    need an explicit empty ``EXPECTED = frozenset()`` + an allow-list
    entry here; flag it loudly rather than silently.
    """
    declared = _declared_by_service()
    empty = sorted(svc for svc, names in declared.items() if not names)
    assert not empty, (
        f"Service package(s) with missing/empty _expected_tools.py: "
        f"{empty}. Every service that registers tools must declare them "
        f"in services/<svc>/_expected_tools.py (EXPECTED frozenset). If a "
        f"service genuinely has zero tools, add an explicit "
        f"`EXPECTED = frozenset()` and update this test's allow-list."
    )


# ---------------------------------------------------------------------
# Witness 3: golden snapshot (independent count-anchor, reviewed diff)
# ---------------------------------------------------------------------


_GOLDEN_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "golden" / "tool_surface.json"
)


def test_golden_surface_matches_registered():
    """The registered surface MUST match ``tests/golden/tool_surface.json``.

    The golden is the THIRD, fully-independent witness. It catches a
    whole-service SYMMETRIC miss that ``declared == registered`` cannot:
    if a service folder is dropped from BOTH the discovery walk AND its
    ``_expected_tools.py`` is absent, ``declared`` and ``registered``
    both lose that folder's tools and still match each other — but the
    golden (frozen from the known-good 39-tool surface, regenerated only
    by an explicit human ``scripts/freeze_tool_surface.py`` + PR diff
    review) would not.

    If this fails after an INTENTIONAL surface change, run
    ``python scripts/freeze_tool_surface.py`` (as a FILE, never -c) and
    commit the diff — the diff IS the reviewed proof of the change.
    """
    assert _GOLDEN_PATH.exists(), (
        f"Golden surface file missing: {_GOLDEN_PATH}. Generate it with "
        f"`python scripts/freeze_tool_surface.py`."
    )
    golden = sorted(json.loads(_GOLDEN_PATH.read_text(encoding="utf-8")))
    registered = sorted(_registered_tool_names())

    added = sorted(set(registered) - set(golden))
    removed = sorted(set(golden) - set(registered))
    assert registered == golden, (
        "Registered tool surface drifted from the golden snapshot:\n"
        f"  added (registered, not in golden):   {added}\n"
        f"  removed (in golden, not registered): {removed}\n"
        f"  golden={len(golden)} registered={len(registered)}\n"
        "If this is an INTENTIONAL change, run "
        "`python scripts/freeze_tool_surface.py` and commit the diff."
    )


# ---------------------------------------------------------------------
# Per-module location guards (KEPT — orthogonal to discovery).
# ---------------------------------------------------------------------
#
# Discovery proves a tool IS registered; these prove it's defined in the
# RIGHT file (and NOT duplicated in server.py). They catch a different
# bug class: a tool accidentally re-added to server.py, or moved to the
# wrong service folder. They iterate the per-service declared sets
# (sourced from _expected_tools.py, not the deleted central frozensets).


def _assert_tools_live_in_module(tool_names, expected_module: str) -> None:
    """Assert each tool is a module-level attr of ``expected_module`` with
    a matching ``__module__``, and is NOT also defined on ``server``."""
    from appscriptly import server

    mod = importlib.import_module(expected_module)
    for tool_name in tool_names:
        assert hasattr(mod, tool_name), (
            f"{tool_name} not found in {expected_module} — ensure it's "
            f"defined there."
        )
        fn = getattr(mod, tool_name)
        assert fn.__module__ == expected_module, (
            f"{tool_name}.__module__ is {fn.__module__!r}, expected "
            f"{expected_module!r}."
        )
        assert not hasattr(server, tool_name), (
            f"{tool_name} ALSO exists in server.py — duplicate definition. "
            f"Tools live in their service module, not server.py."
        )


def test_docs_service_tools_register_from_services_docs_tools_module():
    """The docs-service tools must be defined in
    ``services/docs/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["docs"],
        "appscriptly.services.docs.tools",
    )


def test_drive_service_tools_register_from_services_drive_tools_module():
    """The 10 drive-service tools must be defined in
    ``services/drive/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["drive"],
        "appscriptly.services.drive.tools",
    )


def test_gas_deploy_service_tools_register_from_services_gas_deploy_tools_module():
    """The gas_deploy-service tools (install-automation canonical + alias,
    plus as_deploy_web_app) must be defined in
    ``services/gas_deploy/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["gas_deploy"],
        "appscriptly.services.gas_deploy.tools",
    )


def test_admin_service_tools_register_from_services_admin_tools_module():
    """The 7 admin-service tools must be defined in
    ``services/admin/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["admin"],
        "appscriptly.services.admin.tools",
    )


def test_sheets_service_tools_register_from_services_sheets_tools_module():
    """The 9 sheets-service tools must be defined in
    ``services/sheets/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["sheets"],
        "appscriptly.services.sheets.tools",
    )


def test_slides_service_tools_register_from_services_slides_tools_module():
    """The 8 slides-service tools must be defined in
    ``services/slides/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["slides"],
        "appscriptly.services.slides.tools",
    )


def test_forms_service_tools_register_from_services_forms_tools_module():
    """The 7 forms-service tools must be defined in
    ``services/forms/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["forms"],
        "appscriptly.services.forms.tools",
    )


def test_calendar_service_tools_register_from_services_calendar_tools_module():
    """The 7 calendar-service tools must be defined in
    ``services/calendar/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["calendar"],
        "appscriptly.services.calendar.tools",
    )


def test_contacts_service_tools_register_from_services_contacts_tools_module():
    """The 6 contacts-service tools (People API v1) must be defined in
    ``services/contacts/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["contacts"],
        "appscriptly.services.contacts.tools",
    )


def test_tasks_service_tools_register_from_services_tasks_tools_module():
    """The 7 tasks-service tools must be defined in
    ``services/tasks/tools.py``, NOT server.py."""
    _assert_tools_live_in_module(
        _declared_by_service()["tasks"],
        "appscriptly.services.tasks.tools",
    )


# apps_script spreads its tools across multiple feature files (unlike the
# single-tools.py services). This per-tool → module map is KEPT (the spec
# explicitly preserves it) — it's the location witness for the multi-file
# layout. A new apps_script tool adds an entry here + its own feature file
# + the apps_script _expected_tools.py declaration.
_APPS_SCRIPT_TOOL_MODULE: dict[str, str] = {
    "as_generate_bound_script": "appscriptly.services.apps_script.tools",
    # CASA-free growth — execution-history read tool in its own feature file.
    "as_list_script_processes": "appscriptly.services.apps_script.processes",
    # Stream 3 — activation verification tool in its own feature file.
    "as_check_activation": "appscriptly.services.apps_script.check_activation",
    # Automation lifecycle — inventory + uninstall (both in lifecycle_tools).
    "as_list_installed_automations": (
        "appscriptly.services.apps_script.lifecycle_tools"
    ),
    "as_uninstall_automation": (
        "appscriptly.services.apps_script.lifecycle_tools"
    ),
    "as_install_doc_menu": "appscriptly.services.apps_script.doc_menu",
    "as_install_custom_function": (
        "appscriptly.services.apps_script.custom_function"
    ),
    "as_install_sheet_dashboard": (
        "appscriptly.services.apps_script.sheet_dashboard"
    ),
    "as_install_edit_trigger": (
        "appscriptly.services.apps_script.edit_trigger"
    ),
    "as_install_form_handler": (
        "appscriptly.services.apps_script.form_handler"
    ),
    "as_install_sheet_menu": (
        "appscriptly.services.apps_script.sheet_menu"
    ),
    "as_install_slides_menu": (
        "appscriptly.services.apps_script.slides_menu"
    ),
    "as_refresh_linked_slides": (
        "appscriptly.services.apps_script.refresh_linked_slides"
    ),
    "as_grade_form_responses": (
        "appscriptly.services.apps_script.grade_form_responses"
    ),
    "as_generate_video_deck": (
        "appscriptly.services.apps_script.video_deck"
    ),
    "as_encode_video": (
        "appscriptly.services.apps_script.encode_video"
    ),
    "as_install_calendar_sync": (
        "appscriptly.services.apps_script.calendar_sync"
    ),
    "as_install_task_rollover": (
        "appscriptly.services.apps_script.task_rollover"
    ),
    "as_install_contact_sync": (
        "appscriptly.services.apps_script.contact_sync"
    ),
}


def test_apps_script_service_tools_register_from_services_apps_script_module():
    """Every apps_script-service tool must be defined in its feature file
    under ``services/apps_script/`` (the generic primitive in ``tools.py``;
    each composing convenience tool in its own feature module), NOT
    server.py.

    Generalizes the single-file location guards to apps_script's
    multi-file layout via ``_APPS_SCRIPT_TOOL_MODULE``. Also cross-checks
    that map against the apps_script ``_expected_tools.py`` declaration
    so the two can't drift (every declared apps_script tool must have a
    module entry, and vice versa).

    Also implicitly distinguishes apps_script from gas_deploy: the tools
    must live in the apps_script folder, not get folded into gas_deploy.
    """
    declared_apps_script = _declared_by_service()["apps_script"]

    # The location map and the declaration must cover exactly the same
    # set — a new tool added to one but not the other is a drift bug.
    assert set(_APPS_SCRIPT_TOOL_MODULE) == set(declared_apps_script), (
        "_APPS_SCRIPT_TOOL_MODULE and apps_script/_expected_tools.py "
        "disagree:\n"
        f"  in map, not declared: {sorted(set(_APPS_SCRIPT_TOOL_MODULE) - set(declared_apps_script))}\n"
        f"  declared, not in map: {sorted(set(declared_apps_script) - set(_APPS_SCRIPT_TOOL_MODULE))}\n"
        "Add the missing entry to both."
    )

    for tool_name, expected_module in _APPS_SCRIPT_TOOL_MODULE.items():
        _assert_tools_live_in_module([tool_name], expected_module)


def test_no_tool_definitions_remain_in_server_py():
    """``server.py`` must contain NO tool definitions — every tool lives
    in a service folder. If any registered tool name is also an attr of
    ``server``, it's a half-finished migration (defined in both places).

    Auto-discovery (this refactor) reinforces this: server.py no longer
    even imports the service tool modules by name — it discovers them.
    A tool re-added to server.py would be a regression both here and in
    the per-module location guards.
    """
    from appscriptly import server

    leftover = [name for name in _declared_tools() if hasattr(server, name)]
    assert not leftover, (
        f"Tools still defined in server.py: {leftover}. Every tool must "
        f"live in a service folder (discovered automatically); re-defining "
        f"one in server.py reintroduces the ISP asymmetry Gap #7 closed."
    )


# ---------------------------------------------------------------------
# Sanity: a docs-service tool is callable through the live registry
# ---------------------------------------------------------------------


def test_gdocs_get_tab_url_works_through_registration():
    """End-to-end sanity: call a docs-service tool through its
    registered-from-services-folder path. ``gdocs_get_tab_url`` is the
    cleanest target — pure URL composition, no Google API call needed.
    """
    from appscriptly.services.docs.tools import gdocs_get_tab_url

    result = gdocs_get_tab_url("DOC123", "TAB456")
    assert result == {
        "doc_id": "DOC123",
        "tab_id": "TAB456",
        "url": "https://docs.google.com/document/d/DOC123/edit?tab=TAB456",
    }


# ---------------------------------------------------------------------
# service= annotation invariant (KEPT — orthogonal to discovery).
# ---------------------------------------------------------------------
#
# @workspace_tool(service=...) carries a per-tool service tag on its
# ToolAnnotations for telemetry / routing / observability. These tests
# verify the annotation is PRESENT and CORRECT — orthogonal to whether
# the tool registers (discovery's concern). The expected mapping is now
# derived from the per-service _expected_tools.py declarations (folder
# name = expected service= value) rather than the deleted central
# frozensets.


def test_every_tool_carries_service_annotation():
    """Every registered tool MUST have a ``service=`` value on its
    ToolAnnotations. Without it, per-service telemetry/routing can't
    branch on which service a tool belongs to.

    ToolAnnotations is pydantic-backed with ``extra: "allow"``, so the
    field rides as an extra attribute and round-trips via ``getattr``.
    """
    registered = _registered_tools_by_name()
    missing_service: list[str] = []
    for tool_name in _declared_tools():
        tool = registered[tool_name]
        service = getattr(tool.annotations, "service", None)
        if not service:
            missing_service.append(tool_name)
    assert not missing_service, (
        f"Tools missing service= annotation: {sorted(missing_service)}. "
        f"M4 made service= REQUIRED on @workspace_tool — most likely a new "
        f"tool was added with @workspace_tool(...) but the author forgot "
        f"the service= kwarg, OR a tool was decorated via the @gdocs_tool "
        f"deprecation shim."
    )


def test_service_annotation_matches_expected_per_file_partition():
    """The ``service=`` value of every tool MUST match the per-folder
    mapping (``services/<svc>/`` ⟹ ``service="<svc>"``), derived from
    the ``_expected_tools.py`` declarations.

    Catches: a tool tagged with the wrong ``service=`` literal (copy-
    paste hazard), a tool declared in the wrong folder, or the
    ``@gdocs_tool`` shim (delegates to ``service="docs"``) invoked on a
    non-docs tool.
    """
    registered = _registered_tools_by_name()
    expected_by_tool = _expected_service_by_tool()
    mismatches: list[str] = []
    for tool_name, expected in expected_by_tool.items():
        actual = getattr(registered[tool_name].annotations, "service", None)
        if actual != expected:
            mismatches.append(
                f"{tool_name}: expected service={expected!r}, got {actual!r}"
            )
    assert not mismatches, (
        "service= annotations don't match the per-folder expected "
        "partition:\n  " + "\n  ".join(mismatches)
        + "\nFix the offending @workspace_tool(service=...) call site, or "
        "(if the move was intentional) update the service folder / its "
        "_expected_tools.py."
    )


def test_no_in_repo_callers_use_deprecated_gdocs_tool_decorator():
    """No in-repo source file MAY still use the deprecated ``@gdocs_tool``.

    M4 ships ``@gdocs_tool`` as a one-release backward-compat shim
    (delegates to ``workspace_tool(service="docs", ...)`` + emits a
    DeprecationWarning). Every in-repo call site has been migrated to
    ``@workspace_tool(service=..., ...)``. Static-grep approach (vs
    runtime warning-capture) avoids the sys.modules-cache pollution a
    reload-based test would cause.
    """
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[3] / "src" / "appscriptly"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        # decorators.py legitimately DEFINES ``def gdocs_tool(...)`` as the
        # shim — that's not a decoration call site. We ban the @-prefix
        # usage that actually invokes it.
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("@gdocs_tool("):
                rel = path.relative_to(src_root.parent)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        f"Deprecated @gdocs_tool decoration call sites still present in "
        f"src/: {offenders}. Migrate to @workspace_tool(service=..., ...) "
        f"with the required service= kwarg."
    )
