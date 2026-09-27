from datetime import date

import pytest

from searchiqs_scraper.models import FieldState
from searchiqs_scraper.pages import PageKind, classify
from searchiqs_scraper.parser import ParseError, parse_results

from fakesite import (
    CHALLENGE_HTML, ERROR_HTML, Doc, entry_html, make_docs, no_results_html, results_html, search_html,
)

CRIT = [("Document Group", "Equals", "LR"), ("Recording Date", "Greater Than or Equal To", "07/08/2026"),
        ("Recording Date", "Less Than or Equal To", "09/26/2026")]
WINDOW = (date(2026, 7, 8), date(2026, 9, 26))


def doc(**kw):
    base = dict(record_id="L|1", party1=["SMITH ANNA"], party2=["EXAMPLE BANK NA"], doc_type="MORTGAGE",
                book_page="007-012", recorded=date(2026, 8, 1))
    base.update(kw)
    return Doc(**base)


def page(docs, total=None, p=1, pages=1):
    return results_html("vs1", docs, len(docs) if total is None else total, p, pages, CRIT)


@pytest.mark.parametrize("status,html,kind", [
    (200, entry_html("v"), PageKind.ENTRY),
    (200, search_html("v"), PageKind.SEARCH_FORM),
    (200, results_html("v", make_docs(2), 2, 1, 1, CRIT), PageKind.RESULTS),
    (200, no_results_html("v", CRIT), PageKind.NO_RESULTS),
    (403, CHALLENGE_HTML, PageKind.CLOUDFLARE_CHALLENGE),
    (200, CHALLENGE_HTML, PageKind.CLOUDFLARE_CHALLENGE),
    (500, ERROR_HTML, PageKind.SITE_ERROR),
    (200, ERROR_HTML, PageKind.SITE_ERROR),
    (200, "<html><title>Login</title><body>please sign in</body></html>", PageKind.UNKNOWN),
    (404, "<html></html>", PageKind.UNKNOWN),
])
def test_classify(status, html, kind):
    assert classify(status, html) is kind


def test_parse_fields_and_structure():
    d = doc(party1=["SMITH ANNA", "SMITH, BOB & CO; LTD"], description="12 MAIN ST", additional="",
            related=[("BK: 9 PG:99", "555001")])
    res = parse_results(page([d]), WINDOW)
    assert (res.total, res.page, res.pages, res.next_target) == (1, 1, 1, None)
    assert [(c.field, c.comparison, c.value) for c in res.criteria] == CRIT
    row = res.rows[0]
    f = row.record.fields
    assert row.record.doc_id == "L|1" and row.record.date_iso == "2026-08-01" and row.record.issues == ()
    assert f["Party 1"].text == "SMITH ANNA; SMITH, BOB & CO; LTD"
    assert f["Party 2"].text == "EXAMPLE BANK NA"
    assert (f["Type"].text, f["Book-Page"].text, f["Date"].text) == ("MORTGAGE", "007-012", "08/01/2026")
    assert f["Description"].text == "12 MAIN ST"
    assert f["Additional Description"].state is FieldState.EMPTY
    assert f["Related"].text == "BK: 9 PG:99" and row.related_refs == ("555001",)


def test_multiple_related_links_kept_in_order():
    d = doc(related=[("BK: 1 PG:2", "11"), ("BK: 3 PG:4", "12")])
    row = parse_results(page([d]), WINDOW).rows[0]
    assert row.record.fields["Related"].text == "BK: 1 PG:2; BK: 3 PG:4"
    assert row.related_refs == ("11", "12")


def test_truncated_party_list_recovered_from_tooltip():
    d = doc(party1=["A ONE", "B TWO", "C THREE"], party1_shown=["A ONE"])
    assert parse_results(page([d]), WINDOW).rows[0].record.fields["Party 1"].text == "A ONE; B TWO; C THREE"


def test_party_list_contradicting_tooltip_fails_field():
    d = doc(party1=["A ONE"], party1_shown=["Z OTHER"])
    field = parse_results(page([d]), WINDOW).rows[0].record.fields["Party 1"]
    assert field.state is FieldState.FAILED


def test_out_of_range_date_is_an_issue():
    d = doc(recorded=date(2026, 10, 1))
    assert "Date outside the searched range" in parse_results(page([d]), WINDOW).rows[0].record.issues


def test_pager_positions():
    docs = make_docs(3)
    first = parse_results(page(docs, total=250, p=1, pages=3), WINDOW)
    assert first.next_target == "ctl00$ContentPlaceHolder1$lbNext1" and not first.is_last
    last = parse_results(page(docs, total=250, p=3, pages=3), WINDOW)
    assert last.next_target is None and last.is_last


def test_reordered_columns_are_mapped_by_header():
    html = page([doc(description="DESC", additional="MORE")])
    html = html.replace("<td>Description</td><td>Additional Description</td>",
                        "<td>Additional Description</td><td>Description</td>")
    f = parse_results(html, WINDOW).rows[0].record.fields
    assert (f["Description"].text, f["Additional Description"].text) == ("MORE", "DESC")


def test_missing_header_is_rejected():
    with pytest.raises(ParseError):
        parse_results(page([doc()]).replace("<td>Related</td>", "<td>Links</td>"), WINDOW)


def test_row_with_wrong_cell_count_is_rejected():
    html = page([doc()]).replace('<td valign="top">MORTGAGE</td>', "")
    with pytest.raises(ParseError):
        parse_results(html, WINDOW)


def test_disagreeing_counts_rejected():
    html = page([doc()]).replace("A total of 1 documents", "A total of 2 documents")
    with pytest.raises(ParseError):
        parse_results(html, WINDOW)


def test_fingerprint_ignores_view_state_but_sees_content():
    a = parse_results(page([doc()]), WINDOW).rows[0].fingerprint
    b = parse_results(page([doc()]).replace('value="vs1"', 'value="vs999"'), WINDOW).rows[0].fingerprint
    c = parse_results(page([doc(description="EDITED")]), WINDOW).rows[0].fingerprint
    assert a == b != c


def test_entities_and_nbsp():
    d = doc(description="A &amp; B  LOT  7", party2=["O’BRIEN & SONS"])
    f = parse_results(page([d]), WINDOW).rows[0].record.fields
    assert f["Description"].text == "A &amp; B LOT 7"
    assert f["Party 2"].text == "O’BRIEN & SONS"
