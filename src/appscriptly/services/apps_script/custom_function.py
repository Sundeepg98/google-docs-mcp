"""Install a custom spreadsheet function into a Google Sheet (PR-Δ10).

A *convenience tool* layered on the PR-Δ7 bound-script generator
primitive (``services/apps_script/{api,tools}.py``). It does NOT
re-implement the deploy machinery — it COMPOSES the same four api.py
functions ``as_generate_bound_script`` orchestrates
(``auto_detect_container_kind`` → ``create_bound_project`` →
``set_project_content`` → ``create_deployment``) plus the pure
``build_manifest``. The only thing this module adds on top is the
custom-function-specific shaping + validation:

  * generate the ``.gs`` body = the caller's ``function_body`` with a
    ``@customfunction`` JSDoc tag prepended (so Sheets recognizes the
    function as usable in a cell, e.g. ``=MY_FUNC(A1)``);
  * validate ``function_name`` is a valid JavaScript identifier AND that
    the supplied ``function_body`` actually defines a function with that
    name;
  * a sheet-specific return envelope (``usage_hint: "=FUNCTION(...)"``)
    rather than the generic primitive's container envelope.

**Why a custom function needs NO trigger and NO extra scope.** A custom
spreadsheet function is just a plain Apps Script function carrying a
``@customfunction`` JSDoc tag. Once the bound script is deployed into
the container, Sheets discovers the tag and exposes the function in the
formula autocomplete — there is no ``onOpen``, no installable trigger,
and (because the function runs in the spreadsheet's own evaluation
context, not against another Google service) no ``oauthScopes`` beyond
the container binding itself. So this tool reuses #138's
``build_manifest(None)`` → a bare ``V8`` + ``timeZone`` manifest with no
``oauthScopes`` key. (Reference: Apps Script "Custom Functions in Google
Sheets" — https://developers.google.com/apps-script/guides/sheets/functions.)

**Sheets-only by design.** Custom *spreadsheet* functions only make
sense inside a Sheet — a ``=FUNCTION()`` cell has no analogue in Docs or
Slides. So this tool takes a ``sheet_id`` (not a generic
``container_id``) and uses #138's ``auto_detect_container_kind`` to
*verify* the ID really points at a Spreadsheet, rejecting Docs / Slides
/ Forms / folders with a clear ``ValueError`` before any project is
created. (The generic ``as_generate_bound_script`` accepts all three
container kinds; this convenience tool deliberately narrows to sheets.)

**Honest availability caveat.** After this tool returns, the bound
script is deployed, but Sheets registers the ``@customfunction`` on the
container lazily — the formula is usable once Apps Script has indexed
the new script, which for a freshly-bound project means **the user may
need to reload the spreadsheet tab once** before ``=FUNCTION(...)``
resolves (until then a cell shows ``#NAME?``). This is surfaced in the
tool docstring + the returned ``usage_hint`` so the model can tell the
user to reload if the function doesn't appear immediately.
"""
from __future__ import annotations

import re

from appscriptly.decorators import workspace_tool
from appscriptly.services.apps_script._lifecycle import (
    mint_bound_automation as _mint_bound_automation,
)
from appscriptly.services.apps_script._recipes import (
    RECIPES as _RECIPES,
    render as _render,
)
from appscriptly.services.apps_script.api import (
    auto_detect_container_kind as _auto_detect_container_kind,
)
from appscriptly.services.apps_script.scopes import GAS_BOUND_SCOPES
from appscriptly.tool_schemas import AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA

# Imported for parity with services/apps_script/tools.py; not used on the
# happy path (the @workspace_tool(creds=True) envelope injects creds and
# maps HttpError → ToolError). Kept top-level so a future error-path
# addition doesn't trigger a separate import.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# A valid JavaScript identifier for an Apps Script function name: starts
# with a letter / ``_`` / ``$``, followed by letters / digits / ``_`` /
# ``$``. This is the practical subset custom-function names use (Sheets
# upper-cases the call but the underlying function name follows JS
# identifier rules). Deliberately does NOT allow Unicode identifier
# characters — keeping it ASCII avoids surprising the user whose
# ``=FUNCTION()`` cell must match exactly.
_JS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# The JSDoc tag Sheets looks for to expose a function in cells.
_CUSTOM_FUNCTION_TAG = "@customfunction"


