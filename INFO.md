# claude-monitor — recovery & architecture info

If this thing breaks after a reboot, hand this whole file to a Claude Code
instance and tell it: *"read this and fix what's not working."* Everything you
need is below.

---

## What it does

A live wall-mounted dashboard showing **Claude Code's exact `/usage` numbers**
(5-hour window %, weekly window %, reset times) plus session context, cost,
and a list of other live Claude sessions. Designed to display on a 7" 800x480
Pi screen via Chromium kiosk over WiFi.

The dashboard data is **scraped from Claude Code's `/usage` slash command**
because Anthropic doesn't expose this data via API or in any local file —
it's computed on demand inside Claude Code.

---

## Architecture

```
┌─ Windows desktop (this machine, <desktop-lan-ip>) ──────┐
│                                                      │
│  Claude Code session(s)                              │
│      │                                               │
│      ▼ writes transcripts                            │
│  ~/.claude/projects/*/sessions/*.jsonl               │
│      │                                               │
│      ▼ watched by                                    │
│  server.py (FastAPI + watchdog, port 8765)           │
│      │                                               │
│      ├─ TranscriptCache: parses JSONL                │
│      │                                               │
│      ├─ ScrapeState (every 90s, ~7s each):           │
│      │    • spawns hidden `claude.cmd` via pywinpty  │
│      │    • types `/usage`                           │
│      │    • parses dialog text → 5h%, weekly%, ...   │
│      │                                               │
│      └─ broadcasts state via WebSocket /ws           │
│                                                      │
└──────────────────────────────────────────────────────┘
                       │
              HTTP/WebSocket on :8765
                       │
                       ▼
┌─ Pi 3 + 7" screen (on shelf, on WiFi) ───────────────┐
│  Chromium kiosk → http://<desktop-lan-ip>:8765          │
│  Renders the dashboard fullscreen                    │
└──────────────────────────────────────────────────────┘
```

Pi has **zero code** — it's just a browser.

---

## File map

| Path | What it is |
|---|---|
| `C:\Users\User\claude-monitor\server.py` | The FastAPI server. Run this. |
| `C:\Users\User\claude-monitor\scrape_usage.py` | The PTY scraper for `/usage`. |
| `C:\Users\User\claude-monitor\static\index.html` | Dashboard markup |
| `C:\Users\User\claude-monitor\static\style.css` | Dashboard styling (orange-on-black, 800x480) |
| `C:\Users\User\claude-monitor\static\app.js` | Dashboard JS (WebSocket + render) |
| `C:\Users\User\claude-monitor\requirements.txt` | Python deps: fastapi, uvicorn, watchdog, pywinpty |
| `C:\Users\User\claude-monitor\.venv\` | Python virtualenv (do not delete) |
| `C:\Users\User\claude-monitor\start.bat` | One-click launcher |
| `C:\Users\User\claude-monitor\debug-logs\` | Used by the legacy `--debug=api` path (not used by the scraper) |
| `C:\Users\User\claude-monitor\pi\` | Pi-side scripts (LED matrix driver + animations). Copy to the Pi to run. |
| `C:\Users\User\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1` | Defines a `claude` function that wraps real claude with `--debug=api --debug-file ...`. Was added for an earlier approach that turned out to be unnecessary; harmless to keep. |

---

## Starting the server (after a reboot)

The server is registered as a Scheduled Task that runs at user logon, so
it auto-starts every login. No manual action needed.

### How auto-start works

| Component | Path |
|---|---|
| Scheduled Task | `ClaudeMonitor` (visible in Task Scheduler) |
| Trigger | At logon, current user |
| Action | `wscript.exe "C:\Users\User\claude-monitor\start-hidden.vbs"` |
| `start-hidden.vbs` | Calls `start.bat` with WindowStyle=0 (real console, hidden) |
| `start.bat` | `cd /d %~dp0 && .venv\Scripts\python.exe server.py >> server.log 2>&1` |
| Logs | `claude-monitor\server.log` (rotated only manually) |

The VBScript wrapper matters: under a "Hidden" Scheduled Task action, `cmd`
gets no console at all, which means `pywinpty` can't attach a PTY for the
spawned `claude.cmd` child — and every scrape attempt times out. The VBS
launcher gives the bat a *real but invisible* console, which pywinpty needs.

### Manual start (still works)

```powershell
cd C:\Users\User\claude-monitor
.\start.bat
```

Or use the PowerShell helpers from your profile: `monitor` to start,
`monitor-stop` to kill.

### Re-registering / removing the task

```powershell
# inspect
Get-ScheduledTask -TaskName "ClaudeMonitor"

