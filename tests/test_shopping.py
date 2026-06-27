"""Shared family grocery list + the photo-read pipeline.

The grocery list lives on the box (data/shopping.json) so it's shared across
every device; "done" is a server-side boolean (checking milk clears it
everywhere). Stage B adds a phone photo flow: snap a written list, read it
with Claude vision (or Tesseract), then confirm the items on the wall. Here we
cover the CRUD, the pure structure_items() OCR cleaner, and the photo-window /
pending-review / commit flow (with the cloud + local readers stubbed out).
"""
import io
import json

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SHOPPING_PATH", tmp_path / "shopping.json")
    monkeypatch.setattr(server, "UPLOADS_DIR", tmp_path / "uploads")
    # fresh, closed window + empty review per test (module globals leak otherwise)
    monkeypatch.setattr(server, "grocery_window", {"token": None, "expires_at": 0.0})
    monkeypatch.setattr(server, "grocery_review", None)
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _items(tmp_path):
    return json.loads((tmp_path / "shopping.json").read_text())["items"]


# --------------------------------------------------------------------------- #
# storage + CRUD
# --------------------------------------------------------------------------- #
def test_list_empty_when_absent(client):
    assert client.get("/api/shopping").get_json() == {"items": []}


def test_add_persists(client, tmp_path):
    r = client.post("/api/shopping/add", json={"text": "Milk"})
    item = r.get_json()["item"]
    assert item["id"].startswith("g_")
    assert item["text"] == "Milk" and item["done"] is False
    assert any(it["text"] == "Milk" for it in _items(tmp_path))


def test_add_requires_text(client):
    assert client.post("/api/shopping/add", json={}).status_code == 400
    assert client.post("/api/shopping/add", json={"text": "  "}).status_code == 400


def test_add_truncates_long_text(client):
    item = client.post("/api/shopping/add", json={"text": "x" * 90}).get_json()["item"]
    assert len(item["text"]) == 60


def test_update_text_and_done(client, tmp_path):
    iid = client.post("/api/shopping/add", json={"text": "egg"}).get_json()["item"]["id"]
    r = client.post("/api/shopping/update", json={"id": iid, "done": True})
    assert r.get_json()["item"]["done"] is True
    r = client.post("/api/shopping/update", json={"id": iid, "text": "Eggs"})
    assert r.get_json()["item"]["text"] == "Eggs"
    # the boolean is shared server-side, so it survives a reload
    assert [it for it in _items(tmp_path) if it["id"] == iid][0]["done"] is True
    assert client.post("/api/shopping/update", json={"id": "nope"}).status_code == 404


def test_delete(client, tmp_path):
    iid = client.post("/api/shopping/add", json={"text": "Bread"}).get_json()["item"]["id"]
    assert client.post("/api/shopping/delete", json={"id": iid}).get_json()["ok"] is True
    assert all(it["id"] != iid for it in _items(tmp_path))
    assert client.post("/api/shopping/delete", json={"id": iid}).status_code == 404


def test_clear_done_only_removes_bought(client, tmp_path):
    a = client.post("/api/shopping/add", json={"text": "Milk"}).get_json()["item"]["id"]
    client.post("/api/shopping/add", json={"text": "Bread"})
    client.post("/api/shopping/update", json={"id": a, "done": True})
    r = client.post("/api/shopping/clear-done")
    assert r.get_json()["removed"] == 1
    left = [it["text"] for it in _items(tmp_path)]
    assert left == ["Bread"]


# --------------------------------------------------------------------------- #
# structure_items() — the pure OCR-text → clean list cleaner
# --------------------------------------------------------------------------- #
def test_structure_items_strips_bullets_and_checkboxes():
    raw = "- Milk\n* Eggs\n[ ] Bread\n1. Apples\n☐ Butter"
    assert server.structure_items(raw) == ["Milk", "Eggs", "Bread", "Apples", "Butter"]


def test_structure_items_drops_headers_prices_and_junk():
    raw = "Shopping List\nMilk\n$3.99\n\n12\n---\nEggs"
    assert server.structure_items(raw) == ["Milk", "Eggs"]


