"""Unit tests for the Sheets batchUpdate request-builder (batch.py).

The builders in ``services/sheets/batch.py`` are PURE dict-assembly —
no creds, no Google round-trip — so the bulk of this file asserts the
exact request-dict shapes directly. Only the dispatcher
(``batch_update``) touches the API, and it's exercised through the same
``InMemoryGoogleAPIClient`` stub the rest of the sheets suite uses.

Coverage map:

1. ``grid_range``        — bound inclusion/omission + half-open + index
                           validation.
2. ``color``             — 0-1 float guard.
3. ``cell_format``       — flat-kwargs -> nested CellFormat, partial
                           composition, alignment/size validation.
4. ``_format_field_mask``— mask derived from exactly the set fields.
5. ``repeat_cell_request`` — full repeatCell shape + empty-format
                             rejection.
6. ``add_conditional_format_rule_request`` — the SECOND request type
   (proves the seam generalises) + its validation.
7. ``batch_update``      — dispatch shape (body wrapper) + envelope +
                           empty guard.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from appscriptly.google_api_client import (
    InMemoryGoogleAPIClient,
    with_google_api_client,
)
from appscriptly.services.sheets.batch import (
    _format_field_mask,
    _infer_number_format_type,
    add_conditional_format_rule_request,
    add_protected_range_request,
    add_sheet_request,
    batch_update,
    cell_format,
    color,
    delete_sheet_request,
    duplicate_sheet_request,
    freeze_request,
    grid_range,
    repeat_cell_request,
    update_sheet_title_request,
)


# ---------------------------------------------------------------------
# grid_range
# ---------------------------------------------------------------------


def test_grid_range_includes_only_supplied_bounds():
    """Only the bounds passed appear; omitted bounds are left out so
    Sheets applies its unbounded default."""
    gr = grid_range(0, start_row=0, end_row=3)
    assert gr == {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 3}


def test_grid_range_all_bounds():
    gr = grid_range(7, start_row=1, end_row=5, start_col=2, end_col=4)
    assert gr == {
        "sheetId": 7,
        "startRowIndex": 1,
        "endRowIndex": 5,
        "startColumnIndex": 2,
        "endColumnIndex": 4,
    }


def test_grid_range_whole_sheet_is_just_sheet_id():
    """Omitting all four bounds targets the whole sheet — a valid,
    useful case (format every cell)."""
    assert grid_range(3) == {"sheetId": 3}


def test_grid_range_rejects_negative_index():
    with pytest.raises(ValueError, match="must be >= 0"):
        grid_range(0, start_row=-1)


def test_grid_range_rejects_inverted_rows():
    """Half-open means end must be strictly greater than start."""
    with pytest.raises(ValueError, match="end_row.*must be >"):
        grid_range(0, start_row=5, end_row=5)


def test_grid_range_rejects_inverted_cols():
    with pytest.raises(ValueError, match="end_col.*must be >"):
        grid_range(0, start_col=4, end_col=2)


# ---------------------------------------------------------------------
# color
# ---------------------------------------------------------------------


def test_color_builds_rgb_dict():
    assert color(1.0, 0.5, 0.0) == {"red": 1.0, "green": 0.5, "blue": 0.0}


def test_color_defaults_to_black():
    assert color() == {"red": 0.0, "green": 0.0, "blue": 0.0}


def test_color_rejects_out_of_gamut_255_style():
    """The most common mistake — passing 0-255 ints — is caught."""
    with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
        color(255, 0, 0)


# ---------------------------------------------------------------------
# cell_format
# ---------------------------------------------------------------------


def test_cell_format_nests_text_options_under_text_format():
    fmt = cell_format(bold=True, italic=True, font_size=14)
    assert fmt == {
        "textFormat": {"bold": True, "italic": True, "fontSize": 14},
    }


def test_cell_format_foreground_goes_under_text_format():
    fmt = cell_format(foreground_color=color(1.0, 0.0, 0.0))
    assert fmt == {
        "textFormat": {"foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}},
    }


def test_cell_format_top_level_fields():
    fmt = cell_format(
        background_color=color(0.9, 0.9, 0.9),
        horizontal_alignment="CENTER",
        number_format="0.00%",
    )
    assert fmt == {
        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        "horizontalAlignment": "CENTER",
        # "0.00%" is a percent pattern → type inferred as PERCENT (not the
        # old blanket NUMBER, which mis-rendered it).
        "numberFormat": {"type": "PERCENT", "pattern": "0.00%"},
    }


def test_cell_format_empty_when_nothing_supplied():
    """No options -> empty dict (caller treats as no-op)."""
    assert cell_format() == {}


def test_cell_format_only_includes_supplied_options():
    """Partial format must NOT carry keys for unset options (so the
    derived field mask stays minimal and won't clobber unrelated
    existing formatting)."""
    fmt = cell_format(bold=True)
    assert fmt == {"textFormat": {"bold": True}}
    assert "backgroundColor" not in fmt


def test_cell_format_rejects_bad_alignment():
    with pytest.raises(ValueError, match="horizontal_alignment"):
        cell_format(horizontal_alignment="centre")


def test_cell_format_rejects_nonpositive_font_size():
    with pytest.raises(ValueError, match="font_size must be > 0"):
        cell_format(font_size=0)


# ---------------------------------------------------------------------
# _infer_number_format_type — pattern → NumberFormatType
# ---------------------------------------------------------------------


def test_cell_format_date_pattern_infers_date_type():
    """REGRESSION (the bug): a date pattern must NOT be sent as
    type=NUMBER — that mis-renders it. ``yyyy-mm-dd`` → type=DATE so the
    advertised date format actually renders as a date."""
    fmt = cell_format(number_format="yyyy-mm-dd")
    assert fmt["numberFormat"] == {"type": "DATE", "pattern": "yyyy-mm-dd"}


@pytest.mark.parametrize(
    "pattern,expected_type",
    [
        # date
        ("yyyy-mm-dd", "DATE"),
        ("m/d/yyyy", "DATE"),
        ("dddd, mmmm d", "DATE"),
        # time (hours/seconds/AM-PM, no date component)
        ("hh:mm:ss", "TIME"),
        ("h:mm AM/PM", "TIME"),
        # date + time → DATE_TIME
        ("yyyy-mm-dd hh:mm", "DATE_TIME"),
        ("m/d/yy h:mm:ss", "DATE_TIME"),
        # percent
        ("0.00%", "PERCENT"),
        ("#,##0%", "PERCENT"),
        # currency
        ("$#,##0.00", "CURRENCY"),
        ("€#,##0", "CURRENCY"),
        # plain number (the safe default)
        ("#,##0.00", "NUMBER"),
        ("0.000", "NUMBER"),
        ("#,##0", "NUMBER"),
    ],
)
def test_infer_number_format_type(pattern, expected_type):
    assert _infer_number_format_type(pattern) == expected_type


def test_cell_format_currency_pattern_infers_currency_type():
    fmt = cell_format(number_format="$#,##0.00")
    assert fmt["numberFormat"]["type"] == "CURRENCY"


def test_cell_format_plain_number_pattern_stays_number():
    """A non-date/time/percent/currency pattern still maps to NUMBER —
    the inference doesn't over-classify ordinary numeric patterns."""
    fmt = cell_format(number_format="#,##0.00")
    assert fmt["numberFormat"]["type"] == "NUMBER"


