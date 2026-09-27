"""Synthetic stand-in for the SearchIQS guest flow, modelled on the observed page structure only."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from searchiqs_scraper.http import Response, TransportError

BASE = "https://www.searchiqs.com/CTASH/"
SEARCH = BASE + "SearchAdvancedMP.aspx"
RESULTS = BASE + "SearchResultsMP.aspx"
P = "ctl00$ContentPlaceHolder1$"
SEL = ' selected="selected"'


@dataclass
class Doc:
    record_id: str
    party1: list[str]
    party2: list[str]
    doc_type: str
    book_page: str
    recorded: date
    description: str = ""
    additional: str = ""
    related: list[tuple[str, str]] = field(default_factory=list)
    party1_shown: list[str] | None = None


def make_docs(n: int, start: date = date(2026, 7, 8)) -> list[Doc]:
    docs = []
    for i in range(n):
        day = date.fromordinal(start.toordinal() + (i * 3) % 81)
        docs.append(Doc(
            record_id=f"L|{900000 + i}",
            party1=[f"GRANTOR{i} ALPHA", f"GRANTOR{i} BETA"] if i % 3 == 0 else [f"GRANTOR{i} ALPHA"],
            party2=[f"GRANTEE{i} LLC"],
            doc_type=["DEED", "MORTGAGE", "RELEASE"][i % 3],
            book_page=f"0{i // 10}-{i:03d}",
            recorded=day,
            description=f"{i} EXAMPLE ROAD" if i % 2 else "",
            additional="LINE ONE" if i % 7 == 0 else "",
            related=[(f"BK: 1 PG:{i}", str(800000 + i))] if i % 4 == 0 else [],
        ))
    return sorted(docs, key=lambda d: (d.recorded, d.record_id))


def page_html(title: str, action: str, viewstate: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>{title}</title>
<script src="https://static.cloudflareinsights.com/beacon.min.js"></script></head><body>
<form method="post" action="{action}" id="form1">
<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />
<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{viewstate}" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="ABCDEF12" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="ev-{viewstate}" />
<input id="hiddenWidth" type="hidden" name="BrowserWidth" />
<input id="hiddenHeight" type="hidden" name="BrowserHeight" />
{body}
</form></body></html>"""


def entry_html(vs: str) -> str:
    return page_html("Example Town - SearchIQS", "./", vs, """
<input type="text" name="username" id="username" /><input type="password" name="password" id="password" />
<input type="submit" name="cmdLogin" value="Log In" id="cmdLogin" />
<button onclick="__doPostBack('btnGuestLogin','')" id="btnGuestLogin" type="button">Search Records as Guest</button>""")


def search_html(vs: str, group: str = "(ALL)") -> str:
    opts = "".join(f'<option{SEL if v == group else ""} value="{v}">{v}</option>'
                   for v in ("(ALL)", "FR", "LR", "MAP", "TN"))
    return page_html("Search", "./SearchAdvancedMP.aspx", vs, f"""
<input name="{P}txtName" type="text" id="ContentPlaceHolder1_txtName" />
<input name="{P}txtFromDate" type="text" id="ContentPlaceHolder1_txtFromDate" />
<input name="{P}txtThruDate" type="text" id="ContentPlaceHolder1_txtThruDate" />
<select name="{P}cboDocGroup" onchange="javascript:setTimeout('__doPostBack(\\'{P}cboDocGroup\\',\\'\\')', 0)"
 id="ContentPlaceHolder1_cboDocGroup">{opts}</select>
<select name="{P}cboDocType" id="ContentPlaceHolder1_cboDocType">
<option selected="selected" value="(ALL)">(ALL)</option>
<option value="DEED">DEED</option></select>
<input type="submit" name="{P}cmdSearch" value="Search" id="ContentPlaceHolder1_cmdSearch" />
<input type="submit" name="{P}cmdReset" value="Clear" id="ContentPlaceHolder1_cmdReset" />""")


def criteria_html(criteria: list[tuple[str, str, str]]) -> str:
    rows = "".join(f"<tr><td>{f}</td><td>{c}</td><td>{v}</td></tr>" for f, c, v in criteria)
    return ('<span id="ContentPlaceHolder1_lblSelectionCriteria"><table Border=1><tr bgcolor=LightSkyBlue>'
            f"<td>Field</td><td>Comparison</td><td>Value</td></tr>{rows}</table></span>")


def party_cell(side: int, n: int, names: list[str], tooltip: tuple[list[str], list[str]]) -> str:
    title = "Grantors:\n\n" + "".join(f" {x}\n\n" for x in tooltip[0]) + "\n\nGrantees:\n\n" + \
        "".join(f" {x}\n\n" for x in tooltip[1])
    shown = "<br/>".join(html.escape(x) for x in names)
    span_id = f"ContentPlaceHolder1_grdResults_lblParty{side}_{n}"
    return f'<td><span id="{span_id}" title="{html.escape(title)}">{shown}</span></td>'


