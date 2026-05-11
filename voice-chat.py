"""Push-to-talk voice chat using:
  - faster-whisper (local) for speech-to-text
  - MiniMax LLM API for understanding
  - MiniMax TTS API for the reply voice

Hold Ctrl+Shift+Space to record. Release to send. Reply gets spoken.

Reads MiniMax credentials from .env in this directory.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import winsound
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv
from pynput import keyboard

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env", override=True)

API_KEY  = os.environ.get("MINIMAX_API_KEY", "").strip()
GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "").strip()
VOICE_ID = os.environ.get("MINIMAX_VOICE_ID", "Determined_Man")
MODEL    = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")
API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.chat").rstrip("/")

if not API_KEY:
    print("ERROR: MINIMAX_API_KEY missing from .env. Fill it in then re-run.")
    sys.exit(1)
# GROUP_ID is optional on the new platform.minimax.io API.

DASHBOARD_URL = "http://localhost:8765/api/state"
SAMPLE_RATE = 16000
CHANNELS = 1

print("loading whisper model (first run downloads ~75MB)...")
from faster_whisper import WhisperModel
WHISPER = WhisperModel("tiny.en", device="cpu", compute_type="int8")
print("whisper ready.")


# ---- Audio recording ----

class Recorder:
    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._stream:
                return
            self._frames = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
                callback=self._on_audio,
            )
            self._stream.start()
            print("[recording...]")

    def _on_audio(self, indata, frames, t, status):
        self._frames.append(indata.copy())

    def stop(self) -> Path | None:
        with self._lock:
            if not self._stream:
                return None
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return None
        audio = np.concatenate(self._frames, axis=0)
        path = Path(tempfile.gettempdir()) / f"voice-chat-{int(time.time()*1000)}.wav"
        sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
        duration = len(audio) / SAMPLE_RATE
        print(f"[recorded {duration:.1f}s -> {path.name}]")
        if duration < 0.3:
            return None
        return path


# ---- Whisper transcription ----

def transcribe(wav_path: Path) -> str:
    segments, info = WHISPER.transcribe(str(wav_path), beam_size=1)
    text = " ".join(seg.text for seg in segments).strip()
    return text


# ---- MiniMax LLM ----

def get_dashboard_context() -> str:
    try:
        r = requests.get(DASHBOARD_URL, timeout=2)
        d = r.json()
        ru = d.get("real_usage") or {}
        parts = []
        if "five_hour_pct" in ru:
            parts.append(f"5-hour usage: {ru['five_hour_pct']:.0f}%")
        if "week_all_pct" in ru:
            parts.append(f"weekly usage: {ru['week_all_pct']:.0f}%")
        if d.get("model_name"):
            parts.append(f"current session model: {d['model_name']}")
        if d.get("context_tokens"):
            parts.append(f"context tokens: {d['context_tokens']}")
        if d.get("cost_usd"):
            parts.append(f"session cost so far: ${d['cost_usd']:.2f}")
        if d.get("title"):
            parts.append(f"session title: {d['title']!r}")
        return "\n".join(parts) or "(no live dashboard data)"
    except Exception as e:
        return f"(dashboard unreachable: {e})"


SYSTEM_PROMPT = """You are the voice assistant for a homemade Claude usage
monitoring dashboard. The user (a high-school student) talks to you over a
laptop mic. Be helpful, very concise (1-2 sentences max — your reply is
spoken aloud), and a little sarcastic about their Claude token usage when
appropriate. Don't ramble. If they ask about their usage, refer to the live
dashboard context below. If they ask you to insult them, hit hard but stay
clean (no slurs)."""


def ask_llm(user_text: str) -> str:
    ctx = get_dashboard_context()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nLive dashboard:\n" + ctx},
            {"role": "user",   "content": user_text},
        ],
        "max_tokens": 200,
        "temperature": 0.8,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    r = requests.post(f"{API_BASE}/v1/text/chatcompletion_v2", headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


# ---- MiniMax TTS ----

def speak(text: str) -> None:
    payload = {
        "model": "speech-02-hd",
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": VOICE_ID, "speed": 1.05, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    tts_url = f"{API_BASE}/v1/t2a_v2"
    if GROUP_ID:
        tts_url += f"?GroupId={GROUP_ID}"
    r = requests.post(tts_url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    audio_hex = data.get("data", {}).get("audio")
    if not audio_hex:
        print(f"TTS failed: {data}")
        return
    audio_bytes = bytes.fromhex(audio_hex)
    out_path = Path(tempfile.gettempdir()) / f"reply-{int(time.time()*1000)}.mp3"
    out_path.write_bytes(audio_bytes)
    # winsound only plays WAV, so convert via .NET MediaPlayer or use a subprocess.
    # Easiest: use a PowerShell one-liner with Windows Media Foundation.
    import subprocess
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Add-Type -AssemblyName presentationCore; "
         f"$p = New-Object System.Windows.Media.MediaPlayer; "
         f"$p.Open([uri]'{out_path}'); $p.Play(); "
         f"Start-Sleep -Milliseconds 200; "
         f"while ($p.NaturalDuration.HasTimeSpan -eq $false) {{ Start-Sleep -Milliseconds 100 }}; "
         f"Start-Sleep -Seconds ([int]$p.NaturalDuration.TimeSpan.TotalSeconds + 1); "
         f"$p.Stop()"],
        creationflags=0x08000000,
    )


# ---- Push-to-talk orchestration ----

rec = Recorder()
_busy = threading.Lock()
_combo_held = {"ctrl": False, "shift": False, "space": False}


def _all_held():
    return _combo_held["ctrl"] and _combo_held["shift"] and _combo_held["space"]


def _process_audio(wav_path: Path):
    if not _busy.acquire(blocking=False):
        print("[still processing previous turn, skipping]")
        return
    try:
        text = transcribe(wav_path).strip()
        print(f"YOU: {text!r}")
        if not text or len(text) < 2:
            print("(too short, ignored)")
            return
        reply = ask_llm(text)
        print(f"BOT: {reply}")
        speak(reply)
    except Exception as e:
        print(f"error: {e}")
    finally:
        _busy.release()


def on_press(key):
    try:
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            _combo_held["ctrl"] = True
        elif key in (keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift):
            _combo_held["shift"] = True
        elif key == keyboard.Key.space:
            _combo_held["space"] = True
        if _all_held():
            rec.start()
    except Exception as e:
        print(f"key error: {e}")


def on_release(key):
    was_recording = _all_held()
    try:
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            _combo_held["ctrl"] = False
        elif key in (keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift):
            _combo_held["shift"] = False
        elif key == keyboard.Key.space:
            _combo_held["space"] = False
    except Exception:
        pass
    if was_recording and not _all_held():
        wav = rec.stop()
        if wav:
            threading.Thread(target=_process_audio, args=(wav,), daemon=True).start()


print(f"voice-chat ready. Hold Ctrl+Shift+Space to talk. Voice = {VOICE_ID}. Model = {MODEL}.")
with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
