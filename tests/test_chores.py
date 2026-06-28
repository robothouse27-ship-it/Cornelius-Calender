"""Box-side chore chart + the phone QR add-window flow.

Chore definitions now live on the box so they can be added from a phone. The
wall still does the daily rotation + per-day "done"; here we cover the storage,
CRUD, and the tap-to-open 2-minute QR window that mirrors the calendar flow.
"""
import json

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "CHORES_PATH", tmp_path / "chores.json")
    monkeypatch.setattr(server, "CHORE_IDEAS_PATH", tmp_path / "chore_ideas.json")
    # fresh, closed window per test (module global otherwise leaks between tests)
    monkeypatch.setattr(server, "chore_window",
                        {"token": None, "expires_at": 0.0, "added": None})
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _chores(tmp_path):
    return json.loads((tmp_path / "chores.json").read_text())["chores"]


# --------------------------------------------------------------------------- #
# storage + CRUD
# --------------------------------------------------------------------------- #
def test_list_seeds_defaults_when_absent(client):
    body = client.get("/api/chores").get_json()
    labels = [c["label"] for c in body["chores"]]
    assert labels and "Take out trash" in labels        # default chart


def test_add_persists_and_staggers_seed(client, tmp_path):
    r = client.post("/api/chores/add", json={"label": "Sweep the porch"})
    chore = r.get_json()["chore"]
    assert chore["id"].startswith("c_")
    assert chore["label"] == "Sweep the porch"
    # default chart had 4 → new seed is 4 (lands on the next person)
    assert chore["seed"] == 4
    assert any(c["label"] == "Sweep the porch" for c in _chores(tmp_path))


def test_add_requires_label(client):
    assert client.post("/api/chores/add", json={}).status_code == 400
    assert client.post("/api/chores/add", json={"label": "   "}).status_code == 400


def test_add_truncates_long_label(client):
    long = "x" * 80
    chore = client.post("/api/chores/add", json={"label": long}).get_json()["chore"]
    assert len(chore["label"]) == 40


def test_update_and_delete(client, tmp_path):
    cid = client.post("/api/chores/add", json={"label": "Original"}).get_json()["chore"]["id"]
    r = client.post("/api/chores/update", json={"id": cid, "label": "Renamed"})
    assert r.get_json()["chore"]["label"] == "Renamed"
    assert client.post("/api/chores/update", json={"id": "nope"}).status_code == 404

    r = client.post("/api/chores/delete", json={"id": cid})
    assert r.get_json()["ok"] is True
    assert all(c["id"] != cid for c in _chores(tmp_path))
    assert client.post("/api/chores/delete", json={"id": cid}).status_code == 404


# --------------------------------------------------------------------------- #
# QR add-window flow
# --------------------------------------------------------------------------- #
def _open_window(client):
    return client.post("/api/chore-window/open").get_json()


def test_window_open_and_status(client):
    w = _open_window(client)
    assert w["token"] and w["url"].endswith("/chore?token=" + w["token"])
    assert w["expires_in"] == server.WINDOW_SECS
    s = client.get("/api/chore-window/status?token=" + w["token"]).get_json()
    assert s["open"] is True and s["remaining"] > 0
    # wrong token reads as closed
    assert client.get("/api/chore-window/status?token=bad").get_json()["open"] is False


def test_qr_requires_valid_token(client):
    w = _open_window(client)
    assert client.get("/api/chore-qr?token=" + w["token"]).status_code == 200
    assert client.get("/api/chore-qr?token=bad").status_code == 404


def test_phone_page_open_vs_closed(client):
    w = _open_window(client)
    open_html = client.get("/chore?token=" + w["token"]).get_data(as_text=True)
    assert "Add a chore" in open_html and 'action="/chore"' in open_html
    closed_html = client.get("/chore?token=bad").get_data(as_text=True)
    assert "Window closed" in closed_html


def test_phone_submit_adds_chore_and_consumes_window(client, tmp_path):
    w = _open_window(client)
    r = client.post("/chore", data={"token": w["token"], "label": "Wipe the table 🧽"})
    assert r.status_code == 200
    assert "All set" in r.get_data(as_text=True)
    assert any(c["label"] == "Wipe the table 🧽" for c in _chores(tmp_path))
    # window now reports the add (wall polls this) and is closed
    s = client.get("/api/chore-window/status?token=" + w["token"]).get_json()
    assert s["added"] == {"label": "Wipe the table 🧽"}
    assert s["open"] is False


def test_phone_submit_rejects_bad_token(client):
    _open_window(client)
    r = client.post("/chore", data={"token": "bad", "label": "Sneaky"})
    assert r.status_code == 403


def test_phone_submit_requires_label(client):
    w = _open_window(client)
    r = client.post("/chore", data={"token": w["token"], "label": "   "})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# bulk paste + quick-add ("ideas") — parity with the grocery list
# --------------------------------------------------------------------------- #
def test_add_many_from_pasted_text(client, tmp_path):
    r = client.post("/api/chores/add-many",
                    json={"text": "- Mow lawn\n2. Clean garage\nTake out recycling"})
    assert r.get_json()["count"] == 3
    labels = [c["label"] for c in _chores(tmp_path)]
    assert "Mow lawn" in labels and "Clean garage" in labels


def test_chore_ideas_defaults_and_set(client):
    assert "Make bed" in client.get("/api/chore-ideas").get_json()["items"]
    r = client.post("/api/chore-ideas/set", json={"items": ["Dishes", "dishes", " Sweep "]})
    assert r.get_json()["items"] == ["Dishes", "Sweep"]          # de-duped + trimmed
    assert client.get("/api/chore-ideas").get_json()["items"] == ["Dishes", "Sweep"]


def test_phone_submit_pasted_list_adds_many(client, tmp_path):
    w = _open_window(client)
    r = client.post("/chore", data={"token": w["token"],
                                    "list_text": "Wash car\nRake leaves"})
    assert r.status_code == 200 and "Added 2 chores" in r.get_data(as_text=True)
    labels = [c["label"] for c in _chores(tmp_path)]
    assert "Wash car" in labels and "Rake leaves" in labels
