from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

SITE_TZ_NAME = "America/New_York"
SITE_TZ = ZoneInfo(SITE_TZ_NAME)
LOOKBACK_DAYS = 80
SITE_DATE_FORMAT = "%m/%d/%Y"


def freeze_run_now(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(SITE_TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("run_now must be timezone-aware")
    return now.astimezone(SITE_TZ)


@dataclass(frozen=True, order=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if type(self.start) is not date or type(self.end) is not date:
            raise TypeError("DateRange bounds must be date objects")
        if self.start > self.end:
            raise ValueError(f"start {self.start} is after end {self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def contains(self, other: DateRange) -> bool:
        return self.start <= other.start and other.end <= self.end

    def split_half(self) -> tuple[DateRange, DateRange]:
        if self.days < 2:
            raise ValueError("a single-day range cannot be split by date")
        mid = self.start + timedelta(days=(self.days - 1) // 2)
        return DateRange(self.start, mid), DateRange(mid + timedelta(days=1), self.end)

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def task_range(run_now: datetime) -> DateRange:
    local = freeze_run_now(run_now)
    to_date = local.date()
    return DateRange(to_date - timedelta(days=LOOKBACK_DAYS), to_date)


def is_exact_partition(parent: DateRange, children: list[DateRange]) -> bool:
    if not children:
        return False
    ordered = sorted(children)
    if ordered[0].start != parent.start or ordered[-1].end != parent.end:
        return False
    return all(b.start == a.end + timedelta(days=1) for a, b in pairwise(ordered))


def format_site_date(d: date) -> str:
    return d.strftime(SITE_DATE_FORMAT)


def parse_site_date(text: str) -> date:
    return datetime.strptime(text.strip(), SITE_DATE_FORMAT).date()
