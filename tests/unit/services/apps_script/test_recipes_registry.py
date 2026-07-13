"""Per-entry byte-identity pins for the recipe registry (``_recipes.py``).

THE WAVE'S SPINE. For every registered recipe, ``render(spec, params)`` MUST
equal what the CURRENT generator tool passes to ``mint_bound_automation`` --
the ``.gs`` script body, the manifest dict (INCLUDING the private ``__plan__``
echo ``build_manifest`` adds), the recorded handler names, the derived project
name, the container kind, and the tool key -- BYTE-FOR-BYTE.

Ground truth is captured from REALITY, not re-derived: each test patches
``_mint_bound_automation`` in the generator's own module, drives the real tool
end-to-end (creds + the per-tool pre-mint I/O stubbed, exactly like
``test_sheet_dashboard``), and records the exact kwargs the tool hands the
mint. Because that capture IS the generator's output, a registry entry that
drifts from its generator makes these assertions FAIL loudly -- which is the
migration-safety net the whole wave rests on (Streams 3 + 5 refactor the tools
to delegate to ``render``; these pins prove the delegation changes nothing).

``as_generate_video_deck`` is the one entry whose codegen depends on impure
inputs (a server-minted frames batch id + a user-bound single-use HMAC upload
token). Its pin stubs those three sources to fixed values so the render is
deterministic, then drives BOTH the real tool and the recipe's ``pre_mint``
hook through them -- proving pre_mint + build reproduce the tool byte-for-byte.
"""
from __future__ import annotations

import importlib
import re

import pytest

# Importing anything under ``appscriptly`` runs ``appscriptly/__init__`` ->
# ``from .server import main`` -> registers the FastMCP instance and imports
# every generator module, so the tools below are callable.
from appscriptly.services.apps_script._lifecycle import MintResult
from appscriptly.services.apps_script._recipes import RECIPES, RenderResult, render

# Maps every recipe name to the module its generator tool lives in.
_MODULE: dict[str, str] = {
    "as_install_doc_menu": "doc_menu",
    "as_install_sheet_menu": "sheet_menu",
    "as_install_slides_menu": "slides_menu",
    "as_install_custom_function": "custom_function",
    "as_install_sheet_dashboard": "sheet_dashboard",
    "as_install_calendar_sync": "calendar_sync",
    "as_install_task_rollover": "task_rollover",
    "as_install_edit_trigger": "edit_trigger",
    "as_install_form_handler": "form_handler",
    "as_install_contact_sync": "contact_sync",
    "as_grade_form_responses": "grade_form_responses",
    "as_refresh_linked_slides": "refresh_linked_slides",
    "as_generate_video_deck": "video_deck",
}

# video_deck is pinned separately (its params are minted by pre_mint, not
# passed to the tool). Every OTHER recipe is driven param-for-param.
_VIDEO_DECK = "as_generate_video_deck"
_NON_VIDEO = [n for n in _MODULE if n != _VIDEO_DECK]

# An em / en / figure / horizontal-bar dash. NONE may appear in a
# consent/user-visible string (title / summary / a derived project name).
_FANCY_DASH_RE = re.compile(r"[‒–—―]")


@pytest.fixture
def stub_creds():
    from unittest.mock import MagicMock

    return MagicMock(name="stub-creds")


@pytest.fixture(autouse=True)
def _stub_scope_aware_creds(stub_creds, monkeypatch):
    """Swap creds resolution so the @workspace_tool(scopes=GAS_BOUND_SCOPES)
    envelope resolves without real OAuth. All recipe tools declare scopes, so
    resolution flows through the scope-aware stdio path (auth.load_credentials);
    patch that (mirrors test_sheet_dashboard)."""
    from appscriptly import auth, decorators

    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: stub_creds)
    monkeypatch.setattr(decorators, "_get_credentials_fn", lambda: stub_creds)


