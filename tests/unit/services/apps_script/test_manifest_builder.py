"""Tests for build_manifest() — services/apps_script/api.py (PR-Δ7).

``build_manifest`` is the PURE heart of the bound-script generator: it
translates an operator-friendly high-level dict (menu / triggers /
sidebar_html / oauth_scopes) into the real ``appsscript.json`` manifest.
Because it's pure (no I/O, deterministic), it's the natural place for a
hypothesis property test alongside the example-based cases.

Key reality the tests pin (verified against the official Apps Script
manifest reference): menus / sidebars / triggers are NOT manifest fields
— they're implemented in the .gs code. So build_manifest's job for those
keys is to derive the right ``oauthScopes`` (a menu/sidebar needs
``script.container.ui``; a time trigger needs ``script.scriptapp``) and
to validate + echo the intent under the private ``__plan__`` key. The
manifest ALWAYS carries ``runtimeVersion: "V8"`` + ``timeZone``.

Coverage:

1. **Empty / None** — bare manifest is still valid (V8 + timeZone).
2. **menu** — UI scope derived; entries validated + echoed; malformed
   entries rejected.
3. **triggers** — time → scriptapp scope; edit type accepted; unknown
   type rejected.
4. **sidebar_html** — UI scope derived; non-str rejected.
5. **oauth_scopes** — explicit scopes merged + de-duplicated, order
   stable; non-list / non-str rejected.
6. **combined** — all keys together produce the union of scopes.
7. **hypothesis property test** — any valid-shaped input dict → output
   ALWAYS has runtimeVersion + timeZone + a well-formed structure.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from appscriptly.services.apps_script.api import build_manifest

_UI_SCOPE = "https://www.googleapis.com/auth/script.container.ui"
_TRIGGER_SCOPE = "https://www.googleapis.com/auth/script.scriptapp"


# ---------------------------------------------------------------------
# Empty / None — bare manifest
# ---------------------------------------------------------------------


def test_build_manifest_none_returns_bare_valid_manifest():
    """No manifest_dict → still a valid manifest with V8 + timeZone."""
    m = build_manifest(None)
    assert m["runtimeVersion"] == "V8"
    assert m["timeZone"] == "Etc/UTC"
    # No capabilities → no oauthScopes key (omitted, not empty list).
    assert "oauthScopes" not in m


def test_build_manifest_empty_dict_returns_bare_valid_manifest():
    """An empty dict behaves like None."""
    m = build_manifest({})
    assert m["runtimeVersion"] == "V8"
    assert m["timeZone"] == "Etc/UTC"
    assert "oauthScopes" not in m


def test_build_manifest_custom_timezone_propagates():
    """A non-default timezone lands in the manifest's timeZone field."""
    m = build_manifest({}, timezone="America/New_York")
    assert m["timeZone"] == "America/New_York"
    assert m["runtimeVersion"] == "V8"


def test_build_manifest_always_emits_v8_runtime():
    """runtimeVersion is hard-pinned to V8 regardless of input — the
    legacy Rhino runtime is never selected by this primitive."""
    for inp in (None, {}, {"menu": [{"name": "X", "function_name": "x"}]}):
        assert build_manifest(inp)["runtimeVersion"] == "V8"


# ---------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------


def test_build_manifest_menu_derives_ui_scope():
    """A custom menu requires the script.container.ui scope."""
    m = build_manifest({"menu": [{"name": "Refresh", "function_name": "refresh"}]})
    assert _UI_SCOPE in m["oauthScopes"]


def test_build_manifest_menu_echoes_validated_entries_in_plan():
    """The normalized menu entries are echoed under __plan__ for the
    generated body / orchestration layer to wire."""
    m = build_manifest({
        "menu": [
            {"name": "Refresh now", "function_name": "doRefresh"},
            {"name": "Settings", "function_name": "openSettings"},
        ],
    })
    assert m["__plan__"]["menu"] == [
        {"name": "Refresh now", "function_name": "doRefresh"},
        {"name": "Settings", "function_name": "openSettings"},
    ]


