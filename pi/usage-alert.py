#!/usr/bin/env python3
"""Multi-mode AiP1640 matrix display + green LED + buzzer.

4 modes rotate randomly every 10 minutes. Warn/crit beep alerts unchanged.

Modes (each shows a context bar on the right half except `insult`):
  doomguy  - Doom HUD face on left, expression changes with band
  drunk    - Drunk stick figure walking; wobbles more at higher pct; falls in crit
  insult   - Full-width scrolling random rude phrase
  finger   - Middle finger that extends progressively with pct (0% closed fist,
             100% fully extended)
  plane    - Airplane sprite scrolling right-to-left across the full width

Hardware (unchanged):
  GPIO 5/6 -> matrix DIN/CLK
  GPIO17   -> green LED
  GPIO22   -> active buzzer
"""
from gpiozero import LED, Buzzer, OutputDevice
from time import sleep, monotonic
import urllib.request, json, random

URL = "http://192.168.1.242:8765/api/state"
POLL_SEC = 5.0
TICK_SEC = 0.25
CRIT_REALERT_SEC = 15
MODE_INTERVAL_SEC = 600  # 10 min between rotations

# ---- TM1640 driver ----
DIN = OutputDevice(5); CLK = OutputDevice(6); DIN.on(); CLK.on()

def _start(): DIN.on(); CLK.on(); DIN.off()
def _stop():  CLK.off(); DIN.off(); CLK.on(); DIN.on()
def _wb(b):
    for _ in range(8):
        CLK.off()
        DIN.on() if b & 1 else DIN.off()
        b >>= 1
        CLK.on()

def write_frame(cols, brightness=5):
    cols = list(cols)
    if len(cols) < 16: cols += [0] * (16 - len(cols))
    _start(); _wb(0x40); _stop()
    _start(); _wb(0xC0)
    for b in cols[:16]: _wb(b & 0xFF)
    _stop()
    _start(); _wb(0x88 | (brightness & 7)); _stop()


# ---- Pixel-art helper: list of 8 row strings -> column bytes ----
def art(rows):
    cols = []
    w = len(rows[0])
    for c in range(w):
        b = 0
        for r in range(8):
            if rows[r][c] != ".":
                b |= 1 << r
        cols.append(b)
    return cols


# ---- 3-wide font (some letters wider). Bit 0 = top row. ----
FONT = {
    "A": [0x7C, 0x12, 0x7C],   "B": [0x7E, 0x4A, 0x34],
    "C": [0x3C, 0x42, 0x42],   "D": [0x7E, 0x42, 0x3C],
    "E": [0x7E, 0x4A, 0x42],   "F": [0x7E, 0x0A, 0x02],
    "G": [0x3C, 0x42, 0x72],   "H": [0x7E, 0x08, 0x7E],
    "I": [0x42, 0x7E, 0x42],   "J": [0x20, 0x40, 0x3E],
    "K": [0x7E, 0x18, 0x66],   "L": [0x7E, 0x40, 0x40],
    "M": [0x7E, 0x04, 0x08, 0x7E], "N": [0x7E, 0x0C, 0x30, 0x7E],
    "O": [0x3C, 0x42, 0x3C],   "P": [0x7E, 0x12, 0x0C],
    "R": [0x7E, 0x0A, 0x74],   "S": [0x44, 0x4A, 0x32],
    "T": [0x02, 0x7E, 0x02],   "U": [0x3E, 0x40, 0x3E],
    "V": [0x1E, 0x60, 0x1E],   "W": [0x3E, 0x60, 0x60, 0x3E],
    "X": [0x66, 0x18, 0x66],   "Y": [0x06, 0x78, 0x06],
    "Z": [0x62, 0x5A, 0x46],   "!": [0x5E],   ".": [0x80],
}


def text_cols(text, letter_gap=1, word_gap=2):
    cols = []
    text = text.upper()
    for i, ch in enumerate(text):
        if ch == " ":
            cols += [0] * word_gap
            continue
        if ch not in FONT:
            continue
        if cols and text[i - 1] != " " and ch != ".":
            cols += [0] * letter_gap
        cols += FONT[ch]
    return cols


