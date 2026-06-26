# Family Calendar — wall appliance

An always-on family calendar for a touchscreen on the wall. Real Google &
iCloud calendars flow in read-only; the screen stays pretty and dumb.

**Architecture (see [docs-import-architecture.md](docs-import-architecture.md)):**
the hard work — downloading `.ics` feeds, expanding recurring events, merging
and timezone-normalizing — happens on the box in Python. The browser only ever
reads one clean `events.json` over `http://localhost`, so there's no CORS, no
auth, and no secrets in the page.

```
 feeds.json ──► fetcher.py ──► data/events.json ──► server.py (Flask) ──► family-calendar.html
   (URLs)        (every 10m)     (atomic write)        localhost:8080         (the wall)
```

## Files

| File | What it is |
|------|------------|
| `family-calendar.html` | The styled front-end. Fetches `./events.json`, caches last-good copy, "synced N min ago", live weather chip, event reminders, photo-frame sleep mode. |
| `fetcher.py` | Downloads + parses each feed, expands recurrences, writes `events.json` atomically. Keeps a dead feed's last-known events. |
| `server.py` | Flask: serves the app + `events.json`, the QR-add flow, feed management, `/api/weather`, and `/api/photos`. |
| `data/feeds.json` | Calendar feed list (name, color, url). Ships with the US Holidays feed as a demo. |
| `photos/` | Drop family photos here; they cycle in sleep mode. |
| `deploy/` | systemd units, kiosk autostart, and `install.sh` — see [DEPLOY.md](DEPLOY.md). |

## Run locally (dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python fetcher.py        # build data/events.json from feeds.json
python server.py         # http://localhost:8080
```

## Install on the Mint box (appliance)

```bash
bash deploy/install.sh
```

This creates the venv, primes `events.json`, installs the `familycal-web`
service + `familycal-fetch` 10-min timer, and adds a Chromium kiosk autostart.
Reboot and the wall comes up fullscreen on its own.

## Adding real calendars

**The easy way (QR):** on the wall, open 🎨 settings → **📲 Add a calendar**.
A QR appears; scan it with your phone, paste the calendar's share link, set a
name + color, tap **Add**. It lands in `feeds.json` and shows on the wall within
seconds. The phone form only works while the 2-minute window is open and the
one-time token matches, so it's safe even though it's reachable on the LAN.

**The manual way:** edit `data/feeds.json` directly. Each feed:

```json
{ "id": "f_mom", "name": "Mom", "color": "#FF8FBE", "type": "ics",
  "url": "https://…secret-ical-or-webcal…", "enabled": true }
```

- **Google:** Calendar settings → *Integrate calendar* → **Secret address in iCal format**.
- **iCloud (public):** iCloud Calendar → share icon → **Public Calendar** → copy the `webcal://…` link (the fetcher converts it to `https://`).

After editing, `python fetcher.py` (or wait for the timer) refreshes the wall.

## Status

Built: the full feed pipeline (recurrence, all-day/multi-day), last-good cache
+ "synced" indicator, dead-feed resilience, the QR-add flow, settings-panel
feed management (rename / recolor / show-hide / remove), live weather, event
reminders (chime + "up next" countdown), photo-frame sleep mode, and
systemd/kiosk deploy wiring.
Not yet built: the CalDAV add-on for private iCloud calendars, and week/day
views (the toggle buttons are still placeholders).
