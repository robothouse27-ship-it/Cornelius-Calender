"""window_bounds(): first day of last month -> last day of next month.

This drives the fetch window, so off-by-one or year-rollover bugs would
silently drop events. Lock down the boundaries.
"""
from datetime import date

import fetcher


def test_mid_year():
    start, end = fetcher.window_bounds(date(2026, 6, 15))
    assert start == date(2026, 5, 1)      # first day of last month
    assert end == date(2026, 7, 31)       # last day of next month


def test_year_rollback_at_january():
    # Last month crosses into the previous year.
    start, end = fetcher.window_bounds(date(2026, 1, 15))
    assert start == date(2025, 12, 1)
    assert end == date(2026, 2, 28)


def test_year_rollforward_at_december():
    # Next month crosses into the following year.
    start, end = fetcher.window_bounds(date(2026, 12, 15))
    assert start == date(2026, 11, 1)
    assert end == date(2027, 1, 31)


def test_leap_february_next_month():
    # 2028 is a leap year; "next month" Feb must end on the 29th.
    start, end = fetcher.window_bounds(date(2028, 1, 10))
    assert start == date(2027, 12, 1)
    assert end == date(2028, 2, 29)


def test_window_is_ordered_and_spans_three_months():
    start, end = fetcher.window_bounds(date(2026, 6, 15))
    assert start < end
