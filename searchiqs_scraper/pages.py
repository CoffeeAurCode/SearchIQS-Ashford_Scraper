from __future__ import annotations

import re
from enum import StrEnum

from bs4 import BeautifulSoup

GRID_ID = "ContentPlaceHolder1_grdResults"
RESULT_COUNT_ID = "ContentPlaceHolder1_lblSearchResults"
DOC_GROUP_NAME = "ctl00$ContentPlaceHolder1$cboDocGroup"
SEARCH_BUTTON_NAME = "ctl00$ContentPlaceHolder1$cmdSearch"

_CHALLENGE_MARKERS = ("window._cf_chl_opt", "cf-error-details", "/cdn-cgi/challenge-platform/h/")
_CHALLENGE_TITLES = ("just a moment", "attention required", "access denied")
_ERROR_MARKERS = ("Server Error in '/", "Runtime Error", "An unhandled exception occurred")
_ZERO_RESULTS = re.compile(r"^\s*0\s+documents?\s+found\s*$", re.I)


class PageKind(StrEnum):
    ENTRY = "entry"
    SEARCH_FORM = "search_form"
    RESULTS = "results"
    NO_RESULTS = "no_results"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    SITE_ERROR = "site_error"
    UNKNOWN = "unknown"


def classify(status: int, html: str | BeautifulSoup) -> PageKind:
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    raw = str(soup) if isinstance(html, BeautifulSoup) else html
    if any(m in raw for m in _CHALLENGE_MARKERS) or (status in (403, 429, 503) and title.startswith(_CHALLENGE_TITLES)):
        return PageKind.CLOUDFLARE_CHALLENGE
    if status >= 500 or any(m in raw for m in _ERROR_MARKERS):
        return PageKind.SITE_ERROR
    if status != 200:
        return PageKind.UNKNOWN
    if soup.find(id="btnGuestLogin") is not None:
        return PageKind.ENTRY
    count = soup.find(id=RESULT_COUNT_ID)
    if soup.find("table", id=GRID_ID) is not None and count is not None:
        return PageKind.RESULTS
    if count is not None and _ZERO_RESULTS.match(count.get_text(" ", strip=True)):
        return PageKind.NO_RESULTS
    if soup.find("select", attrs={"name": DOC_GROUP_NAME}) and soup.find("input", attrs={"name": SEARCH_BUTTON_NAME}):
        return PageKind.SEARCH_FORM
    return PageKind.UNKNOWN
