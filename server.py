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
import html
import io
import json
import os
import random
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
SHOPPING_PATH = DATA / "shopping.json"  # shared family grocery list (done = server-side)
STAPLES_PATH = DATA / "staples.json"  # "usuals": one-tap re-add of common items
CHORE_IDEAS_PATH = DATA / "chore_ideas.json"  # one-tap common chores ("quick add")
BIRTHDAYS_PATH = DATA / "birthdays.json"  # family birthdays → wall banner + confetti
UPLOADS_DIR = HERE / "uploads"       # phone-snapped list photos, pending review (gitignored)
PHOTOS_DIR = HERE / "photos"        # drop family photos here for sleep mode
ICONS_DIR = HERE / "icons"          # pastel sticker icons (weather/chores/events)
KIDS_DIR = HERE / "kids"            # cartoon avatars for the First Five widget
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


# --- "quick add" common chores: one-tap chips on the wall (like grocery usuals) #
DEFAULT_CHORE_IDEAS = ["Take out trash", "Make bed", "Do dishes", "Walk the dog 🐶",
                       "Vacuum", "Laundry", "Set the table", "Feed pets 🐾"]


def load_chore_ideas():
    if CHORE_IDEAS_PATH.exists():
        try:
            doc = json.loads(CHORE_IDEAS_PATH.read_text())
            items = [str(x).strip()[:40] for x in doc.get("items", []) if str(x).strip()]
            return {"items": items}
        except (ValueError, OSError):
            pass
    return {"items": list(DEFAULT_CHORE_IDEAS)}


def save_chore_ideas(doc):
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, CHORE_IDEAS_PATH)


# --- shared grocery list (one family list; "done" is a server-side boolean so
#     checking milk clears it on every device) ------------------------------- #
# Items carry a store SECTION (cat) and a QUANTITY (qty). Sections are a fixed,
# store-walk-ordered taxonomy so the wall + printed list group the way you shop.
# (id, label, emoji) — display order is shopping order; "other" is the catch-all.
GROCERY_SECTIONS = [
    ("produce",   "Produce",         "🥬"),
    ("bakery",    "Bakery",          "🥖"),
    ("meat",      "Meat & Seafood",  "🥩"),
    ("dairy",     "Dairy & Eggs",    "🥚"),
    ("frozen",    "Frozen",          "🧊"),
    ("pantry",    "Pantry",          "🥫"),
    ("snacks",    "Snacks",          "🍿"),
    ("drinks",    "Drinks",          "🥤"),
    ("household", "Household",        "🧽"),
    ("personal",  "Personal care",   "🧴"),
    ("other",     "Other",           "🛒"),
]
GROCERY_SECTION_IDS = {s[0] for s in GROCERY_SECTIONS}

# Local keyword → section map (free, offline, instant — no API cost). Good enough
# for groceries; anything unknown falls to "other" and can be re-tagged on the wall.
# Match order matters: frozen before dairy so "ice cream" doesn't read as "cream".
_CAT_KEYWORDS = [
    ("frozen",    ["frozen", "ice cream", "popsicle", "waffle", "frozen pizza"]),
    ("produce",   ["apple", "banana", "orange", "lemon", "lime", "grape", "berry",
                   "strawberr", "blueberr", "raspberr", "melon", "watermelon",
                   "peach", "pear", "plum", "mango", "avocado", "tomato", "potato",
                   "onion", "garlic", "carrot", "celery", "lettuce", "spinach",
                   "kale", "broccoli", "cauliflower", "cucumber", "pepper",
                   "zucchini", "squash", "mushroom", "corn", "peas", "green bean",
                   "cabbage", "ginger", "cilantro", "parsley", "basil", "herb",
                   "salad", "fruit", "veggie", "vegetable", "eggplant", "kiwi"]),
    ("bakery",    ["bread", "bagel", "bun", "roll", "tortilla", "pita", "croissant",
                   "muffin", "donut", "doughnut", "cake", "pastry", "baguette",
                   "naan", "english muffin"]),
    ("meat",      ["chicken", "beef", "steak", "pork", "bacon", "sausage", "turkey",
                   "ham", "lamb", "fish", "salmon", "tuna steak", "shrimp", "crab",
                   "cod", "tilapia", "ground", "mince", "meat", "hot dog", "deli"]),
    ("pantry",    ["rice", "pasta", "noodle", "flour", "sugar", "oil", "vinegar",
                   "sauce", "ketchup", "mustard", "mayo", "salt", "spice", "cereal",
                   "oat", "oatmeal", "bean", "lentil", "soup", "canned", "can of",
                   "peanut butter", "jam", "jelly", "honey", "syrup", "broth",
                   "stock", "salsa", "granola", "coffee", "tea", "cocoa", "baking"]),
    ("dairy",     ["milk", "cheese", "yogurt", "yoghurt", "butter", "cream", "egg",
                   "sour cream", "cottage", "margarine", "half and half", "creamer"]),
    ("drinks",    ["water", "juice", "soda", "cola", "sparkling", "beer",
                   "wine", "seltzer", "gatorade", "lemonade", "kombucha", "drink",
                   "energy drink"]),
    ("snacks",    ["chip", "cookie", "candy", "chocolate", "cracker", "popcorn",
                   "pretzel", "snack", "nuts", "trail mix", "granola bar",
                   "fruit snack", "gum", "jerky"]),
    ("household", ["paper towel", "toilet paper", "tissue", "napkin", "trash bag",
                   "detergent", "dish soap", "cleaner", "bleach", "sponge", "foil",
                   "wrap", "ziploc", "laundry", "fabric softener", "light bulb",
                   "batteries", "trash"]),
    ("personal",  ["shampoo", "conditioner", "toothpaste", "toothbrush", "deodorant",
                   "lotion", "razor", "shaving", "body wash", "floss", "mouthwash",
                   "cotton", "bandage", "medicine", "vitamin", "sunscreen", "makeup",
                   "feminine", "diaper", "wipes", "lip balm", "soap"]),
]