def _capturing_mint(captured: dict):
    """A ``mint_bound_automation`` replacement that records its kwargs and
    returns a schema-valid stub result (no project is created)."""

    def fake_mint(creds, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return MintResult(script_id="SID-1", deployment_id="DEPLOY-1")

    return fake_mint


def _assert_render_matches_capture(spec, params, captured: dict) -> None:
    """The core identity assertion: render == the tool's real mint inputs."""
    r: RenderResult = render(spec, params)
    assert r.script_body == captured["script_body"], (
        f"{spec.name}: rendered .gs body differs from the generator's."
    )
    assert r.manifest == captured["manifest_dict"], (
        f"{spec.name}: rendered manifest differs from the generator's "
        f"(includes the __plan__ echo)."
    )
    assert list(captured.get("handler_functions") or []) == r.handler_functions, (
        f"{spec.name}: rendered handler_functions differ from the ledger record."
    )
    assert spec.project_name(params) == captured["project_name"], (
        f"{spec.name}: derived project name differs from the generator's."
    )
    assert spec.container_kind == captured["container_kind"]
    assert spec.name == captured["tool"]


# ---------------------------------------------------------------------
# The 12 pure recipes: driven param-for-param through the real tool.
# ---------------------------------------------------------------------

_CASES = [
    (name, i)
    for name in _NON_VIDEO
    for i in range(len(RECIPES[name].example_params))
]


@pytest.mark.parametrize(
    ("name", "index"),
    _CASES,
    ids=[f"{name}-{i}" for name, i in _CASES],
)
def test_recipe_render_is_byte_identical_to_generator(name, index, monkeypatch):
    """render(spec, params) reproduces the generator's exact mint inputs."""
    spec = RECIPES[name]
    params = dict(spec.example_params[index])
    mod = importlib.import_module(f"appscriptly.services.apps_script.{_MODULE[name]}")

    # custom_function validates the container is a Sheet via a Drive lookup
    # before minting; stub it (it does not affect codegen).
    if name == "as_install_custom_function":
        monkeypatch.setattr(
            mod, "_auto_detect_container_kind", lambda creds, cid: "sheets"
        )

    captured: dict = {}
    monkeypatch.setattr(mod, "_mint_bound_automation", _capturing_mint(captured))

    # Drive the REAL tool. Its pre-mint validation runs; the (patched) mint
    # captures the exact script_body / manifest / handler_functions.
    getattr(mod, name)(**params)

    _assert_render_matches_capture(spec, params, captured)


# ---------------------------------------------------------------------
# video_deck: the pre_mint (batch + HMAC token) case.
# ---------------------------------------------------------------------


def test_video_deck_render_is_byte_identical_to_generator(monkeypatch):
    """render(pre_mint(params)) reproduces the video_deck generator exactly,
    with the impure batch/token/base-url sources stubbed to fixed values."""
    from appscriptly import credentials, oauth_google
    from appscriptly.services.apps_script import _frames_staging

    spec = RECIPES[_VIDEO_DECK]
    mod = importlib.import_module("appscriptly.services.apps_script.video_deck")

    fixed_batch = "testbatch01"
    fixed_token = "1700000000.testnonce.dGVzdA.abc123sig"
    fixed_base = "https://mcp.appscriptly.com"

    # Stub the three impure sources the tool AND the recipe's pre_mint call
    # (both import them at call time, so patching the source modules works).
    monkeypatch.setattr(_frames_staging, "new_batch_id", lambda: fixed_batch)
    monkeypatch.setattr(
        _frames_staging,
        "sign_frames_batch",
        lambda batch_id, *, user_id=None, **k: fixed_token,
    )
    monkeypatch.setattr(
        oauth_google, "resolve_runtime_oauth_config", lambda: {"base_url": fixed_base}
    )
    monkeypatch.setattr(credentials, "current_user_id_or_none", lambda: None)
    # video_deck validates the container is a Slides deck before minting.
    monkeypatch.setattr(mod, "_auto_detect_container_kind", lambda creds, cid: "slides")

    captured: dict = {}
    monkeypatch.setattr(mod, "_mint_bound_automation", _capturing_mint(captured))

    pres_id = spec.example_params[0]["presentation_id"]
    getattr(mod, _VIDEO_DECK)(presentation_id=pres_id)

    # The wrapper runs pre_mint before render; do the same, through the same
    # stubs, so render reproduces the tool byte-for-byte.
    params = spec.pre_mint({"presentation_id": pres_id})
    _assert_render_matches_capture(spec, params, captured)

    # The pre_mint hook itself produced the expected batch/token/base-url.
    assert params["batch_id"] == fixed_batch
    assert params["upload_token"] == fixed_token
    assert params["upload_base_url"] == f"{fixed_base}/upload/frames/{fixed_batch}"


# ---------------------------------------------------------------------
# Registry shape + guardrails (cheap, no tool calls).
# ---------------------------------------------------------------------


def test_registry_covers_exactly_the_thirteen_bound_generators():
    """The registry holds the 13 bound generators, and NOT the deploy
    primitive / generic passthrough (they resist parameterization)."""
    assert set(RECIPES) == set(_MODULE)
    assert len(RECIPES) == 13
    assert "as_deploy_web_app" not in RECIPES
    assert "as_generate_bound_script" not in RECIPES


def test_every_recipe_names_a_registered_tool():
    """Each recipe name matches a real, registered apps_script / gas tool -- so
    the registry cannot silently name a tool that does not exist."""
    from appscriptly.services.apps_script._expected_tools import EXPECTED

    for name in RECIPES:
        assert name in EXPECTED, f"{name} is not a registered apps_script tool"


def test_every_recipe_has_example_params():
    """A recipe with no example_params could never be harness-gated or pinned."""
    for name, spec in RECIPES.items():
        assert spec.example_params, f"{name} has no example_params"


def test_only_video_deck_carries_a_pre_mint_hook():
    """The pre_mint hook is video_deck's alone (its HMAC frames-batch token)."""
    with_pre_mint = {n for n, s in RECIPES.items() if s.pre_mint is not None}
    assert with_pre_mint == {_VIDEO_DECK}


def test_only_task_rollover_carries_a_manifest_transform():
    """The advanced-service manifest merge is task_rollover's alone."""
    with_transform = {
        n for n, s in RECIPES.items() if s.manifest_transform is not None
    }
    assert with_transform == {"as_install_task_rollover"}


def test_user_visible_strings_have_no_fancy_dashes():
    """Titles, summaries, and every derived project name are consent/user
    visible: they must use the ASCII hyphen only (no em/en dash)."""
    for name, spec in RECIPES.items():
        for label, value in (("title", spec.title), ("summary", spec.summary)):
            assert not _FANCY_DASH_RE.search(value), f"{name}.{label}: fancy dash"
        for i, params in enumerate(spec.example_params):
            pname = spec.project_name(params)
            assert not _FANCY_DASH_RE.search(pname), (
                f"{name} project_name[{i}] {pname!r}: fancy dash"
            )


def test_input_schemas_are_object_schemas():
    """Each recipe's input_schema is a well-formed object schema (a browse tool
    / a future generic installer relies on it)."""
    for name, spec in RECIPES.items():
        schema = spec.input_schema
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name
        assert isinstance(schema.get("required"), list), name
        # Every required key is a declared property.
        props = schema["properties"]
        for key in schema["required"]:
            assert key in props, f"{name}: required {key!r} not in properties"


def test_render_manifest_is_always_valid_apps_script():
    """Every rendered manifest is a valid Apps Script manifest carrying the
    __plan__ echo the mint expects (V8 + a timeZone, no restricted scope)."""
    from appscriptly.services.apps_script.api import is_restricted_scope

    for name, spec in RECIPES.items():
        params = dict(spec.example_params[0])
        r = render(spec, params)
        assert r.manifest["runtimeVersion"] == "V8", name
        assert "timeZone" in r.manifest, name
        assert "__plan__" in r.manifest, name
        for scope in r.manifest.get("oauthScopes", []):
            assert not is_restricted_scope(scope), f"{name}: restricted {scope}"
