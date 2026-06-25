# Family Calendar — Import Architecture Spec

**Purpose:** Bring real Apple/iCloud + Google calendars into the existing single-file family calendar app (read-only display), running as a kiosk on a Linux Mint box. This document is a build brief for Claude Code.

---

## 1. Environment

- **Hardware:** HP All-in-One 24-f0xx — AMD A9-9425, 8 GB RAM, ~24" 1080p touchscreen, single-touch under Linux. (SSD swap optional but recommended.)
- **OS:** Linux Mint 22.3 (Ubuntu 24.04 base, kernel 6.14), XFCE.
- **Use:** Wall/counter family calendar. Always on. Boots straight into a fullscreen browser.
- **Existing asset:** `family-calendar.html` — a finished, styled single-file front-end (pastel/bubbly theme, month grid, agenda, chores, sleep-mode slideshow, in-app color/family editor). This spec wires real data into it; it does **not** redesign it.

---

## 2. Design principle

**Do the hard parsing on the box in Python. Keep the front-end dumb and pretty.**

Browsers (especially `file://`) can't fetch remote `.ics` feeds directly (CORS + auth). So a small local service fetches, parses, and merges calendars into one clean `events.json`, then serves the app + that JSON over `http://localhost`. The front-end only ever reads `events.json` — no parsing, no CORS, no secrets in the browser.

---

## 3. Data flow

```
  Google "secret iCal" feeds ┐
  Apple public webcal feeds  ┤──► fetcher.py (every ~10 min)
                             │      • download each feed
                             │      • parse + expand recurrences (window: this month ±1)
                             │      • tag each event w/ feed's name+color
                             │      • merge, sort, write events.json (atomic)
                             ▼
                        events.json  ◄── served by ──►  Flask app (localhost:8080)
                             ▲                                │
                             │                                ├─ serves family-calendar.html
                             │                                ├─ serves events.json
                             │                                ├─ /add  QR-import flow (see §7)
                             │                                └─ writes feeds.json
                             │                                          │
   front-end (browser) ──────┘   fetch('./events.json') every few min   │
        caches last-good copy to localStorage; shows "synced N min ago"  │
                                                          feeds.json ◄────┘
```

---

## 4. Components

### 4a. Fetcher + parser (`fetcher.py`)
- **Libraries:** `requests` (or `httpx`), `icalendar`, **`recurring-ical-events`** (this is the key one — it expands RRULE recurrences and honors EXDATE cancellations / one-off edited instances within a date window). Optionally `python-dateutil`, `tzdata`.
- **Behavior per run:**
  1. Read `feeds.json`.
  2. For each enabled feed: download the `.ics` (convert `webcal://` → `https://`). On failure, log and **keep that feed's previous events** — never let one dead feed blank the calendar.
  3. Parse with `icalendar`; use `recurring-ical-events` to expand occurrences across the window (default: first day of last month → last day of next month).
  4. Normalize each occurrence to the event schema (§6). Convert times to the box's local timezone (`America/Los_Angeles`). Handle timed, all-day, and multi-day events.
  5. Tag each event with its feed's `id`, `name`, `color`.
  6. Merge all feeds, sort by start, write `events.json` **atomically** (write `events.json.tmp`, then `os.replace`) so the server never serves a half-written file.
- **Scheduling:** systemd timer every 10 min + run once at boot (after network is up). See §4e.

### 4b. Web app (`server.py`, Flask)
Small (~40–60 lines). Responsibilities:
- Serve `family-calendar.html` at `/`.
- Serve `events.json` at `/events.json` (same-origin → no CORS issue).
- Serve `feeds.json`-derived data the front-end needs (feed list = family legend).
- Host the **QR-add flow** (§7): open an add-window, serve the phone-facing `/add` page, receive submissions, write to `feeds.json`, trigger an immediate fetch.
- Detect the box's own LAN IP at runtime to build QR URLs (no hardcoded IP).
- Bind to `0.0.0.0:8080` so phones on the LAN can reach `/add`; everything else is effectively localhost for the wall.

