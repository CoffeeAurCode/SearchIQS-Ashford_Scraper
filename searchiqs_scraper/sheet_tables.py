from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

from .models import canonical_json

CELL_LIMIT = 50_000
CHUNK_LIMIT = 49_000
CONTINUATION_MARKER = "…[continues in Overflow]"
OVERFLOW_HEADER = ("Row Key", "Column", "Part", "Text")
RUN_INFO_HEADER = ("Field", "Value")

Table = tuple[tuple[str, ...], ...]


class SheetTableError(ValueError):
    pass


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_by_utf16(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    size = 0
    for i, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if size + width > limit:
            chunks.append(text[start:i])
            start, size = i, 0
        size += width
    chunks.append(text[start:])
    return chunks


@dataclass(frozen=True)
class SheetTables:
    records: Table
    run_info: Table
    overflow: Table

    @property
    def fidelity(self) -> str:
        return "overflow_used" if len(self.overflow) > 1 else "exact"

    def to_json(self) -> dict[str, Any]:
        return {
            "records": [list(r) for r in self.records],
            "run_info": [list(r) for r in self.run_info],
            "overflow": [list(r) for r in self.overflow],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SheetTables:
        return cls(*(_as_table(data[k]) for k in ("records", "run_info", "overflow")))

    def content_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode("utf-8")).hexdigest()


def to_sheet_tables(
    header: Sequence[str], rows: Sequence[Sequence[str]], run_info: Sequence[tuple[str, str]]
) -> SheetTables:
    header = tuple(header)
    _check_short(header, "header")
    records: list[tuple[str, ...]] = [header]
    overflow: list[tuple[str, ...]] = [OVERFLOW_HEADER]
    for row_number, row in enumerate(rows, start=1):
        row = tuple(row)
        if len(row) != len(header) or not all(isinstance(c, str) for c in row):
            raise SheetTableError(f"row {row_number} must have {len(header)} string cells")
        out_row = []
        for column, cell in zip(header, row, strict=True):
            if utf16_len(cell) <= CHUNK_LIMIT:
                out_row.append(cell)
                continue
            head, *rest = split_by_utf16(cell, CHUNK_LIMIT)
            out_row.append(head + CONTINUATION_MARKER)
            overflow.extend((str(row_number), column, str(part), text) for part, text in enumerate(rest, start=1))
        records.append(tuple(out_row))
    info = [RUN_INFO_HEADER] + [(str(k), str(v)) for k, v in run_info]
    for pair in info:
        _check_short(pair, "Run Info")
    return SheetTables(tuple(records), tuple(info), tuple(overflow))


def from_sheet_tables(tables: SheetTables) -> tuple[tuple[str, ...], list[list[str]]]:
    if not tables.records:
        raise SheetTableError("Records table has no header")
    header = tuple(tables.records[0])
    rows = [normalize_row(r, len(header)) for r in tables.records[1:]]
    if not tables.overflow or tuple(tables.overflow[0]) != OVERFLOW_HEADER:
        raise SheetTableError("Overflow table header mismatch")

    parts: dict[tuple[int, str], list[tuple[int, str]]] = {}
    for entry in tables.overflow[1:]:
        key, column, part, text = normalize_row(entry, 4)
        try:
            row_index, part_no = int(key), int(part)
        except ValueError:
            raise SheetTableError(f"bad overflow key/part: {key!r}/{part!r}") from None
        if not 1 <= row_index <= len(rows) or column not in header:
            raise SheetTableError(f"overflow entry points outside Records: {key}/{column}")
        parts.setdefault((row_index, column), []).append((part_no, text))

    for (row_index, column), chunks in parts.items():
        chunks.sort()
        if [n for n, _ in chunks] != list(range(1, len(chunks) + 1)):
            raise SheetTableError(f"overflow parts for {row_index}/{column} are not contiguous")
        col = header.index(column)
        head = rows[row_index - 1][col]
        if not head.endswith(CONTINUATION_MARKER):
            raise SheetTableError(f"Records cell {row_index}/{column} lacks the continuation marker")
        rows[row_index - 1][col] = head[: -len(CONTINUATION_MARKER)] + "".join(t for _, t in chunks)
    return header, rows


def normalize_row(row: Sequence[str], width: int) -> list[str]:
    row = list(row)
    if len(row) > width:
        if any(row[width:]):
            raise SheetTableError(f"row has values beyond column {width}")
        row = row[:width]
    return row + [""] * (width - len(row))


def _check_short(cells: Sequence[str], where: str) -> None:
    for cell in cells:
        if not isinstance(cell, str) or utf16_len(cell) > CHUNK_LIMIT:
            raise SheetTableError(f"{where} cell is not a string within {CHUNK_LIMIT} characters")


def _as_table(rows: Any) -> Table:
    table = tuple(tuple(r) for r in rows)
    if not all(isinstance(c, str) for r in table for c in r):
        raise SheetTableError("table cells must be strings")
    return table
