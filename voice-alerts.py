"""Voice insults played through Windows SAPI when 5-hour usage hits the
warn/crit bands. Polls the dashboard, fires insults on band transitions,
and re-fires every CRIT_REPEAT_SEC while pct is in crit.

Run via start-voice.bat in this folder, or directly:
    python voice-alerts.py
"""
from __future__ import annotations

import json
import random
import subprocess
import time
import urllib.request
from pathlib import Path

URL = "http://localhost:8765/api/state"
POLL_SEC = 5.0
CRIT_REPEAT_SEC = 30.0

SCRIPT_DIR = Path(__file__).parent
SPEAK_PS1  = SCRIPT_DIR / "speak.ps1"
CREATE_NO_WINDOW = 0x08000000

# Mild insults — fire on entry to warn band (80%)
WARN_INSULTS = [
    "hey. slow down there.",
    "you're at eighty percent. think before you prompt.",
    "context bar's getting full. you good?",
    "consider clearing.",
    "type more, prompt less.",
    "you're burning through your budget.",
    "this prompt did not need to be that long.",
    "have you tried reading the docs.",
]

# Harsh insults — fire on entry to crit band (90%) and every 30s in crit
CRIT_INSULTS = [
    "you have wasted your tokens.",
    "git gud.",
    "rip context. it's over.",
    "stop. you're embarrassing yourself.",
    "claude will not fix your life. type it yourself.",
    "touch grass. then come back.",
    "skill issue.",
    "opus is cooked. budget is cooked. you are cooked.",
    "n p c behavior detected.",
    "hallucinating once again.",
    "back to copy paste with you.",
    "this is why your parents are disappointed.",
    "your prompt was an embarrassment.",
    "imagine being this bad at coding.",
    "your context is gone. just like your hopes.",
]


def speak(text: str, rate: int = 4) -> None:
    """Spawn PowerShell SAPI in the background — non-blocking."""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-File", str(SPEAK_PS1),
             "-text", text, "-rate", str(rate)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        print(f"speak failed: {e}")


def fetch_pct():
    with urllib.request.urlopen(URL, timeout=3) as r:
        data = json.load(r)
    return (data.get("real_usage") or {}).get("five_hour_pct")


def band_for(pct):
    if pct is None: return None
    if pct < 80:    return "ok"
    if pct < 90:    return "warn"
    return "crit"


def main() -> None:
    last_band = None
    last_crit_at = 0.0
    print(f"voice-alerts polling {URL} every {POLL_SEC}s")
    while True:
        try:
            pct = fetch_pct()
        except Exception:
            time.sleep(POLL_SEC); continue

        band = band_for(pct)
        now = time.monotonic()

        if band != last_band:
            if band == "warn":
                text = random.choice(WARN_INSULTS)
                print(f"[warn] -> {text}")
                speak(text, rate=4)
            elif band == "crit":
                text = random.choice(CRIT_INSULTS)
                print(f"[crit] -> {text}")
                speak(text, rate=6)
                last_crit_at = now
            last_band = band
        elif band == "crit" and now - last_crit_at >= CRIT_REPEAT_SEC:
            text = random.choice(CRIT_INSULTS)
            print(f"[crit-repeat] -> {text}")
            speak(text, rate=6)
            last_crit_at = now

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