# ---------------------------------------------------------------------
# _format_field_mask
# ---------------------------------------------------------------------


def test_field_mask_covers_each_text_format_subfield():
    fmt = cell_format(bold=True, font_size=12)
    mask = _format_field_mask(fmt)
    assert set(mask.split(",")) == {
        "userEnteredFormat.textFormat.bold",
        "userEnteredFormat.textFormat.fontSize",
    }


def test_field_mask_covers_top_level_fields():
    fmt = cell_format(
        background_color=color(0.5, 0.5, 0.5),
        horizontal_alignment="RIGHT",
        number_format="$#,##0",
    )
    mask = set(_format_field_mask(fmt).split(","))
    assert mask == {
        "userEnteredFormat.backgroundColor",
        "userEnteredFormat.horizontalAlignment",
        "userEnteredFormat.numberFormat",
    }


def test_field_mask_empty_for_empty_format():
    assert _format_field_mask({}) == ""


# ---------------------------------------------------------------------
# repeat_cell_request
# ---------------------------------------------------------------------


def test_repeat_cell_request_full_shape():
    grid = grid_range(0, start_row=0, end_row=1, start_col=0, end_col=3)
    fmt = cell_format(bold=True, background_color=color(0.2, 0.2, 0.2))
    req = repeat_cell_request(grid, fmt)

    assert set(req) == {"repeatCell"}
    rc = req["repeatCell"]
    assert rc["range"] == grid
    assert rc["cell"] == {"userEnteredFormat": fmt}
    # The mask names exactly the fields the format set — nothing else.
    assert set(rc["fields"].split(",")) == {
        "userEnteredFormat.textFormat.bold",
        "userEnteredFormat.backgroundColor",
    }