def test_build_manifest_menu_must_be_list():
    with pytest.raises(ValueError, match="menu must be a list"):
        build_manifest({"menu": {"name": "x", "function_name": "y"}})


def test_build_manifest_menu_entry_must_be_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        build_manifest({"menu": ["not-a-dict"]})


def test_build_manifest_menu_entry_requires_name():
    with pytest.raises(ValueError, match="missing a non-empty string 'name'"):
        build_manifest({"menu": [{"function_name": "x"}]})


def test_build_manifest_menu_entry_requires_function_name():
    with pytest.raises(ValueError, match="function_name"):
        build_manifest({"menu": [{"name": "Label"}]})


def test_build_manifest_menu_entry_rejects_empty_name():
    with pytest.raises(ValueError, match="'name'"):
        build_manifest({"menu": [{"name": "", "function_name": "x"}]})


# ---------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------


def test_build_manifest_time_trigger_derives_scriptapp_scope():
    """An installable time-driven trigger requires script.scriptapp."""
    m = build_manifest({"triggers": [{"type": "time", "every_hours": 1}]})
    assert _TRIGGER_SCOPE in m["oauthScopes"]


def test_build_manifest_edit_trigger_accepted():
    """An onEdit trigger is a valid type. (A simple onEdit doesn't itself
    require script.scriptapp — only time triggers do — so the edit-only
    case derives no trigger scope; it's still echoed in the plan.)"""
    m = build_manifest({"triggers": [{"type": "edit"}]})
    assert m["__plan__"]["triggers"] == [{"type": "edit"}]
    # edit-only → no scriptapp scope derived
    assert _TRIGGER_SCOPE not in m.get("oauthScopes", [])


def test_build_manifest_trigger_preserves_extra_keys_in_plan():
    """Trigger entries keep their caller-supplied config (e.g.
    every_hours) in the echoed plan — this primitive doesn't prescribe
    the full trigger schema, only that ``type`` is known."""
    m = build_manifest({"triggers": [{"type": "time", "every_hours": 6}]})
    assert m["__plan__"]["triggers"] == [{"type": "time", "every_hours": 6}]


def test_build_manifest_triggers_must_be_list():
    with pytest.raises(ValueError, match="triggers must be a list"):
        build_manifest({"triggers": {"type": "time"}})


def test_build_manifest_trigger_entry_must_be_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        build_manifest({"triggers": ["time"]})


def test_build_manifest_trigger_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown type"):
        build_manifest({"triggers": [{"type": "webhook"}]})


def test_build_manifest_trigger_missing_type_rejected():
    """A trigger entry without a ``type`` is rejected (type=None is not
    in the valid set)."""
    with pytest.raises(ValueError, match="unknown type"):
        build_manifest({"triggers": [{"every_hours": 1}]})


# ---------------------------------------------------------------------
# sidebar_html
# ---------------------------------------------------------------------


def test_build_manifest_sidebar_derives_ui_scope():
    """A sidebar uses HtmlService → requires script.container.ui."""
    m = build_manifest({"sidebar_html": "<p>Hello</p>"})
    assert _UI_SCOPE in m["oauthScopes"]


def test_build_manifest_sidebar_flagged_in_plan():
    m = build_manifest({"sidebar_html": "<div>panel</div>"})
    assert m["__plan__"]["has_sidebar"] is True


def test_build_manifest_no_sidebar_flag_false():
    m = build_manifest({})
    assert m["__plan__"]["has_sidebar"] is False


def test_build_manifest_sidebar_must_be_string():
    with pytest.raises(ValueError, match="sidebar_html must be a string"):
        build_manifest({"sidebar_html": ["<p>x</p>"]})


# ---------------------------------------------------------------------
# oauth_scopes
# ---------------------------------------------------------------------


def test_build_manifest_explicit_oauth_scopes_merged():
    """Caller-supplied scopes land in the manifest oauthScopes."""
    scope = "https://www.googleapis.com/auth/spreadsheets"
    m = build_manifest({"oauth_scopes": [scope]})
    assert scope in m["oauthScopes"]