# disable / enable
Disable-ScheduledTask -TaskName "ClaudeMonitor"
Enable-ScheduledTask -TaskName "ClaudeMonitor"

# remove entirely
Unregister-ScheduledTask -TaskName "ClaudeMonitor" -Confirm:$false
```

You should see (when the server starts):
```
Claude monitor on http://0.0.0.0:8765
Watching: C:\Users\User\.claude\projects
Scrape: every 90s on success, 15s on failure
```

Real `/usage` data appears within ~15 seconds (first scrape).

---

## Health checks

```powershell
# Is the server alive?
curl http://localhost:8765/api/state

# Are scrapes succeeding?
curl http://localhost:8765/api/state | findstr "five_hour_pct"

# Raw scrape state (counters, errors)
curl http://localhost:8765/api/state
# look for "real_usage" → __successes / __failures / __last_error
```

---

## Configuration knobs (env vars)

Set these before launching to tune behavior:

| Env var | Default | What it does |
|---|---|---|
| `CLAUDE_MONITOR_SCRAPE_INTERVAL` | `90` | Seconds between `/usage` scrapes. Lower = fresher, more cost. |
| `CLAUDE_MONITOR_5H_CAP` | `171` | Fallback approximation cap (only used when scraper has no data) |
| `CLAUDE_MONITOR_WEEK_CAP` | `8209` | Fallback weekly cap |
| `CLAUDE_MONITOR_OPUS_1M` | `1` | Treat any Opus session as 1M context |

---

## Common problems

### Dashboard shows "—" forever / no data

Server isn't running, or no Claude Code session has been used yet.
1. Check the server is running: `curl http://localhost:8765/api/state`. If
   that fails, restart it (see "Manual start").
2. Use Claude Code at least once so a transcript exists.

### Real-data bars stuck at old values

The scraper failed. Check error:
```powershell
curl http://localhost:8765/api/state | findstr "__last_error"
```
Common causes:
- `claude.cmd` not on PATH → make sure `C:\Users\User\AppData\Roaming\npm` is in PATH
- pywinpty broken → `cd C:\Users\User\claude-monitor && .\.venv\Scripts\python.exe -m pip install --force-reinstall pywinpty`
- A previous `claude` from the scraper is hung → `taskkill /F /IM claude.exe` (warning: this also kills your interactive Claude Code window if open)

### Pi can't reach the dashboard

1. From the Pi: `curl http://<desktop-lan-ip>:8765` — should return HTML.
2. If "connection refused": Windows firewall is blocking. Open admin PowerShell and run:
   ```
   New-NetFirewallRule -DisplayName "Claude Monitor" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow -Profile Private
   ```