def test_repeat_cell_request_rejects_empty_format():
    """An empty format -> empty field mask -> a no-op Sheets rejects.
    Caught client-side with a clearer message."""
    with pytest.raises(ValueError, match="fmt is empty"):
        repeat_cell_request(grid_range(0), cell_format())


# ---------------------------------------------------------------------
# add_conditional_format_rule_request — the SECOND request type
# ---------------------------------------------------------------------


def test_conditional_format_rule_full_shape():
    grid = grid_range(0, start_row=1, end_row=100, start_col=0, end_col=1)
    req = add_conditional_format_rule_request(
        grid,
        condition_type="NUMBER_GREATER",
        values=["100"],
        background_color=color(1.0, 0.0, 0.0),
        index=0,
    )
    assert set(req) == {"addConditionalFormatRule"}
    acfr = req["addConditionalFormatRule"]
    assert acfr["index"] == 0
    rule = acfr["rule"]
    assert rule["ranges"] == [grid]
    boolean = rule["booleanRule"]
    assert boolean["condition"] == {
        "type": "NUMBER_GREATER",
        "values": [{"userEnteredValue": "100"}],
    }
    # Reuses the same CellFormat primitive as repeatCell.
    assert boolean["format"] == {
        "backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0},
    }


def test_conditional_format_rule_without_values_omits_values_key():
    """Conditions like BLANK take no comparison value."""
    req = add_conditional_format_rule_request(
        grid_range(0),
        condition_type="BLANK",
        background_color=color(0.8, 0.8, 0.8),
    )
    cond = req["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]
    assert cond == {"type": "BLANK"}
    assert "values" not in cond


def test_conditional_format_rule_requires_a_format():
    """A rule with no format to apply does nothing — reject it."""
    with pytest.raises(ValueError, match="needs a format"):
        add_conditional_format_rule_request(
            grid_range(0), condition_type="NOT_BLANK"
        )


# ---------------------------------------------------------------------
# batch_update — the dispatcher (the one API-touching function)
# ---------------------------------------------------------------------


@pytest.fixture
def stub_sheets_for_batch():
    sheets = MagicMock(name="sheets-v4-stub-batch")
    sheets.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
        "replies": [{}],
    }
    with with_google_api_client(InMemoryGoogleAPIClient({("sheets", "v4"): sheets})):
        yield sheets


def _last_batch_kwargs(sheets: MagicMock) -> dict:
    for call in reversed(sheets.spreadsheets().batchUpdate.call_args_list):
        if "spreadsheetId" in call.kwargs:
            return call.kwargs
    raise AssertionError("no batchUpdate() call captured spreadsheetId")


def test_batch_update_wraps_requests_in_body(stub_sheets_for_batch):
    """The Sheets API expects ``body={"requests": [...]}`` — the
    request list goes UNDER the ``requests`` key."""
    req = repeat_cell_request(
        grid_range(0, start_row=0, end_row=1), cell_format(bold=True)
    )
    batch_update(MagicMock(), "SPREAD-XYZ", [req])
    kw = _last_batch_kwargs(stub_sheets_for_batch)
    assert kw["spreadsheetId"] == "SPREAD-XYZ"
    assert kw["body"] == {"requests": [req]}


def test_batch_update_returns_flat_envelope(stub_sheets_for_batch):
    req = repeat_cell_request(grid_range(0), cell_format(bold=True))
    result = batch_update(MagicMock(), "SPREAD1", [req])
    assert result == {
        "spreadsheet_id": "SPREAD1",
        "total_requests": 1,
        "replies": [{}],
    }


def test_batch_update_counts_multiple_requests(stub_sheets_for_batch):
    """total_requests echoes the batch size — composing several builders
    into one round-trip is the whole point of the seam."""
    reqs = [
        repeat_cell_request(
            grid_range(0, start_row=0, end_row=1), cell_format(bold=True)
        ),
        add_conditional_format_rule_request(
            grid_range(0, start_row=1, end_row=10),
            condition_type="NUMBER_GREATER",
            values=["5"],
            background_color=color(1.0, 0.9, 0.9),
        ),
    ]
    result = batch_update(MagicMock(), "SPREAD1", reqs)
    assert result["total_requests"] == 2
    kw = _last_batch_kwargs(stub_sheets_for_batch)
    assert kw["body"] == {"requests": reqs}


def test_batch_update_rejects_empty_requests():
    """An empty batch is a caller bug — Sheets would 400."""
    with pytest.raises(ValueError, match="requests cannot be empty"):
        batch_update(MagicMock(), "SPREAD1", [])