def test_build_manifest_oauth_scopes_deduplicated_order_stable():
    """Derived + explicit scopes union with no duplicates, preserving
    first-seen order (derived first, then explicit)."""
    # menu derives _UI_SCOPE; explicitly passing it again must not dupe.
    sheets_scope = "https://www.googleapis.com/auth/spreadsheets"
    m = build_manifest({
        "menu": [{"name": "X", "function_name": "x"}],
        "oauth_scopes": [_UI_SCOPE, sheets_scope, sheets_scope],
    })
    assert m["oauthScopes"].count(_UI_SCOPE) == 1
    assert m["oauthScopes"].count(sheets_scope) == 1
    # Derived UI scope comes first (predictable ordering).
    assert m["oauthScopes"][0] == _UI_SCOPE


def test_build_manifest_oauth_scopes_must_be_list():
    with pytest.raises(ValueError, match="oauth_scopes must be a list"):
        build_manifest({"oauth_scopes": "https://example.com/scope"})


def test_build_manifest_oauth_scopes_must_be_strings():
    with pytest.raises(ValueError, match="oauth_scopes must be a list"):
        build_manifest({"oauth_scopes": [123]})


# ---------------------------------------------------------------------
# combined
# ---------------------------------------------------------------------


def test_build_manifest_combined_unions_all_scopes():
    """All capabilities together → the manifest declares the union of
    every derived + explicit scope. Uses a SENSITIVE (non-restricted)
    explicit scope so the union is exercised without tripping the
    restricted-scope guard (that guard has its own dedicated tests below)."""
    sheets_scope = "https://www.googleapis.com/auth/spreadsheets"
    m = build_manifest({
        "menu": [{"name": "Run", "function_name": "run"}],
        "triggers": [{"type": "time", "every_hours": 24}, {"type": "edit"}],
        "sidebar_html": "<p>panel</p>",
        "oauth_scopes": [sheets_scope],
    })
    scopes = m["oauthScopes"]
    assert _UI_SCOPE in scopes        # menu + sidebar
    assert _TRIGGER_SCOPE in scopes   # time trigger
    assert sheets_scope in scopes     # explicit
    # Plan echoes everything.
    assert len(m["__plan__"]["menu"]) == 1
    assert len(m["__plan__"]["triggers"]) == 2
    assert m["__plan__"]["has_sidebar"] is True


# ---------------------------------------------------------------------
# restricted-scope guard (v2.0c)
# ---------------------------------------------------------------------

_RESTRICTED_SAMPLES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


@pytest.mark.parametrize("restricted", _RESTRICTED_SAMPLES)
def test_build_manifest_rejects_restricted_scope_by_default(restricted):
    """A RESTRICTED scope in oauth_scopes is refused unless explicitly
    allowed — the generic generator must not silently mint full-Gmail /
    broad-Drive authority."""
    with pytest.raises(ValueError, match="RESTRICTED scope"):
        build_manifest({"oauth_scopes": [restricted]})


def test_build_manifest_restricted_scope_allowed_with_optin():
    """With the explicit opt-in, a restricted scope IS permitted (the
    user-acknowledged escape hatch)."""
    drive = "https://www.googleapis.com/auth/drive"
    m = build_manifest({"oauth_scopes": [drive]}, allow_restricted_scopes=True)
    assert drive in m["oauthScopes"]


def test_build_manifest_restricted_guard_names_only_restricted_scopes():
    """When a mix of sensitive + restricted scopes is passed, the error
    names the RESTRICTED one(s), not the innocent sensitive scope."""
    sheets = "https://www.googleapis.com/auth/spreadsheets"
    gmail = "https://www.googleapis.com/auth/gmail.readonly"
    with pytest.raises(ValueError) as exc:
        build_manifest({"oauth_scopes": [sheets, gmail]})
    assert gmail in str(exc.value)
    assert sheets not in str(exc.value)


def test_build_manifest_sensitive_scopes_pass_without_optin():
    """SENSITIVE (non-restricted) scopes — drive.file, spreadsheets,
    documents, presentations, calendar, tasks, contacts, forms — are
    NOT gated; they pass with no opt-in."""
    sensitive = [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/tasks",
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/forms.body",
    ]
    m = build_manifest({"oauth_scopes": sensitive})
    for s in sensitive:
        assert s in m["oauthScopes"]


