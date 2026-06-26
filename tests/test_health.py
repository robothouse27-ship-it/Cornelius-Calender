"""Phase 1 health/status signal.

Two halves: (1) fetcher records per-feed status into events.json without ever
leaking the secret feed URL, and (2) /api/health turns that into a liveness
report for the wall + future status UI.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import fetcher
import server
from conftest import make_ics, vevent, write_json


# --------------------------------------------------------------------------- #
# fetcher side: per-feed status recording
# --------------------------------------------------------------------------- #
def test_fetcher_records_per_feed_status(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "live", "name": "Live", "url": "http://live"},
        {"id": "dead", "name": "Dead", "url": "http://dead"},
    ]})

    def fake_fetch(url):
        if "dead" in url:
            raise requests.ConnectionError("max retries to secret.example.com")
        return make_ics(vevent("e", "20260615T170000Z", "20260615T180000Z", "x"))

    monkeypatch.setattr(fetcher, "fetch_feed_text", fake_fetch)
    fetcher.main()

    feeds = {f["id"]: f for f in json.loads((data / "events.json").read_text())["feeds"]}
    assert feeds["live"]["status"] == "ok"
    assert feeds["live"]["last_ok"] is not None
    assert feeds["live"]["count"] == 1
    assert feeds["live"]["error"] is None
    assert feeds["dead"]["status"] == "stale"
    assert feeds["dead"]["error"] == "connection error"   # classified, not raw


def test_error_label_never_leaks_feed_url(isolated_data, monkeypatch):
    """The secret webcal URL must not surface in events.json / health."""
    data = isolated_data
    secret = "https://p99-caldav.icloud.com/published/2/SECRETTOKEN123"
    write_json(data / "feeds.json", {"feeds": [
        {"id": "x", "name": "X", "url": secret},
    ]})

    def boom(url):
        raise requests.ConnectionError(f"failed to reach {url}")

    monkeypatch.setattr(fetcher, "fetch_feed_text", boom)
    fetcher.main()

    raw = (data / "events.json").read_text()
    assert "SECRETTOKEN123" not in raw
    assert "icloud.com" not in raw


def test_failed_feed_carries_prior_last_ok(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [{"id": "x", "name": "X", "url": "http://x"}]})
    write_json(data / "events.json", {
        "generated_at": "2026-06-20T08:00:00-07:00",
        "feeds": [{"id": "x", "name": "X", "color": "#111",
                   "status": "ok", "last_ok": "2026-06-20T08:00:00-07:00",
                   "count": 1, "error": None}],
        "events": [{"id": "evt_old", "feed_id": "x", "title": "cached",
                    "start": "2026-06-20T09:00:00-07:00",
                    "end": "2026-06-20T10:00:00-07:00", "all_day": False}],
    })
    monkeypatch.setattr(fetcher, "fetch_feed_text",
                        lambda url: (_ for _ in ()).throw(requests.Timeout()))
    fetcher.main()

    feed = json.loads((data / "events.json").read_text())["feeds"][0]
    assert feed["status"] == "stale"
    assert feed["error"] == "timeout"
    assert feed["last_ok"] == "2026-06-20T08:00:00-07:00"   # carried forward


def test_classify_error_variants():
    resp = requests.Response()
    resp.status_code = 404
    assert fetcher.classify_error(requests.HTTPError(response=resp)) == "HTTP 404"
    assert fetcher.classify_error(requests.Timeout()) == "timeout"
    assert fetcher.classify_error(requests.ConnectionError()) == "connection error"
    assert fetcher.classify_error(ValueError("bad ics")) == "parse error"
    assert fetcher.classify_error(KeyError("x")) == "KeyError"


# --------------------------------------------------------------------------- #
# server side: /api/health
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    events = tmp_path / "events.json"
    monkeypatch.setattr(server, "EVENTS_PATH", events)
    server.app.config["TESTING"] = True
    return server.app.test_client(), events


def _now_iso(delta_secs=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_secs)).isoformat()


def test_health_never_synced(client):
    c, events = client                          # events.json absent
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert body["status"] == "never_synced"
    assert body["feeds"] == []


def test_health_all_good(client):
    c, events = client
    write_json(events, {
        "generated_at": _now_iso(-60),          # synced a minute ago
        "timezone": "America/Los_Angeles",
        "feeds": [{"id": "a", "name": "A", "status": "ok",
                   "last_ok": _now_iso(-60), "count": 3, "error": None}],
        "events": [{}, {}, {}],
    })
    body = c.get("/api/health").get_json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["sync_stale"] is False
    assert body["summary"] == {"total": 1, "ok": 1, "stale": 0}
    assert body["event_count"] == 3
    assert 0 <= body["feeds"][0]["last_ok_age_seconds"] < 600


def test_health_degraded_on_stale_feed(client):
    c, events = client
    write_json(events, {
        "generated_at": _now_iso(-60),
        "feeds": [
            {"id": "a", "name": "A", "status": "ok", "last_ok": _now_iso(-60),
             "count": 2, "error": None},
            {"id": "b", "name": "B", "status": "stale",
             "last_ok": _now_iso(-7200), "count": 1, "error": "HTTP 503"},
        ],
        "events": [{}, {}, {}],
    })
    body = c.get("/api/health").get_json()
    assert body["ok"] is False
    assert body["status"] == "degraded"
    assert body["summary"] == {"total": 2, "ok": 1, "stale": 1}
    stale = next(f for f in body["feeds"] if f["id"] == "b")
    assert stale["error"] == "HTTP 503"


def test_health_degraded_on_stale_sync(client):
    c, events = client                          # feeds ok, but sync itself is old
    write_json(events, {
        "generated_at": _now_iso(-3 * 3600),    # 3h ago > 35 min threshold
        "feeds": [{"id": "a", "name": "A", "status": "ok",
                   "last_ok": _now_iso(-3 * 3600), "count": 1, "error": None}],
        "events": [{}],
    })
    body = c.get("/api/health").get_json()
    assert body["ok"] is False
    assert body["sync_stale"] is True
    assert body["age_seconds"] >= 3 * 3600 - 5


def test_health_unreadable_events_json(client):
    c, events = client
    events.write_text("{ this is not json")
    body = c.get("/api/health").get_json()
    assert body["ok"] is False
    assert body["status"] == "unreadable"
