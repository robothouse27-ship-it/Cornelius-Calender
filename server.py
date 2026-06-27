#!/usr/bin/env python3
"""
server.py — tiny Flask app for the family-calendar appliance.

Serves the front-end + events.json same-origin (no CORS), a manual refresh
endpoint, and the QR-add flow (§5/§7): tap "+ Add calendar" on the wall, scan
the QR with a phone, paste the calendar link, and it lands in feeds.json with
an immediate refetch. The /add endpoint only works while a 2-minute, one-time
token window is open — so even though it's reachable on the LAN, it only does
anything when someone deliberately taps "Add" at the wall.

Run:  python3 server.py        # http://0.0.0.0:8080
"""
import csv
import hmac
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import qrcode
import requests
from flask import Flask, send_file, jsonify, request, Response, abort

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVENTS_PATH = DATA / "events.json"
FEEDS_PATH = DATA / "feeds.json"
EXPORT_PATH = DATA / "export.json"   # persistent secret token for the family feed
CHORES_PATH = DATA / "chores.json"   # box-side chore chart (rotates daily on the wall)
PHOTOS_DIR = HERE / "photos"        # drop family photos here for sleep mode
ICONS_DIR = HERE / "icons"          # pastel sticker icons (weather/chores/events)
INDEX = HERE / "family-calendar.html"
PORT = 8080
WINDOW_SECS = 120  # how long an add-window stays open
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# candy palette offered on the phone form
PALETTE = ["#FF8FBE", "#6FB8F6", "#46D6B4", "#FFC44D", "#A98CFF", "#FF9E7A"]

app = Flask(__name__)

