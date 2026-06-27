#!/usr/bin/env python3
"""
voice.py — local, hands-free voice control for the family-calendar wall.

Phase 4 (Stage C) of docs-grocery-voice-plan.md. The wall is an AMD A9 with 2
cores and no usable ML GPU, so the heavy speech work is offloaded to the cloud
(Deepgram) when a key is present; only the always-listening wake word stays
local. Hearing and the voice fall back to local Whisper/Piper if the cloud key
is missing or a call fails, so the daemon never hard-fails.

Pipeline:
    openWakeWord (local)  →  record until you stop talking (webrtcvad)
      →  Deepgram Nova STT  (local faster-whisper fallback)  →  intent
      →  rule-based core commands (free, instant) or optional Claude fallback
      →  speak with Deepgram Aura  (local Piper fallback), else just log.

Core commands hit the same local HTTP API the wall uses, so a voice "add milk
to the groceries" lands on the shared list exactly like a tap would.

This daemon degrades gracefully: if the mic or a model is missing it logs why
and exits 0 (so systemd doesn't crash-loop) rather than raising. Run:
    .venv/bin/python voice.py
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
API_BASE = f"http://127.0.0.1:{os.environ.get('FAMILYCAL_PORT', '8080')}"
WAKE_WORD = os.environ.get("FAMILYCAL_WAKE_WORD", "hey_jarvis")  # an openWakeWord model name
WAKE_THRESHOLD = float(os.environ.get("FAMILYCAL_WAKE_THRESHOLD", "0.6"))
WHISPER_MODEL = os.environ.get("FAMILYCAL_WHISPER_MODEL", "tiny.en")
SAMPLE_RATE = 16000          # what openWakeWord + Whisper + Deepgram expect
TZ = os.environ.get("FAMILYCAL_TZ", "America/Los_Angeles")

# Cloud offload (Deepgram) — the wall's A9 CPU is too weak for good local STT/TTS,
# so hearing (Nova) and the voice (Aura) run in the cloud when a key is present.
# Without the key (or on any error) we fall back to local Whisper + Piper so the
# daemon never hard-fails. The "brain" stays Claude (see ask_claude).
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_API_KEY", "").strip()
STT_PROVIDER = os.environ.get("FAMILYCAL_STT_PROVIDER", "deepgram").strip().lower()
TTS_PROVIDER = os.environ.get("FAMILYCAL_TTS_PROVIDER", "deepgram").strip().lower()
DG_STT_MODEL = os.environ.get("FAMILYCAL_DG_STT_MODEL", "nova-2").strip()
DG_TTS_VOICE = os.environ.get("FAMILYCAL_DG_TTS_VOICE", "aura-asteria-en").strip()
DG_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DG_SPEAK_URL = "https://api.deepgram.com/v1/speak"


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


# what to say when the free-form brain isn't available — point at what works
CAPABILITIES = ("I can add things to the grocery list, tell you the weather, or "
                "say what's on today.")


def ask_claude(transcript):
    """Optional: let Claude answer a free-form question with a little day context."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return CAPABILITIES
    try:
        import anthropic
    except ImportError:
        return CAPABILITIES
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
                    CAPABILITIES).strip()
    except Exception as e:                       # offline / API error / no credit
        log("claude fallback failed:", e)
        # most common cause is an empty Claude balance — don't pretend it's broken
        if "credit balance is too low" in str(e):
            return CAPABILITIES
        return "I couldn't reach my brain just now."


# --------------------------------------------------------------------------- #
# speech out — Deepgram Aura if a key is set, else local Piper, else just log
# --------------------------------------------------------------------------- #
_piper = None      # lazily-loaded PiperVoice, cached across calls


def _play_wav_bytes(wav):
    """Pipe a complete WAV blob straight to aplay (no temp file)."""
    import subprocess
    subprocess.run(["aplay", "-q"], input=wav, check=False)


