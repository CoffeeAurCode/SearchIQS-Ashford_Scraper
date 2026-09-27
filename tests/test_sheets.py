import gspread
import pytest

from searchiqs_scraper.sheet_tables import to_sheet_tables
from searchiqs_scraper.sheets import PublishError, publish


class FakeWorksheet:
    def __init__(self, title, rows=1, cols=1):
        self.title = title
        self.cells = {}
        self.size = (rows, cols)
        self.updates = []
        self.corrupt = False

    def clear(self):
        self.cells = {}

    def resize(self, rows, cols):
        self.size = (rows, cols)

    def update(self, values, range_name, value_input_option):
        assert value_input_option == "RAW"
        assert range_name.startswith("A")
        first = int(range_name[1:])
        self.updates.append((range_name, len(values)))
        for i, row in enumerate(values):
            assert first + i <= self.size[0] and len(row) <= self.size[1]
            for j, value in enumerate(row):
                assert isinstance(value, str)
                self.cells[(first - 1 + i, j)] = value

    def get_all_values(self):
        if not self.cells:
            return []
        rows = max(r for r, _ in self.cells) + 1
        out = [["" for _ in range(self.size[1])] for _ in range(rows)]
        for (r, c), v in self.cells.items():
            out[r][c] = v
        while out and not any(out[-1]):
            out.pop()
        if self.corrupt and out:
            out[-1][0] = out[-1][0] + "?"
        return out


class FakeSpreadsheet:
    url = "https://docs.google.com/spreadsheets/d/fake"

    def __init__(self, *titles):
        self.tabs = {t: FakeWorksheet(t) for t in titles}

    def worksheet(self, title):
        try:
            return self.tabs[title]
        except KeyError:
            raise gspread.exceptions.WorksheetNotFound(title) from None

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title, rows, cols)
        return self.tabs[title]

    def del_worksheet(self, sheet):
        del self.tabs[sheet.title]

    def values(self, title):
        return self.tabs[title].get_all_values()


def tables(rows, run_info=(("Run ID", "r1"),)):
    return to_sheet_tables(("A", "B", "C"), rows, list(run_info))


def test_publish_writes_verified_tabs_and_keeps_unrelated_ones():
    sheet = FakeSpreadsheet("Sheet1", "Notes")
    rows = [["=1+1", "007", "x\ny"], ["", "ünï", ""]]
    url = publish(tables(rows), sheet)
    assert url == FakeSpreadsheet.url
    assert sheet.values("Records") == [["A", "B", "C"], ["=1+1", "007", "x\ny"], ["", "ünï", ""]]
    assert sheet.values("Run Info") == [["Field", "Value"], ["Run ID", "r1"]]
    assert set(sheet.tabs) == {"Sheet1", "Notes", "Records", "Run Info"}


def test_republish_shorter_dataset_leaves_no_old_rows():
    sheet = FakeSpreadsheet()
    publish(tables([["1", "2", "3"]] * 5), sheet)
    publish(tables([["9", "9", "9"]]), sheet)
    assert sheet.values("Records") == [["A", "B", "C"], ["9", "9", "9"]]


def test_headers_only_export():
    sheet = FakeSpreadsheet()
    publish(tables([]), sheet)
    assert sheet.values("Records") == [["A", "B", "C"]]


def test_large_writes_are_chunked():
    sheet = FakeSpreadsheet()
    publish(tables([[str(i), "", ""] for i in range(5)]), sheet, chunk_rows=2)
    assert sheet.tabs["Records"].updates == [("A1", 2), ("A3", 2), ("A5", 2)]


def test_overflow_tab_created_only_when_needed_and_removed_after():
    sheet = FakeSpreadsheet()
    publish(tables([["x" * 120_000, "", ""]]), sheet)
    assert "Overflow" in sheet.tabs and len(sheet.values("Overflow")) > 1
    publish(tables([["short", "", ""]]), sheet)
    assert "Overflow" not in sheet.tabs


def test_read_back_mismatch_fails():
    sheet = FakeSpreadsheet("Records")
    sheet.tabs["Records"].corrupt = True
    with pytest.raises(PublishError, match="read-back differs"):
        publish(tables([["1", "2", "3"]]), sheet)
