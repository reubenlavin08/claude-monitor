"""Wake-word voice assistant. Say 'hey grassy ...' to talk.

Pipeline:
  webrtcvad continuously segments mic audio into utterances
  faster-whisper transcribes each utterance locally
  Regex checks for the wake word; if found, the rest of the
  utterance is the command (or we listen for the next utterance).
  Command goes to MiniMax LLM, reply goes to MiniMax TTS (with
  Windows SAPI fallback if the API fails).

Reads MiniMax credentials from .env in this folder.
"""
from __future__ import annotations

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import asyncio

# Register the NVIDIA pip-wheel DLL directories (cublas, cudnn, cuda_nvrtc)
# so faster-whisper / ctranslate2 can find them at runtime. Must run BEFORE
# the faster_whisper import below.
try:
    import os as _os
    import nvidia as _nvidia
    # `nvidia` is a namespace package (no __file__). Use __path__ entry instead.
    _nv_root = list(_nvidia.__path__)[0]
    for _sub in ("cublas", "cudnn", "cuda_nvrtc"):
        _bin = _os.path.join(_nv_root, _sub, "bin")
        if _os.path.isdir(_bin):
            if hasattr(_os, "add_dll_directory"):
                _os.add_dll_directory(_bin)
            _os.environ["PATH"] = _bin + _os.pathsep + _os.environ.get("PATH", "")
except Exception as _e:
    print(f"[cuda dll setup skipped: {_e}]")

import http.server
import json as json_mod
import os
import queue
import re
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import edge_tts
import pygame

# Init pygame mixer once at startup so playback is instant later.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)

import numpy as np
import requests
import sounddevice as sd
import webrtcvad
from dotenv import load_dotenv
from faster_whisper import WhisperModel

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env", override=True)

API_KEY  = os.environ.get("MINIMAX_API_KEY", "").strip()
GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "").strip()   # optional on new platform
VOICE_ID = os.environ.get("MINIMAX_VOICE_ID", "Determined_Man")
MODEL    = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")
API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.chat").rstrip("/")

# Edge-TTS — free Microsoft online TTS. A Chinese voice fed English text reads
# the English using Mandarin phonetics → authentic Chinese-accented English.
EDGE_VOICE = os.environ.get("EDGE_VOICE", "zh-CN-YunjianNeural")
DASHBOARD_URL = "http://localhost:8765/api/state"

# Audio config
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SIZE = SAMPLE_RATE * FRAME_MS // 1000   # 480 samples
SILENCE_FRAMES_TO_END = int(600 / FRAME_MS)   # 600ms silence ends an utterance
MAX_UTTERANCE_FRAMES  = int(10_000 / FRAME_MS)  # 10s cap
MIN_SPEECH_FRAMES     = int(200 / FRAME_MS)   # 200ms minimum

# Wake word: "hey grassy" with common Whisper transcription variants.
# Permissive wake word match: triggers on ANY mention of grassy/grass/grace/
# similar Whisper-mishearings — with or without a "hey" prefix. Optional prefix
# words (hey/yo/ok/hi/the/a) are consumed by the match so command extraction
# (text[m.end():]) starts after both prefix and wake word.
WAKE_RE = re.compile(
    r'\b(?:(?:hey|yo|ok|okay|hi|the|a)[\s,.;:!\-]+)?'
    r'(?:grass(?:y|ey|ie|i|e)?|grace[yi]?|gracie|glassy|gassy|grasi|grasey)\b',
    re.IGNORECASE,
)

VAD = webrtcvad.Vad(2)  # 0-3, higher = more aggressive (less false positives)

# ---- Shared state for the dashboard ----

_state_lock = threading.Lock()
_state = {
    "status":         "loading",     # loading | idle | recording | processing | speaking
    "last_heard":     "",
    "last_command":   "",
    "last_reply":     "",
    "last_event_at":  time.time(),
    "started_at":     time.time(),
    "voice_id":       EDGE_VOICE,    # edge-tts is primary
    "model":          MODEL,
    "wake_pattern":   WAKE_RE.pattern,
    "transcripts":    [],            # rolling list of {"at":..., "text":..., "kind":...}
    "awaiting_cmd":   False,
}


def _update(**kw):
    with _state_lock:
        _state.update(kw)
        _state["last_event_at"] = time.time()


