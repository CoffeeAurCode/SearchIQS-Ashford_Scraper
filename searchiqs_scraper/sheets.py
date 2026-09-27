from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from .sheet_tables import SheetTables, normalize_row

log = logging.getLogger(__name__)

TABS = ("Records", "Run Info", "Overflow")
WRITE_CHUNK_ROWS = 5000


class PublishError(Exception):
    pass


def open_spreadsheet(credentials: Path, sheet_id: str) -> Any:
    import gspread

    try:
        return gspread.service_account(filename=str(credentials)).open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        raise PublishError("spreadsheet not found: check GOOGLE_SHEET_ID and share the sheet with the "
                           "service account's email as Editor") from None
    except gspread.exceptions.APIError as exc:
        raise PublishError(f"Google Sheets API error: {exc}") from None


def publish(tables: SheetTables, spreadsheet: Any, *, chunk_rows: int = WRITE_CHUNK_ROWS) -> str:
    for title, table in zip(TABS, (tables.records, tables.run_info, tables.overflow), strict=True):
        if title == "Overflow" and len(table) <= 1:
            _remove_tab(spreadsheet, title)
            continue
        _write_tab(spreadsheet, title, table, chunk_rows)
    return spreadsheet.url


def _write_tab(spreadsheet: Any, title: str, table: Sequence[Sequence[str]], chunk_rows: int) -> None:
    import gspread

    width = max(len(r) for r in table)
    rows = [normalize_row(r, width) for r in table]
    try:
        sheet = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=title, rows=len(rows), cols=width)
    sheet.clear()
    sheet.resize(rows=len(rows), cols=width)
    for start in range(0, len(rows), chunk_rows):
        chunk = rows[start:start + chunk_rows]
        sheet.update(values=chunk, range_name=f"A{start + 1}", value_input_option="RAW")
    written = [normalize_row(r, width) for r in sheet.get_all_values()]
    written += [[""] * width] * (len(rows) - len(written))
    if written != rows:
        raise PublishError(f"tab {title!r}: read-back differs from what was written")
    log.info("tab %r: %d rows written and verified", title, len(rows))


def _remove_tab(spreadsheet: Any, title: str) -> None:
    import gspread

    try:
        spreadsheet.del_worksheet(spreadsheet.worksheet(title))
    except gspread.exceptions.WorksheetNotFound:
        pass


