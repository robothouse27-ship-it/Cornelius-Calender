#!/usr/bin/env python3
"""
voice.py — local, hands-free voice control for the family-calendar wall.

Phase 4 (Stage C) of docs-grocery-voice-plan.md. Claude has no audio/STT API,
so *listening* runs entirely on the box; Claude is only consulted (optionally)
to interpret free-form questions. Everything local is tiny and CPU-only — the
wall is an AMD A9 with 2 cores and no usable ML GPU.

Pipeline:
    openWakeWord ("hey wall")  →  record ~5s (sounddevice)
      →  faster-whisper tiny.en (int8, CPU)  →  intent
      →  rule-based core commands (free, instant) or optional Claude fallback
      →  speak the reply with Piper (local TTS), falling back to logging.

Core commands hit the same local HTTP API the wall uses, so a voice "add milk
to the groceries" lands on the shared list exactly like a tap would.

This daemon degrades gracefully: if the mic or a model is missing it logs why
and exits 0 (so systemd doesn't crash-loop) rather than raising. Run:
    .venv/bin/python voice.py
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
API_BASE = f"http://127.0.0.1:{os.environ.get('FAMILYCAL_PORT', '8080')}"
WAKE_WORD = os.environ.get("FAMILYCAL_WAKE_WORD", "hey_jarvis")  # an openWakeWord model name
WHISPER_MODEL = os.environ.get("FAMILYCAL_WHISPER_MODEL", "tiny.en")
SAMPLE_RATE = 16000          # what openWakeWord + Whisper expect
RECORD_SECONDS = 5
TZ = os.environ.get("FAMILYCAL_TZ", "America/Los_Angeles")


def _piper_voice_path():
    """Explicit PIPER_VOICE wins; else auto-pick a .onnx voice in voices/."""
    env = os.environ.get("PIPER_VOICE", "").strip()
    if env:
        return env
    vdir = os.path.join(HERE, "voices")
    if os.path.isdir(vdir):
        for fn in sorted(os.listdir(vdir)):
            if fn.endswith(".onnx"):
                return os.path.join(vdir, fn)
    return ""


PIPER_VOICE = _piper_voice_path()


def log(*a):
    print("[voice]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# local API helpers (same endpoints the wall front-end uses)
# --------------------------------------------------------------------------- #
def api_get(path):
    with urllib.request.urlopen(API_BASE + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API_BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# intents — rule-based core commands first (free + instant), Claude as fallback
# --------------------------------------------------------------------------- #
def _events_for(predicate):
    try:
        doc = api_get("/events.json")
    except OSError:
        return []
    return [e for e in doc.get("events", []) if predicate(e)]


def _today_key():
    return datetime.now().strftime("%Y-%m-%d")


def _speak_agenda(scope):
    today = _today_key()
    if scope == "today":
        evs = _events_for(lambda e: e.get("date") == today)
        when = "today"
    else:  # this week — today through the next 7 days
        end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        evs = _events_for(lambda e: today <= (e.get("date") or "") <= end)
        when = "this week"
    if not evs:
        return f"Nothing on the calendar {when}."
    titles = [e.get("title", "an event") for e in evs[:5]]
    return f"You have {len(evs)} {'thing' if len(evs) == 1 else 'things'} {when}: " + \
           ", ".join(titles) + "."


def _add_grocery(item):
    item = item.strip(" .")
    if not item:
        return "I didn't catch what to add."
    try:
        api_post("/api/shopping/add", {"text": item})
        return f"Added {item} to the groceries."
    except OSError:
        return "Sorry, I couldn't reach the list."


def _weather():
    try:
        w = api_get("/api/weather")
    except OSError:
        return "I couldn't get the weather right now."
    if not w.get("ok"):
        return "I couldn't get the weather right now."
    return f"It's {w.get('temp', '?')} degrees and {w.get('label', 'out there')}."


# "add <X> to the groceries / shopping / list"
_ADD_RE = re.compile(r"\badd (.+?) to (?:the )?(?:grocery|groceries|shopping|list)", re.I)


def handle(transcript):
    """Return a spoken reply for a transcript, or None to stay silent."""
    t = transcript.lower().strip()
    if not t:
        return None
    m = _ADD_RE.search(t)
    if m:
        return _add_grocery(m.group(1))
    if "weather" in t:
        return _weather()
    if "today" in t and ("what" in t or "schedule" in t or "calendar" in t or "on" in t):
        return _speak_agenda("today")
    if ("week" in t) and ("what" in t or "schedule" in t or "calendar" in t or "on" in t):
        return _speak_agenda("week")
    # free-form → optional Claude fallback (only if a key is configured)
    return ask_claude(t)


def ask_claude(transcript):
    """Optional: let Claude answer a free-form question with a little day context."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "I'm not sure how to help with that."
    try:
        import anthropic
    except ImportError:
        return "I'm not sure how to help with that."
    # compact context: today's agenda + the grocery list
    try:
        agenda = _speak_agenda("today")
        groceries = ", ".join(it["text"] for it in
                              api_get("/api/shopping").get("items", [])) or "empty"
    except OSError:
        agenda, groceries = "", "unknown"
    model = os.environ.get("FAMILYCAL_VOICE_MODEL", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            system=("You are the family's kitchen wall assistant. Answer in one or two "
                    "short spoken sentences — no markdown, no lists. Today's agenda: "
                    f"{agenda} Grocery list: {groceries}."),
            messages=[{"role": "user", "content": transcript}],
        )
        return next((b.text for b in resp.content if b.type == "text"),
                    "I'm not sure how to help with that.").strip()
    except Exception as e:                       # offline / API error → stay graceful
        log("claude fallback failed:", e)
        return "I couldn't reach my brain just now."