def test_batch_update_defaults_to_empty_replies_when_sheets_omits_them(
    stub_sheets_for_batch,
):
    """Defensive: missing ``replies`` -> [] rather than KeyError."""
    stub_sheets_for_batch.spreadsheets().batchUpdate().execute.return_value = {
        "spreadsheetId": "SPREAD1",
    }
    req = repeat_cell_request(grid_range(0), cell_format(bold=True))
    result = batch_update(MagicMock(), "SPREAD1", [req])
    assert result["replies"] == []


# ---------------------------------------------------------------------
# add_sheet_request — sheet-lifecycle builder (tab add)
# ---------------------------------------------------------------------


def test_add_sheet_request_minimal_shape():
    """Just a title -> addSheet with properties.title, no index."""
    req = add_sheet_request("Summary")
    assert req == {"addSheet": {"properties": {"title": "Summary"}}}


def test_add_sheet_request_includes_index_when_supplied():
    """An explicit index lands in properties.index (0 = leftmost)."""
    req = add_sheet_request("Q1", index=0)
    assert req == {"addSheet": {"properties": {"title": "Q1", "index": 0}}}


def test_add_sheet_request_strips_title_whitespace():
    """Leading/trailing whitespace is stripped before the request dict."""
    req = add_sheet_request("  Padded  ")
    assert req["addSheet"]["properties"]["title"] == "Padded"


def test_add_sheet_request_rejects_blank_title():
    with pytest.raises(ValueError, match="title cannot be empty"):
        add_sheet_request("   ")


def test_add_sheet_request_rejects_negative_index():
    with pytest.raises(ValueError, match="index must be >= 0"):
        add_sheet_request("T", index=-1)


# ---------------------------------------------------------------------
# delete_sheet_request — sheet-lifecycle builder (tab delete)
# ---------------------------------------------------------------------


def test_delete_sheet_request_shape():
    assert delete_sheet_request(12345) == {"deleteSheet": {"sheetId": 12345}}


def test_delete_sheet_request_allows_zero_gid():
    """gid 0 (the default/first tab) is a valid delete target."""
    assert delete_sheet_request(0) == {"deleteSheet": {"sheetId": 0}}


def test_delete_sheet_request_rejects_negative_gid():
    with pytest.raises(ValueError, match="sheet_id must be >= 0"):
        delete_sheet_request(-1)


# ---------------------------------------------------------------------
# update_sheet_title_request — sheet-lifecycle builder (tab rename)
# ---------------------------------------------------------------------


def test_update_sheet_title_request_shape_with_scoped_field_mask():
    """Rename request carries sheetId + title and a fields mask scoped
    to exactly ``title`` — so no other sheet property is touched."""
    req = update_sheet_title_request(0, "Renamed")
    assert req == {
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "title": "Renamed"},
            "fields": "title",
        }
    }


def test_update_sheet_title_request_strips_whitespace():
    req = update_sheet_title_request(7, "  Tidy  ")
    assert req["updateSheetProperties"]["properties"]["title"] == "Tidy"


def test_update_sheet_title_request_rejects_blank_title():
    with pytest.raises(ValueError, match="title cannot be empty"):
        update_sheet_title_request(0, "")


def test_update_sheet_title_request_rejects_negative_gid():
    with pytest.raises(ValueError, match="sheet_id must be >= 0"):
        update_sheet_title_request(-5, "X")


# ---------------------------------------------------------------------
# duplicate_sheet_request — sheet-lifecycle builder (tab copy)
# ---------------------------------------------------------------------


def test_duplicate_sheet_request_minimal_shape():
    """Just a source gid -> duplicateSheet with only sheetId (Sheets
    auto-names + auto-places the copy)."""
    req = duplicate_sheet_request(0)
    assert req == {"duplicateSheet": {"sheetId": 0}}


def test_duplicate_sheet_request_with_name_and_index():
    """An explicit name + index land in newSheetName / insertSheetIndex."""
    req = duplicate_sheet_request(7, new_sheet_name="Copy", insert_index=2)
    assert req == {
        "duplicateSheet": {
            "sheetId": 7,
            "insertSheetIndex": 2,
            "newSheetName": "Copy",
        }
    }


def test_duplicate_sheet_request_strips_name_whitespace():
    req = duplicate_sheet_request(0, new_sheet_name="  Padded  ")
    assert req["duplicateSheet"]["newSheetName"] == "Padded"


