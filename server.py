"""Claude Code monitor — watches ~/.claude/projects/ and serves a live
dashboard showing rolling 5-hour + weekly usage windows (primary),
plus session context %, cost, and a sparkline (secondary)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import scrape_usage

# ---------- config ----------

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
HOST = "0.0.0.0"
PORT = 8765
HISTORY_LEN = 120

ASSUME_OPUS_1M = os.environ.get("CLAUDE_MONITOR_OPUS_1M", "1") not in ("0", "false", "False", "")

# Pricing per million tokens (input, output, cache_write_5m, cache_read).
PRICING = {
    "opus":   (15.00, 75.00, 18.75, 1.50),
    "sonnet": ( 3.00, 15.00,  3.75, 0.30),
    "haiku":  ( 1.00,  5.00,  1.25, 0.10),
}
PRICING_1M_OPUS = (18.75, 93.75, 23.4375, 1.875)

def price_for(model_id: str, ctx_tokens: int) -> tuple[float, float, float, float]:
    mid = (model_id or "").lower()
    if "opus" in mid:
        if ctx_tokens > 200_000 or "1m" in mid:
            return PRICING_1M_OPUS
        return PRICING["opus"]
    if "sonnet" in mid: return PRICING["sonnet"]
    if "haiku"  in mid: return PRICING["haiku"]
    return PRICING["sonnet"]


# ---------- transcript cache ----------

def parse_ts(ts: str) -> float:
    """ISO-8601 (with optional 'Z') -> epoch seconds."""
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


class TranscriptCache:
    """Per-file cache of parsed assistant messages. Only re-parses when mtime changes."""
    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def refresh(self) -> None:
        if not CLAUDE_PROJECTS.exists():
            return
        for proj in CLAUDE_PROJECTS.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                key = str(f)
                old = self.entries.get(key)
                if old and old["mtime"] == mt:
                    continue
                self.entries[key] = self._parse(f, mt)

    def _parse(self, path: Path, mtime: float) -> dict:
        """Parse one JSONL into:
          - asst_msgs: assistant turns (with usage), used for context/cost/tokens
          - user_prompts: real user-typed messages (excludes tool_result turns)
          - title: best available title — /rename customTitle > ai-generated
            aiTitle > first user-typed prompt (truncated)
          - cwd: working directory recorded in any line
        Custom-title and ai-title entries can appear many times as the session
        evolves; we keep the LAST one of each.
        """
        asst_msgs: list[dict] = []
        user_prompts: list[dict] = []
        first_prompt_title = ""
        custom_title = ""
        ai_title = ""
        cwd = ""

        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not cwd:
                        cwd = obj.get("cwd", "") or cwd
                    typ = obj.get("type", "")

                    # Title-bearing entries don't carry timestamps; check those first
                    # so the no-timestamp guard below doesn't drop them.
                    if typ == "custom-title":
                        ct = obj.get("customTitle", "")
                        if ct:
                            custom_title = ct
                        continue
                    if typ == "ai-title":
                        at = obj.get("aiTitle", "")
                        if at:
                            ai_title = at
                        continue

                    ts = parse_ts(obj.get("timestamp", ""))
                    if ts == 0.0:
                        continue
                    msg = obj.get("message") or {}

                    if typ == "assistant":
                        u = msg.get("usage")
                        if not u:
                            continue
                        asst_msgs.append({
                            "ts":     ts,
                            "model":  msg.get("model") or "",
                            "input":  int(u.get("input_tokens", 0) or 0),
                            "output": int(u.get("output_tokens", 0) or 0),
                            "cw":     int(u.get("cache_creation_input_tokens", 0) or 0),
                            "cr":     int(u.get("cache_read_input_tokens", 0) or 0),
                        })
                    elif typ == "user":
                        text = self._extract_user_text(msg)
                        if text is None:
                            continue  # tool_result turn, skip
                        user_prompts.append({"ts": ts, "text": text})
                        if not first_prompt_title and text:
                            t = text.strip().replace("\n", " ")
                            first_prompt_title = t if len(t) <= 60 else t[:57] + "..."
        except OSError:
            pass

        # Priority: user-renamed > AI-generated > first prompt
        title = custom_title or ai_title or first_prompt_title

        return {
            "mtime":        mtime,
            "messages":     asst_msgs,        # kept name for backwards compat
            "user_prompts": user_prompts,
            "title":        title,
            "cwd":          cwd,
            "path":         path,
        }

    @staticmethod
    def _extract_user_text(msg: dict) -> str | None:
        """Return the user-typed text, or None if this is a tool_result turn."""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            has_tool_result = False
            for item in content:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t == "tool_result":
                    has_tool_result = True
                elif t == "text":
                    texts.append(item.get("text", ""))
            if texts:
                return "\n".join(texts)
            if has_tool_result:
                return None  # tool turn, no human text
        return None

    # ---- aggregations ----

    def latest_session(self) -> dict | None:
        """Most-recent session by mtime."""
        best: tuple[float, dict] | None = None
        for entry in self.entries.values():
            if not entry["messages"]:
                continue
            if best is None or entry["mtime"] > best[0]:
                best = (entry["mtime"], entry)
        return best[1] if best else None

    def session_by_id(self, session_id: str) -> dict | None:
        for entry in self.entries.values():
            if entry["path"].stem == session_id:
                return entry
        return None

    def live_sessions(self, within_seconds: float) -> list[dict]:
        """Sessions modified within the recent window, newest first."""
        cutoff = time.time() - within_seconds
        out = [e for e in self.entries.values()
               if e["messages"] and e["mtime"] >= cutoff]
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

cache = TranscriptCache()


# ---------- "is a Claude shell open?" detector ----------

# Cache the tasklist result so refresh()-on-every-JSONL-write doesn't shell
# out hundreds of times per second. 2s is plenty fresh for "did the user
# just /exit".
_PROC_TTL_SEC = 2.0
_proc_count_cache: dict = {"n": -1, "at": 0.0}
# CREATE_NO_WINDOW — suppress the brief console flash when spawning tasklist.
_CREATE_NO_WINDOW = 0x08000000


def _count_claude_exe() -> int:
    """Number of running claude.exe processes (any user). Returns -1 if
    enumeration failed — callers should treat -1 as 'unknown, assume alive'
    so a flaky tasklist doesn't wipe the dashboard."""
    now = time.time()
    if now - _proc_count_cache["at"] < _PROC_TTL_SEC:
        return _proc_count_cache["n"]
    try:
        result = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV", "/FI", "IMAGENAME eq claude.exe"],
            capture_output=True, text=True, timeout=3,
            creationflags=_CREATE_NO_WINDOW,
        )
        out = result.stdout or ""
        if "claude.exe" not in out.lower():
            n = 0
        else:
            n = sum(1 for line in out.splitlines()
                    if line.lower().startswith('"claude.exe"'))
    except Exception:
        n = -1
    _proc_count_cache["n"] = n
    _proc_count_cache["at"] = now
    return n