def categorize(text):
    """Best-effort store section for an item name (local, offline)."""
    t = str(text or "").lower()
    for cat, kws in _CAT_KEYWORDS:
        if any(re.search(r"\b" + re.escape(kw), t) for kw in kws):
            return cat
    return "other"


def _norm_item(it):
    """Backfill section/qty on items written before this feature."""
    it["qty"] = max(1, int(it.get("qty", 1) or 1))
    cat = it.get("cat")
    if cat not in GROCERY_SECTION_IDS:
        cat = categorize(it.get("text", ""))
    it["cat"] = cat
    it["done"] = bool(it.get("done"))
    return it


def load_shopping():
    if SHOPPING_PATH.exists():
        try:
            doc = json.loads(SHOPPING_PATH.read_text())
            for it in doc.get("items", []):
                _norm_item(it)
            return doc
        except (ValueError, OSError):
            pass
    return {"items": []}


def save_shopping(doc):
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, SHOPPING_PATH)


def new_grocery(text, qty=1, cat=None):
    text = str(text).strip()[:60]
    return {"id": "g_" + uuid.uuid4().hex[:6],
            "text": text,
            "qty": max(1, int(qty or 1)),
            "cat": cat if cat in GROCERY_SECTION_IDS else categorize(text),
            "done": False}


def add_or_merge(doc, text, qty=1, cat=None):
    """Add an item, or bump the quantity of a matching un-bought item already on
    the list (case-insensitive). Returns (item, merged_bool)."""
    text = str(text).strip()[:60]
    if not text:
        return None, False
    qty = max(1, int(qty or 1))
    key = text.lower()
    for it in doc.setdefault("items", []):
        if not it.get("done") and it.get("text", "").lower() == key:
            it["qty"] = max(1, int(it.get("qty", 1) or 1)) + qty
            if cat in GROCERY_SECTION_IDS:
                it["cat"] = cat
            return it, True
    item = new_grocery(text, qty, cat)
    doc["items"].append(item)
    return item, False


# --- "usuals": a small set of common items you re-add with one tap ---------- #
DEFAULT_STAPLES = ["Milk", "Eggs", "Bread", "Butter", "Bananas", "Coffee"]


def load_staples():
    if STAPLES_PATH.exists():
        try:
            doc = json.loads(STAPLES_PATH.read_text())
            items = [str(x).strip()[:60] for x in doc.get("items", []) if str(x).strip()]
            return {"items": items}
        except (ValueError, OSError):
            pass
    return {"items": list(DEFAULT_STAPLES)}


def save_staples(doc):
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, STAPLES_PATH)


# --- family birthdays (shared) → the wall shows a banner + confetti on the day #
def load_birthdays():
    if BIRTHDAYS_PATH.exists():
        try:
            doc = json.loads(BIRTHDAYS_PATH.read_text())
            return {"items": [b for b in doc.get("items", []) if b.get("name")]}
        except (ValueError, OSError):
            pass
    return {"items": []}


def save_birthdays(doc):
    fd, tmp = tempfile.mkstemp(dir=str(DATA), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, BIRTHDAYS_PATH)


def _clamp_int(v, lo, hi, default=None):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def new_birthday(name, month, day, year=None):
    return {"id": "b_" + uuid.uuid4().hex[:6],
            "name": str(name).strip()[:30],
            "month": _clamp_int(month, 1, 12),
            "day": _clamp_int(day, 1, 31),
            "year": _clamp_int(year, 1900, 2200)}    # optional → None for "age unknown"


# --------------------------------------------------------------------------- #
# photo → grocery items (Stage B): preprocess, then read with Claude vision
# (primary) or Tesseract (free on-box fallback). structure_items() is the pure
# text→list cleaner shared by the fallback path; it's unit-tested.
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = ("This photo shows a handwritten or printed grocery/shopping "
                  "list. List the grocery items, one clean entry each. Ignore "
                  "headers, dates, prices, quantities-only lines, and anything "
                  "crossed out. Return just the item names.")

# a line is junk if it's empty, all punctuation, or an obvious header/price
_PRICE_RE = re.compile(r"^[\$£€]?\d+([.,]\d+)?\s*$")
_HEADER_RE = re.compile(r"^(shopping|grocery|groceries|to\s*buy|todo|to\s*do|"
                        r"notes?)(\s+list)?[:\s]*$", re.I)
# a bracketed checkbox like "[ ]", "[x]", "(✓)" at the very start of a line
_CHECKBOX_RE = re.compile(r"^[\[(]\s*[xX✓✔ ]?\s*[\])]\s*")
# leading bullets, checkbox glyphs, stray brackets — anything before the item
_LEADING_RE = re.compile(r"^[\s\-–—*•·▪◦‣○●□■☐☑☒✔✓\[\]\(\)]+")
_NUM_RE = re.compile(r"^\d+[.)]?\s+")                   # "1. ", "2) ", "3 " quantities/numbering


