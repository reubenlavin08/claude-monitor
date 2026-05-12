"""Touch-Grass nag daemon — keeps reminding the user (in Grassy's voice)
that they're still locked out and need to go outside.

Lockout entry + the 50%-crossing milestone insult are already fired by
voice-wakeword.py's milestone watcher. This watcher fills the gap: it
nags AGAIN every GRASS_REPEAT_SEC while `grass_required` is still True,
so the user can't just ignore the screen and wait it out.

Everything spoken still goes through voice-wakeword.py's /api/speak
endpoint so the personality file + edge-tts voice carry through. No
TTS / LLM code lives here.

Env (read from .env):
    DASHBOARD_URL       http://localhost:8765/api/state
    VOICE_SPEAK_URL     http://localhost:8766/api/speak
    GRASS_REPEAT_SEC    seconds between repeat nags while still locked (default 45)
    GRASS_VOICE_POLL_SEC  poll interval (default 2)

Run:  .venv\\Scripts\\python.exe grass_voice.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env", override=True)

DASHBOARD    = os.environ.get("DASHBOARD_URL",   "http://localhost:8765/api/state")
VOICE_SPEAK  = os.environ.get("VOICE_SPEAK_URL", "http://localhost:8766/api/speak")
POLL_SEC     = float(os.environ.get("GRASS_VOICE_POLL_SEC", "2.0"))
REPEAT_SEC   = float(os.environ.get("GRASS_REPEAT_SEC",    "20.0"))

# Fired the moment grass_required flips True. The milestone watcher in
# voice-wakeword.py also speaks at the 50% crossing, but that's once per
# usage milestone — bouncing the gate (manual /api/grass/require, dismiss
# + re-trigger, etc.) won't re-fire it. So we always speak on rising edge.
ENTER_PROMPT = (
    "The user just got locked out: the touch-grass screen takeover just "
    "fired because they crossed the threshold. They must physically go "
    "outside and hold real grass up to a phone camera — an AI verifies "
    "it. Mock them and order them outside. ONE short spoken sentence."
)

REPEAT_PROMPT = (
    "The user is STILL locked out of the dashboard — they have not yet "
    "gone outside to show real grass to the camera. Nag him again, "
    "sharper this time. Vary the wording. ONE short spoken sentence."
)

# Fired on the falling edge (grass_required True -> False), i.e. the SigLIP
# detector confirmed real grass and auto-dismissed the lockout. Stay in
# character — a tiny moment of approval allowed but mostly still grumpy.
CONGRATS_PROMPT = (
    "The user finally went outside and showed real grass to the camera. "
    "The lockout just cleared. Acknowledge it briefly in character — "
    "begrudging approval is allowed for one breath, then go back to "
    "telling him to stop wasting tokens. ONE short spoken sentence."
)


def main() -> None:
    print(f"[grass-voice] dashboard: {DASHBOARD}")
    print(f"[grass-voice] speak via: {VOICE_SPEAK}")
    print(f"[grass-voice] repeat:    every {REPEAT_SEC:.0f}s while locked")
    last_required = False
    last_spoken_at = 0.0
    while True:
        try:
            d = requests.get(DASHBOARD, timeout=3).json()
            required = bool(d.get("grass_required"))
            now = time.time()
            rising  = required and not last_required
            falling = (not required) and last_required
            repeat  = required and last_required and (now - last_spoken_at) >= REPEAT_SEC
            if rising or repeat or falling:
                if rising:
                    prompt, tag = ENTER_PROMPT,    "ENTER"
                elif falling:
                    prompt, tag = CONGRATS_PROMPT, "CLEAR"
                else:
                    prompt, tag = REPEAT_PROMPT,   "REPEAT"
                try:
                    r = requests.post(VOICE_SPEAK, json={"prompt": prompt}, timeout=60)
                    if r.status_code == 200:
                        spoken = r.json().get("spoken", "")
                        print(f"[grass-voice] {tag} -> {spoken!r}")
                        last_spoken_at = time.time()
                    elif r.status_code == 429:
                        # Mic conversation in flight — back off briefly,
                        # try again on the next poll cycle.
                        print(f"[grass-voice] {tag} skipped: mic busy, retrying next cycle")
                    else:
                        print(f"[grass-voice] {tag} /api/speak {r.status_code}: {r.text[:120]}")
                except Exception as e:
                    print(f"[grass-voice] {tag} /api/speak failed: {e}")
            last_required = required
        except Exception as e:
            print(f"[grass-voice] poll error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
