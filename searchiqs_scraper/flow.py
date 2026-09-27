from __future__ import annotations

import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .config import BASE_URL
from .dates import DateRange, format_site_date
from .http import HttpClient, Response
from .pages import DOC_GROUP_NAME, SEARCH_BUTTON_NAME, PageKind, classify
from .parser import Criterion, GridRow, ParseError, parse_criteria, parse_results, parse_total
from .webforms import FormState, extract_form_state

log = logging.getLogger(__name__)

LAND_RECORDS = "LR"
FROM_DATE_NAME = "ctl00$ContentPlaceHolder1$txtFromDate"
THRU_DATE_NAME = "ctl00$ContentPlaceHolder1$txtThruDate"
GUEST_TARGET = "btnGuestLogin"
BROWSER_SIZE = {"BrowserWidth": "1920", "BrowserHeight": "1080"}


class FlowError(Exception):
    code = "flow-error"


class ChallengeError(FlowError):
    code = "cloudflare-challenge"

    def __init__(self, step: str) -> None:
        super().__init__(f"Cloudflare challenge at {step}: clearance missing or expired, "
                         "refresh SEARCHIQS_CF_CLEARANCE from your browser")


class UnexpectedPage(FlowError):
    code = "unexpected-page"


class CriteriaMismatch(FlowError):
    code = "criteria-mismatch"


class PagingError(FlowError):
    code = "paging-error"


class PageLimitReached(PagingError):
    code = "page-limit"


@dataclass(frozen=True)
class PassResult:
    window: DateRange
    total: int
    rows: tuple[GridRow, ...]
    pages: int

    @property
    def distinct_doc_ids(self) -> int:
        return len({r.record.doc_id for r in self.rows if r.record.doc_id})

    @property
    def reconciled(self) -> bool:
        return (len(self.rows) == self.total and self.distinct_doc_ids == self.total
                and all(r.record.doc_id for r in self.rows))


def expected_criteria(window: DateRange) -> tuple[Criterion, ...]:
    return (
        Criterion("Document Group", "Equals", LAND_RECORDS),
        Criterion("Recording Date", "Greater Than or Equal To", format_site_date(window.start)),
        Criterion("Recording Date", "Less Than or Equal To", format_site_date(window.end)),
    )


class SiteFlow:
    def __init__(self, client: HttpClient, *, base_url: str = BASE_URL, max_pages: int = 1000) -> None:
        self.client = client
        self.base_url = base_url
        self.max_pages = max_pages

    def run_pass(self, window: DateRange) -> PassResult:
        search = self.open_search()
        response = self._select_land_records(search)
        response, soup, kind = self._submit_dates(response, window)
        if kind is PageKind.NO_RESULTS:
            self._check_criteria(parse_criteria(soup), window, "no-results page")
            if parse_total(soup) != 0:
                raise UnexpectedPage("no-results page with a nonzero count")
            return PassResult(window, 0, (), 0)
        return self._collect(response, soup, window)

    def open_search(self) -> Response:
        entry = self.client.get(self.base_url)
        self._expect(entry, PageKind.ENTRY, "entry page")
        state = self._form(entry).postback(GUEST_TARGET)
        response = self.client.post(state.action, state.encode(), referer=entry.url)
        self._expect(response, PageKind.SEARCH_FORM, "guest search page")
        return response

    def _select_land_records(self, search: Response) -> Response:
        state = self._form(search).with_values({DOC_GROUP_NAME: LAND_RECORDS}).postback(DOC_GROUP_NAME)
        response = self.client.post(state.action, state.encode(), referer=search.url)
        soup = self._expect(response, PageKind.SEARCH_FORM, "document group postback")
        select = soup.find("select", attrs={"name": DOC_GROUP_NAME})
        selected = [o.get("value") for o in select.find_all("option") if o.has_attr("selected")]
        if selected != [LAND_RECORDS]:
            raise CriteriaMismatch(f"document group not retained after postback: {selected}")
        return response

    def _submit_dates(self, search: Response, window: DateRange) -> tuple[Response, BeautifulSoup, PageKind]:
        state = self._form(search).with_values({
            FROM_DATE_NAME: format_site_date(window.start),
            THRU_DATE_NAME: format_site_date(window.end),
        }).submit_with(SEARCH_BUTTON_NAME)
        response = self.client.post(state.action, state.encode(), referer=search.url)
        soup = BeautifulSoup(response.text, "lxml")
        kind = self._check_kind(response, soup, "search submit", (PageKind.RESULTS, PageKind.NO_RESULTS))
        return response, soup, kind

    def _collect(self, response: Response, soup: BeautifulSoup, window: DateRange) -> PassResult:
        bounds = (window.start, window.end)
        rows: list[GridRow] = []
        first = None
        previous_page: tuple[str, ...] | None = None
        for expected_page in range(1, self.max_pages + 1):
            try:
                page = parse_results(soup, bounds)
            except ParseError as exc:
                raise UnexpectedPage(f"results page {expected_page}: {exc}") from exc
            self._check_criteria(page.criteria, window, f"results page {expected_page}")
            if first is None:
                first = page
            elif (page.total, page.pages) != (first.total, first.pages):
                raise PagingError(f"total/page count changed on page {expected_page}")
            if page.page != expected_page:
                raise PagingError(f"expected page {expected_page}, got page {page.page}")
            fingerprints = tuple(r.fingerprint for r in page.rows)
            if fingerprints and fingerprints == previous_page:
                raise PagingError(f"page {expected_page} repeats the previous page")
            previous_page = fingerprints
            rows.extend(page.rows)
            log.info("window %s: page %d/%d, %d rows", window, page.page, page.pages, len(page.rows))
            if page.is_last:
                return PassResult(window, first.total, tuple(rows), first.pages)
            state = self._form(response, soup).postback(page.next_target)
            response = self.client.post(state.action, state.encode(), referer=response.url)
            soup = BeautifulSoup(response.text, "lxml")
            self._check_kind(response, soup, f"results page {expected_page + 1}", (PageKind.RESULTS,))
        raise PageLimitReached(f"stopped after max_pages={self.max_pages}")

    def _check_criteria(self, criteria: tuple[Criterion, ...], window: DateRange, where: str) -> None:
        if criteria != expected_criteria(window):
            raise CriteriaMismatch(f"{where}: site reports different search criteria")

    def _expect(self, response: Response, kind: PageKind, step: str) -> BeautifulSoup:
        soup = BeautifulSoup(response.text, "lxml")
        self._check_kind(response, soup, step, (kind,))
        return soup

    @staticmethod
    def _check_kind(response: Response, soup: BeautifulSoup, step: str, allowed: tuple[PageKind, ...]) -> PageKind:
        kind = classify(response.status, soup)
        if kind is PageKind.CLOUDFLARE_CHALLENGE:
            raise ChallengeError(step)
        if kind not in allowed:
            raise UnexpectedPage(f"{step}: expected {'/'.join(allowed)}, got {kind} (HTTP {response.status})")
        return kind

    @staticmethod
    def _form(response: Response, soup: BeautifulSoup | None = None) -> FormState:
        state = extract_form_state(soup or response.text, response.url, "form1")
        present = {k: v for k, v in BROWSER_SIZE.items() if state.get(k) is not None}
        return state.with_values(present)

