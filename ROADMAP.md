# Cornelius Calendar — Roadmap

A sequenced plan for what's next, ordered so each phase makes the next one
cheaper or safer to build. The guiding idea: a few features share hidden
dependencies, so we lay those foundations first and the fun stuff gets cheap.

## The dependency logic

Three things quietly gate everything downstream:

- **A person/owner model** — "per-person lanes," "chore rotation," and "who's
  home" all need the calendar to know *who* an event belongs to. Build it once,
  three features get cheap.
- **A health/status signal** — needed before any UI that shows "is sync alive,"
  and before we trust the wall is running the latest code.
- **A test harness** — recurrence expansion in `fetcher.py` is the gnarliest
  logic; lock it down before building features on top of its output.

---

## Phase 0 — Clear the decks
*No new features; removes risk and uncertainty. Do first.*

- [ ] **Rotate the leaked iCloud URL** — pending security item; the scrub from
      `feeds.json` is done, but the actual Benji calendar URL needs rotating on
      iCloud's side (old one may be cached/indexed).
- [x] **Verify the wall is running latest** — DONE 2026-06-26. Wall at
      `/home/calender/familycal`, on `main` @ `6b9cbeb`, in sync with origin
      (0/0); `familycal-web.service` + `familycal-update.timer` both active. All
      published feature work is live. (Wall: `ssh calender@192.168.1.238`.)

## Phase 1 — Foundation
*Cheap, boring, makes everything after it safe. Do second.*

- [x] **Smoke/test harness** around `fetcher.py` recurrence + merge + timezone
      logic. Catches the highest-risk bugs in the project. DONE 2026-06-26 —
      `tests/` (17 tests, `.venv/bin/pytest`). Caught + fixed a real bug:
      timed events with no DTEND rendered zero-duration (the 1h default never
      fired because `recurring_ical_events` synthesizes `DTEND==DTSTART`).
- [x] **Health/status endpoint** (`/api/health` → last-good-sync, per-feed
      liveness). Feeds Phase 3's status UI. DONE 2026-06-26 — `fetcher.py` now
      records per-feed `status`/`last_ok`/`count`/`error` into events.json
      (errors *classified*, never raw, so the secret feed URL can't leak);
      `/api/health` reports overall `ok`, sync staleness (>35 min = 3 missed
      10-min cycles), and per-feed state. Covered in `tests/test_health.py`.

## Phase 2 — The "who" model (keystone)
*Small data-model change; the hinge three features hang on. Do third.*

- [x] **Person/owner concept** — tag events/feeds with an owner + color +
      avatar. DONE 2026-06-26 — a `people` registry (`{id,name,color,avatar}`,
      emoji avatars) in `feeds.json`; feeds carry `owner_id`; events resolve
      owner via their feed (no per-event stamping, so a dead feed's cached
      events can't carry a stale owner). `fetcher.py` flows `people` +
      per-feed `owner_id` into events.json; `server.py` adds people CRUD
      (`/api/people/{add,update,delete}`), owner assignment via
      `/api/feeds/update`, owner-aware `.ics` CATEGORIES, and people in
      `/api/info`. Wall legend shows avatars; the Settings sheet has a People
      editor (add/rename/recolor/avatar) and an owner dropdown per calendar.
      Covered in `tests/test_people.py`.

## Phase 3 — Features that ride on the keystone

- [ ] **Per-person agenda lanes / "who's home today"** strip — needs the person
      model.
- [ ] **Recurring chore rotation** — auto-assign chores by person/day; needs the
      person model.
- [ ] **Countdown widgets** — "12 days till the trip." Independent, easy win,
      slot in anytime here.

## Phase 4 — Input channels
*Now that the base is stable and tested.*

- [ ] **Photo capture** (see `docs-photo-architecture.md`) — add photos to the
      frame without SSHing in. Simpler of the two; good warm-up.
- [ ] **Voice control** (see `docs-voice-architecture.md`) — "what's on today?"
      reads cleanly once the person model exists.

## Phase 5 — The big one
*Highest risk; wants all the foundation in place. Do last.*

- [ ] **Two-way sync / RSVP** — write events back to Google/iCloud. Means auth,
      conflict handling, and write-failure modes. Depends on the test harness,
      health endpoint, and person model.

## Floating

- [ ] **Theming / seasonal palettes** — no dependencies; drop in anywhere as a
      palate cleanser between heavier phases.

---

**Recommended start:** knock out Phase 0 (mostly verification + one security
task), then do Phase 1 + 2 as a single "foundation" push — the unglamorous work
that makes the next stretch of features fast instead of painful.
