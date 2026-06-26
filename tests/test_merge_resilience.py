"""main(): the resilience guarantee — "one dead feed must never blank the wall."

A failing feed must keep its last-known events from the previous events.json,
while healthy feeds refresh. events.json must always be written atomically and
remain valid JSON.
"""
import json

import fetcher
from conftest import make_ics, vevent, write_json


def test_dead_feed_keeps_cached_events(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "live", "name": "Live", "url": "http://live"},
        {"id": "dead", "name": "Dead", "url": "http://dead"},
    ]})
    # Previous run cached two events for each feed.
    write_json(data / "events.json", {"events": [
        {"id": "evt_old_live", "feed_id": "live", "title": "stale",
         "start": "2026-06-10T09:00:00-07:00", "end": "2026-06-10T10:00:00-07:00",
         "all_day": False},
        {"id": "evt_old_dead", "feed_id": "dead", "title": "cached-dead",
         "start": "2026-06-11T09:00:00-07:00", "end": "2026-06-11T10:00:00-07:00",
         "all_day": False},
    ]})

    def fake_fetch(url):
        if "dead" in url:
            raise RuntimeError("502 Bad Gateway")
        return make_ics(vevent(
            "fresh", "20260615T170000Z", "20260615T180000Z", "fresh-live"))

    monkeypatch.setattr(fetcher, "fetch_feed_text", fake_fetch)

    fetcher.main()

    doc = json.loads((data / "events.json").read_text())
    titles = {e["title"] for e in doc["events"]}
    # Live feed refreshed (stale gone, fresh in); dead feed kept its cache.
    assert "fresh-live" in titles
    assert "stale" not in titles
    assert "cached-dead" in titles


def test_all_feeds_live_replaces_everything(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "a", "name": "A", "url": "http://a"},
    ]})
    write_json(data / "events.json", {"events": [
        {"id": "evt_old", "feed_id": "a", "title": "old",
         "start": "2026-06-01T09:00:00-07:00", "end": "2026-06-01T10:00:00-07:00",
         "all_day": False},
    ]})
    monkeypatch.setattr(fetcher, "fetch_feed_text", lambda url: make_ics(
        vevent("n", "20260615T170000Z", "20260615T180000Z", "new")))

    fetcher.main()

    doc = json.loads((data / "events.json").read_text())
    titles = [e["title"] for e in doc["events"]]
    assert titles == ["new"]


def test_output_doc_shape_and_sorting(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "a", "name": "A", "color": "#111", "url": "http://a"},
    ]})
    monkeypatch.setattr(fetcher, "fetch_feed_text", lambda url: make_ics(
        vevent("late", "20260620T170000Z", "20260620T180000Z", "late"),
        vevent("early", "20260601T170000Z", "20260601T180000Z", "early"),
    ))

    fetcher.main()

    doc = json.loads((data / "events.json").read_text())
    assert set(doc) >= {"generated_at", "window", "timezone", "feeds", "events"}
    assert doc["feeds"][0]["id"] == "a"
    starts = [e["start"] for e in doc["events"]]
    assert starts == sorted(starts), "events must be sorted by start"


def test_disabled_feed_is_skipped(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "on", "name": "On", "url": "http://on"},
        {"id": "off", "name": "Off", "url": "http://off", "enabled": False},
    ]})
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return make_ics(vevent("e", "20260615T170000Z", "20260615T180000Z", "e"))

    monkeypatch.setattr(fetcher, "fetch_feed_text", fake_fetch)

    fetcher.main()

    assert fetched == ["http://on"]              # disabled feed never fetched
    doc = json.loads((data / "events.json").read_text())
    assert [f["id"] for f in doc["feeds"]] == ["on"]
