"""Phase 2 keystone: the person/owner model.

Owners live in a people registry; feeds point at a person via owner_id; events
resolve their owner through their feed. Covers fetcher propagation, the people
CRUD + feed-assignment endpoints, and owner-aware .ics categories.
"""
import json

import pytest

import fetcher
import server
from conftest import make_ics, vevent, write_json


# --------------------------------------------------------------------------- #
# fetcher: people + per-feed owner_id flow into events.json
# --------------------------------------------------------------------------- #
def test_fetcher_propagates_people_and_owner(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {
        "people": [{"id": "p_benji", "name": "Benji", "color": "#46d6b4",
                    "avatar": "🧒"}],
        "feeds": [
            {"id": "f_benji", "name": "Benji cal", "url": "http://b",
             "owner_id": "p_benji"},
            {"id": "f_hol", "name": "Holidays", "url": "http://h"},  # no owner
        ],
    })
    monkeypatch.setattr(fetcher, "fetch_feed_text", lambda url: make_ics(
        vevent("e", "20260615T170000Z", "20260615T180000Z", "x")))
    fetcher.main()

    doc = json.loads((data / "events.json").read_text())
    assert doc["people"] == [{"id": "p_benji", "name": "Benji",
                              "color": "#46d6b4", "avatar": "🧒"}]
    feeds = {f["id"]: f for f in doc["feeds"]}
    assert feeds["f_benji"]["owner_id"] == "p_benji"
    assert feeds["f_hol"]["owner_id"] is None       # unowned feed → null


def test_fetcher_no_people_key_is_safe(isolated_data, monkeypatch):
    data = isolated_data
    write_json(data / "feeds.json", {"feeds": [
        {"id": "a", "name": "A", "url": "http://a"}]})
    monkeypatch.setattr(fetcher, "fetch_feed_text", lambda url: make_ics(
        vevent("e", "20260615T170000Z", "20260615T180000Z", "x")))
    fetcher.main()
    doc = json.loads((data / "events.json").read_text())
    assert doc["people"] == []                      # absent → empty, not crash


# --------------------------------------------------------------------------- #
# server: people CRUD + feed owner assignment
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.json"
    events = tmp_path / "events.json"
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "FEEDS_PATH", feeds)
    monkeypatch.setattr(server, "EVENTS_PATH", events)
    monkeypatch.setattr(server, "trigger_fetch", lambda: True)   # no real subprocess
    server.app.config["TESTING"] = True
    return server.app.test_client(), feeds, events


def _feeds(feeds_path):
    return json.loads(feeds_path.read_text())


def test_people_add_then_info_lists_them(client):
    c, feeds, _ = client
    write_json(feeds, {"feeds": []})
    r = c.post("/api/people/add", json={"name": "Mom", "color": "#FF8FBE",
                                        "avatar": "👩"})
    person = r.get_json()["person"]
    assert person["id"].startswith("p_")
    assert person["name"] == "Mom" and person["avatar"] == "👩"

    info = c.get("/api/info").get_json()
    assert [p["name"] for p in info["people"]] == ["Mom"]


def test_people_add_defaults_and_validation(client):
    c, feeds, _ = client
    write_json(feeds, {"feeds": []})
    # missing name → 400
    assert c.post("/api/people/add", json={}).status_code == 400
    # bad color + bad avatar fall back to defaults
    p = c.post("/api/people/add",
               json={"name": "X", "color": "nope", "avatar": ""}).get_json()["person"]
    assert p["color"] == server.PALETTE[0]
    assert p["avatar"] == "🙂"


def test_people_update(client):
    c, feeds, _ = client
    write_json(feeds, {"people": [{"id": "p1", "name": "Old", "color": "#111111",
                                   "avatar": "🙂"}], "feeds": []})
    r = c.post("/api/people/update", json={"id": "p1", "name": "New",
                                           "avatar": "🧒", "color": "#46d6b4"})
    p = r.get_json()["person"]
    assert (p["name"], p["avatar"], p["color"]) == ("New", "🧒", "#46d6b4")
    assert c.post("/api/people/update", json={"id": "nope"}).status_code == 404


def test_assign_owner_to_feed(client):
    c, feeds, _ = client
    write_json(feeds, {
        "people": [{"id": "p1", "name": "Mom", "color": "#FF8FBE", "avatar": "👩"}],
        "feeds": [{"id": "f1", "name": "Cal", "url": "http://x", "enabled": True}],
    })
    r = c.post("/api/feeds/update", json={"id": "f1", "owner_id": "p1"})
    assert r.get_json()["ok"] is True
    assert _feeds(feeds)["feeds"][0]["owner_id"] == "p1"
    # clearing with "" / null sets it back to None
    c.post("/api/feeds/update", json={"id": "f1", "owner_id": ""})
    assert _feeds(feeds)["feeds"][0]["owner_id"] is None


def test_assign_unknown_owner_rejected(client):
    c, feeds, _ = client
    write_json(feeds, {"people": [], "feeds": [
        {"id": "f1", "name": "Cal", "url": "http://x"}]})
    r = c.post("/api/feeds/update", json={"id": "f1", "owner_id": "ghost"})
    assert r.status_code == 400
    assert "owner" in r.get_json()["error"]


def test_delete_person_orphans_feeds(client):
    c, feeds, _ = client
    write_json(feeds, {
        "people": [{"id": "p1", "name": "Mom", "color": "#FF8FBE", "avatar": "👩"}],
        "feeds": [{"id": "f1", "name": "Cal", "url": "http://x", "owner_id": "p1"}],
    })
    r = c.post("/api/people/delete", json={"id": "p1"})
    assert r.get_json()["ok"] is True
    doc = _feeds(feeds)
    assert doc["people"] == []
    assert doc["feeds"][0]["owner_id"] is None       # feed orphaned, not deleted
    assert c.post("/api/people/delete", json={"id": "p1"}).status_code == 404


# --------------------------------------------------------------------------- #
# export: .ics categories follow the owner, falling back to the feed
# --------------------------------------------------------------------------- #
def test_ics_categories_use_owner_then_feed(client):
    c, _, events = client
    write_json(events, {
        "generated_at": "2026-06-15T00:00:00-07:00",
        "timezone": "America/Los_Angeles",
        "people": [{"id": "p1", "name": "Benji", "color": "#46d6b4", "avatar": "🧒"}],
        "feeds": [
            {"id": "f1", "name": "Benji iCloud", "owner_id": "p1"},
            {"id": "f2", "name": "Holidays", "owner_id": None},
        ],
        "events": [
            {"id": "e1", "feed_id": "f1", "title": "Soccer",
             "start": "2026-06-15T17:00:00-07:00",
             "end": "2026-06-15T18:00:00-07:00", "all_day": False},
            {"id": "e2", "feed_id": "f2", "title": "July 4",
             "start": "2026-07-04", "end": "2026-07-05", "all_day": True},
        ],
    })
    ics = server.build_ics()
    assert "CATEGORIES:Benji" in ics          # owned event → person name
    assert "CATEGORIES:Holidays" in ics       # unowned event → feed name
    assert "CATEGORIES:Benji iCloud" not in ics