# --------------------------------------------------------------------------- #
# speech out — Piper if available, else just log (so it's testable headless)
# --------------------------------------------------------------------------- #
_piper = None      # lazily-loaded PiperVoice, cached across calls


def speak(text):
    log("SAY:", text)
    if not PIPER_VOICE or not os.path.exists(PIPER_VOICE):
        return                                    # no voice model → text-only (logged)
    global _piper
    import subprocess
    import tempfile
    import wave
    try:
        if _piper is None:
            from piper import PiperVoice
            _piper = PiperVoice.load(PIPER_VOICE)
        wav_path = tempfile.mktemp(suffix=".wav")
        with wave.open(wav_path, "wb") as wf:
            _piper.synthesize_wav(text, wf)       # Piper writes a complete WAV
        subprocess.run(["aplay", "-q", wav_path], check=False)
        os.remove(wav_path)
    except Exception as e:                         # never let TTS crash the loop
        log("piper TTS failed:", e)


# --------------------------------------------------------------------------- #
# audio in — record a short utterance after the wake word
# --------------------------------------------------------------------------- #
def transcribe(model, audio):
    """audio: float32 numpy array at SAMPLE_RATE → transcript text."""
    segments, _ = model.transcribe(audio, language="en", beam_size=1)
    return " ".join(s.text for s in segments).strip()


def main():
    # All heavy deps are imported here so a missing one is a clean exit, not a
    # crash-loop. The web app keeps running regardless of the voice daemon.
    try:
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel
        from openwakeword.model import Model as WakeModel
    except ImportError as e:
        log("voice deps not installed (", e, ") — exiting. Run: "
            ".venv/bin/pip install -r requirements-voice.txt + sudo apt install libportaudio2")
        return 0

    try:
        if not any(d["max_input_channels"] > 0 for d in sd.query_devices()):
            log("no microphone present — exiting.")
            return 0
    except Exception as e:
        log("couldn't query audio devices (", e, ") — exiting.")
        return 0

    log("loading models (whisper:", WHISPER_MODEL, "wake:", WAKE_WORD, ")")
    whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    # openWakeWord 0.4.x ships its pretrained models (alexa, hey_jarvis,
    # hey_mycroft, …) as package data and auto-selects ONNX when tflite-runtime
    # isn't installed — which is exactly our case. Loading with no args brings
    # them all up; we then trigger only on the configured wake word below.
    wake = WakeModel()
    log("wake words available:", list(wake.models.keys()))
    speak("Wall voice is ready.")
    log("listening for the wake word…")

    block = int(SAMPLE_RATE * 0.08)              # 80 ms frames for the wake detector
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=block) as stream:
        while True:
            frame, _ = stream.read(block)
            scores = wake.predict(frame[:, 0])
            # fire on the chosen wake word if it's loaded, else on any model
            chosen = scores.get(WAKE_WORD)
            fired = chosen > 0.5 if chosen is not None else any(v > 0.5 for v in scores.values())
            if fired:
                log("wake!")
                speak("Yes?")
                # capture the command
                buf = sd.rec(int(RECORD_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                             channels=1, dtype="float32")
                sd.wait()
                text = transcribe(whisper, buf[:, 0])
                log("heard:", repr(text))
                reply = handle(text)
                if reply:
                    speak(reply)
                if hasattr(wake, "reset"):
                    wake.reset()                 # avoid an immediate re-trigger
                time.sleep(0.3)


if __name__ == "__main__":
    sys.exit(main())