def _add_transcript(text: str, kind: str):
    with _state_lock:
        _state["transcripts"] = (
            [{"at": time.time(), "text": text, "kind": kind}] + _state["transcripts"]
        )[:20]
        _state["last_event_at"] = time.time()


DASHBOARD_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>grassy</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:wght@400;700&display=swap">
<style>
:root { --fg:#ff5a14; --fg-hi:#ffb380; --fg-faint:#7a3008; --bg:#0a0604; --warn:#ffcc00; --bad:#ff3030; }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: 'VT323', monospace; margin: 0; padding: 16px; }
h1 { font-size: 48px; margin: 0 0 8px 0; letter-spacing: 2px; }
.row { display: flex; gap: 16px; }
.box { border: 1px solid var(--fg-faint); padding: 12px; margin-bottom: 12px; background: rgba(255, 90, 20, 0.04); }
.label { color: var(--fg-faint); font-size: 18px; text-transform: uppercase; letter-spacing: 1px; }
.value { font-size: 24px; color: var(--fg-hi); word-wrap: break-word; min-height: 28px; }
.status { font-size: 38px; font-weight: bold; padding: 4px 12px; display: inline-block; }
.status.loading    { color: var(--fg-faint); }
.status.idle       { color: var(--fg); }
.status.recording  { color: var(--warn); animation: pulse 0.5s infinite alternate; }
.status.processing { color: var(--fg-hi); animation: pulse 0.8s infinite alternate; }
.status.speaking   { color: var(--bad); animation: pulse 0.4s infinite alternate; }
@keyframes pulse { from { opacity: 1; } to { opacity: 0.5; } }
.meta { color: var(--fg-faint); font-size: 16px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 4px 8px; font-size: 18px; }
th { color: var(--fg-faint); border-bottom: 1px solid var(--fg-faint); }
td.t { color: var(--fg-faint); width: 80px; }
td.k { color: var(--warn); width: 100px; text-transform: uppercase; }
.k-heard     { color: #ffcc00; }
.k-command   { color: var(--fg-hi); }
.k-reply     { color: #00ff88; }
.k-wake      { color: #66ccff; }
.k-milestone { color: #ff5050; font-weight: bold; }
.col { flex: 1; }
</style>
</head><body>
<h1>grassy ▸ voice dashboard</h1>
<div class="meta">
  voice <span id="voice-id" class="value" style="display:inline">—</span> ·
  model <span id="model-id" class="value" style="display:inline">—</span> ·
  uptime <span id="uptime" class="value" style="display:inline">0s</span> ·
  awaiting <span id="awaiting" class="value" style="display:inline">no</span>
</div>
<div class="box"><span class="label">status</span><br><span id="status" class="status loading">LOADING</span></div>
<div class="row">
  <div class="box col"><div class="label">last heard</div><div id="heard" class="value">—</div></div>
  <div class="box col"><div class="label">last command</div><div id="command" class="value">—</div></div>
  <div class="box col"><div class="label">last reply</div><div id="reply" class="value">—</div></div>
</div>
<div class="box">
  <div class="label">history</div>
  <table id="hist"><thead><tr><th>time</th><th>kind</th><th>text</th></tr></thead><tbody></tbody></table>
</div>
<script>
function fmtAgo(sec) {
  if (sec < 1) return 'now';
  if (sec < 60) return Math.round(sec) + 's ago';
  if (sec < 3600) return Math.round(sec/60) + 'm ago';
  return Math.round(sec/3600) + 'h ago';
}
function fmtUptime(sec) {
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm ' + Math.round(sec%60) + 's';
  const h = Math.floor(sec/3600);
  const m = Math.floor((sec%3600)/60);
  return h + 'h ' + m + 'm';
}
async function poll() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    const s = await r.json();
    const st = document.getElementById('status');
    st.textContent = s.status.toUpperCase();
    st.className = 'status ' + s.status;
    document.getElementById('voice-id').textContent = s.voice_id;
    document.getElementById('model-id').textContent = s.model;
    document.getElementById('uptime').textContent  = fmtUptime((Date.now()/1000) - s.started_at);
    document.getElementById('awaiting').textContent = s.awaiting_cmd ? 'YES' : 'no';
    document.getElementById('heard').textContent   = s.last_heard   || '—';
    document.getElementById('command').textContent = s.last_command || '—';
    document.getElementById('reply').textContent   = s.last_reply   || '—';
    const tb = document.querySelector('#hist tbody');
    tb.innerHTML = '';
    const now = Date.now() / 1000;
    for (const t of s.transcripts) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td class="t">' + fmtAgo(now - t.at) + '</td><td class="k k-' + t.kind + '">' + t.kind + '</td><td>' + (t.text || '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])) + '</td>';
      tb.appendChild(tr);
    }
  } catch (e) {
    document.getElementById('status').textContent = 'OFFLINE';
    document.getElementById('status').className = 'status loading';
  }
}
poll();
setInterval(poll, 500);
</script>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass   # silence per-request logs

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with _state_lock:
                payload = json_mod.dumps(_state).encode()
            self._send(200, payload, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        # External speak endpoint — every nag from elsewhere in the system
        # should come through here so the personality file + edge-tts voice
        # carry over. Body: {"prompt": "user-side LLM instruction"}.
        if self.path == "/api/speak":
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                body = json_mod.loads(raw or "{}")
            except Exception as e:
                self._send(400, json_mod.dumps({"error": f"bad json: {e}"}).encode(), "application/json")
                return
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._send(400, json_mod.dumps({"error": "prompt required"}).encode(), "application/json")
                return
            # Don't trample an in-flight mic conversation.
            if processing:
                self._send(429, json_mod.dumps({"error": "busy with mic"}).encode(), "application/json")
                return
            try:
                reply = ask_llm(prompt)
                _add_transcript(reply, "speak")
                _update(last_reply=reply, status="speaking")
                speak(reply)
                _update(status="idle")
                self._send(200, json_mod.dumps({"spoken": reply}).encode(), "application/json")
            except Exception as e:
                _update(status="idle")
                self._send(500, json_mod.dumps({"error": str(e)}).encode(), "application/json")
            return
        self.send_error(404)


def _run_dashboard():
    with socketserver.ThreadingTCPServer(("0.0.0.0", 8766), _Handler) as httpd:
        httpd.allow_reuse_address = True
        print("voice dashboard on http://localhost:8766")
        httpd.serve_forever()


threading.Thread(target=_run_dashboard, daemon=True).start()

def _try_load_whisper():
    """Try CUDA first; if either model load OR a test inference fails (e.g.
    missing cuBLAS DLL), fall back to CPU."""
    try:
        m = WhisperModel("tiny.en", device="cuda", compute_type="float16")
        # transcribe() returns a generator — must iterate to actually run
        # the encode/decode, which is when cuBLAS DLLs would be needed.
        segments, _info = m.transcribe(np.zeros(8000, dtype=np.float32),
                                       beam_size=1, language="en")
        list(segments)   # force execution
        print("whisper running on CUDA.")
        return m
    except Exception as e:
        print(f"CUDA unavailable ({e}), falling back to CPU.")
        return WhisperModel("tiny.en", device="cpu", compute_type="int8")

print("loading whisper tiny.en...")
WHISPER = _try_load_whisper()
print("ready.\n")
_update(status="idle")

SPEAK_PS1 = SCRIPT_DIR / "speak.ps1"


def transcribe_pcm(pcm_int16: np.ndarray) -> str:
    audio_f32 = pcm_int16.astype(np.float32) / 32768.0
    segments, _ = WHISPER.transcribe(audio_f32, beam_size=1, language="en")
    return " ".join(s.text for s in segments).strip()


def get_dashboard_context() -> str:
    try:
        r = requests.get(DASHBOARD_URL, timeout=2)
        d = r.json()
        ru = d.get("real_usage") or {}
        parts = []
        if "five_hour_pct" in ru: parts.append(f"5-hour usage: {ru['five_hour_pct']:.0f}%")
        if "week_all_pct" in ru: parts.append(f"weekly usage: {ru['week_all_pct']:.0f}%")
        if d.get("model_name"):   parts.append(f"model: {d['model_name']}")
        if d.get("cost_usd"):     parts.append(f"session cost: ${d['cost_usd']:.2f}")
        if d.get("title"):        parts.append(f"session title: {d['title']!r}")
        return "\n".join(parts) or "(no live data)"
    except Exception:
        return "(dashboard unreachable)"


# Personality / system prompt for the LLM. By default the assistant has a
# generic sarcastic-elder vibe; drop a `voice-personality.txt` next to this
# file to override with your own character — it isn't committed (gitignored)
# so each install can have its own voice.
_DEFAULT_SYSTEM_PROMPT = """You are "Grassy", a sarcastic voice assistant for
a Claude usage monitor. Your replies are spoken aloud, so:

  - 1 to 2 short sentences. Never longer.
  - English only. No emoji, no parentheses, no stage directions.
  - Never say "as an AI" or any disclaimer.
  - When asked about status / usage / cost / reset, refer to the live
    dashboard context provided below. Otherwise mock freely.

You sound like a tired older relative who can't believe the user is still
burning tokens on the same problem. Be sharp, brief, never vulgar."""

_personality_file = SCRIPT_DIR / "voice-personality.txt"
if _personality_file.exists():
    SYSTEM_PROMPT = _personality_file.read_text(encoding="utf-8").strip()
    print(f"[loaded personality from {_personality_file.name}]")
else:
    SYSTEM_PROMPT = _DEFAULT_SYSTEM_PROMPT


# Rolling memory of recent replies — fed back to the LLM so it doesn't
# repeat itself across calls. Pure variety mechanism, no other purpose.
from collections import deque as _deque
_recent_replies: _deque = _deque(maxlen=6)


def ask_llm(user_text: str) -> str:
    if not API_KEY:
        return "your minimax key isn't set up yet."
    ctx = get_dashboard_context()
    extra = ""
    if _recent_replies:
        extra += "\n\nThings YOU just said (in your last few replies). Do NOT "
        extra += "repeat any of these phrasings or vocab — vary completely:\n"
        for r in _recent_replies:
            extra += f"  - {r}\n"
    accuracy = (
        "\n\nACCURACY RULE: If you mention any percentage or number, use ONLY "
        "the exact figure(s) from the live dashboard context above. Never "
        "invent or round to a different number. If you cannot state the "
        "accurate number, do not state any number at all."
    )
    sys_msg = SYSTEM_PROMPT + "\n\nLive dashboard:\n" + ctx + extra + accuracy
    try:
        r = requests.post(
            f"{API_BASE}/v1/text/chatcompletion_v2",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": sys_msg},
                    {"role": "user",   "content": user_text},
                ],
                "max_tokens": 80,
                "temperature": 0.9,   # slightly higher = more variety
            },
            timeout=15,
        )
        data = r.json()
        base = data.get("base_resp", {})
        if base.get("status_code", 0) != 0:
            return f"api error: {base.get('status_msg', 'unknown')}"
        if "choices" in data and data["choices"]:
            choice = data["choices"][0]
            text = ""
            if "message" in choice:
                text = choice["message"].get("content", "").strip()
            elif "messages" in choice and choice["messages"]:
                text = choice["messages"][0].get("text", "").strip()
            if text:
                _recent_replies.append(text)
                return text
        return f"unexpected response shape: {list(data.keys())}"
    except Exception as e:
        return f"oops: {e}"


