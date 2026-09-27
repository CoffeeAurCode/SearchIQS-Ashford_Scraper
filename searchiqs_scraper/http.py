from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from .config import Config

log = logging.getLogger(__name__)

SITE_HOSTS = frozenset({"www.searchiqs.com"})
CLEARANCE_DOMAIN = ".searchiqs.com"
EGRESS_URL = "https://api.country.is/"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303})


class HttpError(Exception):
    code = "http-error"


class TransportError(HttpError):
    code = "transport-error"


class AmbiguousPost(HttpError):
    code = "ambiguous-post"


class RetriesExhausted(HttpError):
    code = "retries-exhausted"


class BadRedirect(HttpError):
    code = "bad-redirect"


class DeadlineExceeded(HttpError):
    code = "deadline-exceeded"


class EgressError(HttpError):
    code = "egress-check-failed"


@dataclass(frozen=True)
class Response:
    status: int
    url: str
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class Transport(Protocol):
    def request(self, method: str, url: str, *, data: str | None, headers: Mapping[str, str],
                timeout: tuple[float, float]) -> Response: ...

    def close(self) -> None: ...


class CurlTransport:
    def __init__(self, config: Config) -> None:
        from curl_cffi import requests as curl_requests

        self._errors = curl_requests.exceptions.RequestException
        self._session = curl_requests.Session(
            impersonate=config.impersonate,
            verify=str(config.ca_bundle) if config.ca_bundle else True,
        )
        self._session.headers.update(config.browser_headers())
        if config.cf_clearance:
            self._session.cookies.set("cf_clearance", config.cf_clearance, domain=CLEARANCE_DOMAIN, path="/")

    def request(self, method: str, url: str, *, data: str | None, headers: Mapping[str, str],
                timeout: tuple[float, float]) -> Response:
        try:
            r = self._session.request(method, url, data=data, headers=dict(headers), timeout=timeout,
                                      allow_redirects=False)
        except self._errors as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        return Response(r.status_code, str(r.url), r.text, {k.lower(): v for k, v in r.headers.items()})

    def close(self) -> None:
        self._session.close()


class HttpClient:
    def __init__(self, transport: Transport, config: Config, *, deadline: float | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] = random.random, hosts: frozenset[str] = SITE_HOSTS) -> None:
        self.transport = transport
        self.config = config
        self.deadline = deadline
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._hosts = hosts
        self._last_request: float | None = None
        self.requests_sent = 0

    def get(self, url: str, *, referer: str | None = None) -> Response:
        return self._follow(self._get_with_retries(url, referer), url)

    def post(self, url: str, body: str, *, referer: str) -> Response:
        self._check_url(url)
        headers = {**self._nav_headers(referer), "Content-Type": "application/x-www-form-urlencoded",
                   "Origin": _origin(url), "Cache-Control": "max-age=0"}
        try:
            response = self._send("POST", url, body, headers)
        except TransportError as exc:
            raise AmbiguousPost(f"POST {_path(url)} outcome unknown: {exc}") from exc
        return self._follow(response, url)

    def close(self) -> None:
        self.transport.close()

    def _follow(self, response: Response, url: str) -> Response:
        hops = 0
        while response.status in REDIRECT_STATUS:
            hops += 1
            if hops > self.config.max_redirects:
                raise BadRedirect(f"more than {self.config.max_redirects} redirects from {_path(url)}")
            location = response.header("location")
            if not location:
                raise BadRedirect(f"redirect without Location from {_path(response.url)}")
            referer, url = response.url, urljoin(response.url, location)
            response = self._get_with_retries(url, referer)
        if response.status in (307, 308):
            raise BadRedirect(f"unsupported {response.status} redirect from {_path(response.url)}")
        return response

    def _get_with_retries(self, url: str, referer: str | None) -> Response:
        self._check_url(url)
        headers = self._nav_headers(referer)
        last: str = ""
        for attempt in range(1, self.config.read_attempts + 1):
            try:
                response = self._send("GET", url, None, headers)
            except TransportError as exc:
                last = str(exc)
                wait = self._backoff(attempt)
            else:
                if response.status not in RETRYABLE_STATUS:
                    return response
                last = f"HTTP {response.status}"
                wait = max(self._backoff(attempt), _retry_after(response.header("retry-after")) or 0.0)
            if attempt == self.config.read_attempts:
                break
            self._check_budget(wait)
            log.info("GET %s failed (%s), retry %d in %.1fs", _path(url), last, attempt, wait)
            self._sleep(wait)
        raise RetriesExhausted(f"GET {_path(url)} failed {self.config.read_attempts} times: {last}")

    def _send(self, method: str, url: str, body: str | None, headers: Mapping[str, str]) -> Response:
        self._pace()
        started = self._clock()
        self.requests_sent += 1
        response = self.transport.request(method, url, data=body, headers=headers,
                                          timeout=(self.config.connect_timeout, self.config.request_timeout))
        log.debug("%s %s -> %d (%.2fs)", method, _path(url), response.status, self._clock() - started)
        return response

    def _pace(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            gap = self.config.delay_min + (self.config.delay_max - self.config.delay_min) * self._rng()
            wait = self._last_request + gap - now
            if wait > 0:
                self._check_budget(wait)
                self._sleep(wait)
        self._check_budget(0.0)
        self._last_request = self._clock()

    def _backoff(self, attempt: int) -> float:
        raw = min(self.config.backoff_cap, self.config.backoff_base * 2 ** (attempt - 1))
        return raw * (0.5 + 0.5 * self._rng())

    def _check_budget(self, wait: float) -> None:
        if self.deadline is not None and self._clock() + wait > self.deadline:
            raise DeadlineExceeded("run deadline reached")

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self._hosts:
            raise BadRedirect(f"refusing to request {parts.scheme}://{parts.hostname}")

    @staticmethod
    def _nav_headers(referer: str | None) -> dict[str, str]:
        headers = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1",
                   "Sec-Fetch-Site": "same-origin" if referer else "none"}
        if referer:
            headers["Referer"] = referer
        return headers


def egress_country(transport: Transport, config: Config) -> str:
    try:
        response = transport.request("GET", EGRESS_URL, data=None, headers={"Accept": "application/json"},
                                     timeout=(config.connect_timeout, config.request_timeout))
    except TransportError as exc:
        raise EgressError(f"egress check failed: {exc}") from exc
    if response.status != 200:
        raise EgressError(f"egress check returned HTTP {response.status}")
    try:
        country = json.loads(response.text)["country"]
    except (ValueError, KeyError, TypeError):
        raise EgressError("egress check returned an unexpected body") from None
    if not isinstance(country, str) or len(country) != 2:
        raise EgressError("egress check returned no country code")
    return country.upper()


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _path(url: str) -> str:
    return urlsplit(url).path or "/"