def claude_shells_open() -> int:
    """User-opened claude.exe shells. The scraper spawns its own short-lived
    claude.exe every ~90s, so we subtract one while it's in flight. Returns
    -1 if we couldn't enumerate (caller should fall back to legacy behavior
    and assume the shells exist)."""
    n = _count_claude_exe()
    if n < 0:
        return -1
    if scrape_state.in_flight:
        n = max(0, n - 1)
    return n


# ---------- /usage scraper (the real data source) ----------

SCRAPE_INTERVAL_SEC = float(os.environ.get("CLAUDE_MONITOR_SCRAPE_INTERVAL", "90"))
# After a failed scrape we retry much sooner — a single failure shouldn't leave
# the dashboard without fresh data for 90s.
SCRAPE_RETRY_SEC = float(os.environ.get("CLAUDE_MONITOR_SCRAPE_RETRY", "15"))


def _format_reset_string(s: str) -> str:
    """Re-insert spaces lost during TUI rendering: '1:30am(America/Tijuana)' -> '1:30am (America/Tijuana)'."""
    if not s:
        return s
    # Add space before ( and after , and between digit-letter boundaries we eat
    s = re.sub(r"\(", " (", s)
    s = re.sub(r",", ", ", s)
    s = re.sub(r"([A-Z][a-z]+)(\d)", r"\1 \2", s)  # "May16" -> "May 16"
    s = re.sub(r"\s+", " ", s).strip()
    return s


class ScrapeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.last_scrape_at: float = 0.0
        self.last_attempt_at: float = 0.0
        self.in_flight: bool = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="usage-scrape")
        self.last_error: str = ""
        # Self-cost telemetry
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.total_duration = 0.0
        self.last_duration = 0.0
        self.first_5h_pct: float | None = None  # first observed value
        self.first_5h_at: float = 0.0
        self.scrape_started_at: float = 0.0     # process clock for the current scrape

    def status(self) -> dict:
        d = dict(self.data)
        for key in ("five_hour_reset", "week_all_reset", "week_sonnet_reset"):
            if key in d:
                d[key] = _format_reset_string(d[key])
        d["__last_scrape_at"]  = self.last_scrape_at
        d["__last_attempt_at"] = self.last_attempt_at
        d["__in_flight"]       = self.in_flight
        d["__has_data"]        = bool(self.data and "five_hour_pct" in self.data)
        d["__last_error"]      = self.last_error
        # Self-cost stats
        d["__attempts"]        = self.attempts
        d["__successes"]       = self.successes
        d["__failures"]        = self.failures
        d["__avg_duration"]    = (self.total_duration / self.successes) if self.successes else 0.0
        d["__last_duration"]   = self.last_duration
        d["__first_5h_pct"]    = self.first_5h_pct
        d["__first_5h_at"]     = self.first_5h_at
        # Δ since first scrape — gives a rough idea of how much our scraping has consumed
        cur = d.get("five_hour_pct")
        if isinstance(cur, (int, float)) and isinstance(self.first_5h_pct, (int, float)):
            d["__delta_5h_pct"] = round(cur - self.first_5h_pct, 1)
        else:
            d["__delta_5h_pct"] = None
        return d

    async def maybe_scrape(self) -> bool:
        """Run a scrape if enough time has passed and none is in flight.

        Cadence depends on the last outcome: SCRAPE_INTERVAL_SEC (90s) after a
        successful scrape, SCRAPE_RETRY_SEC (15s) after a failure. So a single
        failure stalls the dashboard for 15s, not 90s.
        """
        now = time.time()
        if self.in_flight:
            return False
        last_failed = self.failures > 0 and self.last_attempt_at > self.last_scrape_at
        gate = SCRAPE_RETRY_SEC if last_failed else SCRAPE_INTERVAL_SEC
        if now - self.last_attempt_at < gate:
            return False
        self.in_flight = True
        self.last_attempt_at = now
        self.scrape_started_at = now
        self.attempts += 1
        loop = asyncio.get_running_loop()
        try:
            # Pass a debug path so we can inspect what claude.cmd actually
            # produced when a scrape fails — useful when the scheduled-task
            # environment behaves differently from interactive PowerShell.
            debug_path = str(Path.home() / "claude-monitor" / "debug-logs" / "last-scrape.txt")
            try:
                Path(debug_path).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                debug_path = None
            result = await loop.run_in_executor(
                self.executor,
                lambda: scrape_usage.scrape(debug_path=debug_path),
            )
            duration = time.time() - self.scrape_started_at
            self.last_duration = duration
            if result and "five_hour_pct" in result:
                self.data = result
                self.last_scrape_at = time.time()
                self.last_error = ""
                self.successes += 1
                self.total_duration += duration
                if self.first_5h_pct is None:
                    self.first_5h_pct = float(result["five_hour_pct"])
                    self.first_5h_at = self.last_scrape_at
                return True
            else:
                err = result.get("error", "no data") if isinstance(result, dict) else "scrape failed"
                tail = result.get("tail", "") if isinstance(result, dict) else ""
                self.last_error = err
                if tail:
                    # Stash the tail snippet so we can diagnose remotely.
                    self.last_error = f"{err} | tail: {tail[-160:]!r}"
                self.failures += 1
                return False
        except Exception as e:
            self.last_error = f"exception: {e}"
            self.failures += 1
            return False
        finally:
            self.in_flight = False


scrape_state = ScrapeState()


