"""Family birthdays — shared list the wall uses to pop a 🎂 banner + confetti
on the morning of one. Year is optional (used to show the age they're turning).
Covers storage, CRUD, validation, and field clamping.
"""
import json

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "BIRTHDAYS_PATH", tmp_path / "birthdays.json")
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _items(tmp_path):
    return json.loads((tmp_path / "birthdays.json").read_text())["items"]


def test_list_empty_when_absent(client):
    assert client.get("/api/birthdays").get_json() == {"items": []}


def test_add_with_year(client, tmp_path):
    item = client.post("/api/birthdays/add",
                       json={"name": "Mom", "month": 6, "day": 28, "year": 1985}
                       ).get_json()["item"]
    assert item["id"].startswith("b_")
    assert item["name"] == "Mom" and item["month"] == 6 and item["day"] == 28
    assert item["year"] == 1985
    assert any(b["name"] == "Mom" for b in _items(tmp_path))


def test_add_without_year_is_none(client):
    item = client.post("/api/birthdays/add",
                       json={"name": "Kiddo", "month": 12, "day": 25}).get_json()["item"]
    assert item["year"] is None


def test_add_requires_name_month_day(client):
    assert client.post("/api/birthdays/add", json={"month": 3, "day": 4}).status_code == 400
    assert client.post("/api/birthdays/add", json={"name": "X", "day": 4}).status_code == 400
    assert client.post("/api/birthdays/add", json={"name": "X", "month": 3}).status_code == 400


def test_add_clamps_out_of_range(client):
    item = client.post("/api/birthdays/add",
                       json={"name": "Edge", "month": 99, "day": 99}).get_json()["item"]
    assert item["month"] == 12 and item["day"] == 31


def test_name_truncated(client):
    item = client.post("/api/birthdays/add",
                       json={"name": "y" * 50, "month": 1, "day": 1}).get_json()["item"]
    assert len(item["name"]) == 30


def test_update_fields_and_clear_year(client):
    bid = client.post("/api/birthdays/add",
                      json={"name": "Sam", "month": 1, "day": 2, "year": 2010}
                      ).get_json()["item"]["id"]
    r = client.post("/api/birthdays/update",
                    json={"id": bid, "name": "Samuel", "month": 5, "day": 9})
    b = r.get_json()["item"]
    assert b["name"] == "Samuel" and b["month"] == 5 and b["day"] == 9
    # passing year:null clears it ("age unknown")
    b = client.post("/api/birthdays/update", json={"id": bid, "year": None}).get_json()["item"]
    assert b["year"] is None
    assert client.post("/api/birthdays/update", json={"id": "nope"}).status_code == 404


def test_delete(client, tmp_path):
    bid = client.post("/api/birthdays/add",
                      json={"name": "Gone", "month": 7, "day": 7}).get_json()["item"]["id"]
    assert client.post("/api/birthdays/delete", json={"id": bid}).get_json()["ok"] is True
    assert all(b["id"] != bid for b in _items(tmp_path))
    assert client.post("/api/birthdays/delete", json={"id": bid}).status_code == 404
