# Plan: Grocery list + photo capture (Claude vision) + local voice

## Context
Replace the **Family widget** with a **shared grocery list**; add **photo capture** of a
written/printed list; and add **local voice control**. This is **Phase 4** of `ROADMAP.md`
(Photo capture + Voice), built on the shopping-list feature from `docs-photo-architecture.md`.

Decisions (confirmed):
- **One shared family grocery list** (not per-person).
- **Read photos with the Claude API (cloud vision)** as the primary reader — the user is
  adding an Anthropic API key. **Tesseract stays as a free on-box fallback** when no key /
  offline. Best quality incl. handwriting; ~1¢/photo.
- **Confirm/fix items on the wall** before they commit (phone just snaps + uploads).
- **Also build local voice** now. Key constraint: **Claude has no audio/STT API**, so
  listening must run on the box (`faster-whisper` + wake word); Claude is used only for
  photo reading and (optionally) interpreting free-form voice questions.

**Hardware (confirmed via SSH):** AMD A9-9425, **2 cores**, **7 GB RAM**, integrated Radeon
(no usable ML GPU), 856 GB free disk, working mic input (ALC236). Everything local runs
CPU-only on 2 weak cores → keep local models tiny; offload heavy reasoning to Claude.

---

## Stage A — Remove Family + shared grocery list

### Remove the Family widget (family-calendar.html)
- Delete `<section id="w-family">` in `#wstore`, `renderFamily()`, and **every**
  `renderFamily()` call site (liveRefresh, syncNow, feed/people editors, boot). Remove
  `'family'` from `WIDGETS` and default `layout.order`.
- **Keep** `FAMILY`/`familyFromFeeds()`/`PEOPLE` + `.dot` colors — they drive calendar
  event colors (`famColor`, `evChip`, swatches). Only the widget + renderer go.

### Grocery list — server (server.py), modeled on chores (:103, :319)
- `data/shopping.json` = `{"items":[{id,text,done}]}`; `load_shopping`/`save_shopping`
  mirror the atomic chore writers (gitignored like `chores.json`). `done` is a **server-side
  boolean** (shared across devices — checking milk clears it everywhere).
- `GET /api/shopping`; `POST /api/shopping/{add,update,delete,clear-done}`.

