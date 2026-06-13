"""Google Forms REST wrapper (new service — sensitive scopes, no CASA).

Ergonomic helpers over the Forms API v1:

  * ``create_form``     — ``forms.create`` (title + optional description)
  * ``get_form``        — ``forms.get`` (structure + items, flattened)
  * ``add_question``    — ``forms.batchUpdate`` with a single
                          ``createItem`` request (text / choice / scale)
  * ``update_item``     — ``forms.batchUpdate`` with ``updateItem``
                          (title / description, precise ``updateMask``)
  * ``delete_item``     — ``forms.batchUpdate`` with ``deleteItem``
  * ``list_responses``  — ``forms.responses.list`` (paginated)
  * ``get_response``    — ``forms.responses.get`` (one submission)

Pairs with the form-submit trigger (``as_install_form_handler``): that
tool reacts to a submission via a bound Apps Script; these tools build
and read the form itself through the REST API.

**Scope note.** Two SENSITIVE (NOT restricted → no CASA) scopes back
this service, added to the single-source ``auth.WORKSPACE_SCOPES``:

  * ``https://www.googleapis.com/auth/forms.body`` — create / edit forms
    (``forms.create``, ``forms.get``, ``forms.batchUpdate``).
  * ``https://www.googleapis.com/auth/forms.responses.readonly`` — read
    responses (``forms.responses.list`` / ``forms.responses.get``).

Both are Google-SENSITIVE, not RESTRICTED — they require app
verification but NOT a CASA security assessment (only RESTRICTED scopes
trigger CASA). Existing user grants pick the new scopes up automatically
on next token refresh via Google's ``include_granted_scopes=true``
incremental-consent flow — the same pattern that handled the Sheets
(PR #119) and Slides (PR #120) scope additions. No forced re-consent.

**Two-phase create (title + description).** ``forms.create`` accepts
ONLY the ``info.title`` (and ``documentTitle``) at creation; a
description must be set in a follow-up ``batchUpdate`` (``updateFormInfo``
with an ``info.description`` + ``updateMask="description"``). ``create_form``
does both in one helper so the caller passes title + description once and
gets a fully-populated form back — mirroring how ``services/slides``
``create_presentation`` keeps a single-call workflow.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from appscriptly.google_api_client import execute_with_retry
from appscriptly.google_clients import get_service

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


# Choice-question types Forms supports (``ChoiceQuestion.type``). RADIO is
# the single-select default; CHECKBOX is multi-select; DROP_DOWN is the
# compact single-select. Restricting to this set gives a helpful
# client-side error instead of a Forms 400 on a typo'd enum.
_CHOICE_TYPES = frozenset({"RADIO", "CHECKBOX", "DROP_DOWN"})

# Supported question kinds for ``add_question``. The Forms question union
# is larger (date, time, fileUpload, rating, …); this curated set covers
# the kinds a form-authoring agent reaches for first. Widen when a real
# consumer needs more (rule-of-three).
_QUESTION_KINDS = frozenset({"text", "choice", "scale"})


def create_form(
    creds: Credentials,
    title: str,
    description: str | None = None,
) -> dict:
    """Create a Google Form via ``forms.create`` (+ optional description).

    ``forms.create`` accepts only ``info.title`` at creation; a
    description requires a follow-up ``batchUpdate`` (``updateFormInfo``).
    This helper does both so the caller gets a fully-populated form in a
    single call.

    Args:
        creds: OAuth credentials carrying the ``forms.body`` scope.
        title: The form's title. Becomes the form's display title AND its
            Drive filename. Empty / whitespace rejected client-side.
        description: Optional form description (shown under the title).
            Omit / None to leave it blank.

    Returns:
        ``{form_id, url, title, description}`` — ``url`` is the responder
        link composed from the returned ``responderUri`` (falls back to a
        synthesized edit URL). ``description`` echoes what was set
        (empty string when none).

    Raises:
        ValueError: empty / whitespace ``title``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")

    forms = get_service("forms", "v1", credentials=creds)
    # NOT idempotent: each call creates ANOTHER form. Matches
    # gslides_create_presentation / gsheets_create_spreadsheet.
    created = forms.forms().create(
        body={"info": {"title": title.strip()}},
    ).execute()
    form_id = created["formId"]

    desc = (description or "").strip()
    if desc:
        # Description can't be set at create time — patch it in now.
        execute_with_retry(
            lambda: forms.forms().batchUpdate(
                formId=form_id,
                body={"requests": [{
                    "updateFormInfo": {
                        "info": {"description": desc},
                        "updateMask": "description",
                    },
                }]},
            ).execute(),
            idempotent=True,
            op_name="forms.forms.batchUpdate.updateFormInfo",
        )

    responder_uri = created.get("responderUri")
    return {
        "form_id": form_id,
        "url": responder_uri
        or f"https://docs.google.com/forms/d/{form_id}/edit",
        "title": created.get("info", {}).get("title", title.strip()),
        "description": desc,
    }