def session_snapshot(entry: dict) -> dict:
    """Per-session: model, context tokens, cost, messages, etc."""
    msgs = entry["messages"]
    if not msgs:
        return {}
    model_id = ""
    cost = 0.0
    for m in msgs:
        if m["model"]:
            model_id = m["model"]
        ctx = m["input"] + m["cw"] + m["cr"]
        p_in, p_out, p_cw, p_cr = price_for(m["model"] or model_id, ctx)
        cost += (
            m["input"]  * p_in  / 1_000_000
            + m["output"] * p_out / 1_000_000
            + m["cw"]     * p_cw  / 1_000_000
            + m["cr"]     * p_cr  / 1_000_000
        )

    last = msgs[-1]
    context_tokens = last["input"] + last["cw"] + last["cr"]

    mid_low = (model_id or "").lower()
    is_1m = (
        "1m" in mid_low
        or context_tokens > 200_000
        or (ASSUME_OPUS_1M and "opus" in mid_low)
    )
    max_context = 1_000_000 if is_1m else 200_000

    name = "Claude"
    for k, v in (("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")):
        if k in mid_low:
            name = v
            break
    m_match = re.search(r"(\d+)-(\d+)", mid_low)
    if m_match:
        name = f"{name} {m_match.group(1)}.{m_match.group(2)}"

    path: Path = entry["path"]
    cwd = entry.get("cwd", "")
    cwd_leaf = ""
    if cwd:
        cwd_leaf = cwd.rstrip("\\/").split("\\")[-1].split("/")[-1]
    return {
        "model_id":      model_id,
        "model_name":    name,
        "context_tokens": context_tokens,
        "max_context":   max_context,
        "cost_usd":      round(cost, 4),
        "session_id":    path.stem,
        "project":       path.parent.name,    # legacy
        "title":         entry.get("title", "") or "(no prompts yet)",
        "cwd":           cwd,
        "cwd_leaf":      cwd_leaf,
        "messages":      len(msgs),
        "user_prompts":  len(entry.get("user_prompts", [])),
        "mtime":         entry["mtime"],
    }


# ---------- live state + WS broadcast ----------

LIVE_WINDOW_SEC = 30 * 60  # "live" = active in last 30 min


class State:
    def __init__(self) -> None:
        self.snapshot: dict = {}
        self.history: deque[dict] = deque(maxlen=HISTORY_LEN)
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.pinned_session_id: str | None = None
        self.grass_required: bool = False

    def _current_entry(self) -> dict | None:
        if self.pinned_session_id:
            entry = cache.session_by_id(self.pinned_session_id)
            if entry:
                return entry
            # pinned session disappeared; fall through to auto-follow
        # No open shells → no current session (dashboard clears). We don't
        # gate on mtime so a long-thinking shell still counts as active.
        if claude_shells_open() == 0:
            return None
        return cache.latest_session()

    async def refresh(self) -> bool:
        cache.refresh()
        current = self._current_entry()
        sess = session_snapshot(current) if current else {}

        # Build "other live sessions" list, excluding current. If no shells
        # are open at all, skip — otherwise we'd keep showing the last few
        # transcripts in the sidebar for 30 min after every shell is closed.
        live = [] if claude_shells_open() == 0 else cache.live_sessions(LIVE_WINDOW_SEC)
        current_id = sess.get("session_id")
        others = []
        for e in live:
            sid = e["path"].stem
            if sid == current_id:
                continue
            snap = session_snapshot(e)
            others.append({
                "session_id":     sid,
                "project":        snap.get("project", ""),
                "title":          snap.get("title", ""),
                "cwd_leaf":       snap.get("cwd_leaf", ""),
                "model_name":     snap.get("model_name", ""),
                "context_tokens": snap.get("context_tokens", 0),
                "max_context":    snap.get("max_context", 200_000),
                "cost_usd":       snap.get("cost_usd", 0.0),
                "messages":       snap.get("messages", 0),
                "user_prompts":   snap.get("user_prompts", 0),
                "mtime":          e["mtime"],
            })

        snap = {
            **sess,
            "other_sessions":    others,
            "pinned":            bool(self.pinned_session_id),
            "pinned_session":    self.pinned_session_id,
            "grass_required":    self.grass_required,
            "real_usage":        scrape_state.status(),
        }

        # Compare a small fingerprint to detect change
        def fp(s: dict) -> tuple:
            ru = s.get("real_usage") or {}
            return (
                s.get("context_tokens"),
                s.get("messages"),
                s.get("session_id"),
                s.get("pinned_session"),
                s.get("grass_required"),
                tuple((o["session_id"], o["mtime"]) for o in s.get("other_sessions", [])),
                ru.get("five_hour_pct"),
                ru.get("week_all_pct"),
                ru.get("week_sonnet_pct"),
                ru.get("__has_data"),
                ru.get("__last_attempt_at"),
            )

        changed = fp(snap) != fp(self.snapshot)
        if changed:
            self.snapshot = snap
            ru = snap.get("real_usage") or {}
            self.history.append({
                "t": time.time(),
                "tokens": snap.get("context_tokens", 0),
                "five_hour_pct": ru.get("five_hour_pct"),
                "week_all_pct": ru.get("week_all_pct"),
            })
        return changed

    async def set_pin(self, session_id: str | None) -> None:
        if session_id == "":
            session_id = None
        self.pinned_session_id = session_id
        async with self.lock:
            await self.refresh()
        await self.broadcast()

    async def set_grass_required(self, required: bool) -> None:
        self.grass_required = bool(required)
        async with self.lock:
            await self.refresh()
        await self.broadcast()

    def payload(self) -> dict:
        return {
            **self.snapshot,
            "history": list(self.history),
            "server_time": time.time(),
        }

    async def broadcast(self) -> None:
        if not self.clients:
            return
        msg = json.dumps(self.payload())
        dead: list[WebSocket] = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