def speak_edge(text: str) -> bool:
    """Free Microsoft Edge TTS using a Mandarin voice on English text →
    Chinese-accented English. Playback is in-process via pygame.mixer."""
    try:
        # Use a unique filename per call so a still-held pygame handle never
        # blocks the next save (Windows file locking).
        out = Path(tempfile.gettempdir()) / f"grassy-edge-{int(time.time()*1000)}.mp3"
        async def _gen():
            comm = edge_tts.Communicate(text, EDGE_VOICE)
            await comm.save(str(out))
        asyncio.run(_gen())
        pygame.mixer.music.load(str(out))
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        # Release the file handle so we can delete it / next file isn't blocked.
        pygame.mixer.music.unload()
        try:
            out.unlink()
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[edge-tts exception: {e}]", flush=True)
        return False


def speak_minimax(text: str) -> bool:
    if not API_KEY:
        return False
    tts_url = f"{API_BASE}/v1/t2a_v2"
    if GROUP_ID:
        tts_url += f"?GroupId={GROUP_ID}"  # only legacy endpoint needs it
    try:
        r = requests.post(
            tts_url,
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "speech-02-turbo",   # faster than HD; minor quality dip but ~half the latency
                "text": text,
                "stream": False,
                "voice_setting": {"voice_id": VOICE_ID, "speed": 1.05, "vol": 1.0, "pitch": 0},
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
            },
            timeout=20,
        )
        data = r.json()
        if data.get("base_resp", {}).get("status_code", 0) != 0:
            print(f"[tts api error: {data.get('base_resp')}]")
            return False
        audio_hex = data.get("data", {}).get("audio")
        if not audio_hex:
            return False
        out = Path(tempfile.gettempdir()) / "grassy-reply.mp3"
        out.write_bytes(bytes.fromhex(audio_hex))
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Add-Type -AssemblyName presentationCore;"
             f"$p = New-Object System.Windows.Media.MediaPlayer;"
             f"$p.Open([uri]'{out}');$p.Play();"
             f"Start-Sleep -Milliseconds 500;"
             f"while ($p.NaturalDuration.HasTimeSpan -eq $false) {{ Start-Sleep -Milliseconds 100 }};"
             f"Start-Sleep -Seconds ([int]$p.NaturalDuration.TimeSpan.TotalSeconds + 1);"
             f"$p.Stop()"],
            creationflags=0x08000000,
        )
        return True
    except Exception as e:
        print(f"[tts exception: {e}]")
        return False


