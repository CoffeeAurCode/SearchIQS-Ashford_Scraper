from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .dates import parse_site_date
from .models import FIELDS, PARSER_VERSION, FieldValue, Record, canonical_json
from .pages import GRID_ID, RESULT_COUNT_ID
from .webforms import parse_postback

RECORD_ID_HEADER = "RecordID"
PARTY_SEPARATOR = "; "
RELATED_SEPARATOR = "; "

_TOTAL = re.compile(r"^(\d+)\s+documents?\s+found$", re.I)
_TOTAL_SENTENCE = re.compile(r"A total of (\d+) documents? (?:was|were) found", re.I)
_PAGE = re.compile(r"You are viewing page (\d+) of (\d+)", re.I)
_TOOLTIP = re.compile(r"^\s*Grantors:(?P<grantors>.*?)Grantees:(?P<grantees>.*)$", re.S)
_WS = re.compile(r"[ \t\r\f\v ]+")


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Criterion:
    field: str
    comparison: str
    value: str


@dataclass(frozen=True)
class GridRow:
    record: Record
    related_refs: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class ResultsPage:
    total: int
    page: int
    pages: int
    criteria: tuple[Criterion, ...]
    rows: tuple[GridRow, ...]
    next_target: str | None

    @property
    def is_last(self) -> bool:
        return self.page == self.pages


def parse_results(html: str | BeautifulSoup, window: tuple[date, date] | None = None) -> ResultsPage:
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    total = _parse_total(soup)
    page, pages = _parse_page_position(soup)
    next_target = _next_target(soup)
    if (next_target is None) != (page == pages):
        state = "present" if next_target else "absent"
        raise ParseError(f"pager inconsistent: page {page} of {pages}, next link {state}")
    grid = soup.find("table", id=GRID_ID)
    if grid is None:
        raise ParseError("results grid not found")
    rows = tuple(_parse_grid(grid, window))
    return ResultsPage(total, page, pages, parse_criteria(soup), rows, next_target)


def parse_total(soup: BeautifulSoup) -> int:
    return _parse_total(soup)


def parse_criteria(soup: BeautifulSoup) -> tuple[Criterion, ...]:
    holder = soup.find(id="ContentPlaceHolder1_lblSelectionCriteria")
    if holder is None:
        raise ParseError("selection criteria not found")
    out = []
    for tr in holder.find_all("tr"):
        cells = [_clean_line(td.get_text(" ")) for td in tr.find_all("td")]
        if len(cells) != 3:
            raise ParseError(f"selection criteria row has {len(cells)} cells")
        if cells == ["Field", "Comparison", "Value"]:
            continue
        out.append(Criterion(*cells))
    if not out:
        raise ParseError("selection criteria are empty")
    return tuple(out)


def _parse_total(soup: BeautifulSoup) -> int:
    node = soup.find(id=RESULT_COUNT_ID)
    if node is None:
        raise ParseError("result count not found")
    match = _TOTAL.match(_clean_line(node.get_text(" ")))
    if not match:
        raise ParseError("result count has an unexpected format")
    total = int(match.group(1))
    sentence = soup.find(id="ContentPlaceHolder1_lblSearchCount")
    if sentence is not None:
        other = _TOTAL_SENTENCE.search(_clean_line(sentence.get_text(" ")))
        if other and int(other.group(1)) != total:
            raise ParseError("the two result counts on the page disagree")
    return total


def _parse_page_position(soup: BeautifulSoup) -> tuple[int, int]:
    positions = set()
    for node in soup.find_all(id=re.compile(r"^ContentPlaceHolder1_lblShowingPage\d$")):
        match = _PAGE.search(node.get_text(" "))
        if match:
            positions.add((int(match.group(1)), int(match.group(2))))
    if len(positions) != 1:
        raise ParseError(f"expected one page position, found {sorted(positions)}")
    page, pages = positions.pop()
    if not 1 <= page <= pages:
        raise ParseError(f"invalid page position {page} of {pages}")
    return page, pages


def _next_target(soup: BeautifulSoup) -> str | None:
    link = soup.find("a", id="ContentPlaceHolder1_lbNext1")
    if link is None:
        raise ParseError("Next link not found")
    postback = parse_postback(link.get("href") or "")
    if "aspNetDisabled" in (link.get("class") or []) or postback is None:
        return None
    return postback[0]