def _jsdoc_safe(text: str) -> list[str]:
    """Render free-text as JSDoc-comment-safe body lines (no code injection).

    The generated ``@customfunction`` block embeds the caller's
    ``description`` INSIDE a ``/** ... */`` comment. A raw ``description``
    containing ``*/`` would CLOSE the comment early and turn the remainder
    into live Apps Script in the deployed bound script — a code-injection
    vector. Sibling codegen escapes user text into generated JS:
    ``doc_menu``/``video_deck`` use ``_js_string`` (json.dumps) for JS
    *string literals*; this is a *comment* body, so we instead neutralize
    the only comment-terminator (``*/``) — mirroring
    ``sheet_dashboard``'s ``replace("*/", "* /")`` — and split into lines
    so a multi-line description stays inside the block (each line is
    prefixed with `` * `` by the caller).

    Returns the description's lines with every ``*/`` defanged to ``* /``;
    an empty / whitespace-only input returns ``[]`` (no description line).
    """
    if not text or not text.strip():
        return []
    safe = text.replace("*/", "* /")
    return safe.splitlines()

# Reserved JS words that can't be a function name. A custom function
# named e.g. ``return`` or ``function`` would be a syntax error in the
# generated .gs; reject early with a clear message rather than letting
# Apps Script's updateContent fail opaquely later.
_JS_RESERVED_WORDS = frozenset({
    "break", "case", "catch", "class", "const", "continue", "debugger",
    "default", "delete", "do", "else", "export", "extends", "finally",
    "for", "function", "if", "import", "in", "instanceof", "new", "return",
    "super", "switch", "this", "throw", "try", "typeof", "var", "void",
    "while", "with", "yield", "enum", "null", "true", "false",
})


def _validate_function_name(function_name: str) -> None:
    """Reject a ``function_name`` that isn't a usable JS identifier.

    Raises ``ValueError`` (→ surfaced to the caller) when the name is
    empty, isn't a syntactically valid JavaScript identifier, or is a
    reserved word — all of which would make the generated ``.gs`` invalid
    or the ``=FUNCTION()`` cell unresolvable.
    """
    if not function_name or not isinstance(function_name, str):
        raise ValueError(
            "function_name must be a non-empty string (the name the user "
            "will type as =FUNCTION_NAME(...) in a cell)."
        )
    if not _JS_IDENTIFIER_RE.match(function_name):
        raise ValueError(
            f"function_name {function_name!r} is not a valid JavaScript "
            f"identifier. A custom-function name must start with a letter, "
            f"'_' or '$' and contain only letters, digits, '_' or '$' "
            f"(no spaces, hyphens, or dots) — Sheets calls it as "
            f"=FUNCTION_NAME(...) so the underlying function name must be a "
            f"valid identifier."
        )
    if function_name in _JS_RESERVED_WORDS:
        raise ValueError(
            f"function_name {function_name!r} is a reserved JavaScript "
            f"word and cannot be used as a function name. Pick a different "
            f"name (e.g. prefix it: 'my{function_name.capitalize()}')."
        )


def _body_defines_function(function_body: str, function_name: str) -> bool:
    """True if ``function_body`` defines a function named ``function_name``.

    Recognizes the three forms a custom function is commonly written in:

      * ``function NAME(...) {...}``                 (declaration)
      * ``NAME = function(...) {...}``               (function expression)
      * ``NAME = (...) => ...`` / ``const NAME = (...) =>`` (arrow)
      * ``var/let/const NAME = function/(`` ...      (assigned)

    The check is a pragmatic textual match (not a full JS parse — Apps
    Script is the real validator). Its job is to catch the obvious
    name/body mismatch (caller passes ``function_name="FOO"`` but a body
    defining ``bar``) BEFORE deploying a script whose ``=FOO()`` cell
    would never resolve. The ``\\b`` word boundaries prevent a substring
    false-match (``computeFoo`` does not satisfy a request for ``Foo``).
    """
    name = re.escape(function_name)
    patterns = (
        # function NAME(           — classic declaration
        rf"\bfunction\s+{name}\s*\(",
        # [var|let|const] NAME = function(   — function expression
        rf"\b{name}\s*=\s*function\b",
        # [var|let|const] NAME = ( ... ) =>  — arrow with paren params
        rf"\b{name}\s*=\s*\([^)]*\)\s*=>",
        # [var|let|const] NAME = arg =>      — arrow with single bare param
        rf"\b{name}\s*=\s*[A-Za-z_$][A-Za-z0-9_$]*\s*=>",
    )
    return any(re.search(p, function_body) for p in patterns)


