"""Spreadsheet parsing (xlsx + csv) -> raw rows keyed by header text."""
from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import load_workbook

from ..errors import BadFileError

# Columns of the Menu Items sheet, in canonical order.
CANONICAL_COLUMNS = [
    "item_id",
    "category",
    "subcategory",
    "item_name",
    "description",
    "price",
    "dietary_type",
    "spice_level",
    "calorie_range",
    "allergens",
    "image_filename",
    "is_featured",
    "is_available",
    "sort_order",
    "tags",
]
REQUIRED_COLUMNS = ["category", "item_name", "price", "dietary_type", "image_filename"]

_HINT_MARKERS = ("required", "optional", "auto-", "defaults to", "e.g.", "pick from")


def _cell_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def read_sheet(data: bytes, filename: str) -> list[dict[str, str | None]]:
    """Parse the spreadsheet and return rows as dicts keyed by header text.

    Finds the header row (the row that looks like the column schema), skips
    the template's hint row and blank rows, and applies the row-count limit
    at the caller's discretion.
    """
    name = (filename or "").lower()
    if name.endswith(".csv"):
        matrix = _read_csv(data)
    else:
        matrix = _read_xlsx(data, filename)
    header_idx = _find_header_row(matrix)
    if header_idx is None:
        raise BadFileError(
            "Could not find a header row: no row in the file matches the expected "
            "menu columns (item_name, category, price, ...)."
        )
    headers = [_cell_to_str(c) or "" for c in matrix[header_idx]]
    rows: list[dict[str, str | None]] = []
    for raw in matrix[header_idx + 1 :]:
        if _is_blank_row(raw):
            continue
        row = {h: _cell_to_str(raw[i]) if i < len(raw) else None for i, h in enumerate(headers)}
        if _is_hint_row(row):
            continue
        rows.append(row)
    return rows


def _read_xlsx(data: bytes, filename: str) -> list[list[Any]]:
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise BadFileError(f"'{filename}' could not be opened as an Excel workbook.") from exc
    # Prefer the sheet that carries the schema; fall back to the first sheet.
    sheet = None
    for ws in wb.worksheets:
        first = next((c for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ()) if c), None)
        if first and str(first).strip().lower() == "item_id":
            sheet = ws
            break
    ws = sheet or wb.worksheets[0]
    matrix = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return matrix


def _read_csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadFileError("CSV is not valid UTF-8 text.") from exc
    return [row for row in csv.reader(io.StringIO(text))]


def _find_header_row(matrix: list[list[Any]]) -> int | None:
    """Scan the first rows for the row matching the canonical column schema.

    A partial re-upload file may carry only 2-3 columns (item_id + the fields
    being fixed), so the threshold is 2 known column names, not most of them.
    """
    target = {c.lower() for c in CANONICAL_COLUMNS}
    for i, row in enumerate(matrix[:10]):
        cells = [str(c).strip().lower() for c in row if c is not None and str(c).strip()]
        if len(cells) >= 2 and len(target.intersection(cells)) >= 2:
            return i
    return None


def _is_blank_row(row: list[Any]) -> bool:
    return all(c is None or str(c).strip() == "" for c in row)


def _is_hint_row(row: dict[str, str | None]) -> bool:
    """Detect the template's per-column instruction row.

    The shipped template has a row under the header explaining each column
    ("Required — pick from dropdown", "Optional — auto-generated if blank", ...).
    """
    markers = 0
    filled = 0
    for value in row.values():
        if value is None:
            continue
        filled += 1
        low = value.lower()
        if any(m in low for m in _HINT_MARKERS):
            markers += 1
    return filled > 0 and markers >= 3 and markers >= filled * 0.5