def row_html(n: int, doc: Doc) -> str:
    related = "&nbsp;"
    title = ""
    if doc.related:
        related = "".join(f"<a href=\"javascript:void(0);\" "
                          f"onclick='javascript:__doPostBack(\"ShowRelatedDocs\",\"{ref}\")'>"
                          f"{html.escape(label)}</a>" for label, ref in doc.related)
        title = f' title="{html.escape(doc.related[0][0])}  DATE: 01/01/2020"'
    ctl = f"ctl{n + 3:02d}"
    tip = (doc.party1, doc.party2)
    return (f'<tr><td><input name="{P}grdResults${ctl}$btnView" type="submit" value="View*" /></td>'
            f'<td><input name="{P}grdResults${ctl}$btnMyDoc" type="submit" value="My Doc" '
            'onclick="return false;" /></td>'
            f'<td><span><input name="{P}grdResults${ctl}$CheckBox1" type="checkbox" /></span></td>'
            f'<td class="HiddenColumn"><a href="javascript:__doPostBack(\'{P}grdResults${ctl}$LinkButton1\',\'\')">'
            f"{html.escape(doc.record_id)}</a></td>"
            + party_cell(1, n, doc.party1_shown or doc.party1, tip) + party_cell(2, n, doc.party2, tip)
            + f'<td valign="top">{html.escape(doc.doc_type)}</td><td valign="top">{html.escape(doc.book_page)}</td>'
            f'<td valign="top">{doc.recorded:%m/%d/%Y}</td>'
            f"<td>{html.escape(doc.description) or '&nbsp;'}</td><td>{html.escape(doc.additional) or '&nbsp;'}</td>"
            f"<td{title}>{related}</td></tr>")


HEADER = ('<tr><td>&nbsp;</td><td>&nbsp;</td><td>Select</td><td class="HiddenColumn">RecordID</td><td>Party 1</td>'
          "<td>Party 2</td><td><a href=\"javascript:__doPostBack('x','')\">Type</a></td>"
          "<td><a href=\"javascript:__doPostBack('y','')\">Book-Page</a></td>"
          "<td><a href=\"javascript:__doPostBack('z','')\"><img src=\"images\\SortAsc2.png\"/>Date</a></td>"
          "<td>Description</td><td>Additional Description</td><td>Related</td></tr>")


def pager_html(k: int, page: int, pages: int) -> str:
    def link(name: str, enabled: bool) -> str:
        if enabled:
            href = f"javascript:__doPostBack('{P}lb{name}{k}','')"
            return f'<a id="ContentPlaceHolder1_lb{name}{k}" href="{href}">{name}</a>'
        return f'<a class="aspNetDisabled" id="ContentPlaceHolder1_lb{name}{k}">{name}</a>'
    opts = "".join(f'<option{SEL if i == page else ""} value="{i}">{i}</option>'
                   for i in range(1, pages + 1))
    return (link("Previous", page > 1) + link("Next", page < pages)
            + link("First", page > 1) + link("Last", page < pages)
            + f'<select name="{P}ddlGoToPage{k}" id="ContentPlaceHolder1_ddlGoToPage{k}">{opts}</select>'
            f'<span id="ContentPlaceHolder1_lblShowingPage{k}">You are viewing page {page} of {pages}</span>')


def results_html(vs: str, docs: list[Doc], total: int, page: int, pages: int,
                 criteria: list[tuple[str, str, str]]) -> str:
    grid = HEADER + "".join(row_html(i, d) for i, d in enumerate(docs))
    return page_html("Search Results", "./SearchResultsMP.aspx", vs, f"""
<span id="ContentPlaceHolder1_lblSearchCount">A total of {total} documents were found</span>
<select name="{P}ddlSource" id="ContentPlaceHolder1_ddlSource">
<option selected="selected" value="0">Search Results</option></select>
<table id="tblResults"><tr><td>{pager_html(1, page, pages)}</td></tr>
<tr><td><table id="ContentPlaceHolder1_grdResults">{grid}</table></td></tr>
<tr><td>{pager_html(2, page, pages)}</td></tr></table>
<span id="ContentPlaceHolder1_lblSearchTime">09/26/2026 06:06:36 PM</span>
<span id="ContentPlaceHolder1_lblSearchResults">{total} documents found</span>
{criteria_html(criteria)}""")


def no_results_html(vs: str, criteria: list[tuple[str, str, str]]) -> str:
    return page_html("Search Results", "./SearchResultsMP.aspx", vs, f"""
<span id="ContentPlaceHolder1_lblSearchCount">A total of 0 documents were found</span>
<span id="ContentPlaceHolder1_lblSearchResults">0 documents found</span>
{criteria_html(criteria)}""")


CHALLENGE_HTML = """<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>
<script>(function(){window._cf_chl_opt = {cType: 'managed'};})();</script>
Enable JavaScript and cookies to continue</body></html>"""

ERROR_HTML = ("<html><head><title>Runtime Error</title></head>"
              "<body><h1>Server Error in '/CTASH' Application.</h1></body></html>")