def build_custom_function_script(
    function_name: str,
    function_body: str,
    description: str | None = None,
) -> str:
    """Build the ``.gs`` source for a custom spreadsheet function (PURE).

    Returns ``function_body`` unchanged except that a ``@customfunction``
    JSDoc block is PREPENDED when the body doesn't already carry the tag.
    Sheets only exposes a function in cells if its preceding JSDoc
    contains ``@customfunction``; this guarantees the tag is present
    without double-adding it when the caller already wrote one.

    Validation (raises ``ValueError`` — surfaced to the tool caller):

      * ``function_name`` must be a valid, non-reserved JS identifier
        (see ``_validate_function_name``);
      * ``function_body`` must be non-empty / non-whitespace;
      * ``function_body`` must define a function named ``function_name``
        (see ``_body_defines_function``) — otherwise the deployed
        ``=FUNCTION_NAME()`` cell could never resolve.

    Args:
        function_name: the cell-callable name (e.g. ``BRAND_CHECK``).
        function_body: the JavaScript source defining that function.
        description: optional one-line human description woven into the
            generated JSDoc (shown in the Sheets formula autocomplete
            help). Ignored when the body already has its own
            ``@customfunction`` JSDoc (we don't rewrite the caller's).

    Returns:
        The ``.gs`` source string ready to hand to
        ``set_project_content``.
    """
    _validate_function_name(function_name)

    if not function_body or not function_body.strip():
        raise ValueError(
            "function_body cannot be empty — pass the JavaScript source "
            f"defining {function_name!r} (the body that runs when a cell "
            f"calls =" + function_name + "(...))."
        )

    if not _body_defines_function(function_body, function_name):
        raise ValueError(
            f"function_body does not define a function named "
            f"{function_name!r}. The body must declare it (e.g. "
            f"'function {function_name}(input) {{ ... }}') so the "
            f"=" + function_name + "() cell resolves. Check that "
            f"function_name matches the function in function_body."
        )

    # If the caller already annotated the function with @customfunction,
    # respect their JSDoc verbatim — don't double-tag or rewrite it.
    if _CUSTOM_FUNCTION_TAG in function_body:
        return function_body

    # Otherwise prepend a minimal JSDoc carrying the tag. The
    # @customfunction tag is what makes Sheets surface the function in a
    # cell; the description line(s) (if any) show in the formula help.
    # description is JSDoc-comment-escaped (a `*/` would otherwise close
    # the comment and the rest would deploy as live code) and split per
    # line so a multi-line description can't break out of the block.
    desc_block = "".join(
        f" * {line}\n" for line in _jsdoc_safe(description or "")
    )
    jsdoc = (
        "/**\n"
        f"{desc_block}"
        f" * {_CUSTOM_FUNCTION_TAG}\n"
        " */\n"
    )
    return jsdoc + function_body


