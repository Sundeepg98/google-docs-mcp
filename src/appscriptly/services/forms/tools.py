"""Google Forms MCP tool registrations (new service — sensitive scopes).

Mirrors the layout established by ``services/sheets/tools.py`` (PR #119)
and ``services/slides/tools.py`` (PR #120): ``@workspace_tool``-decorated
functions registered with the live ``mcp`` instance via ``server.py``'s
auto-discovery side-effect import.

**Tools registered here** (7 forms-service tools):

1. ``gforms_create_form``    — create a new form (title + description)
2. ``gforms_get_form``       — read a form's structure (items + ids)
3. ``gforms_add_question``   — add a question (text / choice / scale)
4. ``gforms_update_item``    — update an item's title / description
5. ``gforms_delete_item``    — delete an item by position
6. ``gforms_list_responses`` — list submitted responses (paginated)
7. ``gforms_get_response``   — read one submitted response

The authoritative declaration of this surface lives in
``services/forms/_expected_tools.py`` (``EXPECTED``), enforced against
the live registry by the registration tests + the golden snapshot.

**Per-tool scope declarations.** Each tool declares the SENSITIVE Forms
scope it exercises via ``@workspace_tool(... scopes=[...])``:

  * create / get / add / update / delete →
    ``https://www.googleapis.com/auth/forms.body``
  * list_responses / get_response →
    ``https://www.googleapis.com/auth/forms.responses.readonly``

Both scopes are in the baseline ``auth.WORKSPACE_SCOPES`` (added with
this service), so the declaration is honest per-tool surfacing — a no-op
for the consent flow (the scopes are already baseline-granted), exactly
like ``services/gas_deploy/tools.py`` declares its already-baseline Apps
Script scopes. The scope rides on ``ToolAnnotations.scopes`` for
observability / dynamic-consent UI.

**Import discipline.** Same as the other services: ``_get_credentials`` +
``_format_http_error`` imported directly from ``_tool_helpers`` (M3 Phase
C extraction); the api module via the standard ``from ... import``
pattern; ``@workspace_tool(service="forms", ...)`` carries the service=
literal driving the partition test + telemetry.
"""
from __future__ import annotations

from typing import Literal

from appscriptly.decorators import workspace_tool
from appscriptly.services.forms.api import (
    add_question as _add_question,
    create_form as _create_form,
    delete_item as _delete_item,
    get_form as _get_form,
    get_response as _get_response,
    list_responses as _list_responses,
    update_item as _update_item,
)
from appscriptly.tool_schemas import (
    GFORMS_ADD_QUESTION_OUTPUT_SCHEMA,
    GFORMS_CREATE_FORM_OUTPUT_SCHEMA,
    GFORMS_DELETE_ITEM_OUTPUT_SCHEMA,
    GFORMS_GET_FORM_OUTPUT_SCHEMA,
    GFORMS_GET_RESPONSE_OUTPUT_SCHEMA,
    GFORMS_LIST_RESPONSES_OUTPUT_SCHEMA,
    GFORMS_UPDATE_ITEM_OUTPUT_SCHEMA,
)

# Imported for parity with the sibling services' tools.py; the Forms
# tools don't currently need _format_http_error (HttpError propagates to
# the standard decorator envelope). Kept as a top-level import so adding a
# tool that DOES need it doesn't trigger a separate import statement.
from appscriptly._tool_helpers import (  # noqa: F401
    _format_http_error,
    _get_credentials,
)

# SENSITIVE (NOT restricted → no CASA) Forms scopes, declared per-tool.
# Both are baseline-granted via auth.WORKSPACE_SCOPES (added with this
# service), so these declarations are honest per-tool surfacing — a no-op
# for the consent flow, the same pattern services/gas_deploy/tools.py uses
# for its already-baseline Apps Script scopes.
_FORMS_BODY_SCOPE = ["https://www.googleapis.com/auth/forms.body"]
_FORMS_RESPONSES_SCOPE = [
    "https://www.googleapis.com/auth/forms.responses.readonly"
]