def _parse_grid(grid: Tag, window: tuple[date, date] | None) -> list[GridRow]:
    trs = grid.find_all("tr", recursive=False) or (grid.tbody.find_all("tr", recursive=False) if grid.tbody else [])
    if not trs:
        raise ParseError("results grid has no header row")
    headers = [_clean_line(td.get_text(" ")) for td in trs[0].find_all(["td", "th"], recursive=False)]
    index = {}
    for name in (RECORD_ID_HEADER,) + FIELDS:
        positions = [i for i, h in enumerate(headers) if h == name]
        if len(positions) != 1:
            raise ParseError(f"grid header {name!r} found {len(positions)} times")
        index[name] = positions[0]
    rows = []
    for number, tr in enumerate(trs[1:], start=1):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) != len(headers):
            raise ParseError(f"grid row {number} has {len(cells)} cells, header has {len(headers)}")
        rows.append(_parse_row(cells, index, window))
    return rows


def _parse_row(cells: list[Tag], index: dict[str, int], window: tuple[date, date] | None) -> GridRow:
    issues: list[str] = []
    doc_id = _clean_line(cells[index[RECORD_ID_HEADER]].get_text(" ")) or None
    if doc_id is None:
        issues.append("missing RecordID")
    tooltip = _party_tooltip(cells[index["Party 1"]]) or _party_tooltip(cells[index["Party 2"]])
    fields = {
        "Party 1": _party_field(cells[index["Party 1"]], tooltip[0] if tooltip else None),
        "Party 2": _party_field(cells[index["Party 2"]], tooltip[1] if tooltip else None),
        "Type": FieldValue.of(_cell_text(cells[index["Type"]])),
        "Book-Page": FieldValue.of(_cell_text(cells[index["Book-Page"]])),
        "Date": FieldValue.of(_cell_text(cells[index["Date"]])),
        "Description": FieldValue.of(_cell_text(cells[index["Description"]])),
        "Additional Description": FieldValue.of(_cell_text(cells[index["Additional Description"]])),
    }
    related, refs = _related(cells[index["Related"]])
    fields["Related"] = related
    date_iso = None
    if fields["Date"].text:
        try:
            parsed = parse_site_date(fields["Date"].text)
        except ValueError:
            issues.append("unparseable Date")
        else:
            date_iso = parsed.isoformat()
            if window is not None and not window[0] <= parsed <= window[1]:
                issues.append("Date outside the searched range")
    else:
        issues.append("missing Date")
    record = Record(fields={name: fields[name] for name in FIELDS}, doc_id=doc_id, date_iso=date_iso,
                    issues=tuple(issues))
    return GridRow(record, refs, fingerprint(record, refs))


def fingerprint(record: Record, related_refs: tuple[str, ...]) -> str:
    payload = [PARSER_VERSION, record.doc_id, [record.fields[n].to_json() for n in FIELDS], list(related_refs)]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _party_field(cell: Tag, tooltip_names: list[str] | None) -> FieldValue:
    names = _lines(cell)
    if tooltip_names is None or names == tooltip_names:
        return FieldValue.of(PARTY_SEPARATOR.join(names))
    if len(tooltip_names) > len(names) and tooltip_names[:len(names)] == names:
        return FieldValue.of(PARTY_SEPARATOR.join(tooltip_names))
    return FieldValue.failed("party list differs from its tooltip")


def _party_tooltip(cell: Tag) -> tuple[list[str], list[str]] | None:
    holder = cell.find("span", title=True) or (cell if cell.get("title") else None)
    if holder is None:
        return None
    match = _TOOLTIP.match(holder["title"])
    if not match:
        return None
    split = lambda block: [_clean_line(x) for x in block.split("\n") if _clean_line(x)]  # noqa: E731
    return split(match.group("grantors")), split(match.group("grantees"))


def _related(cell: Tag) -> tuple[FieldValue, tuple[str, ...]]:
    links = cell.find_all("a")
    if not links:
        return FieldValue.of(_cell_text(cell)), ()
    labels, refs = [], []
    for link in links:
        label = _clean_line(link.get_text(" "))
        if label:
            labels.append(label)
        postback = parse_postback(link.get("onclick") or link.get("href") or "")
        if postback and postback[0] == "ShowRelatedDocs" and postback[1]:
            refs.append(postback[1])
    return FieldValue.of(RELATED_SEPARATOR.join(labels)), tuple(refs)


def _cell_text(cell: Tag) -> str:
    return "\n".join(_lines(cell))


def _lines(cell: Tag) -> list[str]:
    parts: list[str] = [""]
    for node in cell.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            parts[-1] += str(node)
        elif node.name == "br":
            parts.append("")
    return [line for line in (_clean_line(p) for p in parts) if line]


def _clean_line(text: str) -> str:
    return _WS.sub(" ", text.replace("\n", " ")).strip()
