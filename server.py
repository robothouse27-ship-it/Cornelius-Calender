#!/usr/bin/env python3
"""
server.py — tiny Flask app for the family-calendar appliance.

Serves the front-end + events.json same-origin (no CORS), and offers a
manual refresh endpoint. The QR-add flow (§5/§7) is stubbed for a later
milestone; the schema and feed handling already support it.

Run:  python3 server.py        # http://0.0.0.0:8080
"""
import json
import socket
import subprocess
import sys
from pathlib import Path

from flask import Flask, send_file, jsonify, Response

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVENTS_PATH = DATA / "events.json"
FEEDS_PATH = DATA / "feeds.json"
INDEX = HERE / "family-calendar.html"
PORT = 8080

app = Flask(__name__)


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


@app.route("/")
def index():
    return send_file(INDEX)


@app.route("/events.json")
def events():
    if EVENTS_PATH.exists():
        return send_file(EVENTS_PATH, mimetype="application/json")
    # graceful empty state before the first fetch
    return jsonify({"generated_at": None, "feeds": [], "events": []})


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """Kick off an immediate fetch (best effort)."""
    try:
        subprocess.Popen([sys.executable, str(HERE / "fetcher.py")])
        return jsonify({"ok": True})
    except OSError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/api/info")
def info():
    feeds = json.loads(FEEDS_PATH.read_text()) if FEEDS_PATH.exists() else {"feeds": []}
    return jsonify({"lan_ip": lan_ip(), "port": PORT, "feeds": feeds.get("feeds", [])})


if __name__ == "__main__":
    print(f"Family Calendar on http://{lan_ip()}:{PORT}  (and http://localhost:{PORT})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