def speak_sapi(text: str, rate: int = 2) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-File", str(SPEAK_PS1),
         "-text", text, "-rate", str(rate)],
        creationflags=0x08000000,
    )


def speak(text: str) -> None:
    # edge-tts (Chinese voice on English) is the primary path now.
    # Falls back to MiniMax then SAPI if Edge is unreachable.
    if speak_edge(text):
        return
    if speak_minimax(text):
        return
    speak_sapi(text)


# ---- Milestone watcher: escalating proactive insults + grass-gate trigger ----

MILESTONES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
ESCALATION_TIERS = {
    # Tone descriptors only — no specific vocab forced, so the LLM picks
    # freshly from the personality file's pool every call.
    10:  "warm, grandfatherly, almost approving — barely a remark",
    20:  "gentle, like a kindly old man pouring tea — light observation",
    30:  "light teasing, raised eyebrow, observational not yet annoyed",
    40:  "mild concern about the trajectory, still patient",
    50:  "first frown, irritation begins",
    60:  "scolding, getting annoyed",
    70:  "stronger reprimand, real disappointment",
    80:  "harsh criticism, anger rising, invoke ancestors",
    90:  "savage condemnation, full uncle rage",
    100: "complete contempt, total disownment",
}
GRASS_REQUIRE_PCT = 50.0

