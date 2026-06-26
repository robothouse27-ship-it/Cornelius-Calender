# Family Calendar — Photo Capture (OCR) Spec

**Purpose:** Snap a photo of a grocery list or handwritten calendar on your phone and have it land on the wall as list items or calendar events. Local OCR by default; an opt-in cloud read for messy handwriting; always a confirm-before-it-lands preview. Build brief for Claude Code.

---

## 1. Design principles

1. **Reuse existing infra.** Capture rides the phone-to-box LAN pattern (QR/add-window from the import spec); structuring reuses the local LLM "brain" from the voice spec.
2. **Local by default, cloud by choice.** Tesseract handles printed/neat text on the box. Messy handwriting can be sent to a cloud vision model **only when the user opts in, per photo.**
3. **Never auto-commit.** OCR makes mistakes — every read goes to an editable preview the user confirms before anything touches the calendar or list.
4. **Privacy-first on the cloud path.** API (not consumer) tier, Zero Data Retention, metadata stripped, photo deleted after — see §5.

---

## 2. Pipeline

```
 phone camera ─► upload to box (Flask, token/add-window)
   │
   ▼
 preprocess (box): strip EXIF/GPS · auto-orient · downscale · optional deskew/crop (OpenCV)
   │
   ▼
 READ:
   • default  → Tesseract (local)              [printed / neat]
   • opt-in   → cloud vision API               [messy handwriting / calendar grids]
   • optional → local handwriting model        [PaddleOCR-VL / TrOCR — slow on A9]
   │
   ▼
 STRUCTURE: local LLM (Ollama) → JSON
   • list mode     → clean, de-duped items
   • calendar mode → events [{title, date_phrase, time, person?}]  (+ chrono-node resolves dates)
   │
   ▼
 PREVIEW & EDIT (wall or phone) — user fixes/confirms   ◄── REQUIRED GATE
   │
   ▼
 COMMIT: items → shopping list · events → calendar (local manual events)
   │
   ▼
 cleanup: delete uploaded photo (and never sent to cloud unless opted in)
```

---

## 3. The two modes

- **Grocery / to-do (high confidence).** Printed or neatly hand-printed lists read reliably with local Tesseract. Output is a flat item list. **Note:** committing requires the **shopping-list feature** (currently in the "future features" bucket) — a small add, build it alongside this.
- **Handwritten calendar (ambitious).** Hard for two reasons: reading the handwriting *and* mapping each scribble to the right day (layout). Local on the A9 is imperfect and slow; the **cloud vision path is dramatically better** here (near-perfect handwriting + layout understanding in one shot). Set expectations accordingly; lean on opt-in cloud for this mode.

---

## 4. OCR options (current landscape)

| Path | Tool | Strength | On the A9 |
|---|---|---|---|
| Local default | **Tesseract** | Printed/neat text | Fast, reliable |
| Local handwriting (optional) | **PaddleOCR-VL** / **TrOCR** | Some handwriting | Slow, imperfect — optional |
| Opt-in cloud | **Vision API** (e.g. Anthropic) | Messy handwriting + layout, near-perfect | N/A (offloaded) |

**Recommendation:** ship **Tesseract-local for print** + **opt-in cloud for handwriting**. Skip heavy local handwriting models initially — they're slow on the A9 and the cloud path covers that need far better. Add a local handwriting model later only if going cloud-free for handwriting becomes a priority (and ideally after a RAM upgrade).

---

## 5. Cloud privacy (when the user opts in)

- Use the provider's **API**, not a consumer login — API inputs are **not used for training** and have short/zero retention (e.g., Anthropic API: 7-day default, **Zero Data Retention** available).
- **Strip EXIF/GPS** in preprocessing before anything leaves the box.
- **One-shot, no cloud storage**; prefer ZDR so nothing persists remotely.
- **Opt-in per photo** with a clear on-screen indicator; sensitive notes can stay local-only.
- Default remains local; cloud is a deliberate "read this tricky one better" button.

---

## 6. UX requirements
- Clear local-vs-cloud choice per photo (default local; "read better (cloud)" as the opt-in).
- Editable preview before commit — add/remove/fix items, fix event dates/people.
- Show a "reading…" state (cloud or slow local takes a beat).
- Low-confidence / empty result → say so and offer the cloud read or a retake.

---

## 7. Build order
1. Capture + upload + preprocess (strip metadata, orient, downscale).
2. Tesseract local read → LLM structure (list mode) → preview → commit to shopping list (build the list feature here).
3. Calendar mode: events structuring + chrono-node + preview → commit.
4. Opt-in cloud vision path (API, ZDR, metadata-stripped) with the per-photo toggle.
5. (Optional/later) local handwriting model.

---

## 8. Things Benji decides
- Cloud vision provider/API for the opt-in handwriting path.
- Whether to build any local handwriting model now or rely on Tesseract-local + opt-in-cloud.
- Shopping-list feature scope (shared one list vs. per-person, recurring staples, etc.).
