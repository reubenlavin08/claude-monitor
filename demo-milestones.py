"""Walk through every escalation tier — show the LLM's actual reminder text
and speak it via edge-tts. Mirrors voice-wakeword's milestone watcher exactly."""
import os, asyncio, tempfile, time, sys
from pathlib import Path
from dotenv import load_dotenv
import requests, edge_tts, pygame

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env", override=True)
API_KEY = os.environ["MINIMAX_API_KEY"].strip()
BASE    = os.environ.get("MINIMAX_API_BASE", "https://api.minimax.io").rstrip("/")
MODEL   = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")
VOICE   = os.environ.get("EDGE_VOICE", "zh-CN-YunjianNeural")

pers = SCRIPT_DIR / "voice-personality.txt"
SYSTEM_PROMPT = pers.read_text(encoding="utf-8").strip() if pers.exists() else "You are a sarcastic voice assistant. Reply in one sentence."

MILESTONES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
TIERS = {
    10:  "warm, grandfatherly, barely a remark — almost approving him for being mindful",
    20:  "gentle, kindly old man pouring tea — light observation",
    30:  "light teasing, raised eyebrow, observational not yet angry",
    40:  "mild concern about the trajectory, still patient",
    50:  "first frown — mild irritation, calls him a stupid melon",
    60:  "scolding, pig-headed remarks, getting annoyed",
    70:  "stronger reprimand, rice-bucket language, eats and produces nothing",
    80:  "harsh criticism, invokes ancestors briefly, real anger",
    90:  "savage condemnation, full uncle rage, calls him young rabbit",
    100: "complete contempt, total disownment, throws him out verbally as trash human",
}

def ask(prompt):
    r = requests.post(
        f"{BASE}/v1/text/chatcompletion_v2",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user",   "content": prompt}],
              "max_tokens": 80, "temperature": 0.8},
        timeout=20)
    return r.json()["choices"][0]["message"]["content"].strip()

def say(text, idx):
    out = Path(tempfile.gettempdir()) / f"milestone-demo-{idx}-{int(time.time()*1000)}.mp3"
    async def go(): await edge_tts.Communicate(text, VOICE).save(str(out))
    asyncio.run(go())
    pygame.mixer.music.load(str(out))
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy(): time.sleep(0.05)
    pygame.mixer.music.unload()

pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)
print(f"Voice: {VOICE}   Model: {MODEL}\n")

for m in MILESTONES:
    tier = TIERS[m]
    user_prompt = (
        f"The user just crossed {m}% of their five-hour Claude "
        f"token budget. Reply in this tone: '{tier}'. State the percent "
        f"number. Stay under 14 words. No greetings, no questions — just "
        f"one short spoken line."
    )
    reply = ask(user_prompt)
    print(f"=== {m}% — {tier} ===")
    print(f"  {reply}\n")
    say(reply, m)
    time.sleep(0.6)
print("done")
