from urllib.parse import parse_qsl

import pytest
from bs4 import BeautifulSoup

from searchiqs_scraper.webforms import FormError, extract_form_state, parse_postback

BASE = "https://example.test/App/Search.aspx?x=1"

PAGE = """
<html><body>
<form method="post" action="./Search.aspx?mode=guest" id="form1">
  <input type="hidden" name="__EVENTTARGET" value="" />
  <input type="hidden" name="__EVENTARGUMENT" value="" />
  <input type="hidden" name="__VIEWSTATEFIELDCOUNT" value="3" />
  <input type="hidden" name="__VIEWSTATE" value="part0+/=" />
  <input type="hidden" name="__VIEWSTATE1" value="part1" />
  <input type="hidden" name="__VIEWSTATE2" value="part2" />
  <input type="hidden" name="__EVENTVALIDATION" value="ev" />
  <input name="txtFrom" value="01/02/2026" />
  <input type="text" name="txtTo" />
  <input type="text" value="no name" />
  <input type="text" name="txtDisabled" value="x" disabled />
  <select name="ddlGroup">
    <option value="">All</option>
    <option value="LAND" selected>Land Records</option>
  </select>
  <select name="ddlNoneSelected">
    <option value="a" disabled>A</option>
    <option>  Plain   text  </option>
  </select>
  <select name="ddlMulti" multiple>
    <option value="1" selected>1</option>
    <option value="2">2</option>
    <option value="3" selected>3</option>
  </select>
  <select name="ddlOff" disabled><option value="z" selected>z</option></select>
  <input type="checkbox" name="chkOn" checked />
  <input type="checkbox" name="chkOff" value="y" />
  <input type="radio" name="rdo" value="r1" />
  <input type="radio" name="rdo" value="r2" checked />
  <input type="hidden" name="dup" value="first" />
  <input type="hidden" name="dup" value="second" />
  <textarea name="notes">line1
line2</textarea>
  <fieldset disabled>
    <legend><input name="inLegend" value="kept" /></legend>
    <input name="inFieldset" value="dropped" />
  </fieldset>
  <input type="file" name="upload" />
  <input type="reset" name="rst" value="Reset" />
  <input type="submit" name="btnSearch" value="Search" />
  <input type="submit" name="btnClear" value="Clear" />
  <input type="image" name="imgGo" src="go.png" />
  <button name="btnGuest" value="guest">Guest</button>
  <button type="button" name="btnJs">JS</button>
</form>
</body></html>
"""


@pytest.fixture
def state():
    return extract_form_state(PAGE, BASE)


def test_action_and_method(state):
    assert state.action == "https://example.test/App/Search.aspx?mode=guest"
    assert state.method == "POST"


def test_successful_controls_in_document_order(state):
    assert list(state.fields) == [
        ("__EVENTTARGET", ""),
        ("__EVENTARGUMENT", ""),
        ("__VIEWSTATEFIELDCOUNT", "3"),
        ("__VIEWSTATE", "part0+/="),
        ("__VIEWSTATE1", "part1"),
        ("__VIEWSTATE2", "part2"),
        ("__EVENTVALIDATION", "ev"),
        ("txtFrom", "01/02/2026"),
        ("txtTo", ""),
        ("ddlGroup", "LAND"),
        ("ddlNoneSelected", "Plain text"),
        ("ddlMulti", "1"),
        ("ddlMulti", "3"),
        ("chkOn", "on"),
        ("rdo", "r2"),
        ("dup", "first"),
        ("dup", "second"),
        ("notes", "line1\r\nline2"),
        ("inLegend", "kept"),
    ]


def test_no_submitter_sent_by_default(state):
    names = state.names()
    for name in ("btnSearch", "btnClear", "imgGo", "btnGuest", "btnJs", "rst", "upload"):
        assert name not in names


def test_only_clicked_button_is_sent(state):
    submitted = state.submit_with("btnSearch")
    assert submitted.fields[-1] == ("btnSearch", "Search")
    assert "btnClear" not in submitted.names()
    assert state.submit_with("imgGo").fields[-2:] == (("imgGo.x", "0"), ("imgGo.y", "0"))
    assert state.submit_with("btnGuest").fields[-1] == ("btnGuest", "guest")
    with pytest.raises(FormError):
        state.submit_with("btnJs")


def test_split_viewstate_round_trips_through_encoding(state):
    assert parse_qsl(state.encode(), keep_blank_values=True) == list(state.fields)


def test_overrides_replace_in_place_and_apply_last(state):
    updated = state.with_values({"txtTo": "03/04/2026", "dup": "only", "chkOn": None, "extra": ["a", "b"]})
    assert updated.get("txtTo") == "03/04/2026"
    assert updated.get_all("dup") == ["only"]
    assert updated.names().index("dup") == state.names().index("dup") - 1
    assert "chkOn" not in updated.names()
    assert updated.fields[-2:] == (("extra", "a"), ("extra", "b"))
    assert state.get("txtTo") == ""


def test_postback_sets_event_fields_without_submitter(state):
    pb = state.postback("ctl00$grid", "Page$2")
    assert pb.get("__EVENTTARGET") == "ctl00$grid"
    assert pb.get("__EVENTARGUMENT") == "Page$2"
    assert pb.names().index("__EVENTTARGET") == 0
    assert "btnSearch" not in pb.names()


def test_form_selection():
    two = "<form id='a'><input name='x' value='1'></form><form id='b' action='/b'><input name='y' value='2'></form>"
    with pytest.raises(FormError):
        extract_form_state(two, BASE)
    b = extract_form_state(two, BASE, form_id="b")
    assert b.action == "https://example.test/b" and b.method == "GET" and b.fields == (("y", "2"),)
    with pytest.raises(FormError):
        extract_form_state(two, BASE, form_id="missing")


def test_missing_action_posts_back_to_page():
    assert extract_form_state("<form method='post'></form>", BASE).action == BASE


def test_parse_postback_from_decoded_href():
    soup = BeautifulSoup(
        "<a href=\"javascript:__doPostBack(&#39;ctl00$ContentPlaceHolder1$grid&#39;,&#39;Page$3&#39;)\">3</a>", "lxml"
    )
    assert parse_postback(soup.a["href"]) == ("ctl00$ContentPlaceHolder1$grid", "Page$3")


@pytest.mark.parametrize(
    "script, expected",
    [
        ('__doPostBack("lnkNext","")', ("lnkNext", "")),
        (r"__doPostBack('a\'b', 'x\\y')", ("a'b", "x\\y")),
        (
            'WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions("ctl00$btn", "", true, "", "", false, true))',
            ("ctl00$btn", ""),
        ),
        ("window.open('x')", None),
    ],
)
def test_parse_postback_variants(script, expected):
    assert parse_postback(script) == expected