class FakeSite:
    """One guest session. Every POST must carry the latest view state, like the real WebForms site."""

    def __init__(self, docs: list[Doc], *, page_size: int = 100, challenge_on: set[int] | None = None,
                 fail_on: Mapping[int, Exception] | None = None, ignore_group: bool = False,
                 extra_criteria: list[tuple[str, str, str]] | None = None, repeat_page: bool = False,
                 page_step: int = 1, stale_rows: bool = False,
                 statuses: Mapping[int, tuple[int, dict[str, str]]] | None = None) -> None:
        self.docs = docs
        self.page_size = page_size
        self.challenge_on = challenge_on or set()
        self.fail_on = dict(fail_on or {})
        self.ignore_group = ignore_group
        self.extra_criteria = extra_criteria or []
        self.repeat_page = repeat_page
        self.page_step = page_step
        self.stale_rows = stale_rows
        self.statuses = dict(statuses or {})
        self.log: list[tuple[str, str, dict[str, list[str]]]] = []
        self.version = 0
        self.guest = False
        self.group = "(ALL)"
        self.found: list[Doc] = []
        self.criteria: list[tuple[str, str, str]] = []
        self.page = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def request(self, method: str, url: str, *, data: str | None, headers: Mapping[str, str],
                timeout: tuple[float, float]) -> Response:
        form: dict[str, list[str]] = {}
        for k, v in parse_qsl(data or "", keep_blank_values=True):
            form.setdefault(k, []).append(v)
        self.log.append((method, url, form))
        n = len(self.log)
        if n in self.fail_on:
            raise self.fail_on[n]
        if n in self.statuses:
            status, hdrs = self.statuses[n]
            return Response(status, url, "<html><head><title>busy</title></head></html>", hdrs)
        if n in self.challenge_on:
            return Response(403, url, CHALLENGE_HTML)
        path = urlsplit(url).path
        if method == "GET":
            return self._get(path, url)
        if form.get("__VIEWSTATE") != [self._vs()]:
            return Response(500, url, ERROR_HTML)
        return self._post(path, url, form)

    def _vs(self) -> str:
        return f"vs{self.version}"

    def _next_vs(self) -> str:
        self.version += 1
        return self._vs()

    def _get(self, path: str, url: str) -> Response:
        if path == "/CTASH/":
            return Response(200, url, entry_html(self._next_vs()))
        if path == "/CTASH/SearchAdvancedMP.aspx" and self.guest:
            return Response(200, url, search_html(self._next_vs(), self.group))
        if path == "/CTASH/SearchResultsMP.aspx" and self.criteria:
            return self._results(url)
        return Response(404, url, "<html><title>Not found</title></html>")

    def _post(self, path: str, url: str, form: dict[str, list[str]]) -> Response:
        target = form.get("__EVENTTARGET", [""])[0]
        if path == "/CTASH/" and target == "btnGuestLogin" and "cmdLogin" not in form:
            self.guest = True
            return Response(302, url, "", {"location": "/CTASH/SearchAdvancedMP.aspx"})
        if path == "/CTASH/SearchAdvancedMP.aspx" and self.guest:
            if target == P + "cboDocGroup":
                if not self.ignore_group:
                    self.group = form[P + "cboDocGroup"][0]
                return Response(200, url, search_html(self._next_vs(), self.group))
            if form.get(P + "cmdSearch") == ["Search"] and P + "cmdReset" not in form:
                start = datetime.strptime(form[P + "txtFromDate"][0], "%m/%d/%Y").date()
                end = datetime.strptime(form[P + "txtThruDate"][0], "%m/%d/%Y").date()
                group = form[P + "cboDocGroup"][0]
                self.found = [d for d in self.docs if start <= d.recorded <= end]
                self.criteria = ([("Document Group", "Equals", group)] if group != "(ALL)" else []) + [
                    ("Recording Date", "Greater Than or Equal To", f"{start:%m/%d/%Y}"),
                    ("Recording Date", "Less Than or Equal To", f"{end:%m/%d/%Y}")] + self.extra_criteria
                self.page = 1
                return Response(302, url, "", {"location": "/CTASH/SearchResultsMP.aspx"})
        if path == "/CTASH/SearchResultsMP.aspx" and self.criteria and target == P + "lbNext1":
            if not self.repeat_page:
                self.page += self.page_step
            return self._results(url)
        return Response(500, url, ERROR_HTML)

    def _results(self, url: str) -> Response:
        if not self.found:
            return Response(200, url, no_results_html(self._next_vs(), self.criteria))
        pages = (len(self.found) + self.page_size - 1) // self.page_size
        shown = 1 if self.stale_rows else self.page
        chunk = self.found[(shown - 1) * self.page_size:shown * self.page_size]
        return Response(200, url, results_html(self._next_vs(), chunk, len(self.found), self.page, pages,
                                               self.criteria))


class Flaky(TransportError):
    pass