def get_form(creds: Credentials, form_id: str) -> dict:
    """Read a form's structure via ``forms.get``.

    Args:
        creds: OAuth credentials carrying the ``forms.body`` scope (the
            ``forms.get`` method also accepts ``forms.body.readonly`` /
            Drive scopes; ``forms.body`` is in our baseline so the read
            works with no extra grant).
        form_id: The Form ID (the ID part of the form's URL).

    Returns:
        ``{form_id, title, description, url, items: [...]}``. Each entry
        in ``items`` is ``{item_id, title, type}`` — ``type`` is a coarse
        kind (``"question"``, ``"question_group"``, ``"page_break"``,
        ``"text"``, ``"image"``, ``"video"``, or ``"unknown"``) derived
        from which item field is present, so a caller can locate an item's
        ``item_id`` for a later ``update_item`` / ``delete_item`` without
        decoding the full Forms item union.

    Raises:
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    forms = get_service("forms", "v1", credentials=creds)
    # Read-only + idempotent.
    form = execute_with_retry(
        lambda: forms.forms().get(formId=form_id).execute(),
        idempotent=True,
        op_name="forms.forms.get",
    )
    info = form.get("info", {})
    return {
        "form_id": form_id,
        "title": info.get("title", ""),
        "description": info.get("description", ""),
        "url": form.get("responderUri")
        or f"https://docs.google.com/forms/d/{form_id}/edit",
        "items": [
            {
                "item_id": item.get("itemId", ""),
                "title": item.get("title", ""),
                "type": _classify_item(item),
            }
            for item in form.get("items", [])
        ],
    }


def add_question(
    creds: Credentials,
    form_id: str,
    title: str,
    question_type: str = "text",
    *,
    index: int = 0,
    paragraph: bool = False,
    options: list[str] | None = None,
    choice_type: str = "RADIO",
    required: bool = False,
    low: int = 1,
    high: int = 5,
    low_label: str | None = None,
    high_label: str | None = None,
) -> dict:
    """Add a question to a form via ``batchUpdate`` (``createItem``).

    Builds a single ``createItem`` request whose ``item.questionItem``
    carries one of three question shapes, discriminated by
    ``question_type``:

      * ``"text"``   — a ``textQuestion`` (set ``paragraph=True`` for a
        long-answer paragraph; default is a short single-line answer).
      * ``"choice"`` — a ``choiceQuestion`` (``choice_type`` RADIO /
        CHECKBOX / DROP_DOWN over ``options``).
      * ``"scale"``  — a ``scaleQuestion`` (a ``low``..``high`` linear
        scale with optional end labels).

    Args:
        creds: OAuth credentials carrying the ``forms.body`` scope.
        form_id: The Form ID.
        title: The question text. Empty / whitespace rejected.
        question_type: ``"text"`` (default), ``"choice"``, or ``"scale"``.
        index: 0-based insertion position among the form's items
            (default 0 — inserts at the top). Must be >= 0.
        paragraph: For ``question_type="text"`` only — long-answer
            (paragraph) vs short answer. Default False.
        options: For ``question_type="choice"`` — the list of choice
            strings. Required + non-empty for choice questions.
        choice_type: For ``question_type="choice"`` — RADIO (default),
            CHECKBOX, or DROP_DOWN.
        required: Whether an answer is required. Default False.
        low: For ``question_type="scale"`` — the scale's low bound
            (default 1).
        high: For ``question_type="scale"`` — the scale's high bound
            (default 5). Must be > ``low``.
        low_label / high_label: Optional end labels for a scale question.

    Returns:
        ``{form_id, item_id, question_type, index}`` — ``item_id`` is the
        new item's stable ID (from the ``createItem`` reply), usable as a
        later ``update_item`` / ``delete_item`` target.

    Raises:
        ValueError: empty title; unknown ``question_type``; negative
            ``index``; choice without options / with a bad ``choice_type``;
            scale with ``high <= low``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty.")
    if question_type not in _QUESTION_KINDS:
        raise ValueError(
            f"question_type must be one of {sorted(_QUESTION_KINDS)} — "
            f"got {question_type!r}."
        )
    if index < 0:
        raise ValueError("index must be >= 0.")

    question = _build_question(
        question_type,
        paragraph=paragraph,
        options=options,
        choice_type=choice_type,
        required=required,
        low=low,
        high=high,
        low_label=low_label,
        high_label=high_label,
    )

    forms = get_service("forms", "v1", credentials=creds)
    request = {
        "createItem": {
            "item": {
                "title": title.strip(),
                "questionItem": {"question": question},
            },
            "location": {"index": index},
        },
    }
    # NOT idempotent: each call creates ANOTHER item. Matches
    # gslides_add_slide / gdocs_insert_table.
    resp = execute_with_retry(
        lambda: forms.forms().batchUpdate(
            formId=form_id,
            body={"requests": [request]},
        ).execute(),
        idempotent=False,
        op_name="forms.forms.batchUpdate.createItem",
    )

    # The createItem reply echoes the new item's id under
    # replies[].createItem.itemId.
    item_id = ""
    for reply in resp.get("replies", []) or []:
        ci = reply.get("createItem")
        if ci and ci.get("itemId"):
            item_id = ci["itemId"]
            break

    return {
        "form_id": form_id,
        "item_id": item_id,
        "question_type": question_type,
        "index": index,
    }