def test_structure_items_dedupes_case_insensitively():
    assert server.structure_items("Milk\nmilk\nMILK\nEggs") == ["Milk", "Eggs"]


# --------------------------------------------------------------------------- #
# photo window + QR
# --------------------------------------------------------------------------- #
def _open_window(client):
    return client.post("/api/grocery-window/open").get_json()


def test_window_open_and_status(client):
    w = _open_window(client)
    assert w["token"] and w["url"].endswith("/grocery?token=" + w["token"])
    assert "has_key" in w
    s = client.get("/api/grocery-window/status?token=" + w["token"]).get_json()
    assert s["open"] is True and s["received"] is False
    assert client.get("/api/grocery-window/status?token=bad").get_json()["open"] is False


def test_qr_requires_valid_token(client):
    w = _open_window(client)
    assert client.get("/api/grocery-qr?token=" + w["token"]).status_code == 200
    assert client.get("/api/grocery-qr?token=bad").status_code == 404


def test_phone_page_open_vs_closed(client):
    w = _open_window(client)
    html = client.get("/grocery?token=" + w["token"]).get_data(as_text=True)
    assert "Snap" in html and 'enctype="multipart/form-data"' in html and 'name="photo"' in html
    closed = client.get("/grocery?token=bad").get_data(as_text=True)
    assert "Window closed" in closed


# --------------------------------------------------------------------------- #
# photo submit → pending review → commit (readers stubbed)
# --------------------------------------------------------------------------- #
def _fake_photo():
    return (io.BytesIO(b"not-a-real-jpeg"), "list.jpg")


def test_photo_submit_reads_and_stores_review(client, monkeypatch):
    monkeypatch.setattr(server, "preprocess_image", lambda *a, **k: a[0])
    monkeypatch.setattr(server, "read_photo", lambda p: (["Milk", "Eggs"], "local"))
    w = _open_window(client)
    r = client.post("/grocery", data={"token": w["token"], "photo": _fake_photo()},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and "confirm it on your wall" in r.get_data(as_text=True)
    # the wall sees a pending batch (window is now consumed)
    pend = client.get("/api/grocery/pending").get_json()
    assert pend["pending"] is True and pend["items"] == ["Milk", "Eggs"]
    assert client.get("/api/grocery-window/status?token=" + w["token"]).get_json()["open"] is False


def test_photo_submit_rejects_bad_token(client):
    _open_window(client)
    r = client.post("/grocery", data={"token": "bad", "photo": _fake_photo()},
                    content_type="multipart/form-data")
    assert r.status_code == 403


def test_photo_submit_requires_a_file(client):
    w = _open_window(client)
    r = client.post("/grocery", data={"token": w["token"]},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_commit_adds_kept_items_and_clears_review(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "preprocess_image", lambda *a, **k: a[0])
    monkeypatch.setattr(server, "read_photo", lambda p: (["Milk", "Eggs", "junk"], "local"))
    w = _open_window(client)
    client.post("/grocery", data={"token": w["token"], "photo": _fake_photo()},
                content_type="multipart/form-data")
    # the wall edits the batch down to two items, then commits
    r = client.post("/api/grocery/commit", json={"items": ["Milk", "Eggs"]})
    assert r.get_json()["ok"] is True
    assert sorted(it["text"] for it in _items(tmp_path)) == ["Eggs", "Milk"]
    # review is cleared + the photo is deleted
    assert client.get("/api/grocery/pending").get_json()["pending"] is False
    assert not any((tmp_path / "uploads").glob("*"))


def test_dismiss_clears_without_adding(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "preprocess_image", lambda *a, **k: a[0])
    monkeypatch.setattr(server, "read_photo", lambda p: (["Milk"], "local"))
    w = _open_window(client)
    client.post("/grocery", data={"token": w["token"], "photo": _fake_photo()},
                content_type="multipart/form-data")
    assert client.post("/api/grocery/dismiss").get_json()["ok"] is True
    assert client.get("/api/shopping").get_json()["items"] == []
    assert client.post("/api/grocery/dismiss").status_code == 404


def test_commit_with_nothing_pending_404s(client):
    assert client.post("/api/grocery/commit", json={"items": []}).status_code == 404
