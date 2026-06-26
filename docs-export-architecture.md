# Family Calendar — Export & Phone Sync Spec

**Purpose:** Get the wall calendar's data *out* to everyone's phones — events as a subscribable feed, grocery/to-do lists in your pocket, and plain file export as a backstop. This is the "wall → phones" half of the data layer; it complements `calendar-import-architecture.md` (the "phones → wall" half). Build brief for Claude Code.

---

## 1. Design principles

1. **The box already has clean, merged data** (`events.json` from the import pipeline) — export just re-publishes it in formats phones understand.
2. **Read-only on phones.** Subscriptions and exports are for *viewing*. Editing happens in each person's own Google/Apple calendar, which already flows back to the wall via import. This closes the loop without two-way sync (see §6).
3. **Keep the box private to home wifi by default.** Anything that needs to work *away from home* uses a deliberate, secure path (§5) — never by exposing the box to the public internet.
4. **Reuse the existing Flask server + phone-to-box LAN pattern** (same infra as the QR-add flow).

---

## 2. Components

### 2a. Published merged calendar feed (`.ics`)
- Flask generates a single combined **family `.ics`** from `events.json` (all people, color/name as event categories), served at e.g. `/feed/family.ics?token=<secret>`.
- Phones **subscribe** to it (Apple Calendar / Google Calendar "Add calendar by URL"). The merged family view then appears natively inside everyone's phone calendar app, auto-refreshing.
- **Known behavior to document for the user:** subscribed feeds are **read-only** on the phone, and phones refresh them on *their own* schedule — Apple in particular can lag from ~15 min up to hours. Good for visibility, not live editing.

### 2b. Subscribe-QR (on the wall)
- A "Show phone feed" action on the wall renders a QR encoding the `webcal://<reachable-host>/feed/family.ics?token=…`.
- Scan → one tap → phone subscribes. Same UX as the add-QR, inverse direction.
- The token makes the feed URL an unguessable secret (treat like Google's "secret iCal address").

### 2c. List hand-off (grocery / to-do)
Two tiers:
- **At home (simple):** Flask serves a mobile list page on the LAN. Scan a QR → list opens on the phone → check items off → syncs back to the wall live (same box, same network). Great in the kitchen.
- **Travels with you (upgrade):** push the list into a phone-native app the phone already syncs everywhere — **Apple Reminders / Google Tasks / a shared note**. This is the "actually useful at the store, no wifi needed" version. Requires per-service integration; start with one (whichever the family uses).

### 2d. Plain export (backstop)
- An "Export" action: events → downloadable `.ics`; lists → `.txt`/`.csv`. AirDrop / email / save. Guarantees nothing is ever trapped on the box. Trivial to build.

---

## 3. The in-house vs. out-of-house question (the key decision)

Everything lives on the box on home wifi. "On our phones **at home**" is easy. "On our phones **at the store / at work**" means the phone must reach the box from outside — and that's where the real choice is. Three paths, pick one:

- **A. Home-only sync (simplest, most private).** Feed/lists are LAN-only. Phones refresh when on home wifi; away from home they still *show* the last-synced events (cached), they just won't pull new changes until back home. For many families this is plenty.
- **B. Private remote access via Tailscale (recommended for "everywhere").** Put the box + phones on a **Tailscale** tailnet (free, WireGuard-based mesh VPN). The feed/lists become reachable from anywhere over the private mesh — **without exposing the box to the public internet or opening any ports.** Best balance of "works everywhere" + "stays private." Modest one-time setup (install Tailscale on the box and each phone).
- **C. Publish to a cloud calendar.** The box writes the merged events into a dedicated shared **Google/iCloud "Family Wall" calendar** via API; phones sync it natively everywhere. Works flawlessly off-network, but reintroduces a cloud dependency + write-API/OAuth setup, and events then live in the cloud.

**Recommendation:** start with **A** (zero extra setup), add **B (Tailscale)** when you want true away-from-home sync. **C** only if you specifically want it inside Google/Apple's ecosystem.

---

## 4. Security

- Feed URLs carry an unguessable **token** (rotatable); treat as a secret like a private iCal address.
- **Never expose the box directly to the public internet / port-forward.** Out-of-house access goes through Tailscale (B) or a cloud calendar (C), both of which avoid a public attack surface.
- Lists page on the LAN: same add-window / token approach as the import QR flow if write access is involved.

---

## 5. The two-way loop (how editing still works without two-way sync)

```
  phone (own Google/Apple calendar)  ──edit──►  source calendar
            ▲                                         │  import pipeline
            │ subscribe (read-only family feed)       ▼
        merged feed  ◄── re-publish ──  wall  ◄──  events.json
```
Add an event on your phone in your normal calendar → import pulls it → it appears on the wall → the wall re-publishes the merged family feed → everyone sees it. No event editing on the wall feed itself is needed.

---

## 6. Build order

1. Generate the merged `family.ics` from `events.json`; serve it from Flask with a token.
2. Subscribe-QR on the wall; test subscribing on an iPhone + Android.
3. Plain export (`.ics` / `.csv` download).
4. List hand-off: LAN mobile page first; then one phone-native target (Reminders/Tasks).
5. (If chosen) Tailscale on box + phones for away-from-home access.

---

## 7. Things Benji decides

- **Away-from-home sync:** A (home-only) / B (Tailscale) / C (cloud calendar). Default A, B when wanted.
- Which phone-native app the travel list pushes to (Apple Reminders / Google Tasks / shared note).
- Whether the family feed is one merged calendar or one-per-person (per-person lets phones toggle people individually, but is more subscriptions to add).
```
