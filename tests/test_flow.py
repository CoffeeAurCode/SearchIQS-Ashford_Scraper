from datetime import date

import pytest

from searchiqs_scraper.config import Config
from searchiqs_scraper.dates import DateRange
from searchiqs_scraper.flow import ChallengeError, CriteriaMismatch, PagingError, SiteFlow
from searchiqs_scraper.http import AmbiguousPost, HttpClient, TransportError

from fakesite import FakeSite, make_docs

WINDOW = DateRange(date(2026, 7, 8), date(2026, 9, 26))


def run(site, window=WINDOW, max_pages=1000):
    client = HttpClient(site, Config(), sleep=lambda s: None, rng=lambda: 0.0)
    return SiteFlow(client, max_pages=max_pages).run_pass(window)


def test_multi_page_pass_collects_everything():
    docs = make_docs(159)
    site = FakeSite(docs)
    result = run(site)
    assert (result.total, result.pages, len(result.rows)) == (159, 2, 159)
    assert result.reconciled
    assert [r.record.doc_id for r in result.rows] == [d.record_id for d in docs]


def test_request_sequence_matches_observed_flow():
    site = FakeSite(make_docs(5), page_size=2)
    run(site)
    steps = [(m, u.rsplit("/", 1)[-1], f.get("__EVENTTARGET", [""])[0]) for m, u, f in site.log]
    assert steps == [
        ("GET", "", ""),
        ("POST", "", "btnGuestLogin"),
        ("GET", "SearchAdvancedMP.aspx", ""),
        ("POST", "SearchAdvancedMP.aspx", "ctl00$ContentPlaceHolder1$cboDocGroup"),
        ("POST", "SearchAdvancedMP.aspx", ""),
        ("GET", "SearchResultsMP.aspx", ""),
        ("POST", "SearchResultsMP.aspx", "ctl00$ContentPlaceHolder1$lbNext1"),
        ("POST", "SearchResultsMP.aspx", "ctl00$ContentPlaceHolder1$lbNext1"),
    ]


def test_search_post_payload():
    site = FakeSite(make_docs(3))
    run(site)
    form = site.log[4][2]
    assert form["ctl00$ContentPlaceHolder1$cboDocGroup"] == ["LR"]
    assert form["ctl00$ContentPlaceHolder1$txtFromDate"] == ["07/08/2026"]
    assert form["ctl00$ContentPlaceHolder1$txtThruDate"] == ["09/26/2026"]
    assert form["ctl00$ContentPlaceHolder1$cmdSearch"] == ["Search"]
    assert "ctl00$ContentPlaceHolder1$cmdReset" not in form
    assert form["BrowserWidth"] == ["1920"]
    assert "username" in site.log[1][2] and "cmdLogin" not in site.log[1][2]


def test_no_results_is_explicit():
    result = run(FakeSite(make_docs(3)), DateRange(date(2026, 1, 1), date(2026, 1, 2)))
    assert (result.total, result.rows) == (0, ())
    assert result.reconciled


def test_challenge_mid_pagination_stops_with_clear_error():
    with pytest.raises(ChallengeError, match="refresh SEARCHIQS_CF_CLEARANCE"):
        run(FakeSite(make_docs(250), challenge_on={7}))


def test_challenge_on_entry():
    with pytest.raises(ChallengeError):
        run(FakeSite(make_docs(1), challenge_on={1}))


def test_group_not_retained_is_rejected():
    with pytest.raises(CriteriaMismatch, match="document group not retained"):
        run(FakeSite(make_docs(3), ignore_group=True))


def test_extra_criteria_rejected():
    with pytest.raises(CriteriaMismatch):
        run(FakeSite(make_docs(3), extra_criteria=[("Party Name", "Contains", "X")]))


def test_repeated_page_detected():
    with pytest.raises(PagingError, match="expected page 2, got page 1"):
        run(FakeSite(make_docs(250), repeat_page=True))


def test_skipped_page_detected():
    with pytest.raises(PagingError, match="expected page 2, got page 3"):
        run(FakeSite(make_docs(250), page_step=2))


def test_page_label_advancing_with_same_rows_detected():
    with pytest.raises(PagingError, match="repeats the previous page"):
        run(FakeSite(make_docs(250), stale_rows=True))


def test_page_ceiling():
    with pytest.raises(PagingError, match="max_pages"):
        run(FakeSite(make_docs(250)), max_pages=2)


def test_ambiguous_post_not_replayed():
    site = FakeSite(make_docs(3), fail_on={5: TransportError("reset")})
    with pytest.raises(AmbiguousPost):
        run(site)
    assert len(site.log) == 5