@workspace_tool(
    title="Install a custom function into a Google Sheet",
    service="apps_script",
    readonly=False,
    destructive=False,
    # Each call creates a NEW bound project + deployment (it composes
    # create_bound_project, which is itself non-idempotent — re-running
    # yields a duplicate script bound to the same sheet). Same convention
    # as as_generate_bound_script.
    idempotent=False,
    external=True,
    creds=True,
    scopes=GAS_BOUND_SCOPES,
    output_schema=AS_INSTALL_CUSTOM_FUNCTION_OUTPUT_SCHEMA,
)
def as_install_custom_function(
    creds,
    sheet_id: str,
    function_name: str,
    function_body: str,
    description: str | None = None,
    name: str | None = None,
    on_conflict: str = "new",
) -> dict:
    """Install a custom spreadsheet function usable as =FUNCTION_NAME(...) in cells.

    Deploys a bound Apps Script defining ``function_name`` (with a
    ``@customfunction`` JSDoc tag so Sheets recognizes it), so the user
    can type ``=FUNCTION_NAME(args)`` into any cell of the Sheet. The
    function then runs IN the spreadsheet, on Google's infrastructure,
    WITHOUT Claude in the loop — e.g. a ``=BRAND_CHECK(A1)`` cell that
    scores the text in A1 against a brand guide, recomputed by Sheets
    whenever A1 changes.

    This is a convenience wrapper over the generic bound-script
    generator (``as_generate_bound_script``): it shapes the ``.gs`` body
    (prepending the ``@customfunction`` tag), verifies the target is a
    Spreadsheet, deploys via the same machinery, and returns a
    Sheets-friendly envelope. For a menu / sidebar / time-trigger
    automation (not a cell function), use ``as_generate_bound_script``
    directly.

    USE WHEN: the user wants a reusable spreadsheet formula that does
    custom work in a cell — anything they'd want to type as
    ``=SOMENAME(...)`` and have recalculated like a built-in function.
    For a ONE-OFF computation, just compute the values and write them
    with ``gsheets_write_range`` instead (no script needed).

    Args:
        sheet_id: Drive ID of the target Google Sheet (the ID in the
            spreadsheet's URL). Verified to be a Spreadsheet — passing a
            Doc / Slides / Form / folder ID is rejected with a clear
            error (custom *spreadsheet* functions only make sense in a
            Sheet).
        function_name: the cell-callable name, e.g. ``BRAND_CHECK``. Must
            be a valid JavaScript identifier (letters / digits / ``_`` /
            ``$``, not starting with a digit, not a reserved word) — it's
            what the user types as ``=FUNCTION_NAME(...)``, so it has to
            be a legal function name. Required.
        function_body: the JavaScript source defining ``function_name``,
            e.g. ``function BRAND_CHECK(text) { return text.length; }``.
            Claude authors this. Must be non-empty AND must define a
            function whose name matches ``function_name`` (else the
            ``=FUNCTION_NAME()`` cell could never resolve). A
            ``@customfunction`` JSDoc tag is prepended automatically if
            the body doesn't already carry one. Required.
        description: OPTIONAL one-line human description of what the
            function does. When the body has no ``@customfunction`` JSDoc
            of its own, this line is woven into the generated JSDoc and
            shows up in the Sheets formula autocomplete help.
        name: OPTIONAL title for the new Apps Script project. Defaults to
            a generated name referencing the function.
        on_conflict: what to do when an automation from THIS tool already
            exists on this Sheet. "new" (the default) always installs a
            fresh one (which can leave duplicates); "replace" uninstalls
            the prior install(s) on this Sheet first (no duplicate, no
            orphan); "skip" returns the existing install unchanged instead
            of adding a duplicate. The match is keyed by (this tool, this
            container) via appscriptly's automation ledger.

    Returns:
        ``{script_id, deployment_id, sheet_id, function_name,
        usage_hint, project_url}`` plus ``on_conflict`` (echoed),
        ``reused_existing`` (True when ``on_conflict="skip"`` returned a
        prior install), and ``replaced_count`` (prior installs removed for
        ``on_conflict="replace"``). ``usage_hint`` is the literal
        ``"=FUNCTION_NAME(...)"`` the user types; ``project_url``
        deep-links to the script editor
        (``https://script.google.com/d/{script_id}/edit``) so the user
        can inspect / tweak the function.

    Raises:
        ToolError: an Apps Script / Drive API error — the standard
            decorator envelope renders these as user-facing ``ToolError``.
        ValueError: the target isn't a Spreadsheet, ``function_name``
            isn't a valid identifier, or ``function_body`` is empty /
            doesn't define ``function_name`` — cheap client-side
            rejection before any project is created.

    Availability caveat: after this returns, the bound script is
    deployed, but Sheets registers the ``@customfunction`` on the
    container lazily — **the user may need to reload the spreadsheet tab
    once** before ``=FUNCTION_NAME(...)`` resolves (a cell shows
    ``#NAME?`` until the new script is indexed). Tell the user to reload
    if the function doesn't appear immediately.
    """
    # 1. Verify the target really is a Spreadsheet (reuse #138's Drive
    #    mimeType detection). Reject Docs / Slides / Forms / folders here
    #    — a =FUNCTION() cell only makes sense in a Sheet — BEFORE any
    #    project is created.
    kind = _auto_detect_container_kind(creds, sheet_id)
    if kind != "sheets":
        raise ValueError(
            f"as_install_custom_function targets a Google Sheet, but "
            f"{sheet_id!r} is a {kind!r} container. Custom spreadsheet "
            f"functions (=FUNCTION()) only exist in Sheets. Pass a Sheet "
            f"ID, or use as_generate_bound_script for a Doc/Slides bound "
            f"automation."
        )

    # 2. Codegen via the recipe registry (_recipes.py) — the SINGLE source
    #    for this tool's .gs body + manifest. render() shapes the same
    #    @customfunction-tagged body (build_custom_function_script, which
    #    validates the identifier + that the body defines it + non-empty) and
    #    the same bare manifest (build_manifest(None) — a custom function
    #    needs no scope beyond the container binding); the byte-identity pins
    #    guarantee the output is unchanged.
    spec = _RECIPES["as_install_custom_function"]
    params = {
        "sheet_id": sheet_id,
        "function_name": function_name,
        "function_body": function_body,
        "description": description,
        "name": name,
    }
    rendered = _render(spec, params)

    # 3. Deploy via the SAME machinery as_generate_bound_script uses:
    #    create bound project → push content → cut version + deploy.
    result = _mint_bound_automation(
        creds,
        tool=spec.name,
        container_id=sheet_id,
        container_kind=spec.container_kind,
        project_name=spec.project_name(params),
        script_body=rendered.script_body,
        manifest_dict=rendered.manifest,
        on_conflict=on_conflict,
    )
    script_id = result.script_id
    deployment_id = result.deployment_id

    return {
        "script_id": script_id,
        "deployment_id": deployment_id,
        "on_conflict": on_conflict,
        "reused_existing": result.reused,
        "replaced_count": result.replaced,
        "sheet_id": sheet_id,
        "function_name": function_name,
        "usage_hint": f"={function_name}(...)",
        "project_url": f"https://script.google.com/d/{script_id}/edit",
    }