# ---------------------------------------------------------------------
# 1. gforms_create_form — forms.create (+ updateFormInfo for description)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Create a new Google Form",
    # Creating a fresh resource isn't a mutation of existing state.
    # Matches gslides_create_presentation / gsheets_create_spreadsheet.
    readonly=False,
    destructive=False,
    # Re-running creates ANOTHER form — NOT idempotent.
    idempotent=False,
    external=True,
    creds=True,
    scopes=_FORMS_BODY_SCOPE,
    output_schema=GFORMS_CREATE_FORM_OUTPUT_SCHEMA,
)
def gforms_create_form(creds, title: str, description: str | None = None) -> dict:
    """Create a Google Form (optionally with a description).

    USE WHEN: starting a new form / survey / quiz — typically the FIRST
    call in a create → add_question (×N) → get_form workflow.

    Uses Forms' ``forms.create`` REST endpoint. Because ``forms.create``
    accepts only the title at creation, a non-empty ``description`` is
    applied in a follow-up ``batchUpdate`` automatically, so you get a
    fully-populated form in one call. The form is owned by the OAuth user
    and lands in Drive root; move it with ``gdocs_move_to_folder``.

    Args:
        title: Title for the new form (its display title AND Drive
            filename).
        description: Optional description shown under the title.

    Returns:
        ``{form_id, url, title, description}`` — ``url`` is the responder
        link. Pipe ``form_id`` into ``gforms_add_question`` /
        ``gforms_get_form``.

    Choreography: ``gforms_create_form`` → ``gforms_add_question`` (×N) →
    ``gforms_get_form`` to verify. Pair with ``gdocs_move_to_folder`` /
    ``gdocs_share_file`` to file + share it, and
    ``as_install_form_handler`` to react to submissions.
    """
    try:
        return _create_form(creds, title, description=description)
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 2. gforms_get_form — forms.get (pure read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Read a Google Form's structure",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=_FORMS_BODY_SCOPE,
    output_schema=GFORMS_GET_FORM_OUTPUT_SCHEMA,
)
def gforms_get_form(creds, form_id: str) -> dict:
    """Read a form's title, description, and items (with their ids).

    USE WHEN: you need to inspect a form — summarize it, or find an
    item's position / ``item_id`` before editing it with
    ``gforms_update_item`` / ``gforms_delete_item``.

    Uses Forms' ``forms.get`` REST endpoint.

    Args:
        form_id: The Form ID (the ID part of the form's URL).

    Returns:
        ``{form_id, title, description, url, items: [{item_id, title,
        type}, ...]}``. ``type`` is a coarse kind (``"question"``,
        ``"page_break"``, ``"text"``, ``"image"``, ``"video"``,
        ``"question_group"``, or ``"unknown"``). ``items`` is empty for a
        form with no items yet.

    Choreography: the discovery step before ``gforms_update_item`` /
    ``gforms_delete_item`` (both address items by 0-based position — the
    order here IS that position). ``form_id`` typically comes from
    ``gforms_create_form`` or the user.
    """
    return _get_form(creds, form_id)