def test_duplicate_sheet_request_rejects_negative_source_gid():
    with pytest.raises(ValueError, match="source_sheet_id must be >= 0"):
        duplicate_sheet_request(-1)


def test_duplicate_sheet_request_rejects_negative_index():
    with pytest.raises(ValueError, match="insert_index must be >= 0"):
        duplicate_sheet_request(0, insert_index=-1)


def test_duplicate_sheet_request_rejects_blank_name():
    """A blank (non-None) name is a caller bug — omit it for auto-naming."""
    with pytest.raises(ValueError, match="new_sheet_name cannot be blank"):
        duplicate_sheet_request(0, new_sheet_name="   ")


# ---------------------------------------------------------------------
# freeze_request — sheet-lifecycle builder (freeze rows/cols)
# ---------------------------------------------------------------------


def test_freeze_request_rows_only_scoped_mask():
    """Freezing rows sets only frozenRowCount + a mask scoped to it (the
    column count is untouched)."""
    req = freeze_request(0, frozen_row_count=1)
    assert req == {
        "updateSheetProperties": {
            "properties": {
                "sheetId": 0,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def test_freeze_request_cols_only_scoped_mask():
    req = freeze_request(3, frozen_column_count=2)
    assert req == {
        "updateSheetProperties": {
            "properties": {
                "sheetId": 3,
                "gridProperties": {"frozenColumnCount": 2},
            },
            "fields": "gridProperties.frozenColumnCount",
        }
    }


def test_freeze_request_both_rows_and_cols():
    """Both counts -> both gridProperties fields + a two-field mask."""
    req = freeze_request(0, frozen_row_count=1, frozen_column_count=1)
    props = req["updateSheetProperties"]["properties"]["gridProperties"]
    assert props == {"frozenRowCount": 1, "frozenColumnCount": 1}
    mask = set(req["updateSheetProperties"]["fields"].split(","))
    assert mask == {
        "gridProperties.frozenRowCount",
        "gridProperties.frozenColumnCount",
    }


def test_freeze_request_zero_count_unfreezes():
    """``0`` is a valid value (unfreeze) — not rejected, and it appears in
    the request so Sheets clears the freeze."""
    req = freeze_request(0, frozen_row_count=0)
    props = req["updateSheetProperties"]["properties"]["gridProperties"]
    assert props == {"frozenRowCount": 0}


def test_freeze_request_rejects_negative_gid():
    with pytest.raises(ValueError, match="sheet_id must be >= 0"):
        freeze_request(-1, frozen_row_count=1)


def test_freeze_request_rejects_negative_count():
    with pytest.raises(ValueError, match="frozen_row_count must be >= 0"):
        freeze_request(0, frozen_row_count=-1)


def test_freeze_request_rejects_no_counts():
    """An all-None freeze is a no-op Sheets rejects — caught client-side."""
    with pytest.raises(ValueError, match="at least one of frozen_row_count"):
        freeze_request(0)


# ---------------------------------------------------------------------
# add_protected_range_request — range-protection builder
# ---------------------------------------------------------------------


def test_add_protected_range_request_restricted_default():
    """Default (warning_only=False, no editors) -> a bare protectedRange
    with just the range — only the owner can edit."""
    grid = grid_range(0, start_row=0, end_row=10, start_col=0, end_col=1)
    req = add_protected_range_request(grid)
    assert req == {"addProtectedRange": {"protectedRange": {"range": grid}}}


def test_add_protected_range_request_warning_only():
    """warning_only=True sets warningOnly and does NOT add an editors list."""
    grid = grid_range(0)
    req = add_protected_range_request(grid, warning_only=True)
    pr = req["addProtectedRange"]["protectedRange"]
    assert pr["warningOnly"] is True
    assert "editors" not in pr


def test_add_protected_range_request_with_editors():
    """Editors land under editors.users; description is carried through."""
    grid = grid_range(0, start_col=0, end_col=2)
    req = add_protected_range_request(
        grid,
        description="Locked totals",
        editor_emails=["a@x.com", "b@x.com"],
    )
    pr = req["addProtectedRange"]["protectedRange"]
    assert pr["description"] == "Locked totals"
    assert pr["editors"] == {"users": ["a@x.com", "b@x.com"]}
    assert "warningOnly" not in pr


def test_add_protected_range_request_rejects_warning_with_editors():
    """warning_only + editor_emails are mutually exclusive."""
    with pytest.raises(ValueError, match="incompatible with warning_only"):
        add_protected_range_request(
            grid_range(0), warning_only=True, editor_emails=["a@x.com"],
        )