# single in-memory add-window: opened by the wall, consumed by one phone submit
add_window = {"token": None, "expires_at": 0.0, "added": None}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def lan_ip():
    """Best-effort LAN IP (no traffic actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load_feeds():
    if FEEDS_PATH.exists():
        return json.loads(FEEDS_PATH.read_text())
    return {"feeds": []}


def save_feeds(doc):
    """Atomic write + lock to the owner (§7 security)."""
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, FEEDS_PATH)
    try:
        os.chmod(FEEDS_PATH, 0o600)
    except OSError:
        pass


def window_open():
    return add_window["token"] is not None and time.time() < add_window["expires_at"]


# default chart, seeded only when chores.json doesn't exist yet
DEFAULT_CHORES = [
    {"id": "c_seed1", "label": "Feed the pig 🐷", "seed": 0},
    {"id": "c_seed2", "label": "Tidy play room", "seed": 1},
    {"id": "c_seed3", "label": "Take out trash", "seed": 2},
    {"id": "c_seed4", "label": "Water plants 🌱", "seed": 3},
]


def load_chores():
    if CHORES_PATH.exists():
        try:
            return json.loads(CHORES_PATH.read_text())
        except (ValueError, OSError):
            pass
    return {"chores": [dict(c) for c in DEFAULT_CHORES]}


def save_chores(doc):
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, CHORES_PATH)


def new_chore(label, existing):
    """A chore dict with a staggered seed so it lands on the next person."""
    return {"id": "c_" + uuid.uuid4().hex[:6],
            "label": str(label).strip()[:40],
            "seed": len(existing)}


def trigger_fetch():
    try:
        subprocess.Popen([sys.executable, str(HERE / "fetcher.py")])
        return True
    except OSError:
        return False


def valid_feed_url(url):
    return bool(re.match(r"^(https?|webcal)://", url.strip(), re.I))


def valid_color(c):
    return isinstance(c, str) and bool(re.match(r"^#[0-9a-fA-F]{6}$", c))


def valid_avatar(a):
    """A short emoji/glyph. Allow ZWJ sequences but no newlines or essays."""
    return isinstance(a, str) and 0 < len(a.strip()) <= 8 and "\n" not in a


# --------------------------------------------------------------------------- #
# app + data
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_file(INDEX)


@app.route("/api/version")
def version():
    # mtime of the app file — bumps whenever auto-update pulls new code,
    # so the wall can reload itself without anyone touching it.
    try:
        return jsonify({"v": int(INDEX.stat().st_mtime)})
    except OSError:
        return jsonify({"v": 0})


@app.route("/events.json")
def events():
    if EVENTS_PATH.exists():
        return send_file(EVENTS_PATH, mimetype="application/json")
    return jsonify({"generated_at": None, "feeds": [], "events": []})


@app.route("/api/refresh", methods=["POST"])
def refresh():
    return jsonify({"ok": trigger_fetch()})


@app.route("/api/info")
def info():
    doc = load_feeds()
    return jsonify({"lan_ip": lan_ip(), "port": PORT,
                    "feeds": doc.get("feeds", []),
                    "people": doc.get("people", [])})


# --------------------------------------------------------------------------- #
# people / owners (Phase 2 keystone)
#   A person is {id, name, color, avatar}. Feeds point at a person via
#   owner_id; events resolve their owner through their feed. This is the hinge
#   the per-person lanes, who's-home, and chore rotation hang on.
# --------------------------------------------------------------------------- #
@app.route("/api/people/add", methods=["POST"])
def people_add():
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name", "")).strip()[:30]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    person = {
        "id": "p_" + uuid.uuid4().hex[:6],
        "name": name,
        "color": body["color"] if valid_color(body.get("color")) else PALETTE[0],
        "avatar": body["avatar"].strip() if valid_avatar(body.get("avatar")) else "🙂",
    }
    doc = load_feeds()
    doc.setdefault("people", []).append(person)
    save_feeds(doc)
    return jsonify({"ok": True, "person": person})


@app.route("/api/people/update", methods=["POST"])
def people_update():
    body = request.get_json(force=True, silent=True) or {}
    pid = body.get("id")
    doc = load_feeds()
    for p in doc.get("people", []):
        if p.get("id") == pid:
            if "name" in body:
                p["name"] = (str(body["name"]).strip()[:30] or p["name"])
            if "color" in body and valid_color(body["color"]):
                p["color"] = body["color"]
            if "avatar" in body and valid_avatar(body["avatar"]):
                p["avatar"] = body["avatar"].strip()
            save_feeds(doc)
            trigger_fetch()                      # owner color/name flows to events.json
            return jsonify({"ok": True, "person": p})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/people/delete", methods=["POST"])
def people_delete():
    body = request.get_json(force=True, silent=True) or {}
    pid = body.get("id")
    doc = load_feeds()
    before = len(doc.get("people", []))
    doc["people"] = [p for p in doc.get("people", []) if p.get("id") != pid]
    if len(doc["people"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    # orphan any feeds that pointed at the deleted person
    for f in doc.get("feeds", []):
        if f.get("owner_id") == pid:
            f["owner_id"] = None
    save_feeds(doc)
    trigger_fetch()
    return jsonify({"ok": True})


# how long the merged sync may go without a fresh run before it's "stale".
# The fetch timer runs every 10 min; 35 min ≈ 3 missed cycles.
SYNC_STALE_SECS = 35 * 60


def _iso_age(iso, now):
    """Seconds between an ISO timestamp and now (tz-safe); None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((now - dt).total_seconds())


@app.route("/api/health")
def health():
    """Liveness for the wall + future status UI: last-good sync + per-feed state.

    Reads the same events.json the fetcher writes (it records per-feed
    status/last_ok/error). Unauthenticated like /api/info and deliberately
    leaks no feed URLs — errors are pre-classified by the fetcher.
    """
    now = datetime.now(timezone.utc)
    if not EVENTS_PATH.exists():
        return jsonify({"ok": False, "status": "never_synced",
                        "generated_at": None, "age_seconds": None,
                        "feeds": [], "summary": {"total": 0, "ok": 0, "stale": 0}})
    try:
        doc = json.loads(EVENTS_PATH.read_text())
    except (ValueError, OSError):
        return jsonify({"ok": False, "status": "unreadable",
                        "generated_at": None, "age_seconds": None,
                        "feeds": [], "summary": {"total": 0, "ok": 0, "stale": 0}})

    gen = doc.get("generated_at")
    age = _iso_age(gen, now)
    sync_stale = age is not None and age > SYNC_STALE_SECS

    feeds_out, n_ok, n_stale = [], 0, 0
    for f in doc.get("feeds", []):
        status = f.get("status", "unknown")
        if status == "ok":
            n_ok += 1
        elif status == "stale":
            n_stale += 1
        feeds_out.append({
            "id": f.get("id"), "name": f.get("name"), "status": status,
            "last_ok": f.get("last_ok"),
            "last_ok_age_seconds": _iso_age(f.get("last_ok"), now),
            "events": f.get("count"), "error": f.get("error"),
        })

    healthy = gen is not None and n_stale == 0 and not sync_stale
    return jsonify({
        "ok": healthy,
        "status": "ok" if healthy else "degraded",
        "generated_at": gen,
        "age_seconds": age,
        "sync_stale": sync_stale,
        "timezone": doc.get("timezone"),
        "event_count": len(doc.get("events", [])),
        "feeds": feeds_out,
        "summary": {"total": len(feeds_out), "ok": n_ok, "stale": n_stale},
    })


