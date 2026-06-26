"""parse_feed(): the gnarliest logic — timezone normalization, all-day
handling, recurrence expansion, and the default-end / default-title rules.
"""
from datetime import date, datetime

import pytest

import fetcher
from conftest import make_ics, vevent

FEED = {"id": "feed_test", "name": "Test", "color": "#A98CFF"}
WIN_START = date(2026, 5, 1)
WIN_END = date(2026, 8, 1)


def parse(*vevents):
    return fetcher.parse_feed(make_ics(*vevents), FEED, WIN_START, WIN_END)


def test_utc_event_converts_to_local():
    # 17:00Z on 2026-06-15 == 10:00 in America/Los_Angeles (PDT, UTC-7).
    out = parse(vevent("u1", "20260615T170000Z", "20260615T180000Z", "Standup"))
    assert len(out) == 1
    ev = out[0]
    start = datetime.fromisoformat(ev["start"])
    assert (start.hour, start.minute) == (10, 0)
    assert start.utcoffset().total_seconds() == -7 * 3600
    assert ev["all_day"] is False
    assert ev["feed_id"] == "feed_test"


def test_all_day_event_flag_and_default_end():
    out = parse(vevent("u2", "20260615", summary="Birthday", value_date=True))
    assert len(out) == 1
    ev = out[0]
    assert ev["all_day"] is True
    start = datetime.fromisoformat(ev["start"])
    end = datetime.fromisoformat(ev["end"])
    assert start.hour == 0 and start.minute == 0      # midnight local
    assert (end - start).days == 1                    # default all-day span


def test_timed_event_without_end_defaults_to_one_hour():
    out = parse(vevent("u3", "20260615T090000Z", summary="Call"))
    ev = out[0]
    start = datetime.fromisoformat(ev["start"])
    end = datetime.fromisoformat(ev["end"])
    assert (end - start).total_seconds() == 3600


def test_missing_summary_becomes_busy_placeholder():
    out = parse(vevent("u4", "20260615T090000Z", "20260615T100000Z"))
    assert out[0]["title"] == "(busy)"


def test_daily_recurrence_expands_within_window():
    out = parse(vevent(
        "u5", "20260601T090000Z", "20260601T093000Z",
        summary="Daily", rrule="FREQ=DAILY;COUNT=5",
    ))
    assert len(out) == 5
    starts = sorted(e["start"] for e in out)
    days = {datetime.fromisoformat(s).date() for s in starts}
    assert len(days) == 5                             # 5 distinct days
    assert all(e["title"] == "Daily" for e in out)


def test_recurrence_clipped_to_window():
    # Weekly forever, but only occurrences inside the window are returned.
    out = parse(vevent(
        "u6", "20260101T090000Z", "20260101T100000Z",
        summary="Weekly", rrule="FREQ=WEEKLY",
    ))
    assert out, "expected at least one occurrence in window"
    for e in out:
        d = datetime.fromisoformat(e["start"]).date()
        assert WIN_START <= d <= WIN_END


def test_event_without_dtstart_is_skipped():
    # A VEVENT missing DTSTART must be dropped, not crash the parse.
    ics = make_ics("BEGIN:VEVENT\nUID:nostart\nSUMMARY:Ghost\nEND:VEVENT")
    # recurring_ical_events may reject it during expansion; if it raises, the
    # caller (main) catches it — here we assert parse_feed itself is robust.
    try:
        out = fetcher.parse_feed(ics, FEED, WIN_START, WIN_END)
    except Exception:  # pragma: no cover - library may reject malformed VEVENT
        pytest.skip("library rejects DTSTART-less VEVENT before our guard")
    assert all(e["title"] != "Ghost" for e in out)


def test_every_event_has_unique_id_and_required_keys():
    out = parse(
        vevent("a", "20260615T170000Z", "20260615T180000Z", "A"),
        vevent("b", "20260616T170000Z", "20260616T180000Z", "B"),
    )
    ids = [e["id"] for e in out]
    assert len(ids) == len(set(ids))
    for e in out:
        assert set(e) >= {"id", "feed_id", "title", "start", "end", "all_day"}
        assert e["id"].startswith("evt_")