def bar(pct, width=8):
    n = max(0, min(width, int(round((pct or 0) / 100 * width))))
    return [0xFF] * n + [0] * (width - n)


def bright_for(pct):
    return max(1, min(7, int(round((pct or 0) / 100 * 7))))


# ---- 8-wide sprites ----
DOOMGUY_OK = art([
    "........",
    ".XX..XX.",
    ".XX..XX.",
    "........",
    "...XX...",
    "X......X",
    ".XXXXXX.",
    "........",
])
DOOMGUY_WARN = art([
    "........",
    ".XX..XX.",
    "..X..X..",
    "........",
    "........",
    "........",
    ".XXXXXX.",
    "........",
])
DOOMGUY_CRIT = art([
    "X......X",
    ".XX..XX.",
    ".XX..XX.",
    "........",
    "...XX...",
    ".XXXXXX.",
    "X......X",
    ".XXXXXX.",
])

STICK_STAND = art([
    "....",
    ".X..",
    "XXX.",
    ".X..",
    "XXX.",
    ".X..",
    "X.X.",
    "X.X.",
])
STICK_L = art([
    "....",
    ".X..",
    "XXX.",
    ".X..",
    "XXX.",
    ".X..",
    "X...",
    ".X..",
])
STICK_R = art([
    "....",
    ".X..",
    "XXX.",
    ".X..",
    "XXX.",
    ".X..",
    "..X.",
    "..X.",
])
STICK_FALL = art([
    "........",
    "........",
    "X.......",
    "X.X.....",
    "XXXXX...",
    ".XXX....",
    "..X.....",
    "..XX....",
])

INSULTS = [
    "WASTING TOKENS",
    "GIT GUD",
    "TRY HARDER",
    "RIP CONTEXT",
    "USE YOUR BRAIN",
    "BACK TO CLEAR",
    "BUDGET COOKED",
    "OPUS BURNED",
    "TAP THE SIGN",
    "TOUCH GRASS",
    "STOP IT",
    "CRY ABOUT IT",
    "TYPE IT YOURSELF",
    "AI SLOP",
    "HALLUCINATING",
    "READ THE DOCS",
    "COPING HARD",
    "LEARN PYTHON",
    "NPC BEHAVIOR",
    "IT IS OVER",
    "PROMPT MORE",
    "CLAUDE WONT FIX YOU",
    "TOUCH KEYBOARD",
    "AI BAD",
    "SKILL ISSUE",
    "HOPE NOT VIBES",
]


def finger_sprite(pct):
    """Return 8-col sprite: closed fist at 0% -> fully extended finger at 100%.
    Finger height grows in 4 stages so the gesture animates smoothly with pct."""
    height = max(0, min(4, int(round((pct or 0) / 100 * 4))))
    rows = []
    # Empty top filler, then finger rows of growing count, then fist (always 4 rows)
    for i in range(4):
        rows.append("..XXX..." if i >= 4 - height else "........")
    rows.append(".XXXXXX.")
    rows.append("XXXXXXX.")
    rows.append("XXXXXXX.")
    rows.append(".XXXXXX.")
    return art(rows)


# ---- Per-mode state ----
_scroll = {"insult": {"frame": [], "pos": 0}, "plane": {"x": 16}}


PLANE_SPRITE = art([
    ".............",
    ".X...........",
    ".XX....X.....",
    ".XXXXXXXXXXX.",
    "XXXXXXXXXXXXX",
    ".XXXXXXXXXXX.",
    ".......X.....",
    ".............",
])


# ---- Mode renderers — each returns (16-byte frame, brightness 0-7) ----

def mode_doomguy(pct, band, t):
    face = DOOMGUY_CRIT if band == "crit" else DOOMGUY_WARN if band == "warn" else DOOMGUY_OK
    # Center the 8-col face in the 16-col matrix
    return [0]*4 + face + [0]*4, max(2, bright_for(pct))