def structure_items(text):
    """Turn raw OCR text into a clean, de-duped list of grocery items."""
    out, seen = [], set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        line = _CHECKBOX_RE.sub("", line)              # "[ ] " / "[x] " / "(✓) "
        line = _LEADING_RE.sub("", line)               # bullet / checkbox glyph
        line = _NUM_RE.sub("", line)                   # leading number / quantity
        line = _LEADING_RE.sub("", line).strip()       # any trailing bullet remnant
        if not line or len(line) < 2:
            continue
        if _PRICE_RE.match(line) or _HEADER_RE.match(line):
            continue
        if not re.search(r"[A-Za-z]", line):           # drop pure punctuation/digits
            continue
        line = line[:60]
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def preprocess_image(src_path, dst_path=None):
    """EXIF-rotate, RGB, shrink to 1600px, re-save as JPEG (drops EXIF/GPS).
    Returns the path written, or the original path if Pillow is unavailable."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return src_path
    dst_path = dst_path or src_path
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1600, 1600))
        im.save(dst_path, format="JPEG", quality=85)
    return dst_path


def vision_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def read_cloud(path):
    """Primary reader: Claude vision → clean item list (structured output)."""
    import base64
    import anthropic

    # Haiku reads a grocery list well and is ~5x cheaper than Opus; override
    # with FAMILYCAL_VISION_MODEL if a tougher photo ever needs a stronger model.
    model = os.environ.get("FAMILYCAL_VISION_MODEL", "claude-haiku-4-5")
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")
    client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        }}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": EXTRACT_PROMPT},
        ]}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    items = (json.loads(text) or {}).get("items", [])
    # final cleanup pass (trim, cap length, de-dupe) — cheap and consistent
    out, seen = [], set()
    for it in items:
        s = str(it).strip()[:60]
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def read_local(path):
    """Free on-box fallback: Tesseract OCR → structure_items()."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return []
    try:
        with Image.open(path) as im:
            raw = pytesseract.image_to_string(im)
    except Exception:
        return []
    return structure_items(raw)


def read_photo(path):
    """Read items from a list photo. Cloud first (if a key is set), else local.
    Returns (items, source) where source is 'cloud' or 'local'."""
    if vision_key():
        try:
            return read_cloud(path), "cloud"
        except Exception:
            pass                          # offline / SDK missing / API error → fall back
    return read_local(path), "local"


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


@app.route("/api/chores/add-many", methods=["POST"])
def chores_add_many():
    """Bulk add from pasted text (or an explicit list), one chore per line."""
    body = request.get_json(force=True, silent=True) or {}
    if isinstance(body.get("items"), list):
        labels = [str(x) for x in body["items"]]
    else:
        labels = structure_items(body.get("text", ""))
    doc = load_chores()
    added = []
    for label in labels:
        label = str(label).strip()
        if not label:
            continue
        chore = new_chore(label, doc.get("chores", []))
        doc.setdefault("chores", []).append(chore)
        added.append(chore)
    save_chores(doc)
    return jsonify({"ok": True, "added": added, "count": len(added)})


@app.route("/api/chore-ideas")
def chore_ideas_list():
    return jsonify(load_chore_ideas())