3. If desktop's IP changed: it's not <desktop-lan-ip> anymore. Find the new
   IP with `ipconfig` (look for the WiFi adapter's IPv4 Address) and update
   the URL on the Pi (in `~/.config/autostart/claude-monitor.desktop`).

### Colors look washed out / orange looks white

The Pi LCD has poor color reproduction. Adjust `--fg` and `--fg-hi` in
`static/style.css` toward more saturated red:
- Try `--fg: #ff5a14` (current)
- Or push harder: `--fg: #e63a00`, `--fg-hi: #ff5500`

### Layout overflow / clipped on the right

Adjust in `static/style.css`:
- Drop `.window-pct` font-size and min-width
- Reduce `.app` padding
- Reduce `.other-list .row` grid-template-columns

The dashboard targets 800x480. Specific media queries handle larger screens.

### Server crashes / uvicorn exits unexpectedly

Usually one of:
- Port 8765 in use → `netstat -ano | findstr :8765`, kill the offender
- Python deps drifted → `cd C:\Users\User\claude-monitor && .\.venv\Scripts\pip install -r requirements.txt`
- The claude binary moved/upgraded and broke pywinpty spawn → check `claude.cmd` resolves: `where.exe claude.cmd`

### Calibration is off (dashboard % doesn't match `/usage`)

This shouldn't happen now that we use the scraper. If it does, the scraper
is failing and the dashboard fell back to approximation. Fix the scraper
(see "Real-data bars stuck").

---

## How the scraper works (for diagnostics)

`scrape_usage.py:scrape()` does this every 90 seconds:

1. `winpty.PtyProcess.spawn("claude.cmd", dimensions=(40, 140))`
2. Spawn a background reader thread that pumps `proc.read()` output into a queue.
3. Drain the queue until `❯` appears (Claude Code's prompt indicator) — that's
   "ready for input".
4. Send `"/usage\r"` once. If no dialog appears within 6s, send `"\r"` again.
   Last resort: `"\n"`.
5. Read until both "Resets" and "% used" appear in the **ANSI-stripped** buffer.
6. Parse via tolerant regex (TUI rendering eats some inter-character spacing).
7. Send Ctrl-C twice and `proc.terminate(force=True)`.

The whole cycle takes ~6-8 seconds per scrape.

The scraper was originally part of an attempt to read API rate-limit headers
from `claude --debug=api` debug logs. That turned out not to work because
Claude Code (a) doesn't include rate-limit headers in OAuth-authenticated
responses, and (b) computes `/usage` locally rather than asking the API.
Hence the `/usage` PTY-scrape approach.

---

## What the dashboard shows

| Element | Source | Live? |
|---|---|---|
| `5-hour window %` | `/usage` scrape | every 90s |
| `weekly window %` | `/usage` scrape | every 90s |
| Reset times (e.g. "1:30 am (America/Tijuana)") | `/usage` scrape | every 90s |
| Context % (top right) | latest assistant message in active session's JSONL | live (file watcher) |
| Session cost | sum of token usage × pricing table | live |
| Other live sessions list | session JSONLs modified in last 30 min | live |
| Scraper self-cost stat | counters maintained in `ScrapeState` | live |

---

## Re-deriving things if needed

- **My desktop's IP**: `ipconfig` → WiFi adapter → IPv4 Address
- **My Claude install**: `where.exe claude.cmd`
- **My Claude data dir**: `%USERPROFILE%\.claude\` and `%USERPROFILE%\.claude.json`
- **My PowerShell profile**: `$PROFILE` (run in PowerShell)

---

## If everything's nuked, full reinstall

```powershell
# 1. Recreate venv
cd C:\Users\User\claude-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Open firewall (admin PS, one-time)
New-NetFirewallRule -DisplayName "Claude Monitor" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow -Profile Private

# 3. Start server
.\start.bat

# 4. Test
# Browser → http://localhost:8765
```

---

## Pi-side LED matrix (AIP1640 8x16)

In addition to the 7" Chromium-kiosk screen, the Pi has an **8x16 LED matrix**
wired up for ambient animations / status indicators.

| Wire | Pi pin |
|---|---|
| CLK (silkscreened `SCL`) | GPIO 6 (header pin 31) |
| DIN (silkscreened `SDA`) | GPIO 5 (header pin 29) |
| VCC | 5V or 3.3V (module-dependent) |
| GND | GND |

The driver chip is **AIP1640** — a TM1640 clone with a custom bit-banged
2-wire serial protocol. It is *not* I2C, so the standard `i2cdetect` /
`smbus` tooling will not find it, and the regular I2C pins (GPIO 2/3) are
unused by this device.

**Code:** [`pi/aip1640.py`](pi/aip1640.py) — driver class
**Demo:** [`pi/plane.py`](pi/plane.py) — scrolling airplane animation

To copy to the Pi (from the Windows desktop):
```powershell
scp C:\Users\User\claude-monitor\pi\*.py pi@<pi-ip>:~/led/
ssh pi@<pi-ip> "cd ~/led && python3 plane.py"
```

If `RPi.GPIO` isn't on the Pi: `sudo apt install -y python3-rpi.gpio`.

If the airplane renders mirrored or upside-down, the `render()` function
in `plane.py` takes `flip_h` / `flip_v` flags.

---

## Calibration values for fallback approximation

The hard-coded caps `CAP_5H = 171` and `CAP_WEEK = 8209` were calibrated
against the user's `/usage` reading on **Max 5x** plan when 5h was at 56%.
These values only matter if the scraper is broken. To recalibrate:

1. Run `/usage` in Claude Code → note the % shown.
2. `curl http://localhost:8765/api/state` → note `five_hour_msgs`.
3. New cap = `five_hour_msgs / (pct_shown / 100)`. Update `CAP_5H` in `server.py`.
4. Repeat for weekly with `week_msgs`.
