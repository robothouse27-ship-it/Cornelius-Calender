# Family Calendar — Voice Control Architecture Spec

**Purpose:** Add fully-local, open-source voice control to the family calendar app, running on the single Linux Mint box (no separate server). This is a companion to `calendar-import-architecture.md` and a build brief for Claude Code.

> **⚠️ Status (superseded in part).** The fully-local design below was the original
> aspiration, but the A9 CPU proved too weak for good local STT/TTS. The shipped
> daemon (`voice.py`) now offloads the heavy stages to the cloud:
> - **Wake word:** local openWakeWord (unchanged) — fires only on the configured
>   word, with a threshold + 2-frame debounce to kill false triggers.
> - **Recording:** records *until you stop talking* via `webrtcvad`, not a fixed window.
> - **Hearing (STT):** **Deepgram Nova** (cloud). Local faster-whisper is the fallback.
> - **Understanding:** **Claude Haiku** (cloud), not a local Ollama model.
> - **Voice (TTS):** **Deepgram Aura** (cloud). Local Piper is the fallback.
>
> Keys (`DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`) go in the systemd unit; without
> them the daemon degrades to the local Whisper + Piper + rule-based path instead
> of failing. The §-by-§ local plan below is retained for context/fallback design.

---

## 1. Environment & constraints

- **Single machine:** HP All-in-One 24-f0xx — AMD A9-9425 (2 cores, ~2017), 8 GB RAM, spinning HDD (SSD strongly recommended), no usable GPU for ML. Linux Mint 22.3.
- **Hard requirements:** fully **open-source**, fully **local/offline**, **no accounts/keys**, nothing leaves the house.
- **Realistic target:** genuinely good understanding *of calendar commands*, with a short "thinking" latency (~1–4 s) after speech ends. Not a general open-domain assistant — the A9 can't do that snappily.

---

## 2. Design principles

1. **Separate hearing from understanding.** Transcription (Whisper) and intent-understanding (small LLM) are different stages with different costs.
2. **Scope the understanding tightly.** The model only ever classifies a calendar command and extracts fields — never free chats. Narrow task + strict output = a 1B model performs reliably.
3. **Force structured output.** The LLM must return JSON only (Ollama `format: json` or a llama.cpp GBNF grammar). This makes a tiny model reliable and faster (no wasted tokens).
4. **Offload the precise bits to deterministic code.** Dates → `chrono-node`; person/chore names → fuzzy-match against the known list. The model does the fuzzy "what do they want," code does the exact values.
5. **Keep the model small enough to stay resident.** Target ~1B (Q4, ~0.7–1 GB) so it coexists with the browser + Whisper in 8 GB. (16 GB RAM unlocks a ~3B model — see §9.)
6. **A dedicated voice service owns the mic** and drives the app — voice works independently of the browser.

---

## 3. Stack (all open-source, all local)

| Stage | Tool | Notes |
|---|---|---|
| Wake word | **openWakeWord** | Custom "Hey Calendar," permanent (no expiry), runs in the Python voice service. |
| Hearing (STT) | **faster-whisper** (Whisper tiny/base) | Accurate on short commands; CPU int8. |
| Understanding | **Ollama** + a tiny instruct model (**Llama 3.2 1B** / **Gemma 3 1B** / **Qwen 1.5B**) | JSON-constrained output. Swap model tag to scale with hardware. |
| Date parsing | **chrono-node** (in front-end) or `dateparser` (Python) | Deterministic; resolves "next Tuesday 3:30." |
| Voice out (TTS) | **Piper** | Local neural voice through the box speakers. |

---

## 4. Architecture & data flow

A new local **voice service** (Python) owns the microphone and runs the full loop, then talks to the existing Flask app over a websocket. The browser front-end only shows state + reflects actions.

```
 mic ─► voice_service.py
          │  1. openWakeWord  ── waits for "Hey Calendar"  (or push-to-talk signal from UI)
          │  2. faster-whisper ── records utterance ► text
          │  3. Ollama (tiny LLM, JSON mode) ── text ► {action, person, date_phrase, title, ...}
          │  4. chrono-node / dateparser + name-match ── normalize to real date + known person
          │  5. Piper ── speak confirmation through speakers
          ▼
     Flask app  ◄── websocket ──►  front-end (browser)
       • applies action (add event / navigate / mark chore / read agenda / sleep)
       • front-end shows "listening… / thinking…" + updates the calendar
       • for read-out: app sends today's agenda text back to the service to speak
```