def mode_drunk(pct, band, t):
    walk = [STICK_STAND, STICK_L, STICK_STAND, STICK_R]
    fig = STICK_FALL if band == "crit" else walk[(t // 2) % 4]
    pos = t % 13
    out = [0] * 16
    wobble = (pct or 0) / 100
    for i, b in enumerate(fig):
        ix = pos + i
        if 0 <= ix < 16:
            shift = ((t + i) % 3) - 1 if wobble > 0.4 else 0
            if shift > 0:
                out[ix] = (b << shift) & 0xFF
            elif shift < 0:
                out[ix] = b >> -shift
            else:
                out[ix] = b
    return out, bright_for(pct)


def mode_insult(pct, band, t):
    s = _scroll["insult"]
    if not s["frame"] or s["pos"] >= len(s["frame"]) - 16:
        s["frame"] = [0]*16 + text_cols(random.choice(INSULTS)) + [0]*16
        s["pos"] = 0
    cols = s["frame"][s["pos"]:s["pos"] + 16]
    s["pos"] += 2   # 2 cols per tick = 8 cols/sec; finishes each insult in ~6s
    return cols, bright_for(pct)


def mode_finger(pct, band, t):
    return finger_sprite(pct) + bar(pct, 8), max(2, bright_for(pct))


def mode_plane(pct, band, t):
    s = _scroll["plane"]
    if s["x"] <= -len(PLANE_SPRITE):
        s["x"] = 16
    out = [0] * 16
    for i, b in enumerate(PLANE_SPRITE):
        px = s["x"] + i
        if 0 <= px < 16:
            out[px] = b
    s["x"] -= 1
    return out, max(2, bright_for(pct))


MODES = [
    ("doomguy", mode_doomguy),
    ("insult",  mode_insult),
    ("finger",  mode_finger),
    ("plane",   mode_plane),
]


# ---- Audio ----
green  = LED(17)
buzzer = Buzzer(22)


def beep(times, on_s, off_s):
    for _ in range(times):
        buzzer.on(); sleep(on_s)
        buzzer.off(); sleep(off_s)


def alert(kind):
    if kind == "warn":
        beep(3, 0.1, 0.1)
    else:
        beep(10, 0.05, 0.05)


def fetch_pct():
    with urllib.request.urlopen(URL, timeout=3) as r:
        data = json.load(r)
    return (data.get("real_usage") or {}).get("five_hour_pct")


# ---- Main loop ----
last_band = None
last_crit_alert = 0.0
last_fetch = 0.0
last_mode_change = monotonic()
pct = None
tick = 0
mode_idx = random.randrange(len(MODES))

print(f"start in mode: {MODES[mode_idx][0]}")
write_frame([0]*16, 0)

while True:
    now = monotonic()

    if now - last_fetch >= POLL_SEC:
        try:
            pct = fetch_pct()
        except Exception:
            pass
        last_fetch = now

    if pct is None:
        band = None
    elif pct < 80:
        band = "ok"
    elif pct < 90:
        band = "warn"
    else:
        band = "crit"

    if now - last_mode_change >= MODE_INTERVAL_SEC and len(MODES) > 1:
        new = mode_idx
        while new == mode_idx:
            new = random.randrange(len(MODES))
        mode_idx = new
        last_mode_change = now
        tick = 0
        print(f"rotate -> {MODES[mode_idx][0]}")

    name, fn = MODES[mode_idx]

    if band is None:
        write_frame([0]*16, 0)
        green.off()
    else:
        cols, br = fn(pct, band, tick)
        # Crit overlay: flash off every other tick + random pixel noise
        # on the "on" frames. Makes the display look genuinely angry.
        if band == "crit":
            if tick % 2 == 1:
                cols = [0]*16
                br = 0
            else:
                cols = list(cols)
                for i in range(16):
                    if random.random() < 0.15:
                        cols[i] = cols[i] | random.randint(0, 0xFF)
                br = 7
        write_frame(cols, br)
        green.on() if band == "ok" else green.off()

    if band != last_band:
        if band == "warn":   alert("warn")
        elif band == "crit": alert("crit"); last_crit_alert = now
        last_band = band
    elif band == "crit" and now - last_crit_alert >= CRIT_REALERT_SEC:
        alert("crit")
        last_crit_alert = now

    tick += 1
    sleep(TICK_SEC)
