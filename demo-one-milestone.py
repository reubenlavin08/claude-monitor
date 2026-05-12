"""Demo a single milestone tier. Pass the percent as argv[1] (default 90)."""
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

TIERS = {
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

pct = int(sys.argv[1]) if len(sys.argv) > 1 else 90
tier = TIERS[pct]

prompt = (f"The user just crossed {pct}% of their five-hour Claude "
          f"token budget. Reply in this tone: '{tier}'. State the percent "
          f"number. Stay under 14 words. No greetings, no questions — just "
          f"one short spoken line.")

r = requests.post(f"{BASE}/v1/text/chatcompletion_v2",
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type":"application/json"},
    json={"model":MODEL,
          "messages":[{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}],
          "max_tokens":80, "temperature":0.8}, timeout=20)
reply = r.json()["choices"][0]["message"]["content"].strip()
print(f"=== {pct}% — {tier} ===")
print(f"  {reply}")

out = Path(tempfile.gettempdir()) / f"one-milestone-{pct}-{int(time.time()*1000)}.mp3"
async def go(): await edge_tts.Communicate(reply, VOICE).save(str(out))
asyncio.run(go())
pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=512)
pygame.mixer.music.load(str(out))
pygame.mixer.music.play()
while pygame.mixer.music.get_busy(): time.sleep(0.05)
pygame.mixer.music.unload()
print("done")
