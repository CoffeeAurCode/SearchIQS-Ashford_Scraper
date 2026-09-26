import pytest

from searchiqs_scraper.sheet_tables import (
    CELL_LIMIT,
    CHUNK_LIMIT,
    CONTINUATION_MARKER,
    OVERFLOW_HEADER,
    SheetTableError,
    SheetTables,
    from_sheet_tables,
    to_sheet_tables,
    utf16_len,
)

HEADER = ("A", "B", "C")
INFO = [("Run ID", "r1")]


def round_trip(rows):
    tables = to_sheet_tables(HEADER, rows, INFO)
    header, rebuilt = from_sheet_tables(SheetTables.from_json(tables.to_json()))
    assert header == HEADER
    return tables, rebuilt


def test_small_values_exact():
    rows = [["=1+1", "007", "x\ny"], ["+1", "-2", "@home"], ["ümlaut ✓", " lead", "trail "]]
    tables, rebuilt = round_trip(rows)
    assert tables.fidelity == "exact"
    assert tables.overflow == (OVERFLOW_HEADER,)
    assert rebuilt == rows
    assert tables.records[1] == ("=1+1", "007", "x\ny")


def test_zero_rows_still_has_header_and_run_info():
    tables, rebuilt = round_trip([])
    assert tables.records == (HEADER,)
    assert tables.run_info[0] == ("Field", "Value") and tables.run_info[1] == ("Run ID", "r1")
    assert rebuilt == []


def test_long_cell_uses_overflow_losslessly():
    long = "".join(chr(ord("a") + i % 26) for i in range(120_000))
    rows = [["short", long, ""], ["dup", "dup", "dup"], ["dup", "dup", "dup"]]
    tables, rebuilt = round_trip(rows)
    assert tables.fidelity == "overflow_used"
    assert rebuilt == rows
    head = tables.records[1][1]
    assert head.endswith(CONTINUATION_MARKER) and len(head) <= CELL_LIMIT
    assert [(k, c, p) for k, c, p, _ in tables.overflow[1:]] == [("1", "B", "1"), ("1", "B", "2")]
    assert all(utf16_len(t) <= CHUNK_LIMIT for *_, t in tables.overflow[1:])


def test_astral_characters_never_split():
    emoji = "😀" * 30_000
    tables, rebuilt = round_trip([[emoji, "", ""]])
    assert rebuilt[0][0] == emoji
    cells = [tables.records[1][0][: -len(CONTINUATION_MARKER)]] + [t for *_, t in tables.overflow[1:]]
    for cell in cells:
        assert utf16_len(cell) <= CHUNK_LIMIT
        cell.encode("utf-8")


def test_marker_like_user_text_is_not_stripped():
    rows = [["ends with" + CONTINUATION_MARKER, "", ""]]
    assert round_trip(rows)[1] == rows


def test_omitted_trailing_cells_are_normalized():
    tables = SheetTables(records=(HEADER, ("x",), ()), run_info=(("Field", "Value"),), overflow=(OVERFLOW_HEADER,))
    assert from_sheet_tables(tables)[1] == [["x", "", ""], ["", "", ""]]


def test_values_beyond_header_rejected():
    tables = SheetTables(records=(HEADER, ("x", "y", "z", "extra")), run_info=(), overflow=(OVERFLOW_HEADER,))
    with pytest.raises(SheetTableError):
        from_sheet_tables(tables)


def test_missing_overflow_part_detected():
    tables = to_sheet_tables(HEADER, [["y" * 150_000, "", ""]], INFO)
    broken = SheetTables(tables.records, tables.run_info, tables.overflow[:1] + tables.overflow[2:])
    with pytest.raises(SheetTableError, match="contiguous"):
        from_sheet_tables(broken)


def test_overflow_pointing_outside_records_detected():
    tables = to_sheet_tables(HEADER, [["a", "b", "c"]], INFO)
    broken = SheetTables(tables.records, tables.run_info, tables.overflow + (("9", "A", "1", "x"),))
    with pytest.raises(SheetTableError):
        from_sheet_tables(broken)


def test_row_width_and_type_validated():
    with pytest.raises(SheetTableError):
        to_sheet_tables(HEADER, [["a", "b"]], INFO)
    with pytest.raises(SheetTableError):
        to_sheet_tables(HEADER, [["a", "b", 3]], INFO)
    with pytest.raises(SheetTableError):
        to_sheet_tables(HEADER, [], [("Issues", "z" * 60_000)])


def test_content_hash_is_deterministic_and_content_sensitive():
    a = to_sheet_tables(HEADER, [["a", "b", "c"]], INFO)
    b = to_sheet_tables(HEADER, [["a", "b", "c"]], INFO)
    c = to_sheet_tables(HEADER, [["a", "b", "d"]], INFO)
    assert a.content_sha256() == b.content_sha256() != c.content_sha256()