@app.route("/api/chore-ideas/set", methods=["POST"])
def chore_ideas_set():
    """Replace the whole quick-add set (the Settings editor saves the full list)."""
    body = request.get_json(force=True, silent=True) or {}
    items, seen = [], set()
    for x in body.get("items", []):
        t = str(x).strip()[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            items.append(t)
    save_chore_ideas({"items": items})
    return jsonify({"ok": True, "items": items})


# --------------------------------------------------------------------------- #
# family birthdays — the wall pops a 🎂 banner + confetti on the morning of one
# --------------------------------------------------------------------------- #
@app.route("/api/birthdays")
def birthdays_list():
    return jsonify(load_birthdays())


@app.route("/api/birthdays/add", methods=["POST"])
def birthdays_add():
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name", "")).strip()
    bday = new_birthday(name, body.get("month"), body.get("day"), body.get("year"))
    if not bday["name"] or bday["month"] is None or bday["day"] is None:
        return jsonify({"ok": False, "error": "name, month and day required"}), 400
    doc = load_birthdays()
    doc.setdefault("items", []).append(bday)
    save_birthdays(doc)
    return jsonify({"ok": True, "item": bday})


@app.route("/api/birthdays/update", methods=["POST"])
def birthdays_update():
    body = request.get_json(force=True, silent=True) or {}
    bid = body.get("id")
    doc = load_birthdays()
    for b in doc.get("items", []):
        if b.get("id") == bid:
            if "name" in body:
                b["name"] = str(body["name"]).strip()[:30] or b["name"]
            if "month" in body:
                b["month"] = _clamp_int(body["month"], 1, 12, b.get("month"))
            if "day" in body:
                b["day"] = _clamp_int(body["day"], 1, 31, b.get("day"))
            if "year" in body:
                b["year"] = _clamp_int(body["year"], 1900, 2200)
            save_birthdays(doc)
            return jsonify({"ok": True, "item": b})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/birthdays/delete", methods=["POST"])
def birthdays_delete():
    body = request.get_json(force=True, silent=True) or {}
    bid = body.get("id")
    doc = load_birthdays()
    before = len(doc.get("items", []))
    doc["items"] = [b for b in doc.get("items", []) if b.get("id") != bid]
    if len(doc["items"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    save_birthdays(doc)
    return jsonify({"ok": True})


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


# --------------------------------------------------------------------------- #
# grocery list (one shared family list; "done" is a server-side boolean shared
# across every device — checking milk on the wall clears it on a phone too)
# --------------------------------------------------------------------------- #
@app.route("/api/shopping")
def shopping_list():
    doc = load_shopping()
    doc["sections"] = [{"id": i, "label": l, "emoji": e}
                       for (i, l, e) in GROCERY_SECTIONS]
    return jsonify(doc)


@app.route("/api/shopping/add", methods=["POST"])
def shopping_add():
    body = request.get_json(force=True, silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "text required"}), 400
    doc = load_shopping()
    item, merged = add_or_merge(doc, text, body.get("qty", 1), body.get("cat"))
    save_shopping(doc)
    return jsonify({"ok": True, "item": item, "merged": merged})


@app.route("/api/shopping/add-many", methods=["POST"])
def shopping_add_many():
    """Bulk add from pasted text (or an explicit list). Each line is cleaned,
    auto-categorized, and merged with any matching item already on the list."""
    body = request.get_json(force=True, silent=True) or {}
    if isinstance(body.get("items"), list):
        names = [str(x) for x in body["items"]]
    else:
        names = structure_items(body.get("text", ""))
    doc = load_shopping()
    added = []
    for name in names:
        item, _ = add_or_merge(doc, name)
        if item:
            added.append(item)
    save_shopping(doc)
    return jsonify({"ok": True, "added": added, "count": len(added)})


@app.route("/api/shopping/update", methods=["POST"])
def shopping_update():
    body = request.get_json(force=True, silent=True) or {}
    iid = body.get("id")
    doc = load_shopping()
    for it in doc.get("items", []):
        if it.get("id") == iid:
            if "text" in body:
                new_text = str(body["text"]).strip()[:60]
                if new_text and new_text.lower() != it["text"].lower():
                    it["cat"] = categorize(new_text)   # re-tag on rename
                it["text"] = new_text or it["text"]
            if "done" in body:
                it["done"] = bool(body["done"])
            if "qty" in body:
                it["qty"] = max(1, int(body.get("qty") or 1))
            if body.get("cat") in GROCERY_SECTION_IDS:
                it["cat"] = body["cat"]
            save_shopping(doc)
            return jsonify({"ok": True, "item": it})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/shopping/delete", methods=["POST"])
def shopping_delete():
    body = request.get_json(force=True, silent=True) or {}
    iid = body.get("id")
    doc = load_shopping()
    before = len(doc.get("items", []))
    doc["items"] = [it for it in doc.get("items", []) if it.get("id") != iid]
    if len(doc["items"]) == before:
        return jsonify({"ok": False, "error": "not found"}), 404
    save_shopping(doc)
    return jsonify({"ok": True})


@app.route("/api/shopping/clear-done", methods=["POST"])
def shopping_clear_done():
    doc = load_shopping()
    before = len(doc.get("items", []))
    doc["items"] = [it for it in doc.get("items", []) if not it.get("done")]
    save_shopping(doc)
    return jsonify({"ok": True, "removed": before - len(doc["items"])})


@app.route("/api/shopping/clear-all", methods=["POST"])
def shopping_clear_all():
    doc = load_shopping()
    before = len(doc.get("items", []))
    doc["items"] = []
    save_shopping(doc)
    return jsonify({"ok": True, "removed": before})


# --- "usuals" / staples: a curated set you re-add to the list with one tap --- #
@app.route("/api/staples")
def staples_list():
    return jsonify(load_staples())


@app.route("/api/staples/set", methods=["POST"])
def staples_set():
    """Replace the whole usuals set (the Settings editor saves the full list)."""
    body = request.get_json(force=True, silent=True) or {}
    items, seen = [], set()
    for x in body.get("items", []):
        t = str(x).strip()[:60]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            items.append(t)
    save_staples({"items": items})
    return jsonify({"ok": True, "items": items})


# Export the list: a clean printable page a phone can open over WiFi, plus a QR
# that points at it. Read-only and LAN-only (same trust model as the wall).
@app.route("/grocery/list", methods=["GET"])
def grocery_list_page():
    doc = load_shopping()
    return Response(render_grocery_list_page(doc.get("items", [])),
                    mimetype="text/html")


@app.route("/api/grocery-list-qr")
def grocery_list_qr():
    url = f"http://{lan_ip()}:{PORT}/grocery/list"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# --------------------------------------------------------------------------- #
# grocery photo capture (Stage B): phone snaps a written list → upload → read
# with Claude vision (or Tesseract) → the wall reviews + commits the items.
# Mirrors the chore QR-window pattern, but the phone POSTs a photo (multipart)
# and the result lands in a pending-review buffer instead of straight on the list.
# --------------------------------------------------------------------------- #
grocery_window = {"token": None, "expires_at": 0.0}
# pending = {"photo_path", "items":[...], "source":"cloud|local", "at": ts}
grocery_review = None


def grocery_window_open():
    return (grocery_window["token"] is not None
            and time.time() < grocery_window["expires_at"])


@app.route("/api/grocery-window/open", methods=["POST"])
def grocery_window_open_route():
    token = uuid.uuid4().hex
    grocery_window.update(token=token, expires_at=time.time() + WINDOW_SECS)
    url = f"http://{lan_ip()}:{PORT}/grocery?token={token}"
    return jsonify({"token": token, "url": url, "expires_in": WINDOW_SECS,
                    "has_key": bool(vision_key())})


@app.route("/api/grocery-window/status")
def grocery_window_status():
    token = request.args.get("token", "")
    if token != grocery_window["token"]:
        return jsonify({"open": False, "received": False, "remaining": 0})
    remaining = max(0, int(grocery_window["expires_at"] - time.time()))
    return jsonify({"open": remaining > 0,
                    "received": grocery_review is not None,
                    "remaining": remaining})


@app.route("/api/grocery-qr")
def grocery_qr():
    token = request.args.get("token", "")
    if token != grocery_window["token"]:
        abort(404)
    url = f"http://{lan_ip()}:{PORT}/grocery?token={token}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/grocery", methods=["GET"])
def grocery_page():
    token = request.args.get("token", "")
    ok = token == grocery_window["token"] and grocery_window_open()
    return Response(render_grocery_page(token, ok), mimetype="text/html")


@app.route("/grocery", methods=["POST"])
def grocery_submit():
    global grocery_review
    token = request.form.get("token", "")
    if token != grocery_window["token"] or not grocery_window_open():
        return Response(render_result_page(False, "This window has closed. "
                        "Tap “Add from phone” on the wall again."),
                        mimetype="text/html", status=403)

    # Pasted/typed text adds straight to the shared list (no wall review needed).
    pasted = (request.form.get("list_text", "") or "").strip()
    if pasted:
        doc = load_shopping()
        added = []
        for name in structure_items(pasted):
            item, _ = add_or_merge(doc, name)
            if item:
                added.append(item)
        save_shopping(doc)
        grocery_window["expires_at"] = 0          # consume the window
        if added:
            return Response(render_result_page(
                True, f"Added {len(added)} item{'s' if len(added) != 1 else ''} "
                "to the list. 🛒"), mimetype="text/html")
        return Response(render_result_page(False, "Couldn't find any items in that "
                        "text — try again."), mimetype="text/html", status=400)

    photo = request.files.get("photo")
    if not photo or not photo.filename:
        return Response(render_result_page(False, "No photo came through — try again."),
                        mimetype="text/html", status=400)

    UPLOADS_DIR.mkdir(exist_ok=True)
    dest = UPLOADS_DIR / ("list_" + uuid.uuid4().hex[:8] + ".jpg")
    photo.save(str(dest))
    try:
        preprocess_image(str(dest))           # rotate/shrink/strip EXIF in place
    except Exception:
        pass
    items, source = read_photo(str(dest))

    grocery_review = {"photo_path": str(dest), "items": items,
                      "source": source, "at": time.time()}
    grocery_window["expires_at"] = 0          # consume the window
    return Response(render_result_page(True, "Sent — confirm it on your wall."),
                    mimetype="text/html")


@app.route("/api/grocery/pending")
def grocery_pending():
    if grocery_review is None:
        return jsonify({"pending": False, "has_key": bool(vision_key())})
    return jsonify({"pending": True, "items": grocery_review["items"],
                    "source": grocery_review["source"],
                    "has_key": bool(vision_key())})


def _clear_review(delete_photo=True):
    global grocery_review
    if grocery_review and delete_photo:
        try:
            os.remove(grocery_review["photo_path"])
        except OSError:
            pass
    grocery_review = None


@app.route("/api/grocery/commit", methods=["POST"])
def grocery_commit():
    if grocery_review is None:
        return jsonify({"ok": False, "error": "nothing pending"}), 404
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items", grocery_review["items"])
    doc = load_shopping()
    added = []
    for text in items:
        item, _ = add_or_merge(doc, text)   # auto-categorize + merge dupes
        if item:
            added.append(item)
    save_shopping(doc)
    _clear_review(delete_photo=True)
    return jsonify({"ok": True, "added": added})


@app.route("/api/grocery/dismiss", methods=["POST"])
def grocery_dismiss():
    if grocery_review is None:
        return jsonify({"ok": False, "error": "nothing pending"}), 404
    _clear_review(delete_photo=True)
    return jsonify({"ok": True})


@app.route("/api/grocery/read-better", methods=["POST"])
def grocery_read_better():
    """Re-run the cloud reader on the retained photo (used when the offline
    read was rough and a key is now available)."""
    if grocery_review is None:
        return jsonify({"ok": False, "error": "nothing pending"}), 404
    if not vision_key():
        return jsonify({"ok": False, "error": "no API key configured"}), 400
    try:
        items = read_cloud(grocery_review["photo_path"])
    except Exception as e:
        return jsonify({"ok": False, "error": "cloud read failed"}), 502
    grocery_review["items"] = items
    grocery_review["source"] = "cloud"
    return jsonify({"ok": True, "items": items})


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
                        "Tap “Add from phone” on the wall again."),
                        mimetype="text/html", status=403)

    # Pasted/typed multi-line text adds several chores at once.
    pasted = (request.form.get("list_text", "") or "").strip()
    if pasted:
        doc = load_chores()
        added = []
        for lbl in structure_items(pasted):
            chore = new_chore(lbl, doc.get("chores", []))
            doc.setdefault("chores", []).append(chore)
            added.append(chore)
        save_chores(doc)
        chore_window["added"] = {"label": f"{len(added)} chores"}
        chore_window["expires_at"] = 0
        if added:
            return Response(render_result_page(
                True, f"Added {len(added)} chore{'s' if len(added) != 1 else ''} "
                "to the chart. They'll show on the wall in a few seconds."),
                mimetype="text/html")
        return Response(render_result_page(False, "Couldn't find any chores in that "
                        "text — try again."), mimetype="text/html", status=400)

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
      <p class="sub">Add chores for the family chart. It rotates through everyone,
      a new person each day.</p>
      <form method="POST" action="/chore">
        <input type="hidden" name="token" value="{token}">
        <label>Chore</label>
        <input type="text" name="label" placeholder="Take out the trash" maxlength="40"
               autocapitalize="sentences">
        <button type="submit">Add to the chart</button>
      </form>

      <div class="orline"><span>or</span></div>

      <form method="POST" action="/chore">
        <input type="hidden" name="token" value="{token}">
        <label>Add several</label>
        <textarea name="list_text" rows="6" placeholder="One chore per line, e.g.&#10;Take out trash&#10;Make beds&#10;Walk the dog"></textarea>
        <button type="submit">Add these to the chart</button>
      </form>

      <p class="hint">Tip: end a chore with an emoji and it becomes a sticker on the
      wall — e.g. “Water plants 🌱”.</p>
      <style>
        .orline{{display:flex;align-items:center;gap:10px;color:var(--soft);
          font-weight:800;font-size:13px;margin:18px 0}}
        .orline::before,.orline::after{{content:"";flex:1;height:1px;background:var(--line)}}
        textarea{{width:100%;box-sizing:border-box;border:1.5px solid var(--line);
          border-radius:14px;padding:12px;font:inherit;font-weight:600;resize:vertical}}
      </style>
    """, title="Add a chore")


def render_grocery_page(token, ok):
    if not ok:
        return _page('<div class="big">⌛</div><h1>Window closed</h1>'
                     '<p class="sub">Tap “Add from phone” on the wall to start again.</p>',
                     title="Add to the grocery list")
    return _page(f"""
      <h1>Add to the list 🛒</h1>
      <p class="sub">Snap or pick a photo of a written list, or paste your items —
      they'll land on the wall.</p>

      <form method="POST" action="/grocery" enctype="multipart/form-data" id="photoForm">
        <input type="hidden" name="token" value="{token}">
        <label>Photo of a list</label>
        <label id="drop" class="drop">
          <input type="file" name="photo" accept="image/*" id="photoInput" hidden>
          <span id="dropText">📷 Take a photo · choose from library · or drop one here</span>
        </label>
        <button type="submit" id="photoBtn" disabled>Send photo to the wall</button>
      </form>

      <div class="orline"><span>or</span></div>

      <form method="POST" action="/grocery">
        <input type="hidden" name="token" value="{token}">
        <label>Paste a list</label>
        <textarea name="list_text" rows="6" placeholder="One item per line, e.g.&#10;Milk&#10;Eggs&#10;Bananas"></textarea>
        <button type="submit">Add these to the list</button>
      </form>

      <p class="hint">Photos go to the wall to confirm; pasted items are added
      straight away. For photos: lay the list flat, fill the frame, good light.</p>
      <style>
        .drop{{display:flex;align-items:center;justify-content:center;text-align:center;
          min-height:96px;padding:16px;border:2px dashed #C9BEEC;border-radius:16px;
          color:var(--soft);font-weight:700;cursor:pointer;background:#FAF8FF}}
        .drop.has{{border-style:solid;border-color:#46D6B4;color:#2E9E86;background:#F0FBF8}}
        .drop.over{{border-color:#8B7BD8;background:#F1ECFF}}
        .orline{{display:flex;align-items:center;gap:10px;color:var(--soft);
          font-weight:800;font-size:13px;margin:18px 0}}
        .orline::before,.orline::after{{content:"";flex:1;height:1px;background:var(--line)}}
        textarea{{width:100%;box-sizing:border-box;border:1.5px solid var(--line);
          border-radius:14px;padding:12px;font:inherit;font-weight:600;resize:vertical}}
      </style>
      <script>
        const inp=document.getElementById('photoInput'), drop=document.getElementById('drop'),
              txt=document.getElementById('dropText'), btn=document.getElementById('photoBtn');
        function refresh(){{
          if(inp.files && inp.files.length){{
            drop.classList.add('has'); btn.disabled=false;
            txt.textContent='✓ '+inp.files[0].name;
          }}else{{ drop.classList.remove('has'); btn.disabled=true; }}
        }}
        inp.addEventListener('change',refresh);
        ['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{{
          ev.preventDefault(); drop.classList.add('over'); }}));
        ['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{{
          ev.preventDefault(); drop.classList.remove('over'); }}));
        drop.addEventListener('drop',ev=>{{
          if(ev.dataTransfer.files && ev.dataTransfer.files.length){{
            inp.files=ev.dataTransfer.files; refresh(); }}
        }});
      </script>
    """, title="Add to the grocery list")


def render_grocery_list_page(items):
    """A clean, printable, phone-friendly copy of the list — scan the QR on the
    wall to open it. Matches the wall's look (Fredoka/Nunito + candy palette);
    the Copy / Share / Print buttons are screen-only and hidden when printed."""
    todo = [it for it in items if not it.get("done")]
    done = [it for it in items if it.get("done")]
    today = datetime.now().strftime("%A, %B ") + str(datetime.now().day)

    def _qty(it):
        q = int(it.get("qty", 1) or 1)
        return f" ×{q}" if q > 1 else ""

    if not items:
        rows = '<li class="empty">The list is empty right now.</li>'
    else:
        rows = ""
        # group un-bought items under store sections, in shopping order
        for cat, label, emoji in GROCERY_SECTIONS:
            grp = [it for it in todo if it.get("cat", "other") == cat]
            if not grp:
                continue
            rows += f'<li class="ghdr">{emoji} {html.escape(label)}</li>'
            rows += "".join(
                f'<li>{html.escape(str(it.get("text", "")))}'
                f'<span class="qty">{_qty(it)}</span></li>' for it in grp)
        rows += "".join(
            f'<li class="got">{html.escape(str(it.get("text", "")))}'
            f'<span class="qty">{_qty(it)}</span></li>' for it in done)
    n = len(todo)
    sub = (f"{n} item{'s' if n != 1 else ''} to get"
           if n else "Everything's checked off 🎉")
    # plain-text version powering Copy / Share — grouped by section (safe JS string)
    lines = ["🛒 Grocery list"]
    for cat, label, emoji in GROCERY_SECTIONS:
        grp = [it for it in todo if it.get("cat", "other") == cat]
        if not grp:
            continue
        lines.append(f"\n{emoji} {label}")
        lines += [f"• {it.get('text', '')}{_qty(it)}" for it in grp]
    lines += [f"✓ {it.get('text', '')}{_qty(it)}" for it in done]
    list_text = json.dumps("\n".join(lines))
    return _page(f"""
      <link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@600;700;800&display=swap" rel="stylesheet">
      <div class="ghead"><span class="gemoji">🛒</span>
        <div><h1>Grocery list</h1><div class="gdate">{today}</div></div></div>
      <p class="sub">{sub}</p>
      <ul class="glist">{rows}</ul>
      <div class="grow">
        <button class="gbtn gprimary" id="gshare">📤 Share</button>
        <button class="gbtn" id="gcopy">📋 Copy</button>
        <button class="gbtn" onclick="window.print()">🖨️ Print</button>
      </div>
      <p class="hint">A snapshot from the wall — re-scan the code on the wall for
      the latest list.</p>
      <script>
        const TEXT={list_text};
        const flash=(b,t)=>{{const o=b.textContent;b.textContent=t;setTimeout(()=>b.textContent=o,1500);}};
        async function copyText(){{
          try{{ if(navigator.clipboard){{ await navigator.clipboard.writeText(TEXT); return true; }} }}catch(e){{}}
          try{{ const ta=document.createElement('textarea'); ta.value=TEXT;
            ta.style.cssText='position:fixed;opacity:0'; document.body.appendChild(ta);
            ta.select(); const ok=document.execCommand('copy'); ta.remove(); return ok;
          }}catch(e){{ return false; }}
        }}
        gcopy.onclick=async()=>flash(gcopy, (await copyText())?'✓ Copied!':'⚠️ couldn’t copy');
        gshare.onclick=async()=>{{
          if(navigator.share){{ try{{ await navigator.share({{title:'Grocery list',text:TEXT}}); }}catch(e){{}} }}
          else{{ flash(gshare, (await copyText())?'✓ Copied!':'⚠️ no share'); }}
        }};
      </script>
      <style>
        .ghead{{display:flex;align-items:center;gap:12px;margin-bottom:2px}}
        .gemoji{{font-size:42px;line-height:1;flex:none}}
        .ghead h1{{font-family:"Fredoka",system-ui,sans-serif;font-weight:600;font-size:30px;margin:0;
          background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}}
        .gdate{{color:var(--soft);font-weight:700;font-size:13px;margin-top:2px}}
        .glist{{list-style:none;padding:0;margin:12px 0 0}}
        .glist li{{font-family:"Nunito",sans-serif;font-size:18px;font-weight:700;
          padding:13px 6px 13px 36px;border-bottom:1px solid var(--line);position:relative}}
        .glist li::before{{content:"";position:absolute;left:4px;top:50%;width:19px;height:19px;
          margin-top:-10px;border:2px solid #C9BEEC;border-radius:6px}}
        .glist li.ghdr{{font-family:"Fredoka",sans-serif;font-weight:700;font-size:14px;
          color:var(--soft);text-transform:uppercase;letter-spacing:.04em;
          padding:16px 6px 5px;border-bottom:0}}
        .glist li.ghdr::before{{display:none}}
        .qty{{color:var(--soft);font-weight:800;font-size:14px;margin-left:6px}}
        .glist li.got{{color:var(--soft);text-decoration:line-through}}
        .glist li.got::before{{content:"✓";color:#46D6B4;border-color:#46D6B4;font-weight:900;
          font-size:13px;text-align:center;line-height:16px;text-decoration:none}}
        .glist li.empty{{color:var(--soft);padding-left:6px}} .glist li.empty::before{{display:none}}
        .grow{{display:flex;gap:9px;margin-top:18px}}
        .gbtn{{flex:1;width:auto;margin:0;border:0;border-radius:14px;padding:13px 6px;cursor:pointer;
          font-family:"Nunito",sans-serif;font-weight:800;font-size:14px;color:var(--ink);background:#F1ECFF}}
        .gbtn.gprimary{{color:#fff;background:var(--grad)}}
        @media print{{
          body{{background:#fff;padding:0}} .card{{box-shadow:none;margin:0;width:100%}}
          .grow,.hint{{display:none}}
          .ghead h1{{-webkit-text-fill-color:#5B4AA0;color:#5B4AA0}}
        }}
      </style>
    """, title="Grocery list")


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


# Funny one-liners keyed by the sticker-icon "bucket", picked at random so the
# wall gets a fresh joke each refresh. Temperature extremes override below.
WX_QUIPS = {
    "wx_clear_day": [
        "Suspiciously perfect. San Diego showing off again. ☀️",
        "Blue skies, zero excuses to skip the beach. 🏖️",
        "The sun called in for a full shift today.",
        "Sunglasses weather. You know the drill.",
    ],
    "wx_clear_night": [
        "Clear and starry — tuck the sun in, it earned it. 🌙",
        "Crisp night skies. Wish upon something.",
        "Not a cloud in sight. The moon's got the floor. ✨",
    ],
    "wx_partly": [
        "A few clouds loitering, nothing to report.",
        "Sun and clouds sharing custody of the sky.",
        "Partly cloudy — the sky can't quite commit.",
    ],
    "wx_cloudy": [
        "Gray ceiling installed. Mood: pensive.",
        "The sun is working from home today.",
        "Overcast — the sky forgot to pay its lighting bill.",
    ],
    "wx_fog": [
        "Classic June gloom — the marine layer is hogging the sky. 🌫️",
        "Foggy. Somewhere out there, the sun exists. Allegedly.",
        "Marine layer special: low visibility, high cozy.",
        "The sky pulled a gray blanket over its head again.",
    ],
    "wx_rain": [
        "Rain in San Diego?! Quick, alert the neighbors. ☔",
        "Liquid sunshine. Grab a jacket you forgot you owned.",
        "It's wet out there — dramatic, by local standards.",
        "Sky's leaking. Umbrella optional, complaining mandatory.",
    ],
    "wx_snow": [
        "Snow. In San Diego. Check the calendar — and the apocalypse. ❄️",
        "Frozen sky confetti. Highly unusual around here.",
    ],
    "wx_storm": [
        "Thunder's putting on a show. Bring the pets in. ⛈️",
        "Storm brewing — nature's drama queen is back.",
        "Boom and flash incoming. Cozy up.",
    ],
}


def wx_quip(code, temp, is_day):
    """A playful one-liner for the current conditions (San Diego flavored)."""
    if temp is not None:
        if temp >= 95:
            return random.choice([
                "Absolutely roasting. Hydrate or evaporate. 🥵",
                "It's a 'living in the fridge' kind of day.",
                "Hot enough to fry an egg on the sidewalk. Don't.",
            ])
        if temp >= 88:
            return random.choice([
                "Toasty out — ice cream is now a food group. 🍦",
                "Beach-and-A/C weather. Pick wisely.",
            ])
        if temp <= 40:
            return random.choice([
                "Brisk for San Diego — locals are basically in parkas. 🧥",
                "Cold by our standards, which means: a light sweater.",
            ])
    icon = wx_icon(code, is_day)
    return random.choice(WX_QUIPS.get(icon, ["Weather is happening. As it does."]))


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


@app.route("/kids/<path:fn>")
def kid_file(fn):
    """Cartoon kid avatars for the First Five widget. 404 → widget shows emoji."""
    p = (KIDS_DIR / fn).resolve()
    if KIDS_DIR not in p.parents or not p.is_file():    # no path traversal
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
        ctemp = round(cur["temperature_2m"])
        data = {"ok": True, "temp": ctemp,
                "emoji": emoji, "label": label, "city": g.get("city", ""),
                "icon": wx_icon(cur["weather_code"], cur.get("is_day", 1)),
                "quip": wx_quip(cur["weather_code"], ctemp, cur.get("is_day", 1)),
                "daily": daily}
        _weather_cache.update(at=time.time(), data=data)
        return jsonify(data)
    except (requests.RequestException, KeyError, ValueError):
        if _weather_cache["data"]:
            return jsonify(_weather_cache["data"])  # stale-but-good
        return jsonify({"ok": False})


# --------------------------------------------------------------------------- #
# "On this day" — a fun history quip from Wikipedia's free On-This-Day feed
# (no API key). Cached once per day. We drop grim events (deaths, war, disasters)
# so it stays kid-friendly on the wall and bias toward the quirky/interesting.
# Each event already carries a one-line blurb + a Wikipedia link + a thumbnail;
# tapping the wall card shows the blurb + a QR to open the page on a phone.
# --------------------------------------------------------------------------- #
# whole-word match (so "war" doesn't trip on "warm"/"reward"), plus a couple of
# compounds the boundaries miss. Best-effort kid-safety, not a guarantee.
_OTD_GRIM = re.compile(
    r"\b(?:war|warfare|battle|battles|fire|wildfire|gunfire|dead|deadly|die|dies|"
    r"died|death|deaths|kill|kills|killed|killing|fatal|fatally|murder|murders|"
    r"murdered|wound|wounded|attack|attacks|attacked|bomb|bombs|bombed|bombing|"
    r"shoot|shooting|shootings|shot|gun|guns|gunman|weapon|weapons|terror|"
    r"terrorist|terrorism|massacre|massacres|massacred|genocide|assassinate|"
    r"assassinated|assassination|slaughter|execution|executed|nazi|holocaust|"
    r"hanged|hanging|riot|riots|crash|crashed|crashes|disaster|earthquake|"
    r"tsunami|hurricane|tornado|famine|plague|epidemic|pandemic|invasion|"
    r"invaded|siege|casualty|casualties|explosion|explosions|exploded|sank|sunk|"
    r"drowned|drowns|rape|raped|slavery|slave|slaves|violence|violent|coup|"
    r"ebola|outbreak|mutiny|rebellion|uprising|battleship|warship|warplane)\b"
    r"|cyberattack|cyber attack",
    re.I)
_otd_cache = {"day": "", "data": None}


def _otd_pick(events):
    """Keep kid-friendly events that have a Wikipedia link; skip grim ones."""
    out = []
    for e in events:
        text = str(e.get("text", "")).strip()
        if not text or _OTD_GRIM.search(text):
            continue
        pages = e.get("pages") or []
        if not pages:
            continue
        p = pages[0]
        urls = p.get("content_urls") or {}
        url = ((urls.get("desktop") or {}).get("page")
               or (urls.get("mobile") or {}).get("page") or "")
        if not url:
            continue
        out.append({
            "year": e.get("year"),
            "text": text,
            "url": url,
            "title": p.get("normalizedtitle") or p.get("titles", {}).get("normalized")
                     or p.get("title", ""),
            "thumb": (p.get("thumbnail") or {}).get("source", ""),
            "extract": str(p.get("extract", "")).strip(),
        })
    return out


def _otd_fetch(kind, mm, dd):
    r = requests.get(
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/{kind}/{mm}/{dd}",
        timeout=8, headers={"accept": "application/json",
                            "User-Agent": "CorneliusFamilyCalendar/1.0 (family wall)"})
    return _otd_pick((r.json() or {}).get(kind, []))


@app.route("/api/onthisday-qr")
def onthisday_qr():
    """QR for a Wikipedia link so you can open the full story on your phone."""
    url = request.args.get("url", "")
    if not re.match(r"^https?://[a-z]+\.(m\.)?wikipedia\.org/", url):
        abort(404)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/onthisday")
def onthisday():
    today = datetime.now().strftime("%m/%d")
    if _otd_cache["data"] and _otd_cache["day"] == today:
        return jsonify(_otd_cache["data"])
    mm, dd = today.split("/")
    try:
        events = _otd_fetch("selected", mm, dd)        # curated, higher quality
        if len(events) < 6:                            # top up from the full feed
            seen = {e["url"] for e in events}
            events += [e for e in _otd_fetch("events", mm, dd) if e["url"] not in seen]
        random.shuffle(events)
        data = {"ok": bool(events),
                "date": datetime.now().strftime("%B ") + str(datetime.now().day),
                "events": events[:10]}
        _otd_cache.update(day=today, data=data)
        return jsonify(data)
    except (requests.RequestException, ValueError, KeyError):
        if _otd_cache["data"]:
            return jsonify(_otd_cache["data"])         # stale-but-good
        return jsonify({"ok": False, "events": []})


if __name__ == "__main__":
    print(f"Family Calendar on http://{lan_ip()}:{PORT}  (and http://localhost:{PORT})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