def deepgram_tts(text):
    """Synthesize speech with Deepgram Aura → WAV bytes (raises on error)."""
    q = urllib.parse.urlencode({"model": DG_TTS_VOICE, "encoding": "linear16",
                                "sample_rate": "24000", "container": "wav"})
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{DG_SPEAK_URL}?{q}", data=body, method="POST",
        headers={"Authorization": f"Token {DEEPGRAM_KEY}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def _piper_speak(text):
    """Local fallback voice. Never raises — degrades to a text-only log."""
    if not PIPER_VOICE or not os.path.exists(PIPER_VOICE):
        return                                    # no voice model → text-only (logged)
    global _piper
    import tempfile
    try:
        if _piper is None:
            from piper import PiperVoice
            _piper = PiperVoice.load(PIPER_VOICE)
        wav_path = tempfile.mktemp(suffix=".wav")
        with wave.open(wav_path, "wb") as wf:
            _piper.synthesize_wav(text, wf)       # Piper writes a complete WAV
        import subprocess
        subprocess.run(["aplay", "-q", wav_path], check=False)
        os.remove(wav_path)
    except Exception as e:                         # never let TTS crash the loop
        log("piper TTS failed:", e)


def speak(text):
    log("SAY:", text)
    if TTS_PROVIDER == "deepgram" and DEEPGRAM_KEY:
        try:
            _play_wav_bytes(deepgram_tts(text))
            return
        except Exception as e:                     # offline / API error / bad key
            log("deepgram TTS failed, falling back to piper:", e)
    _piper_speak(text)


# --------------------------------------------------------------------------- #
# audio in — record until the speaker stops, then transcribe
# --------------------------------------------------------------------------- #
def _pcm16_to_wav_bytes(pcm, rate=SAMPLE_RATE):
    """Wrap raw 16-bit mono PCM in a WAV container → bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def record_until_silence(stream):
    """Read 16k mono int16 frames from an open InputStream until the speaker
    stops talking. Returns raw PCM16 bytes. Falls back to a fixed window if
    webrtcvad isn't installed."""
    frame_ms = 20
    frame_len = int(SAMPLE_RATE * frame_ms / 1000)        # 320 samples @ 16k
    min_frames = int(1500 / frame_ms)                     # speak for ≥1.5s
    max_frames = int(12000 / frame_ms)                    # hard cap ~12s
    try:
        import webrtcvad
        vad = webrtcvad.Vad(2)                            # 0 lax … 3 strict
    except ImportError:
        vad = None
        log("webrtcvad not installed — using a fixed 4s window")
        max_frames = int(4000 / frame_ms)
    silence_limit = int(800 / frame_ms)                   # stop after ~800ms quiet
    collected = bytearray()
    silent = n = 0
    while True:
        frame, _ = stream.read(frame_len)
        pcm = frame[:, 0].tobytes()
        collected += pcm
        n += 1
        if vad is not None:
            if vad.is_speech(pcm, SAMPLE_RATE):
                silent = 0
            else:
                silent += 1
            if n >= min_frames and silent >= silence_limit:
                break
        if n >= max_frames:
            break
    return bytes(collected)


def deepgram_stt(wav_bytes):
    """Transcribe WAV bytes with Deepgram Nova → text (raises on error)."""
    q = urllib.parse.urlencode({"model": DG_STT_MODEL, "smart_format": "true",
                                "language": "en"})
    req = urllib.request.Request(
        f"{DG_LISTEN_URL}?{q}", data=wav_bytes, method="POST",
        headers={"Authorization": f"Token {DEEPGRAM_KEY}",
                 "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read().decode("utf-8"))
    return doc["results"]["channels"][0]["alternatives"][0]["transcript"].strip()


_whisper = None    # lazily-loaded local fallback model, cached across calls


def _local_whisper_stt(pcm_bytes):
    """Local fallback STT. Returns "" (never raises) if Whisper isn't installed."""
    global _whisper
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        log("local whisper not installed — no STT fallback available")
        return ""
    if _whisper is None:
        log("loading local whisper:", WHISPER_MODEL)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = _whisper.transcribe(audio, language="en", beam_size=1)
    return " ".join(s.text for s in segments).strip()


def stt(pcm_bytes):
    """PCM16 mono @ SAMPLE_RATE → transcript. Deepgram first, local Whisper next."""
    if STT_PROVIDER == "deepgram" and DEEPGRAM_KEY:
        try:
            return deepgram_stt(_pcm16_to_wav_bytes(pcm_bytes))
        except Exception as e:                     # offline / API error / bad key
            log("deepgram STT failed, falling back to local whisper:", e)
    return _local_whisper_stt(pcm_bytes)


def selftest():
    """Prove the cloud keys work without a mic: Aura synthesizes a phrase, Nova
    transcribes it back, and we check the round-trip. Run: voice.py --selftest"""
    if not DEEPGRAM_KEY:
        log("DEEPGRAM_API_KEY not set — nothing to self-test.")
        return 1
    phrase = "Add bananas to the grocery list."
    try:
        log("Aura: synthesizing", repr(phrase))
        wav = deepgram_tts(phrase)
        log("got", len(wav), "bytes of audio; Nova: transcribing…")
        heard = deepgram_stt(wav)                 # Aura returns a WAV container
    except Exception as e:
        log("SELFTEST FAILED:", e)
        return 1
    ok = "banana" in heard.lower()
    log("round-tripped transcript:", repr(heard))
    log("SELFTEST", "PASS" if ok else "CHECK — transcript didn't match")
    return 0 if ok else 2


def main():
    # Only the always-on wake word + audio I/O are required deps; Whisper is an
    # optional local fallback (loaded lazily in _local_whisper_stt). A missing
    # dep is a clean exit, not a crash-loop. The web app runs regardless.
    try:
        import sounddevice as sd
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

    log("STT:", STT_PROVIDER if DEEPGRAM_KEY else "whisper (no Deepgram key)",
        "| TTS:", TTS_PROVIDER if DEEPGRAM_KEY else "piper (no Deepgram key)")
    # openWakeWord 0.4.x ships its pretrained models (alexa, hey_jarvis,
    # hey_mycroft, …) as package data and auto-selects ONNX when tflite-runtime
    # isn't installed — which is exactly our case. Loading with no args brings
    # them all up; we then trigger only on the configured wake word below.
    wake = WakeModel()
    available = list(wake.models.keys())
    log("wake words available:", available)
    # Fire only on the configured word — no "any model" catch-all (false triggers).
    effective_wake = WAKE_WORD if WAKE_WORD in wake.models else (available[0] if available else None)
    if effective_wake != WAKE_WORD:
        log("configured wake word", repr(WAKE_WORD), "not loaded; using", repr(effective_wake))
    speak("Wall voice is ready.")
    log("listening for", repr(effective_wake), "(threshold", WAKE_THRESHOLD, ")…")

    block = int(SAMPLE_RATE * 0.08)              # 80 ms frames for the wake detector
    consec = 0                                   # consecutive frames over threshold
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=block) as stream:
        while True:
            frame, _ = stream.read(block)
            scores = wake.predict(frame[:, 0])
            score = scores.get(effective_wake, 0.0)
            consec = consec + 1 if score > WAKE_THRESHOLD else 0
            if consec >= 2:                       # debounce: need 2 frames in a row
                consec = 0
                log("wake! (score %.2f)" % score)
                speak("Yes?")
                pcm = record_until_silence(stream)   # record until you stop talking
                text = stt(pcm)
                log("heard:", repr(text))
                reply = handle(text)
                if reply:
                    speak(reply)
                if hasattr(wake, "reset"):
                    wake.reset()                 # avoid an immediate re-trigger
                time.sleep(0.3)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