- **Ollama** runs locally (`localhost:11434`); the voice service calls its `/api/chat` with `format: json`.
- **Mic permission:** if any capture happens in the browser, note that `getUserMedia` needs a secure context — `localhost` qualifies, so serving at `http://localhost:8080` works without HTTPS. (In this design the Python service owns the mic, so this mostly doesn't apply, but keep it in mind if any browser-side audio is added.)

---

## 5. Intent schema & command set

The LLM is prompted with the schema + few-shot examples and must output **only** JSON:

```json
{
  "action": "add_event | query_day | query_person | delete_event | clear_day | navigate | mark_chore | read_agenda | sleep | wake | none",
  "person": "string|null",        // matched against known feed/family names
  "date_phrase": "string|null",   // raw, e.g. "next Tuesday afternoon" — resolved by chrono-node
  "title": "string|null",
  "target": "string|null"         // e.g. chore name, or 'today'/'next month' for navigate
}
```

Supported spoken commands (examples):
- "What's on today / this week?" → `read_agenda`
- "What's Mom doing this weekend?" → `query_person`
- "Add soccer for Tommy next Tuesday at 3:30." → `add_event`
- "Clear my Friday." → `clear_day` *(confirm first — see §6)*
- "Mark feed-the-pig done." → `mark_chore`
- "Go to next month / take me to today." → `navigate`
- "Go to sleep." / wake on any speech → `sleep` / `wake`

---

## 6. UX & safety

- **Confirm destructive actions by voice before doing them.** `delete_event` / `clear_day` must read back what they'll remove and wait for a "yes." Never silently delete.
- **Visible state:** front-end shows a clear "listening…" and "thinking…" indicator so the latency beat is understood, not confusing.
- **Mute toggle** for the wake word (always-listening on/off), persisted.
- **Low-confidence fallback:** if Whisper confidence is low or the LLM returns `action: none`, speak "Sorry, didn't catch that" rather than guessing.
- **Manual-event scope:** voice-added events are local (consistent with read-only import design); they live in the app's local store, not pushed back to source calendars.

---

## 7. Phasing (each phase reuses the previous)

1. **Read-out (talk-back).** Wire Piper (or browser TTS) + a "What's on today?" trigger. Proves the speak path. Minimal.
2. **Push-to-talk (the real pipeline).** Mic button in the UI → voice service records → Whisper → Ollama JSON → normalize → execute → speak confirm. This builds the entire command loop. ~80% of the work.
3. **Wake word.** Add openWakeWord in front of phase 2 so it's hands-free. Mostly "swap the tap for a trigger" + the mute toggle + listening indicator.

---

## 8. Resilience

- Voice service runs as its own `systemd` service, `Restart=always`, after audio + network are up.
- If Ollama is unreachable or slow, time out gracefully and say so — never hang the UI.
- Keep model + Whisper **resident** (don't reload per request) to avoid cold-start stalls.
- Voice failure must never affect the calendar display — the app works fully without the voice service running.

---

## 9. Hardware notes (highest-leverage first)

- **Microphone is the #1 success factor.** Built-in AIO mic is okay for close-up push-to-talk; **always-listening across a kitchen needs a far-field USB mic** (~$30–60). Budget this for phase 3.
- **RAM 8 → 16 GB (~$25, in-box SODIMM):** the single best "understand better" upgrade — lets you run a ~3B model (Llama 3.2 3B / Phi-4 Mini) instead of 1B, noticeably smarter, still fully local.
- **SSD swap:** big help for model load times and overall responsiveness.

---

## 10. Out of scope / future

- **Open-domain conversation** (the A9 can't do this snappily; not a goal).
- **Offloading the LLM to a stronger LAN machine:** not available now (single box). If one is ever added, only the Ollama host address changes — the schema, voice service, and app stay identical. Design keeps this door open.

---

## 11. Build order

1. Stand up Ollama on the box; pull a 1B instruct model; confirm JSON-mode intent extraction from typed text (no audio yet).
2. Add `chrono-node`/`dateparser` + name-matching; verify `{action,…}` → real calendar actions.
3. Voice service: faster-whisper push-to-talk → existing pipeline; wire to app over websocket; Piper confirmations.
4. Front-end: mic button + "listening/thinking" indicators + action handling.
5. openWakeWord wake phrase + mute toggle.
6. `systemd` units; resilience; destructive-action confirmations.

---

## 12. Things Benji provides / decides

- Wake phrase (default "Hey Calendar").
- Final command list (the §5 set is the starting point).
- A far-field USB mic (by phase 3).
- Optional: 16 GB RAM and/or SSD for a smarter model + snappier feel.
- Confirm tiny-model pick after testing (1B for speed vs 3B if RAM is upgraded).
```