state = State()


class TranscriptHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self.loop = loop
        self.queue = queue

    def _poke(self, path: str) -> None:
        if path.endswith(".jsonl"):
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)

    def on_modified(self, event):
        if not event.is_directory: self._poke(event.src_path)
    def on_created(self, event):
        if not event.is_directory: self._poke(event.src_path)


async def watcher_loop(queue: asyncio.Queue) -> None:
    while True:
        await queue.get()
        await asyncio.sleep(0.15)
        while not queue.empty():
            queue.get_nowait()
        async with state.lock:
            changed = await state.refresh()
        if changed:
            await state.broadcast()


async def heartbeat_loop() -> None:
    """Refresh periodically so rolling windows tick down even with no fs events."""
    while True:
        await asyncio.sleep(10.0)
        async with state.lock:
            changed = await state.refresh()
        if changed:
            await state.broadcast()


async def scrape_loop() -> None:
    """Periodically scrape /usage for the authoritative numbers.
    First scrape fires soon after startup; thereafter at SCRAPE_INTERVAL_SEC."""
    await asyncio.sleep(3.0)  # let server settle
    while True:
        did = await scrape_state.maybe_scrape()
        if did:
            async with state.lock:
                await state.refresh()
            await state.broadcast()
        await asyncio.sleep(15.0)


# ---------- FastAPI app ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    async with state.lock:
        await state.refresh()

    if CLAUDE_PROJECTS.exists():
        observer = Observer()
        observer.schedule(TranscriptHandler(loop, queue), str(CLAUDE_PROJECTS), recursive=True)
        observer.start()
    else:
        observer = None
        print(f"[warn] {CLAUDE_PROJECTS} does not exist yet")

    tasks = [
        asyncio.create_task(watcher_loop(queue)),
        asyncio.create_task(heartbeat_loop()),
        asyncio.create_task(scrape_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)


app = FastAPI(lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


# No-cache headers on every static asset so the Pi's kiosk Chromium always
# picks up frontend changes on reload instead of holding onto a stale build.
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        for k, v in NO_CACHE_HEADERS.items():
            resp.headers[k] = v
        return resp


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE_HEADERS)

@app.get("/api/state")
async def api_state():
    return state.payload()

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.add(ws)
    try:
        await ws.send_text(json.dumps(state.payload()))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = (msg or {}).get("action")
            if action == "focus":
                await state.set_pin(msg.get("session_id") or None)
            elif action == "auto":
                await state.set_pin(None)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.clients.discard(ws)


@app.post("/api/focus")
async def api_focus(session_id: str = ""):
    """Convenience HTTP endpoint to pin or clear (empty string)."""
    await state.set_pin(session_id or None)
    return {"pinned": state.pinned_session_id}


@app.post("/api/grass/require")
async def api_grass_require():
    await state.set_grass_required(True)
    return {"grass_required": True}


@app.post("/api/grass/dismiss")
async def api_grass_dismiss():
    await state.set_grass_required(False)
    return {"grass_required": False}


if __name__ == "__main__":
    import uvicorn
    print(f"Claude monitor on http://{HOST}:{PORT}")
    print(f"Watching: {CLAUDE_PROJECTS}")
    print(f"Scrape: every {SCRAPE_INTERVAL_SEC:.0f}s on success, {SCRAPE_RETRY_SEC:.0f}s on failure")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