# ---------------------------------------------------------------------
# 3. gforms_add_question — batchUpdate (createItem)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Add a question to a Google Form",
    readonly=False,
    destructive=False,
    # Re-running adds ANOTHER question — NOT idempotent. Matches
    # gslides_add_slide / gdocs_insert_table.
    idempotent=False,
    external=True,
    creds=True,
    scopes=_FORMS_BODY_SCOPE,
    output_schema=GFORMS_ADD_QUESTION_OUTPUT_SCHEMA,
)
def gforms_add_question(
    creds,
    form_id: str,
    title: str,
    question_type: Literal["text", "choice", "scale"] = "text",
    index: int = 0,
    paragraph: bool = False,
    options: list[str] | None = None,
    choice_type: Literal["RADIO", "CHECKBOX", "DROP_DOWN"] = "RADIO",
    required: bool = False,
    low: int = 1,
    high: int = 5,
    low_label: str | None = None,
    high_label: str | None = None,
) -> dict:
    """Add a question to a form (text, choice, or scale).

    USE WHEN: building out a form's CONTENT — this is what turns a
    freshly-created (empty) form into a real one. Pairs with
    ``gforms_create_form``: create the form, then call this once per
    question.

    Uses Forms' ``batchUpdate`` with a single ``createItem`` request.
    The question shape is discriminated by ``question_type``:

      * ``"text"``   — short answer, or a paragraph (``paragraph=True``).
      * ``"choice"`` — RADIO / CHECKBOX / DROP_DOWN over ``options``
        (``options`` required + non-empty; pick via ``choice_type``).
      * ``"scale"``  — a linear scale from ``low`` to ``high`` with
        optional end labels (``low_label`` / ``high_label``).

    Args:
        form_id: The Form ID (from ``gforms_create_form`` /
            ``gforms_get_form``).
        title: The question text.
        question_type: ``"text"`` (default), ``"choice"``, or ``"scale"``.
        index: 0-based insertion position among the form's items (default
            0 — at the top). Must be >= 0.
        paragraph: ``question_type="text"`` only — long (paragraph) vs
            short answer. Default False.
        options: ``question_type="choice"`` — the choice strings (required
            + non-empty for choice questions).
        choice_type: ``question_type="choice"`` — RADIO (default),
            CHECKBOX, or DROP_DOWN.
        required: Whether an answer is required. Default False.
        low / high: ``question_type="scale"`` — scale bounds (default
            1..5; ``high`` must be > ``low``).
        low_label / high_label: ``question_type="scale"`` — optional end
            labels.

    Returns:
        ``{form_id, item_id, question_type, index}``. ``item_id`` is the
        new item's stable ID (a valid ``gforms_update_item`` /
        ``gforms_delete_item`` target — though those address by position).

    Choreography: ``gforms_create_form`` → ``gforms_add_question`` (×N) →
    ``gforms_get_form`` to verify.
    """
    try:
        return _add_question(
            creds,
            form_id,
            title,
            question_type=question_type,
            index=index,
            paragraph=paragraph,
            options=options,
            choice_type=choice_type,
            required=required,
            low=low,
            high=high,
            low_label=low_label,
            high_label=high_label,
        )
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 4. gforms_update_item — batchUpdate (updateItem)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Update a Google Form item's title / description",
    # Restyling an item's text in place is not destructive (content
    # persists; can be re-applied). Idempotent: same fields + position
    # twice = same state. Matches gdocs_format_range.
    readonly=False,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=_FORMS_BODY_SCOPE,
    output_schema=GFORMS_UPDATE_ITEM_OUTPUT_SCHEMA,
)
def gforms_update_item(
    creds,
    form_id: str,
    index: int,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """Update the title and/or description of a form item by position.

    USE WHEN: editing an existing question/item's wording — change its
    title or its helper description without recreating it.

    Uses Forms' ``batchUpdate`` with an ``updateItem`` request scoped to
    the item at ``index`` (Forms addresses items by 0-based position) and
    a precise ``updateMask``, so only the fields you pass change. Pass at
    least one of ``title`` / ``description``.

    Args:
        form_id: The Form ID.
        index: 0-based position of the item to update (>= 0). Find it
            with ``gforms_get_form`` (the items list IS in position
            order).
        title: New item title, or None to leave unchanged.
        description: New item description, or None to leave unchanged.

    Returns:
        ``{form_id, index, updated_fields}`` — ``updated_fields`` lists
        the fields that were set.

    Choreography: ``gforms_get_form`` to find the item's position →
    ``gforms_update_item`` to edit it.
    """
    try:
        return _update_item(
            creds, form_id, index, title=title, description=description,
        )
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 5. gforms_delete_item — batchUpdate (deleteItem)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Delete an item from a Google Form",
    # Removes an item — destructive=True (unlike update_item which only
    # restyles). idempotent=True in the same framing as gdocs_delete_tab:
    # deleting an already-gone item returns a 400 (non-retryable) that
    # propagates.
    readonly=False,
    destructive=True,
    idempotent=True,
    external=True,
    creds=True,
    scopes=_FORMS_BODY_SCOPE,
    output_schema=GFORMS_DELETE_ITEM_OUTPUT_SCHEMA,
)
def gforms_delete_item(creds, form_id: str, index: int) -> dict:
    """Delete the item at a given position from a form.

    USE WHEN: removing a question/item from a form.

    Uses Forms' ``batchUpdate`` with a ``deleteItem`` request addressing
    the item by its 0-based ``index``. Deleting an item shifts the
    positions of items after it — re-read with ``gforms_get_form`` before
    computing another index.

    Args:
        form_id: The Form ID.
        index: 0-based position of the item to delete (>= 0). Find it with
            ``gforms_get_form``.

    Returns:
        ``{form_id, deleted_index}``.

    Choreography: ``gforms_get_form`` to find the item's position →
    ``gforms_delete_item``. To delete the ENTIRE form (not one item) use
    ``gdocs_trash_file`` (forms are Drive files).

    WARNING: deletion shifts subsequent items' positions — re-read before
    the next index-based edit.
    """
    try:
        return _delete_item(creds, form_id, index)
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 6. gforms_list_responses — forms.responses.list (paginated read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="List a Google Form's responses",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=_FORMS_RESPONSES_SCOPE,
    output_schema=GFORMS_LIST_RESPONSES_OUTPUT_SCHEMA,
)
def gforms_list_responses(
    creds,
    form_id: str,
    page_size: int = 5000,
    page_token: str | None = None,
) -> dict:
    """List the responses submitted to a form (paginated).

    USE WHEN: collecting / analyzing what people submitted — survey
    results, quiz answers, sign-ups.

    Uses Forms' ``forms.responses.list`` REST endpoint. Requires the
    ``forms.responses.readonly`` scope.

    Args:
        form_id: The Form ID.
        page_size: Max responses per page (1..5000; default 5000 — the
            Forms maximum). Larger values are clamped to 5000.
        page_token: Opaque token from a prior call's ``next_page_token``
            to fetch the next page; omit for the first page.

    Returns:
        ``{form_id, responses: [{response_id, create_time,
        last_submitted_time, answers}, ...], next_page_token}``.
        ``answers`` is the raw Forms answer map (keyed by question id).
        ``next_page_token`` is the empty string when there are no more
        pages.

    Choreography: ``gforms_list_responses`` to enumerate, then
    ``gforms_get_response`` for a single submission's detail. Page by
    feeding ``next_page_token`` back as ``page_token``.
    """
    try:
        return _list_responses(
            creds, form_id, page_size=page_size, page_token=page_token,
        )
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------
# 7. gforms_get_response — forms.responses.get (single read)
# ---------------------------------------------------------------------