# --------------------------------------------------------------------------- #
# chores (box-side chart; the wall does the daily rotation + per-day "done")
# --------------------------------------------------------------------------- #
@app.route("/api/chores")
def chores_list():
    return jsonify(load_chores())


@app.route("/api/chores/add", methods=["POST"])
def chores_add():
    body = request.get_json(force=True, silent=True) or {}
    label = str(body.get("label", "")).strip()
    if not label:
        return jsonify({"ok": False, "error": "label required"}), 400
    doc = load_chores()
    chore = new_chore(label, doc.get("chores", []))
    doc.setdefault("chores", []).append(chore)
    save_chores(doc)
    return jsonify({"ok": True, "chore": chore})


@app.route("/api/chores/update", methods=["POST"])
def chores_update():
    body = request.get_json(force=True, silent=True) or {}
    cid = body.get("id")
    doc = load_chores()
    for ch in doc.get("chores", []):
        if ch.get("id") == cid:
            if "label" in body:
                ch["label"] = str(body["label"]).strip()[:40] or ch["label"]
            save_chores(doc)
            return jsonify({"ok": True, "chore": ch})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/chores/delete", methods=["POST"])
def chores_delete():
    body = request.get_json(force=True, silent=True) or {}
    cid = body.get("id")
    doc = load_chores()
    before = len(doc.get("chores", []))
    doc["chores"] = [c for c in doc.get("chores", []) if c.get("id") != cid]
    if len(doc["chores"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    save_chores(doc)
    return jsonify({"ok": True})


@app.route("/api/feeds/update", methods=["POST"])
def feeds_update():
    """Rename / recolor / enable-disable an existing feed (milestone 5)."""
    body = request.get_json(force=True, silent=True) or {}
    fid = body.get("id")
    doc = load_feeds()
    valid_owner_ids = {p.get("id") for p in doc.get("people", [])}
    for f in doc.get("feeds", []):
        if f.get("id") == fid:
            if "name" in body:
                f["name"] = (str(body["name"]).strip()[:30] or f["name"])
            if "color" in body and valid_color(body["color"]):
                f["color"] = body["color"]
            if "enabled" in body:
                f["enabled"] = bool(body["enabled"])
            if "owner_id" in body:
                owner = body["owner_id"]
                if owner in (None, "") or owner in valid_owner_ids:
                    f["owner_id"] = owner or None    # "" / None both clear it
                else:
                    return jsonify({"ok": False, "error": "unknown owner_id"}), 400
            save_feeds(doc)
            trigger_fetch()
            return jsonify({"ok": True, "feed": f})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/feeds/delete", methods=["POST"])
def feeds_delete():
    body = request.get_json(force=True, silent=True) or {}
    fid = body.get("id")
    doc = load_feeds()
    before = len(doc.get("feeds", []))
    doc["feeds"] = [f for f in doc.get("feeds", []) if f.get("id") != fid]
    if len(doc["feeds"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    save_feeds(doc)
    trigger_fetch()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# QR-add flow
# --------------------------------------------------------------------------- #
@app.route("/api/add-window/open", methods=["POST"])
def add_window_open():
    """Wall taps '+ Add calendar': mint a one-time token + 2-min window."""
    token = uuid.uuid4().hex
    add_window.update(token=token, expires_at=time.time() + WINDOW_SECS, added=None)
    url = f"http://{lan_ip()}:{PORT}/add?token={token}"
    return jsonify({"token": token, "url": url, "expires_in": WINDOW_SECS})


@app.route("/api/add-window/status")
def add_window_status():
    """Wall polls this to render the QR countdown and detect a successful add."""
    token = request.args.get("token", "")
    if token != add_window["token"]:
        return jsonify({"open": False, "added": None, "remaining": 0})
    remaining = max(0, int(add_window["expires_at"] - time.time()))
    return jsonify({"open": remaining > 0, "added": add_window["added"],
                    "remaining": remaining})


@app.route("/api/qr")
def api_qr():
    """PNG QR for the open window's add URL (server-side, no JS dep)."""
    token = request.args.get("token", "")
    if token != add_window["token"]:
        abort(404)
    url = f"http://{lan_ip()}:{PORT}/add?token={token}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/add", methods=["GET"])
def add_page():
    """Phone-facing form. Only meaningful while the window is open."""
    token = request.args.get("token", "")
    ok = token == add_window["token"] and window_open()
    return Response(render_add_page(token, ok), mimetype="text/html")


@app.route("/add", methods=["POST"])
def add_submit():
    token = request.form.get("token", "")
    if token != add_window["token"] or not window_open():
        return Response(render_result_page(False, "This add window has closed. "
                        "Tap “+ Add calendar” on the wall again."),
                        mimetype="text/html", status=403)
    url = request.form.get("url", "").strip()
    name = request.form.get("name", "").strip() or "Calendar"
    color = request.form.get("color", "").strip() or PALETTE[0]
    if not valid_feed_url(url):
        return Response(render_result_page(False, "That doesn't look like a "
                        "calendar link. It should start with http(s):// or "
                        "webcal://."), mimetype="text/html", status=400)

    feed = {"id": "f_" + uuid.uuid4().hex[:6], "name": name, "color": color,
            "type": "ics", "url": url, "enabled": True}
    doc = load_feeds()
    doc.setdefault("feeds", []).append(feed)
    save_feeds(doc)

    # consume the window + tell the wall, and refresh immediately
    add_window["added"] = {"name": name, "color": color}
    add_window["expires_at"] = 0
    trigger_fetch()
    return Response(render_result_page(True, f"“{name}” was added. "
                    "It'll appear on the wall in a few seconds."),
                    mimetype="text/html")


# --------------------------------------------------------------------------- #
# QR-add flow for CHORES (mirrors the calendar flow: tap the wall to open a
# one-time 2-min window, scan, type a chore, it lands on the chart)
# --------------------------------------------------------------------------- #
chore_window = {"token": None, "expires_at": 0.0, "added": None}


def chore_window_open():
    return (chore_window["token"] is not None
            and time.time() < chore_window["expires_at"])


@app.route("/api/chore-window/open", methods=["POST"])
def chore_window_open_route():
    token = uuid.uuid4().hex
    chore_window.update(token=token, expires_at=time.time() + WINDOW_SECS, added=None)
    url = f"http://{lan_ip()}:{PORT}/chore?token={token}"
    return jsonify({"token": token, "url": url, "expires_in": WINDOW_SECS})


@app.route("/api/chore-window/status")
def chore_window_status():
    token = request.args.get("token", "")
    if token != chore_window["token"]:
        return jsonify({"open": False, "added": None, "remaining": 0})
    remaining = max(0, int(chore_window["expires_at"] - time.time()))
    return jsonify({"open": remaining > 0, "added": chore_window["added"],
                    "remaining": remaining})


@app.route("/api/chore-qr")
def chore_qr():
    token = request.args.get("token", "")
    if token != chore_window["token"]:
        abort(404)
    url = f"http://{lan_ip()}:{PORT}/chore?token={token}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/chore", methods=["GET"])
def chore_page():
    token = request.args.get("token", "")
    ok = token == chore_window["token"] and chore_window_open()
    return Response(render_chore_page(token, ok), mimetype="text/html")


@app.route("/chore", methods=["POST"])
def chore_submit():
    token = request.form.get("token", "")
    if token != chore_window["token"] or not chore_window_open():
        return Response(render_result_page(False, "This add window has closed. "
                        "Tap “Add chore by phone” on the wall again."),
                        mimetype="text/html", status=403)
    label = request.form.get("label", "").strip()
    if not label:
        return Response(render_result_page(False, "Please type a chore name."),
                        mimetype="text/html", status=400)
    doc = load_chores()
    chore = new_chore(label, doc.get("chores", []))
    doc.setdefault("chores", []).append(chore)
    save_chores(doc)
    chore_window["added"] = {"label": chore["label"]}
    chore_window["expires_at"] = 0
    return Response(render_result_page(True, f"“{chore['label']}” was added to the "
                    "chore chart. It'll show on the wall in a few seconds."),
                    mimetype="text/html")


# --------------------------------------------------------------------------- #
# phone-facing HTML (self-contained, lightly themed)
# --------------------------------------------------------------------------- #
def _page(body, title="Add a calendar"):
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
  :root{{--ink:#3A3357;--soft:#8C84AB;--line:#EFEAFB;--grad:linear-gradient(120deg,#FFB3D1,#B39CFF 50%,#8FD0FF);}}
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--ink);
        background:linear-gradient(160deg,#F3F1FF,#FFF2FA);min-height:100vh;
        display:flex;align-items:flex-start;justify-content:center;padding:22px}}
  .card{{background:#fff;border-radius:26px;box-shadow:0 14px 40px rgba(94,72,150,.18);
         width:min(440px,100%);padding:26px;margin-top:6vh}}
  h1{{font-size:24px;margin:0 0 4px}} .sub{{color:var(--soft);font-weight:600;margin:0 0 18px;font-size:14px}}
  label{{display:block;font-weight:800;font-size:13px;color:var(--soft);margin:16px 0 6px}}
  input[type=url],input[type=text]{{width:100%;border:2px solid var(--line);border-radius:14px;
         padding:13px 14px;font-size:16px;font-weight:600;color:var(--ink);background:#FBFAFF}}
  input:focus{{outline:none;border-color:#A98CFF}}
  .sw{{display:flex;gap:12px;flex-wrap:wrap;margin-top:4px}}
  .sw label{{margin:0;cursor:pointer}} .sw input{{position:absolute;opacity:0}}
  .dot{{width:40px;height:40px;border-radius:50%;border:4px solid transparent;display:block;transition:.12s}}
  .sw input:checked+.dot{{border-color:var(--ink);transform:scale(1.08)}}
  button{{width:100%;margin-top:22px;border:0;border-radius:16px;padding:15px;font-size:17px;
          font-weight:800;color:#fff;background:var(--grad);cursor:pointer}}
  .hint{{font-size:12.5px;color:var(--soft);margin-top:14px;line-height:1.5}}
  .big{{font-size:46px;text-align:center;margin:6px 0 10px}}
</style></head><body><div class="card">{body}</div></body></html>"""


def render_add_page(token, ok):
    if not ok:
        return _page('<div class="big">⌛</div><h1>Window closed</h1>'
                     '<p class="sub">Tap “+ Add calendar” on the wall to start again.</p>')
    swatches = "".join(
        f'<label><input type="radio" name="color" value="{c}"'
        f'{" checked" if i == 0 else ""}><span class="dot" style="background:{c}"></span></label>'
        for i, c in enumerate(PALETTE))
    return _page(f"""
      <h1>Add a calendar ✨</h1>
      <p class="sub">Paste a calendar's share link and give it a name &amp; color.</p>
      <form method="POST" action="/add">
        <input type="hidden" name="token" value="{token}">
        <label>Calendar link</label>
        <input type="url" name="url" placeholder="https://… or webcal://…" required
               autocapitalize="off" autocorrect="off" spellcheck="false">
        <label>Name</label>
        <input type="text" name="name" placeholder="Mom" maxlength="20" required>
        <label>Color</label>
        <div class="sw">{swatches}</div>
        <button type="submit">Add to the wall</button>
      </form>
      <p class="hint"><b>Google:</b> Calendar settings → Integrate calendar →
      “Secret address in iCal format”.<br>
      <b>iCloud:</b> share a calendar → Public Calendar → copy the webcal link.</p>
    """)


def render_result_page(ok, msg):
    icon = "🎉" if ok else "⚠️"
    title = "All set!" if ok else "Hmm…"
    return _page(f'<div class="big">{icon}</div><h1>{title}</h1>'
                 f'<p class="sub">{msg}</p>')


def render_chore_page(token, ok):
    if not ok:
        return _page('<div class="big">⌛</div><h1>Window closed</h1>'
                     '<p class="sub">Tap “Add chore by phone” on the wall to start again.</p>',
                     title="Add a chore")
    return _page(f"""
      <h1>Add a chore 🧹</h1>
      <p class="sub">Type a chore for the family chart. It rotates through everyone,
      a new person each day.</p>
      <form method="POST" action="/chore">
        <input type="hidden" name="token" value="{token}">
        <label>Chore</label>
        <input type="text" name="label" placeholder="Take out the trash" maxlength="40"
               required autocapitalize="sentences">
        <button type="submit">Add to the chart</button>
      </form>
      <p class="hint">Tip: end it with an emoji and it becomes a sticker on the
      wall — e.g. “Water plants 🌱”.</p>
    """, title="Add a chore")


# --------------------------------------------------------------------------- #
# export / phone sync (docs-export-architecture.md)
#   Re-publish the already-merged events.json as a single subscribable family
#   .ics (phones "Add calendar by URL"), plus plain .ics/.csv downloads as a
#   backstop. A persistent, rotatable secret token makes the feed URL an
#   unguessable secret — treat it like Google's "secret iCal address".
# --------------------------------------------------------------------------- #
def export_token(rotate=False):
    """Load the persistent feed token (creating it on first use, or rotating)."""
    if not rotate and EXPORT_PATH.exists():
        try:
            return json.loads(EXPORT_PATH.read_text())["token"]
        except (ValueError, KeyError, OSError):
            pass
    tok = uuid.uuid4().hex
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump({"token": tok}, fh)
    os.replace(tmp, EXPORT_PATH)
    try:
        os.chmod(EXPORT_PATH, 0o600)
    except OSError:
        pass
    return tok


def _token_ok():
    return hmac.compare_digest(request.args.get("token", ""), export_token())


def _feed_urls():
    host = f"{lan_ip()}:{PORT}"
    tok = export_token()
    path = f"/feed/family.ics?token={tok}"
    return {
        "token": tok,
        "webcal": f"webcal://{host}{path}",       # tap-to-subscribe on phones
        "https": f"http://{host}{path}",          # same feed over http
        "ics_download": f"http://{host}/export/family.ics?token={tok}",
        "csv_download": f"http://{host}/export/family.csv?token={tok}",
    }


def _ics_escape(s):
    return (str(s or "")
            .replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n").replace("\r", ""))


def _fold(line):
    """RFC 5545 line folding to <=75 octets, never splitting a UTF-8 char."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start, limit = [], 0, 75
    while len(raw) - start > limit:
        cut = start + limit
        while raw[cut] & 0xC0 == 0x80:   # don't split a multibyte sequence
            cut -= 1
        chunks.append(raw[start:cut])
        start, limit = cut, 74           # continuation lines carry a leading space
    chunks.append(raw[start:])
    return "\r\n ".join(c.decode("utf-8") for c in chunks)


def _ics_dt(iso):
    """Timed event → UTC 'Z' stamp (phones localize it themselves)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_date(iso):
    return datetime.fromisoformat(iso).strftime("%Y%m%d")


def _load_events():
    doc = json.loads(EVENTS_PATH.read_text()) if EVENTS_PATH.exists() else {}
    feeds = {f["id"]: f for f in doc.get("feeds", [])}
    return doc, feeds


def build_ics():
    """Merged family .ics from events.json (one calendar, person = category)."""
    doc, feeds = _load_events()
    people = {p["id"]: p for p in doc.get("people", [])}
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//Cornelius Family Calendar//Wall//EN", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:Family Calendar",
           f"X-WR-TIMEZONE:{doc.get('timezone', 'America/Los_Angeles')}"]
    for e in doc.get("events", []):
        feed = feeds.get(e.get("feed_id"), {})
        # categorize by owner (a person) when assigned, else the feed itself
        owner = people.get(feed.get("owner_id"))
        name = (owner or feed).get("name", "")
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{e.get('id', 'evt_' + uuid.uuid4().hex[:8])}@familycal")
        out.append(f"DTSTAMP:{now}")
        if e.get("all_day"):
            out.append("DTSTART;VALUE=DATE:" + _ics_date(e["start"]))
            if e.get("end"):
                out.append("DTEND;VALUE=DATE:" + _ics_date(e["end"]))
        else:
            out.append("DTSTART:" + _ics_dt(e["start"]))
            if e.get("end"):
                out.append("DTEND:" + _ics_dt(e["end"]))
        out.append("SUMMARY:" + _ics_escape(e.get("title", "(busy)")))
        if name:
            out.append("CATEGORIES:" + _ics_escape(name))
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in out) + "\r\n"


def build_csv():
    doc, feeds = _load_events()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Calendar", "Title", "Start", "End", "All day"])
    for e in doc.get("events", []):
        w.writerow([feeds.get(e.get("feed_id"), {}).get("name", ""),
                    e.get("title", ""), e.get("start", ""), e.get("end", ""),
                    "yes" if e.get("all_day") else "no"])
    return buf.getvalue()


@app.route("/api/feed/info")
def feed_info():
    """Wall reads this to render the subscribe-QR + export links."""
    return jsonify(_feed_urls())


@app.route("/api/feed/qr")
def feed_qr():
    """PNG QR of the webcal:// subscribe URL (server-side, no JS dep)."""
    img = qrcode.make(_feed_urls()["webcal"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/feed/rotate", methods=["POST"])
def feed_rotate():
    """Invalidate the old feed URL and mint a fresh one (security §4)."""
    return jsonify({"ok": True, "feed": _feed_urls() if export_token(rotate=True) else None})


@app.route("/feed/family.ics")
def feed_family():
    if not _token_ok():
        abort(403)
    return Response(build_ics(), mimetype="text/calendar; charset=utf-8")


@app.route("/export/family.ics")
def export_ics():
    if not _token_ok():
        abort(403)
    return Response(build_ics(), mimetype="text/calendar; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=family-calendar.ics"})


@app.route("/export/family.csv")
def export_csv():
    if not _token_ok():
        abort(403)
    return Response(build_csv(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=family-calendar.csv"})


# --------------------------------------------------------------------------- #
# weather (Open-Meteo, no API key) with IP-based geolocation + caching
# --------------------------------------------------------------------------- #
# WMO weather codes → (emoji, short label)
WMO = {
    0: ("☀️", "Clear"), 1: ("🌤️", "Mostly clear"), 2: ("⛅", "Partly cloudy"),
    3: ("☁️", "Cloudy"), 45: ("🌫️", "Fog"), 48: ("🌫️", "Fog"),
    51: ("🌦️", "Drizzle"), 53: ("🌦️", "Drizzle"), 55: ("🌦️", "Drizzle"),
    61: ("🌧️", "Rain"), 63: ("🌧️", "Rain"), 65: ("🌧️", "Heavy rain"),
    66: ("🌧️", "Freezing rain"), 67: ("🌧️", "Freezing rain"),
    71: ("🌨️", "Snow"), 73: ("🌨️", "Snow"), 75: ("❄️", "Heavy snow"),
    77: ("🌨️", "Snow grains"), 80: ("🌦️", "Showers"), 81: ("🌦️", "Showers"),
    82: ("⛈️", "Heavy showers"), 85: ("🌨️", "Snow showers"),
    86: ("🌨️", "Snow showers"), 95: ("⛈️", "Thunderstorm"),
    96: ("⛈️", "Thunderstorm"), 99: ("⛈️", "Thunderstorm"),
}
# WMO weather code → sticker icon name (see icons/). Clear/partly skies pick a
# night variant when it's dark out (is_day == 0).
WMO_ICON = {
    0: "wx_clear", 1: "wx_clear", 2: "wx_partly", 3: "wx_cloudy",
    45: "wx_fog", 48: "wx_fog",
    51: "wx_rain", 53: "wx_rain", 55: "wx_rain", 56: "wx_rain", 57: "wx_rain",
    61: "wx_rain", 63: "wx_rain", 65: "wx_rain", 66: "wx_rain", 67: "wx_rain",
    71: "wx_snow", 73: "wx_snow", 75: "wx_snow", 77: "wx_snow",
    80: "wx_rain", 81: "wx_rain", 82: "wx_storm",
    85: "wx_snow", 86: "wx_snow",
    95: "wx_storm", 96: "wx_storm", 99: "wx_storm",
}


def wx_icon(code, is_day):
    name = WMO_ICON.get(code, "wx_partly")
    if name == "wx_clear":
        return "wx_clear_day" if is_day else "wx_clear_night"
    return name


# Pinned location: Montgomery-Gibbs Executive Airport, Aero Drive, San Diego.
# The wall is a fixed device, so a hard-pinned spot beats unreliable IP
# geolocation (which tends to resolve to the ISP's city). Override with env.
DEFAULT_LAT, DEFAULT_LON, DEFAULT_CITY = 32.8157, -117.1397, "San Diego"

_geo = {"lat": None, "lon": None, "city": None}
_weather_cache = {"at": 0.0, "data": None}


def geolocate():
    """lat/lon from env if set, else the pinned San Diego default (cached)."""
    if _geo["lat"] is not None:
        return _geo
    lat = os.environ.get("FAMILYCAL_LAT")
    lon = os.environ.get("FAMILYCAL_LON")
    if lat and lon:
        _geo.update(lat=float(lat), lon=float(lon),
                    city=os.environ.get("FAMILYCAL_CITY", DEFAULT_CITY))
    else:
        _geo.update(lat=DEFAULT_LAT, lon=DEFAULT_LON, city=DEFAULT_CITY)
    return _geo


@app.route("/api/photos")
def photos():
    """List family photos for sleep-mode slideshow (empty list = use gradients)."""
    if not PHOTOS_DIR.is_dir():
        return jsonify({"photos": []})
    names = sorted(p.name for p in PHOTOS_DIR.iterdir()
                   if p.suffix.lower() in PHOTO_EXTS)
    return jsonify({"photos": ["/photos/" + n for n in names]})


@app.route("/photos/<path:fn>")
def photo_file(fn):
    p = (PHOTOS_DIR / fn).resolve()
    if PHOTOS_DIR not in p.parents or not p.is_file():  # no path traversal
        abort(404)
    return send_file(p)


@app.route("/icons/<path:fn>")
def icon_file(fn):
    p = (ICONS_DIR / fn).resolve()
    if ICONS_DIR not in p.parents or not p.is_file():   # no path traversal
        abort(404)
    return send_file(p)


@app.route("/api/weather")
def weather():
    """Current conditions in °F. Cached ~15 min so we don't hammer the API."""
    if _weather_cache["data"] and time.time() - _weather_cache["at"] < 900:
        return jsonify(_weather_cache["data"])
    g = geolocate()
    if g["lat"] is None:
        return jsonify({"ok": False})
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
            "latitude": g["lat"], "longitude": g["lon"],
            "current": "temperature_2m,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit", "timezone": "auto",
            "forecast_days": 5})
        j = r.json()
        cur = j["current"]
        emoji, label = WMO.get(cur["weather_code"], ("🌡️", ""))
        daily = []
        d = j.get("daily", {})
        for i, date in enumerate(d.get("time", [])):
            code = d["weather_code"][i]
            de, dl = WMO.get(code, ("🌡️", ""))
            daily.append({"date": date, "code": code, "emoji": de, "label": dl,
                          "hi": round(d["temperature_2m_max"][i]),
                          "lo": round(d["temperature_2m_min"][i]),
                          "icon": wx_icon(code, 1)})
        data = {"ok": True, "temp": round(cur["temperature_2m"]),
                "emoji": emoji, "label": label, "city": g.get("city", ""),
                "icon": wx_icon(cur["weather_code"], cur.get("is_day", 1)),
                "daily": daily}
        _weather_cache.update(at=time.time(), data=data)
        return jsonify(data)
    except (requests.RequestException, KeyError, ValueError):
        if _weather_cache["data"]:
            return jsonify(_weather_cache["data"])  # stale-but-good
        return jsonify({"ok": False})


if __name__ == "__main__":
    print(f"Family Calendar on http://{lan_ip()}:{PORT}  (and http://localhost:{PORT})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
