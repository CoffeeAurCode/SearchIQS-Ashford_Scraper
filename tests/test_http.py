import pytest

from searchiqs_scraper.config import Config
from searchiqs_scraper.http import (
    AmbiguousPost, BadRedirect, DeadlineExceeded, EgressError, HttpClient, Response, RetriesExhausted,
    TransportError, _retry_after, egress_country,
)


class Script:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.closed = False

    def request(self, method, url, *, data, headers, timeout):
        self.calls.append((method, url, data, dict(headers)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


URL = "https://www.searchiqs.com/CTASH/"


def client(script, clock=None, deadline=None, **cfg):
    clock = clock or Clock()
    config = Config(delay_min=1, delay_max=1, read_attempts=3, backoff_base=2, backoff_cap=8, **cfg)
    return HttpClient(script, config, deadline=deadline, sleep=clock.sleep, clock=clock.time, rng=lambda: 1.0), clock


def ok(url=URL, text="<html></html>", status=200, headers=None):
    return Response(status, url, text, headers or {})


def test_get_paces_consecutive_requests():
    c, clock = client(Script(ok(), ok()))
    c.get(URL)
    c.get(URL)
    assert clock.sleeps == [1.0]


def test_navigation_headers_and_referer():
    script = Script(ok(), ok())
    c, _ = client(script)
    c.get(URL)
    c.get(URL + "x", referer=URL)
    assert script.calls[0][3]["Sec-Fetch-Site"] == "none" and "Referer" not in script.calls[0][3]
    assert script.calls[1][3]["Sec-Fetch-Site"] == "same-origin" and script.calls[1][3]["Referer"] == URL


def test_post_follows_302_with_get_and_sends_form_headers():
    script = Script(ok(status=302, headers={"location": "/CTASH/SearchAdvancedMP.aspx"}),
                    ok(URL + "SearchAdvancedMP.aspx", "search"))
    c, _ = client(script)
    r = c.post(URL, "a=1", referer=URL)
    assert r.text == "search"
    method, url, data, headers = script.calls[0]
    assert (method, data) == ("POST", "a=1")
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert headers["Origin"] == "https://www.searchiqs.com"
    assert script.calls[1][:3] == ("GET", URL + "SearchAdvancedMP.aspx", None)
    assert script.calls[1][3]["Referer"] == URL


def test_get_retries_transport_errors_then_succeeds():
    c, clock = client(Script(TransportError("reset"), ok(text="fine")))
    assert c.get(URL).text == "fine"
    assert clock.sleeps == [2.0]


def test_get_honours_longer_retry_after():
    c, clock = client(Script(ok(status=429, headers={"retry-after": "5"}), ok()))
    c.get(URL)
    assert clock.sleeps == [5.0]


def test_get_gives_up_after_read_attempts():
    c, _ = client(Script(ok(status=503), ok(status=503), ok(status=503)))
    with pytest.raises(RetriesExhausted):
        c.get(URL)


def test_post_transport_error_is_ambiguous_and_not_replayed():
    script = Script(TransportError("timeout"))
    c, _ = client(script)
    with pytest.raises(AmbiguousPost):
        c.post(URL, "a=1", referer=URL)
    assert len(script.calls) == 1


def test_post_error_status_is_returned_not_replayed():
    script = Script(ok(status=503))
    c, _ = client(script)
    assert c.post(URL, "a=1", referer=URL).status == 503
    assert len(script.calls) == 1


def test_redirect_to_foreign_host_refused():
    c, _ = client(Script(ok(status=302, headers={"location": "https://evil.example/x"})))
    with pytest.raises(BadRedirect):
        c.get(URL)


def test_plain_http_refused():
    c, _ = client(Script())
    with pytest.raises(BadRedirect):
        c.get("http://www.searchiqs.com/CTASH/")


def test_redirect_loop_bounded():
    loop = ok(status=302, headers={"location": "/CTASH/"})
    c, _ = client(Script(*[loop] * 3), max_redirects=2)
    with pytest.raises(BadRedirect):
        c.get(URL)


def test_deadline_stops_before_request():
    script = Script(ok(), ok())
    c, clock = client(script, deadline=100.5)
    c.get(URL)
    with pytest.raises(DeadlineExceeded):
        c.get(URL)
    assert len(script.calls) == 1


def test_retry_after_forms():
    assert _retry_after("7") == 7.0
    assert _retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0.0
    assert _retry_after("soon") is None
    assert _retry_after(None) is None


def test_egress_country():
    config = Config()
    assert egress_country(Script(ok("https://api.country.is/", '{"ip":"x","country":"us"}')), config) == "US"
    with pytest.raises(EgressError):
        egress_country(Script(ok(text="<html>")), config)
    with pytest.raises(EgressError):
        egress_country(Script(TransportError("down")), config)
    with pytest.raises(EgressError):
        egress_country(Script(ok(status=429, text="{}")), config)
