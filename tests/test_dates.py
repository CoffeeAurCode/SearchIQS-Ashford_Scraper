from datetime import date, datetime, timedelta, timezone

import pytest

from searchiqs_scraper.dates import (
    SITE_TZ,
    DateRange,
    format_site_date,
    freeze_run_now,
    is_exact_partition,
    parse_site_date,
    task_range,
)

UTC = timezone.utc


def test_task_range_is_81_inclusive_dates():
    r = task_range(datetime(2026, 9, 26, 12, 0, tzinfo=SITE_TZ))
    assert (r.start, r.end) == (date(2026, 7, 8), date(2026, 9, 26))
    assert r.days == 81
    assert r.dates()[0] == r.start and r.dates()[-1] == r.end


@pytest.mark.parametrize(
    "utc_now, expected_to",
    [
        (datetime(2026, 9, 27, 3, 59, tzinfo=UTC), date(2026, 9, 26)),
        (datetime(2026, 9, 27, 4, 1, tzinfo=UTC), date(2026, 9, 27)),
        (datetime(2026, 1, 1, 4, 59, tzinfo=UTC), date(2025, 12, 31)),
        (datetime(2026, 1, 1, 5, 1, tzinfo=UTC), date(2026, 1, 1)),
    ],
)
def test_new_york_midnight_boundary(utc_now, expected_to):
    r = task_range(utc_now)
    assert r.end == expected_to
    assert r.start == expected_to - timedelta(days=80)


def test_year_rollover():
    r = task_range(datetime(2025, 12, 31, 23, 30, tzinfo=SITE_TZ))
    assert r.start == date(2025, 10, 12)


def test_leap_day_start():
    assert task_range(datetime(2028, 5, 19, 9, 0, tzinfo=SITE_TZ)).start == date(2028, 2, 29)


@pytest.mark.parametrize(
    "utc_now, expected_to",
    [
        (datetime(2026, 3, 8, 7, 30, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), date(2026, 11, 1)),
    ],
)
def test_dst_transition_days(utc_now, expected_to):
    r = task_range(utc_now)
    assert r.end == expected_to and r.days == 81


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        freeze_run_now(datetime(2026, 9, 26, 12, 0))


def test_freeze_converts_to_new_york():
    frozen = freeze_run_now(datetime(2026, 9, 27, 2, 0, tzinfo=UTC))
    assert frozen.tzinfo is SITE_TZ and frozen.date() == date(2026, 9, 26)


def test_split_half_is_exact_partition():
    r = DateRange(date(2026, 7, 8), date(2026, 9, 26))
    left, right = r.split_half()
    assert left.end + timedelta(days=1) == right.start
    assert is_exact_partition(r, [left, right])
    assert left.days + right.days == r.days


def test_recursive_split_to_single_days_covers_range_exactly():
    root = DateRange(date(2026, 7, 8), date(2026, 9, 26))
    leaves, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.days == 1:
            leaves.append(node)
        else:
            stack.extend(node.split_half())
    assert is_exact_partition(root, leaves)
    assert sorted(leaf.start for leaf in leaves) == root.dates()


def test_single_day_cannot_split():
    with pytest.raises(ValueError):
        DateRange(date(2026, 1, 1), date(2026, 1, 1)).split_half()


def test_two_day_split():
    left, right = DateRange(date(2026, 1, 1), date(2026, 1, 2)).split_half()
    assert left.days == right.days == 1


@pytest.mark.parametrize(
    "children",
    [
        [],
        [DateRange(date(2026, 1, 1), date(2026, 1, 4)), DateRange(date(2026, 1, 6), date(2026, 1, 10))],
        [DateRange(date(2026, 1, 1), date(2026, 1, 5)), DateRange(date(2026, 1, 5), date(2026, 1, 10))],
        [DateRange(date(2026, 1, 1), date(2026, 1, 5)), DateRange(date(2026, 1, 6), date(2026, 1, 11))],
        [DateRange(date(2025, 12, 31), date(2026, 1, 5)), DateRange(date(2026, 1, 6), date(2026, 1, 10))],
    ],
    ids=["empty", "gap", "overlap", "past-end", "before-start"],
)
def test_bad_partitions_rejected(children):
    assert not is_exact_partition(DateRange(date(2026, 1, 1), date(2026, 1, 10)), children)


def test_invalid_range():
    with pytest.raises(ValueError):
        DateRange(date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(TypeError):
        DateRange(datetime(2026, 1, 1), date(2026, 1, 2))


def test_site_date_format_keeps_leading_zeros():
    assert format_site_date(date(2026, 7, 8)) == "07/08/2026"
    assert parse_site_date(" 07/08/2026 ") == date(2026, 7, 8)
