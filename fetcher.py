#!/usr/bin/env python3
"""
fetcher.py — download + parse + merge calendar feeds into events.json

Design: do the hard work on the box. The browser only ever reads events.json.
Resilience: one dead feed must never blank the wall — we keep that feed's
last-known events and carry on. events.json is written atomically.

See docs-import-architecture.md (§4a, §6, §9).
"""
import json
import os
import sys
import uuid
import tempfile
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path

import requests
from icalendar import Calendar
import recurring_ical_events
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
FEEDS_PATH = DATA / "feeds.json"
EVENTS_PATH = DATA / "events.json"

# --- config (overridable via env) ---------------------------------------
TZ_NAME = os.environ.get("FAMILYCAL_TZ", "America/Los_Angeles")
TZ = ZoneInfo(TZ_NAME)
HTTP_TIMEOUT = 20
USER_AGENT = "FamilyCal/1.0 (+https://localhost)"


def log(*a):
    print("[fetcher]", *a, file=sys.stderr, flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def window_bounds(today=None):
    """First day of last month -> last day of next month."""
    today = today or date.today()
    start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    # first day of month after next
    nxt = today.replace(day=1)
    for _ in range(2):
        nxt = (nxt + timedelta(days=32)).replace(day=1)
    end = nxt - timedelta(days=1)
    return start, end


def to_local_aware(value):
    """Normalize a date/datetime (naive or aware) to a tz-aware local datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ)
        return value.astimezone(TZ)
    # plain date -> midnight local
    return datetime.combine(value, dtime.min, tzinfo=TZ)


def classify_error(ex):
    """A short, safe error label for the health endpoint.

    Never return the raw exception string: requests embeds the full feed URL
    (the secret webcal link) in its messages, and the health endpoint is
    unauthenticated on the LAN. Keep it useful but leak nothing.
    """
    if isinstance(ex, requests.HTTPError) and ex.response is not None:
        return f"HTTP {ex.response.status_code}"
    if isinstance(ex, requests.Timeout):
        return "timeout"
    if isinstance(ex, requests.ConnectionError):
        return "connection error"
    if isinstance(ex, ValueError):
        return "parse error"
    return type(ex).__name__


def fetch_feed_text(url):
    """Download an .ics feed. webcal:// -> https://."""
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return r.text


def parse_feed(text, feed, win_start, win_end):
    """Parse one ICS body into normalized event dicts for the window."""
    cal = Calendar.from_ical(text)
    occurrences = recurring_ical_events.of(cal).between(win_start, win_end)
    out = []
    for comp in occurrences:
        summary = str(comp.get("SUMMARY", "")).strip() or "(busy)"
        dtstart = comp.get("DTSTART")
        dtend = comp.get("DTEND")
        if dtstart is None:
            continue
        raw_start = dtstart.dt
        all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
        start = to_local_aware(raw_start)
        end = to_local_aware(dtend.dt) if dtend is not None else None
        # No end, or a zero-length timed event (recurring_ical_events
        # synthesizes DTEND==DTSTART when a feed omits it): apply a sane
        # default span so nothing renders as an instant on the wall.
        if end is None or (not all_day and end <= start):
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        out.append({
            "id": "evt_" + uuid.uuid4().hex[:12],
            "feed_id": feed["id"],
            "title": summary,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": all_day,
        })
    return out


def main():
    DATA.mkdir(exist_ok=True)
    feeds_doc = load_json(FEEDS_PATH, {"feeds": []})
    feeds = [f for f in feeds_doc.get("feeds", []) if f.get("enabled", True)]
    win_start, win_end = window_bounds()

    # last-good events, grouped by feed, so one dead feed keeps its old data
    prev = load_json(EVENTS_PATH, {"events": []})
    prev_by_feed = {}
    for e in prev.get("events", []):
        prev_by_feed.setdefault(e.get("feed_id"), []).append(e)
    # prior per-feed health, so a feed that fails this run keeps its last_ok
    prev_health = {f.get("id"): f for f in prev.get("feeds", [])}

    now = datetime.now(TZ)
    now_iso = now.isoformat()
    all_events = []
    feed_meta = []
    for feed in feeds:
        meta = {"id": feed["id"], "name": feed.get("name", "Calendar"),
                "color": feed.get("color", "#A98CFF")}
        feed_meta.append(meta)
        prior = prev_health.get(feed["id"], {})
        try:
            text = fetch_feed_text(feed["url"])
            evts = parse_feed(text, feed, win_start, win_end)
            log(f"{feed.get('name')}: {len(evts)} events")
            all_events.extend(evts)
            meta.update(status="ok", last_ok=now_iso, count=len(evts), error=None)
        except Exception as ex:  # noqa: BLE001 — resilience is the whole point
            kept = prev_by_feed.get(feed["id"], [])
            log(f"{feed.get('name')}: FAILED ({ex}); keeping {len(kept)} cached")
            all_events.extend(kept)
            meta.update(status="stale", last_ok=prior.get("last_ok"),
                        count=len(kept), error=classify_error(ex))

    all_events.sort(key=lambda e: e["start"])
    doc = {
        "generated_at": now_iso,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "timezone": TZ_NAME,
        "feeds": feed_meta,
        "events": all_events,
    }

    # atomic write
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, EVENTS_PATH)
    log(f"wrote {EVENTS_PATH} — {len(all_events)} events from {len(feeds)} feeds")


if __name__ == "__main__":
    main()