### Grocery widget (family-calendar.html), modeled on chores (:1378)
- `<section id="w-grocery" data-w="grocery">` (cart header, `<div id="grocery">`); add
  `'grocery'` to `WIDGETS` + `layout.order` (Family's slot); `renderGrocery()` wired into
  `relayout`, `liveRefresh`, boot.
- `renderGrocery()` renders `.chore`-style rows; tap toggles `done` via `/api/shopping/update`
  (bought items grey/struck, sink to bottom); footer "N to get"; header **+ type** add and
  **📷 Add by photo**. Settings tab **"Groceries"** (editor + add/clear-done + phone/photo).

---

## Stage B — Photo capture → Claude vision (+ Tesseract fallback), review on wall

### Server (server.py)
- **Phone window + QR** reusing the chore-window pattern (:484): `/api/grocery-window/open`
  → `/grocery?token=`, `/api/grocery-qr`, `/api/grocery-window/status`.
- **Phone page** `render_grocery_page` + `/grocery` GET/POST (mirror `chore_page`/`chore_submit`
  :523). Form is `multipart/form-data` with `<input type=file accept=image/* capture=environment
  name=photo>` (opens the phone camera). POST reads `request.files["photo"]` (first multipart
  upload in the app) → save to gitignored `uploads/` (not served).
- **Preprocess** (Pillow, already on the wall via `qrcode[pil]`): `ImageOps.exif_transpose`
  → `convert("RGB")` → `thumbnail((1600,1600))` → re-save JPEG (drops EXIF/GPS).
- **Read → structure**:
  - **Primary `read_cloud()`** (when `ANTHROPIC_API_KEY` set): official **`anthropic` SDK**,
    `client.messages.create(model = env FAMILYCAL_VISION_MODEL or "claude-opus-4-8",
    output_config={"format": json_schema {items:[string]}}, messages=[base64 image block +
    extract-prompt: "list the grocery items, one clean entry each; ignore headers, prices,
    crossed-out items"])`. Cheap (~1¢); Haiku selectable via env for less.
  - **Fallback `read_local()`**: `pytesseract.image_to_string` → `structure_items()`
    (split lines, strip bullets/checkboxes/numbers, drop garbage/headers, de-dupe). Used
    when no key or the cloud call fails. Degrades to empty + "type them in" if Tesseract
    isn't installed.
- **Pending review** `grocery_review = {photo_path, items, source, at}` (module-level, like
  `chore_window`). Phone POST stores it, returns "✅ Sent — confirm it on your wall." Wall
  endpoints: `GET /api/grocery/pending`, `POST /api/grocery/commit {items[]}` (adds kept
  items, clears review, **deletes the photo**), `/api/grocery/dismiss`,
  `/api/grocery/read-better` (re-run `read_cloud` on the retained photo). `/api/health`-style
  flag exposes whether a key is configured (to show/hide cloud buttons).

### Wall UI (family-calendar.html)
- **Review modal** `#groceryReview`: poller (~2s, like `pollChoreQr`) hits
  `/api/grocery/pending`; on a batch, pops a sheet of editable item rows (keep/drop, fix
  text, add a line), **Add to list** / **Discard** (`commit`/`dismiss`), and a **"Read
  better"** button when offline-read was used and a key exists. Commit → reload + confetti.
- **Photo QR modal** `#grqrmodal` mirrors `#chqrmodal` (:612): QR + LAN URL → "📷 received —
  confirm on the wall."

---

## Stage C — Local voice control

A separate `voice.py` daemon (its own systemd service) — Flask stays the UI/API.
- **Wake word**: `openWakeWord` (CPU-light, pip) — e.g. "hey wall". Continuous listen.
- **Capture**: `sounddevice` (needs `libportaudio2` via apt) records ~5s after wake.
- **STT**: `faster-whisper` `tiny.en`/`base.en`, int8, CPU — ~1-2s for a short command.
- **Intent**:
  - **Rule-based core commands** (free, instant): "what's on today / this week" → read
    `events.json`, summarize; "add <X> to the groceries" → `POST /api/shopping/add`;
    "what's the weather" → `/api/weather`.
  - **Optional Claude fallback** for free-form questions (when key set): send the transcript
    + a compact day/agenda context to `client.messages.create` and speak the reply.
- **Speak back**: `Piper` (fast local TTS; download one voice model) via the box's speaker.
- **Service**: `deploy/familycal-voice.service` (user service, `WantedBy=default.target`),
  `ANTHROPIC_API_KEY` in `Environment=`. Gated on a real mic being present.

---

## Dependencies & wall setup
- `requirements.txt`: `anthropic>=0.40`, `Pillow>=10`, `pytesseract>=0.3`,
  `faster-whisper>=1.0`, `openwakeword>=0.6`, `sounddevice>=0.4` (+ `piper-tts` or the Piper
  binary).
- Wall (over SSH, user-owned venv — no sudo): `.venv/bin/pip install -r requirements.txt`;
  restart `familycal-web`; install + enable `familycal-voice.service`.
- **Sudo steps for the user** (box password, per wall-deployment memory): optional
  `apt install -y tesseract-ocr` (fallback OCR) and `apt install -y libportaudio2` (mic
  capture for voice). Provide exact commands.
- **API key**: user supplies Anthropic key → add `Environment=ANTHROPIC_API_KEY=…` to the
  web (and voice) systemd units. Cloud read + Claude voice intent switch on automatically.

## Files
- `server.py` — shopping CRUD; grocery window/QR; `/grocery` multipart page; preprocess +
  `read_cloud`/`read_local`/`structure_items`; pending-review + commit/dismiss/read-better;
  key-present flag.
- `family-calendar.html` — remove Family widget; grocery widget + `renderGrocery`; review
  modal + poller; photo QR modal; Settings "Groceries" tab; WIDGETS/layout/boot wiring.
- `voice.py` — wake word → record → faster-whisper → intent (rules + optional Claude) →
  Piper TTS; calls the existing HTTP API for actions.
- `deploy/familycal-voice.service` — systemd unit.
- `requirements.txt` — new deps above.
- `tests/test_shopping.py` — shopping CRUD + `structure_items()` (mirror `tests/test_chores.py`).
- `uploads/.gitkeep` + `.gitignore` for `uploads/*`.

## Verification
1. Local: `/api/shopping` CRUD via curl; grocery widget renders + checks; Family gone but
   calendar still colors events; `.venv/bin/pytest` green (incl. new test_shopping).
2. Photo: POST a test list image to `/grocery` (multipart curl); with a key set, confirm
   `read_cloud` returns clean items; without, `read_local`/`structure_items` (unit-tested).
   `/api/grocery/pending` → review modal edit → `commit` adds items + deletes the photo.
   Headless-screenshot the widget + review modal.
3. Deploy: push → wall pull → `.venv/bin/pip install -r requirements.txt` → restart web;
   add the API key to systemd; user runs the apt commands. Verify a real phone photo flows
   to the wall review and commits.
4. Voice (after mic confirmed + libportaudio2): wake word triggers, "what's on today" speaks
   the agenda, "add milk to the groceries" lands on the list.

## Build order / staging
A (grocery list + remove Family) → B (photo capture with Claude vision + review) → C (voice).
Ship A+B first (immediately useful); C lands once the API key + audio deps are in place.

## Deferred (not v1)
- Handwritten-**calendar** mode (events from a photo) — grocery/list mode only.
- Local vision model (moondream/Qwen-VL) — Claude cloud + Tesseract fallback cover reading.