_crossed_milestones: set[int] = set()
_grass_armed = True
_last_pct: float | None = None


def _milestone_watcher_thread():
    """Polls 5-hour usage every 30s. Fires one proactive milestone insult per
    cycle. Posts to /api/grass/require once when crossing GRASS_REQUIRE_PCT.
    State resets when pct drops sharply (new 5-hour window started)."""
    global _last_pct, _grass_armed
    while True:
        time.sleep(30)
        if processing:
            continue   # don't talk over an active conversation
        try:
            data = requests.get(DASHBOARD_URL, timeout=3).json()
            pct = (data.get("real_usage") or {}).get("five_hour_pct")
        except Exception:
            continue
        if pct is None:
            continue
        # Reset when pct drops a lot — assume new 5h window opened
        if _last_pct is not None and pct < _last_pct - 20:
            _crossed_milestones.clear()
            _grass_armed = True
        _last_pct = pct

        # Grass-gate trigger (fires once per window)
        if pct >= GRASS_REQUIRE_PCT and _grass_armed:
            try:
                requests.post("http://localhost:8765/api/grass/require", timeout=3)
                print(f"[milestone] requested grass at {pct:.0f}%", flush=True)
                _grass_armed = False
            except Exception as e:
                print(f"[grass require failed: {e}]", flush=True)

        # Find the HIGHEST un-crossed milestone the user has reached. If pct
        # jumped past several at once (e.g. 0% -> 65% between polls), we fire
        # only the top tier and mark all the lower ones as already crossed,
        # so the user gets ONE pointed insult, not a spam of escalating ones.
        highest = None
        for m in MILESTONES:
            if pct >= m and m not in _crossed_milestones:
                highest = m
        if highest is None:
            continue
        # Mark every milestone at or below current pct as fired
        for m in MILESTONES:
            if pct >= m:
                _crossed_milestones.add(m)

        tier = ESCALATION_TIERS[highest]
        actual = int(round(pct))
        prompt = (
            f"User just crossed the {int(highest)}% milestone on their "
            f"five-hour Claude token budget. ACTUAL current usage right now: "
            f"{actual}%. Reply in this tone: '{tier}'. If you state a "
            f"percent, use exactly {actual} (the current actual figure). "
            f"Never invent a different number. Stay under 14 words. No "
            f"greetings, no questions — one short spoken line."
        )
        try:
            text = ask_llm(prompt)
            _add_transcript(text, "milestone")
            _update(last_reply=text, status="speaking")
            speak(text)
            _update(status="idle")
        except Exception as e:
            print(f"[milestone insult failed: {e}]", flush=True)