def update_item(
    creds: Credentials,
    form_id: str,
    index: int,
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an item's title and/or description via ``updateItem``.

    Scoped to the item at ``location.index`` (Forms ``updateItem``
    addresses items by position). Builds a precise ``updateMask`` so only
    the fields you pass change — unmasked fields are left untouched.

    Args:
        creds: OAuth credentials carrying the ``forms.body`` scope.
        form_id: The Form ID.
        index: 0-based position of the item to update (>= 0). Use
            ``get_form`` to find an item's position.
        title: New item title, or None to leave unchanged.
        description: New item description, or None to leave unchanged.

    Returns:
        ``{form_id, index, updated_fields}`` — ``updated_fields`` is the
        list of fields that were set (the ``updateMask`` components).

    Raises:
        ValueError: negative ``index``, or neither ``title`` nor
            ``description`` supplied (nothing to update).
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if index < 0:
        raise ValueError("index must be >= 0.")

    item: dict[str, Any] = {}
    fields: list[str] = []
    if title is not None:
        item["title"] = title
        fields.append("title")
    if description is not None:
        item["description"] = description
        fields.append("description")
    if not fields:
        raise ValueError(
            "no fields supplied — pass at least one of title or description."
        )

    forms = get_service("forms", "v1", credentials=creds)
    request = {
        "updateItem": {
            "item": item,
            "location": {"index": index},
            "updateMask": ",".join(fields),
        },
    }
    # Idempotent: applying the same title/description to the same item
    # twice yields the same form state. Matches gdocs_format_range.
    execute_with_retry(
        lambda: forms.forms().batchUpdate(
            formId=form_id,
            body={"requests": [request]},
        ).execute(),
        idempotent=True,
        op_name="forms.forms.batchUpdate.updateItem",
    )
    return {"form_id": form_id, "index": index, "updated_fields": fields}


def delete_item(creds: Credentials, form_id: str, index: int) -> dict:
    """Delete the item at ``index`` via ``batchUpdate`` (``deleteItem``).

    Args:
        creds: OAuth credentials carrying the ``forms.body`` scope.
        form_id: The Form ID.
        index: 0-based position of the item to delete (>= 0). Use
            ``get_form`` to find an item's position.

    Returns:
        ``{form_id, deleted_index}``.

    Raises:
        ValueError: negative ``index``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if index < 0:
        raise ValueError("index must be >= 0.")

    forms = get_service("forms", "v1", credentials=creds)
    # destructive=True at the tool layer, but idempotent here in the same
    # framing as gdocs_delete_tab: deleting an already-gone item returns a
    # 400 (non-retryable), which propagates to the caller.
    execute_with_retry(
        lambda: forms.forms().batchUpdate(
            formId=form_id,
            body={"requests": [{"deleteItem": {"location": {"index": index}}}]},
        ).execute(),
        idempotent=True,
        op_name="forms.forms.batchUpdate.deleteItem",
    )
    return {"form_id": form_id, "deleted_index": index}


def list_responses(
    creds: Credentials,
    form_id: str,
    *,
    page_size: int = 5000,
    page_token: str | None = None,
) -> dict:
    """List a form's responses via ``forms.responses.list`` (paginated).

    Args:
        creds: OAuth credentials carrying the
            ``forms.responses.readonly`` scope.
        form_id: The Form ID.
        page_size: Max responses per page (1..5000; default 5000 — the
            Forms maximum). Clamped client-side to that range.
        page_token: Opaque token from a prior call's
            ``next_page_token`` to fetch the next page; omit for page 1.

    Returns:
        ``{form_id, responses: [...], next_page_token}``. Each entry in
        ``responses`` is ``{response_id, create_time, last_submitted_time,
        answers}`` (``answers`` is the raw Forms answer map, keyed by
        question id). ``next_page_token`` is the empty string when there
        are no more pages.

    Raises:
        ValueError: ``page_size`` < 1.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1.")
    page_size = min(page_size, 5000)

    forms = get_service("forms", "v1", credentials=creds)
    # Read-only + idempotent.
    resp = execute_with_retry(
        lambda: forms.forms().responses().list(
            formId=form_id,
            pageSize=page_size,
            pageToken=page_token,
        ).execute(),
        idempotent=True,
        op_name="forms.forms.responses.list",
    )
    return {
        "form_id": form_id,
        "responses": [
            _summarize_response(r) for r in resp.get("responses", []) or []
        ],
        "next_page_token": resp.get("nextPageToken", ""),
    }


def get_response(creds: Credentials, form_id: str, response_id: str) -> dict:
    """Read a single form response via ``forms.responses.get``.

    Args:
        creds: OAuth credentials carrying the
            ``forms.responses.readonly`` scope.
        form_id: The Form ID.
        response_id: The response's ID (from ``list_responses``).

    Returns:
        ``{form_id, response_id, create_time, last_submitted_time,
        answers}`` — ``answers`` is the raw Forms answer map keyed by
        question id.

    Raises:
        ValueError: empty ``response_id``.
        HttpError: from the underlying SDK on 4xx / 5xx — propagated.
    """
    if not response_id or not response_id.strip():
        raise ValueError("response_id cannot be empty.")

    forms = get_service("forms", "v1", credentials=creds)
    # Read-only + idempotent.
    resp = execute_with_retry(
        lambda: forms.forms().responses().get(
            formId=form_id,
            responseId=response_id,
        ).execute(),
        idempotent=True,
        op_name="forms.forms.responses.get",
    )
    summary = _summarize_response(resp)
    return {"form_id": form_id, **summary}


# ---------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------


def _build_question(
    question_type: str,
    *,
    paragraph: bool,
    options: list[str] | None,
    choice_type: str,
    required: bool,
    low: int,
    high: int,
    low_label: str | None,
    high_label: str | None,
) -> dict:
    """Build the ``Question`` sub-object for a ``createItem`` request.

    Discriminated by ``question_type`` (already validated by the caller
    to be one of ``_QUESTION_KINDS``). Raises ``ValueError`` on the
    type-specific constraints (choice needs options, scale needs
    ``high > low``).
    """
    question: dict[str, Any] = {"required": required}

    if question_type == "text":
        question["textQuestion"] = {"paragraph": paragraph}
    elif question_type == "choice":
        if not options:
            raise ValueError(
                "choice questions require a non-empty options list."
            )
        if choice_type not in _CHOICE_TYPES:
            raise ValueError(
                f"choice_type must be one of {sorted(_CHOICE_TYPES)} — "
                f"got {choice_type!r}."
            )
        question["choiceQuestion"] = {
            "type": choice_type,
            "options": [{"value": opt} for opt in options],
        }
    else:  # "scale"
        if high <= low:
            raise ValueError(
                f"scale high ({high}) must be greater than low ({low})."
            )
        scale: dict[str, Any] = {"low": low, "high": high}
        if low_label is not None:
            scale["lowLabel"] = low_label
        if high_label is not None:
            scale["highLabel"] = high_label
        question["scaleQuestion"] = scale

    return question


def _classify_item(item: dict) -> str:
    """Coarse kind for a Forms item, from which union field is present.

    Forms items are a tagged union (questionItem / questionGroupItem /
    pageBreakItem / textItem / imageItem / videoItem). The consumer of
    ``get_form`` just wants a label + the item_id to target a later
    edit; the full per-type decode is out of scope here.
    """
    for field, label in (
        ("questionItem", "question"),
        ("questionGroupItem", "question_group"),
        ("pageBreakItem", "page_break"),
        ("textItem", "text"),
        ("imageItem", "image"),
        ("videoItem", "video"),
    ):
        if field in item:
            return label
    return "unknown"


def _summarize_response(resp: dict) -> dict:
    """Flatten a Forms ``FormResponse`` to the fields the tools surface.

    Keeps the raw ``answers`` map as-is (keyed by question id) — decoding
    each answer's value union is a follow-up if a consumer needs it;
    callers today just want the response id + timestamps + the raw
    answers. Stable identity for JSON consumers: missing string fields
    become ``""`` and a missing answers map becomes ``{}``.
    """
    return {
        "response_id": resp.get("responseId", ""),
        "create_time": resp.get("createTime", ""),
        "last_submitted_time": resp.get("lastSubmittedTime", ""),
        "answers": resp.get("answers", {}),
    }


__all__ = [
    "add_question",
    "create_form",
    "delete_item",
    "get_form",
    "get_response",
    "list_responses",
    "update_item",
]