@workspace_tool(
    service="forms",
    title="Read a single Google Form response",
    readonly=True,
    destructive=False,
    idempotent=True,
    external=True,
    creds=True,
    scopes=_FORMS_RESPONSES_SCOPE,
    output_schema=GFORMS_GET_RESPONSE_OUTPUT_SCHEMA,
)
def gforms_get_response(creds, form_id: str, response_id: str) -> dict:
    """Read a single submitted form response by its id.

    USE WHEN: you need the full detail of ONE submission (e.g. after
    ``gforms_list_responses`` surfaced its ``response_id``, or a
    submission webhook handed you the id).

    Uses Forms' ``forms.responses.get`` REST endpoint. Requires the
    ``forms.responses.readonly`` scope.

    Args:
        form_id: The Form ID.
        response_id: The response's ID (from ``gforms_list_responses``).

    Returns:
        ``{form_id, response_id, create_time, last_submitted_time,
        answers}`` — ``answers`` is the raw Forms answer map keyed by
        question id.

    Choreography: ``gforms_list_responses`` → ``gforms_get_response``
    (with a ``response_id`` from the list).
    """
    try:
        return _get_response(creds, form_id, response_id)
    except ValueError as e:
        from fastmcp.exceptions import ToolError

        raise ToolError(str(e)) from e
