# Family Calendar — Project Overview & Build Blueprint

A DIY family command-center calendar — a private, self-hosted "better-than-Skylight" — running on a repurposed HP all-in-one touchscreen migrated to Linux Mint. This is the master index for the build; each linked spec is a self-contained brief for Claude Code.

---

## 1. Vision

A 24" pastel, touch-friendly family calendar that lives on the kitchen counter. It imports everyone's Apple/Google calendars (color-coded per person), takes voice commands locally ("Hey Calendar, add soccer Tuesday at 3"), turns a photo of a grocery list or handwritten calendar into entries, publishes back to everyone's phones, and shows a photo slideshow when idle. Free, private, and fully owned — no subscription, no required cloud.

---

## 2. Hardware & guiding constraints

- **One machine:** HP All-in-One 24-f0xx — AMD A9-9425 (2 cores, ~2017), 8 GB RAM, spinning HDD, ~24" 1080p single-touch screen, no usable GPU. Linux Mint 22.3 (XFCE).
- **Principles:** open-source, local-first, private. The weak A9 is the binding constraint — keep models small and scope tasks tightly. **Selective, opt-in cloud** is allowed for a couple of accuracy-critical, low-frequency tasks (messy-handwriting OCR), via privacy-strong API paths only.
- **Read-only sync model:** the wall *displays* calendars and *publishes* a merged view; editing happens in each person's own phone calendar, which flows back via import. This avoids two-way-sync complexity while still closing the loop.

---

## 3. System at a glance

```
   PHONES ──(import: .ics feeds)──►  ┌──────────── THE BOX (Linux Mint) ────────────┐
   (Google/Apple, each a person)     │  fetcher → events.json  (icalendar +          │
                                      │            recurring-ical-events)             │
   PHONES ◄─(export: merged .ics)──   │  Flask web server  → serves the app + JSON    │
                                      │  Ollama (tiny LLM) → the "understanding brain"│
   PHONE PHOTO ─(grocery/calendar)─►  │  voice service → openWakeWord+Whisper+Piper   │
                                      │  family-calendar.html → the pretty front-end  │
                                      │  kiosk layer → boots fullscreen, never sleeps │
                                      └───────────────────────────────────────────────┘
```

Everything lives on the box. The front-end stays "dumb and pretty"; all the hard work (parsing, understanding, recurrence) happens in local Python services that talk to it over Flask.

---

## 4. The documents

| # | Spec | Covers |
|---|------|--------|
| — | **family-calendar.html** | The working, styled app (month grid, agenda, chores, sleep slideshow, in-app color/family editor). The thing all specs wire into. |
| 1 | **calendar-import-architecture.md** | Phones → wall. The fetcher/parser, `events.json`, local web server, and the **QR-add flow** for adding calendars without typing. *The hard part.* |
| 2 | **export-phone-sync-architecture.md** | Wall → phones. Subscribable merged `.ics` feed + subscribe-QR, list hand-off, plain export, and home-vs-everywhere access (Tailscale). |
| 3 | **voice-control-architecture.md** | Local "Hey Calendar" voice — openWakeWord + faster-whisper + tiny LLM (JSON intents) + Piper, built for the A9. |
| 4 | **photo-capture-architecture.md** | Snap a grocery list / handwritten calendar → entries. Tesseract-local default, opt-in cloud for handwriting, confirm-before-commit. |
| 5 | **kiosk-setup-architecture.md** | The appliance layer — autologin, boot-to-fullscreen, never sleep, crash recovery, remote management. |

---

## 5. Unified tech stack

- **Front-end:** single-file HTML/CSS/JS (vanilla), pastel theme, touch-first, localStorage cache.
- **Web/server:** Python + **Flask** (serves app, JSON, QR flows, uploads).
- **Calendar parsing:** `icalendar` + **`recurring-ical-events`** (recurrence expansion).
- **The "brain" (voice + photo structuring):** **Ollama** running a small instruct model (Llama 3.2 1B / Gemma 3 1B / Qwen; 3B if RAM upgraded), JSON-constrained output.
- **Voice:** **openWakeWord** (wake) · **faster-whisper** (STT) · **Piper** (TTS) · `chrono-node` (dates).
- **OCR:** **Tesseract** (local) · opt-in cloud vision API (handwriting).
- **Scheduling/boot:** `systemd` services + timers; XFCE autostart for the kiosk browser.
- **Optional remote access:** **Tailscale** (private mesh, no public exposure).

---

## 6. Recommended overall build order

Each phase produces something usable; later phases reuse earlier ones.

- **Phase 0 — Appliance loop** (kiosk spec, partial import spec): Flask serves the existing app; box boots fullscreen, never sleeps. *Now it's a wall calendar, even before real data.*
- **Phase 1 — Calendar import** (import spec): fetcher → `events.json` → app reads real events; QR-add flow. *The core value.*
- **Phase 2 — Export / phone sync** (export spec): merged feed + subscribe-QR + plain export. *Closes the two-way loop.*
- **Phase 3 — Voice** (voice spec): read-out → push-to-talk → wake word. Stand up Ollama here (also used by Phase 4).
- **Phase 4 — Photo capture** (photo spec): grocery list (build the shopping-list feature) → handwritten calendar; opt-in cloud.
- **Sprinkled features** (separate small adds): shopping/to-do lists (needed by Phase 4), real weather API, meal planner, sticky notes, Week/Day views.

---

## 7. Hardware upgrades (by leverage)

1. **Far-field USB mic (~$30–60)** — the make-or-break for hands-free voice (Phase 3).
2. **8 → 16 GB RAM (~$25, in-box SODIMM)** — lets the brain run a smarter ~3B model; best "understands better" upgrade.
3. **SSD swap** — the single biggest fix for the "runs terribly" feel; faster boot, model loads, responsiveness.
4. **(Future) a stronger always-on PC** — if ever added, the Ollama brain moves to it over the LAN with zero redesign.

---

## 8. Cross-cutting decisions (Benji)

- Apple import: start with **public share links** (+ Google secret iCal); CalDAV private feeds later if needed.
- Away-from-home phone sync: **home-only** to start, **Tailscale** when "everywhere" is wanted.
- Voice engine: **fully open-source** (openWakeWord + Whisper + Ollama + Piper), single-box.
- Cloud: **off by default**; opt-in **per photo** for messy handwriting only, via API + ZDR + metadata-stripped.
- Wake phrase, family names/colors, refresh intervals, nightly reboot/dimming — set during build.

---

*Status: app built; all six specs drafted. Ready to hand to Claude Code, phase by phase.*