def test_build_manifest_combined_runtime_and_timezone_still_present():
    """Even the busiest manifest keeps the mandatory V8 + timeZone."""
    m = build_manifest(
        {
            "menu": [{"name": "Run", "function_name": "run"}],
            "triggers": [{"type": "time"}],
            "sidebar_html": "<p>x</p>",
        },
        timezone="Europe/London",
    )
    assert m["runtimeVersion"] == "V8"
    assert m["timeZone"] == "Europe/London"


# ---------------------------------------------------------------------
# hypothesis property test — invariants over any valid-shaped input
# ---------------------------------------------------------------------


# Strategies for valid-shaped fragments. Each is INDEPENDENTLY valid so
# the composed dict always passes validation — the property under test is
# "valid input always yields a structurally-sound manifest," not "garbage
# is rejected" (the example tests above cover rejection).
_menu_item = st.fixed_dictionaries({
    "name": st.text(min_size=1, max_size=20),
    "function_name": st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{0,19}", fullmatch=True),
})
_trigger_item = st.one_of(
    st.fixed_dictionaries({"type": st.just("edit")}),
    st.fixed_dictionaries({
        "type": st.just("time"),
        "every_hours": st.integers(min_value=1, max_value=24),
    }),
)
# Draw only NON-restricted scopes: the restricted-scope guard (its own
# tests above) would reject a randomly-generated drive/gmail scope, which is
# orthogonal to the structural invariants this property test pins. A curated
# sensitive-scope pool keeps the de-dup / union / ordering coverage intact.
_scope = st.sampled_from([
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/script.external_request",
])

_valid_manifest_dict = st.fixed_dictionaries(
    {},
    optional={
        "menu": st.lists(_menu_item, max_size=5),
        "triggers": st.lists(_trigger_item, max_size=5),
        "sidebar_html": st.text(max_size=50),
        "oauth_scopes": st.lists(_scope, max_size=5),
    },
)


@settings(max_examples=200)
@given(manifest_dict=_valid_manifest_dict, tz=st.sampled_from([
    "Etc/UTC", "America/New_York", "Europe/London", "Asia/Kolkata",
]))
def test_build_manifest_property_always_valid_structure(manifest_dict, tz):
    """PROPERTY: for ANY valid-shaped input dict, build_manifest returns
    a manifest that:

      * ALWAYS has runtimeVersion == "V8";
      * ALWAYS has timeZone == the requested tz;
      * has an oauthScopes that, if present, is a list of unique strings;
      * has a well-formed __plan__ echo (menu list, triggers list,
        has_sidebar bool).

    This is the load-bearing invariant the orchestration relies on: no
    matter what combination of capabilities the caller requests, the
    resulting appsscript.json is always deployable (V8 + timeZone) and
    the scope list is never malformed.
    """
    m = build_manifest(manifest_dict, timezone=tz)

    # Mandatory fields, always.
    assert m["runtimeVersion"] == "V8"
    assert m["timeZone"] == tz

    # oauthScopes, when present, is a list of unique strings.
    if "oauthScopes" in m:
        scopes = m["oauthScopes"]
        assert isinstance(scopes, list)
        assert all(isinstance(s, str) for s in scopes)
        assert len(scopes) == len(set(scopes)), "oauthScopes has duplicates"
        # Non-empty when present (empty is omitted, not emitted as []).
        assert len(scopes) > 0

    # __plan__ echo is always well-formed.
    plan = m["__plan__"]
    assert isinstance(plan["menu"], list)
    assert isinstance(plan["triggers"], list)
    assert isinstance(plan["has_sidebar"], bool)

    # Derived-scope invariants: a menu or sidebar implies the UI scope.
    if manifest_dict.get("menu") or manifest_dict.get("sidebar_html"):
        assert _UI_SCOPE in m["oauthScopes"]
    # A time trigger implies the scriptapp scope.
    if any(t.get("type") == "time" for t in manifest_dict.get("triggers", [])):
        assert _TRIGGER_SCOPE in m["oauthScopes"]
