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
from pathlib import Path

import qrcode
import requests
from flask import Flask, send_file, jsonify, request, Response, abort

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVENTS_PATH = DATA / "events.json"
FEEDS_PATH = DATA / "feeds.json"
INDEX = HERE / "family-calendar.html"
PORT = 8080
WINDOW_SECS = 120  # how long an add-window stays open

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


# --------------------------------------------------------------------------- #
# app + data
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_file(INDEX)


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
    return jsonify({"lan_ip": lan_ip(), "port": PORT,
                    "feeds": load_feeds().get("feeds", [])})


@app.route("/api/feeds/update", methods=["POST"])
def feeds_update():
    """Rename / recolor / enable-disable an existing feed (milestone 5)."""
    body = request.get_json(force=True, silent=True) or {}
    fid = body.get("id")
    doc = load_feeds()
    for f in doc.get("feeds", []):
        if f.get("id") == fid:
            if "name" in body:
                f["name"] = (str(body["name"]).strip()[:30] or f["name"])
            if "color" in body and valid_color(body["color"]):
                f["color"] = body["color"]
            if "enabled" in body:
                f["enabled"] = bool(body["enabled"])
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
# phone-facing HTML (self-contained, lightly themed)
# --------------------------------------------------------------------------- #
def _page(body):
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add a calendar</title><style>
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
_geo = {"lat": None, "lon": None, "city": None}
_weather_cache = {"at": 0.0, "data": None}


def geolocate():
    """lat/lon from env if set, else best-effort IP geolocation (cached)."""
    if _geo["lat"] is not None:
        return _geo
    lat = os.environ.get("FAMILYCAL_LAT")
    lon = os.environ.get("FAMILYCAL_LON")
    if lat and lon:
        _geo.update(lat=float(lat), lon=float(lon),
                    city=os.environ.get("FAMILYCAL_CITY", ""))
        return _geo
    try:
        r = requests.get("http://ip-api.com/json/?fields=lat,lon,city", timeout=6)
        j = r.json()
        _geo.update(lat=j["lat"], lon=j["lon"], city=j.get("city", ""))
    except (requests.RequestException, KeyError, ValueError):
        pass
    return _geo


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
            "current": "temperature_2m,weather_code",
            "temperature_unit": "fahrenheit", "timezone": "auto"})
        cur = r.json()["current"]
        emoji, label = WMO.get(cur["weather_code"], ("🌡️", ""))
        data = {"ok": True, "temp": round(cur["temperature_2m"]),
                "emoji": emoji, "label": label, "city": g.get("city", "")}
        _weather_cache.update(at=time.time(), data=data)
        return jsonify(data)
    except (requests.RequestException, KeyError, ValueError):
        if _weather_cache["data"]:
            return jsonify(_weather_cache["data"])  # stale-but-good
        return jsonify({"ok": False})


if __name__ == "__main__":
    print(f"Family Calendar on http://{lan_ip()}:{PORT}  (and http://localhost:{PORT})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