### 4c. Front-end changes (to `family-calendar.html`)
Minimal, surgical edits — the look stays identical:
- Replace the `SAMPLE_EVENTS` block with `fetch('./events.json')`.
- **Cache last-good** `events.json` to localStorage; on fetch failure, render the cached copy (so reboots/network blips don't blank the wall).
- Poll `events.json` every few minutes (or compare `generated_at`).
- Add a small "synced N min ago" indicator (the only visible new element).
- **Merge sources for display:** shown events = imported feed events (`events.json`) **+** local manual events the user adds via the existing `+` button (kept in localStorage, box-local, not synced back — consistent with read-only design).
- **Reconcile the existing settings panel with feeds:** the "🎨 Colors & family" editor now edits the *feeds* — rename a feed, recolor it, toggle/remove it. The "+ Add person" button launches the QR-add flow (§7) instead of adding a blank row. Removing a person removes its feed.

### 4d. Config (`feeds.json`) — never hand-edited
Written by the app via the QR-add flow and the settings panel. Schema in §6.

### 4e. Boot wiring (systemd)
- `familycal-web.service` — runs `server.py`; `After=network-online.target`, `Wants=network-online.target`, `Restart=always`.
- `familycal-fetch.service` + `familycal-fetch.timer` — runs `fetcher.py` on a 10-min cadence and once shortly after boot.
- Browser autostart into kiosk fullscreen pointing at `http://localhost:8080` (covered in the separate kiosk-setup bucket).

---

## 5. The QR-add flow (standard way to add a calendar)

**Goal:** never type a long `webcal://` URL on the touchscreen. The wall shows a QR; the phone (which already has the link copied) does the pasting.

1. On the wall, tap **"+ Add calendar."** Front-end calls `POST /api/add-window/open`.
2. Server opens a **2-minute add-window**, mints a one-time `token`, detects its LAN IP, and returns `http://<lan-ip>:8080/add?token=<token>`.
3. Front-end renders that URL as a **QR code** (server returns a QR PNG via the Python `qrcode` library, or front-end renders it from the URL — server-side PNG preferred, no JS dependency).
4. User scans with phone camera → phone browser opens the `/add` page served by the box.
5. On the phone: a clean form — paste the calendar's share link, set **name** + **color**, tap **Add.**
6. `POST /add` validates the token + window, appends a feed to `feeds.json` (with a generated `id`), and kicks off an immediate fetch. New person appears on the wall within seconds.
7. Window auto-closes after 2 minutes or after a successful add.

**Security:** the `/add` endpoint only accepts submissions while a window is open and the token matches — so even though it's reachable on the LAN, it only works when someone deliberately taps "Add calendar" at the wall. Lock `feeds.json` to the service user (`chmod 600`).

**Optional extra (note for later, not this phase):** an *export* QR on the wall that subscribes a phone to the merged family calendar (nice for guests / new family phones).

---

## 6. Data schemas

**`feeds.json`** (written by app):
```json
{
  "feeds": [
    {
      "id": "f_a1b2c3",
      "name": "Mom",
      "color": "#FF8FBE",
      "type": "ics",
      "url": "https://p01.icloud.com/published/2/MWx...",
      "enabled": true
    }
  ]
}
```
(`type` is `"ics"` now; `"caldav"` is reserved for a future private-calendar add-on — see §8. The schema already supports it so nothing needs redoing later.)

**`events.json`** (written by fetcher, read by front-end):
```json
{
  "generated_at": "2026-06-25T14:30:00-07:00",
  "window": { "start": "2026-05-01", "end": "2026-07-31" },
  "timezone": "America/Los_Angeles",
  "feeds": [
    { "id": "f_a1b2c3", "name": "Mom", "color": "#FF8FBE" }
  ],
  "events": [
    {
      "id": "evt_...",
      "feed_id": "f_a1b2c3",
      "title": "Soccer practice",
      "start": "2026-06-25T15:30:00-07:00",
      "end":   "2026-06-25T16:30:00-07:00",
      "all_day": false
    }
  ]
}
```

---

## 7. Where the feed URLs come from (Benji provides these via the QR flow)

- **Google Calendar:** Settings → *Settings for my calendars* → pick the calendar → **Integrate calendar** → copy **"Secret address in iCal format."**
- **Apple / iCloud (this phase = public share links):** iCloud.com Calendar → click the share/broadcast icon next to a calendar → enable **Public Calendar** → **Copy Link** (`webcal://…`). The fetcher converts `webcal://` → `https://`.
  - *Privacy note:* a public link is an obscure-but-unauthenticated URL — fine for shared/household/kid calendars; don't publish genuinely sensitive ones. Private calendars wait for the CalDAV add-on (§8).

---

## 8. Out of scope for this phase (don't build yet)

- **CalDAV + app-specific-password** import for private iCloud calendars. Schema already reserves `type: "caldav"`; add later without redesign.
- Two-way sync / editing source calendars (design is intentionally read-only).
- Weather API, to-do/shopping lists, meal planner, sticky notes, Week/Day views — separate feature bucket.
- The export/subscribe QR (§5 optional).

---

## 9. Resilience requirements (don't skip — it's a wall appliance)

- One dead/404 feed must never blank the calendar — keep its last-known events.
- Atomic write of `events.json` (no half-read files).
- Front-end caches last-good `events.json` to localStorage; renders cache on any fetch failure.
- Fetcher runs once at boot *after* network is online, then on the timer.
- Graceful empty/error states ("synced N min ago", "couldn't reach a calendar").
- All-day and multi-day events render across the correct cells; verify a known weekly-recurring event shows on every correct day.

---

## 10. Suggested build order (milestones)

1. **Serve the existing app over HTTP.** Flask serving `family-calendar.html` at `localhost:8080`, autostart on boot, kiosk fullscreen. (Prove the appliance loop.)
2. **Prove the pipeline with one feed.** Hardcode one Google secret-iCal URL in `feeds.json`; build `fetcher.py` → `events.json`; point the front-end at it. Confirm real events render.
3. **Resilience + recurrence.** localStorage cache, last-synced indicator, recurring-event expansion verified, all-day/multi-day correct.
4. **QR-add flow.** Flask `/api/add-window/open`, `/add` page, token + 2-min window, write `feeds.json`, immediate refetch.
5. **Wire settings UI to feeds.** Rename/recolor/remove feeds; "+ Add" launches QR flow.
6. **systemd units** for web + fetch timer; boot ordering.

---

## 11. Things Benji must supply / decide

- The actual feed URLs (gathered on phone, added via QR flow) — Google secret iCal + Apple public links.
- Each person's display **name + color** (the existing app palette is the starting set).
- A stable way to reach the box on the LAN: runtime LAN-IP detection is the default; optionally set an mDNS hostname (e.g. `familycal.local`) via avahi for a friendlier URL.
- Timezone confirmed as `America/Los_Angeles`.
- Refresh interval (default 10 min).
```