threading.Thread(target=_milestone_watcher_thread, daemon=True).start()


# ---- Main listening loop ----

frame_q: queue.Queue = queue.Queue()

def audio_cb(indata, frames, time_info, status):
    pcm = (indata[:, 0] * 32767).clip(-32768, 32767).astype(np.int16)
    frame_q.put(pcm.tobytes())


print("listening. say 'hey grassy ...' to talk. Ctrl-C to quit.\n")

stream = sd.InputStream(
    samplerate=SAMPLE_RATE, channels=1, dtype="float32",
    blocksize=FRAME_SIZE, callback=audio_cb,
)
stream.start()

speech_frames: list[bytes] = []
silence_count = 0
in_speech = False
speech_frame_count = 0
waiting_for_command_until = 0.0
processing = False  # set while we're transcribing/calling APIs/speaking


def handle_utterance(pcm_bytes: bytes) -> None:
    global waiting_for_command_until, processing
    processing = True
    _update(status="processing")
    try:
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
        text = transcribe_pcm(pcm)
        print(f"HEARD: {text!r}")
        if not text:
            return
        _update(last_heard=text)
        _add_transcript(text, "heard")
        now = time.monotonic()
        command = None
        m = WAKE_RE.search(text)
        if m:
            command = text[m.end():].strip().lstrip(",.!?:")
            _add_transcript(m.group(0), "wake")
            if not command:
                waiting_for_command_until = now + 20.0   # generous window
                _update(awaiting_cmd=True, status="idle")
                print("(wake word detected, listening for command for 20s...)")
                # No audible "yeah?" cue anymore — it ate the response window
                # and could echo into the mic. Watch dashboard AWAITING flag.
                return
        elif now < waiting_for_command_until and text:
            command = text
            waiting_for_command_until = 0.0
            _update(awaiting_cmd=False)
        if not command:
            return
        _update(last_command=command, awaiting_cmd=False)
        _add_transcript(command, "command")
        print(f"COMMAND: {command!r}")
        reply = ask_llm(command)
        _update(last_reply=reply)
        _add_transcript(reply, "reply")
        print(f"REPLY: {reply}\n")
        _update(status="speaking")
        speak(reply)
    finally:
        processing = False
        _update(status="idle")
        # Drain queue so we don't process our own TTS playback as new audio
        while not frame_q.empty():
            try: frame_q.get_nowait()
            except queue.Empty: break


try:
    while True:
        chunk = frame_q.get()
        if processing:
            continue
        is_speech = VAD.is_speech(chunk, SAMPLE_RATE)
        if is_speech:
            if not in_speech:
                in_speech = True
                speech_frames = []
                speech_frame_count = 0
                _update(status="recording")
            speech_frames.append(chunk)
            speech_frame_count += 1
            silence_count = 0
            if speech_frame_count >= MAX_UTTERANCE_FRAMES:
                in_speech = False
        else:
            if in_speech:
                speech_frames.append(chunk)
                silence_count += 1
                if silence_count >= SILENCE_FRAMES_TO_END:
                    in_speech = False

        if not in_speech and speech_frames and speech_frame_count >= MIN_SPEECH_FRAMES:
            handle_utterance(b"".join(speech_frames))
            speech_frames = []
            speech_frame_count = 0
            silence_count = 0
        elif not in_speech and speech_frames:
            # Too short — discard
            speech_frames = []
            speech_frame_count = 0
            silence_count = 0
except KeyboardInterrupt:
    print("\nbye")
finally:
    stream.stop()
    stream.close()
