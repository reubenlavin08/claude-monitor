"""Scrape Claude Code's /usage dialog by driving an interactive PTY.

This is the only reliable way to get the exact 5h/weekly percentages a
Claude subscription user sees in /usage — that data isn't exposed as a
file or API; Claude Code computes it from local sessions on the fly.

Strategy:
  1. Spawn `claude.cmd` in a 140x40 PTY.
  2. Background reader thread + queue (pywinpty's read() is blocking).
  3. Wait ~3s for the TUI to be ready.
  4. Send "/usage\\r".
  5. Wait until we see "% used" + "Resets" or hit hard deadline.
  6. Send Ctrl-C twice and force-terminate.

Returns a dict with parsed values, or {"error": ...} on failure.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
from typing import Any

try:
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None


def _resolve_claude_cmd() -> str:
    """Find the absolute path to claude.cmd. Required because the server may
    run under a Scheduled Task with a minimal PATH that omits %APPDATA%\\npm."""
    override = os.environ.get("CLAUDE_MONITOR_CLAUDE_CMD")
    if override and os.path.isfile(override):
        return override
    found = shutil.which("claude.cmd") or shutil.which("claude")
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = os.path.join(appdata, "npm", "claude.cmd")
        if os.path.isfile(candidate):
            return candidate
    return "claude.cmd"  # last-resort: hope PATH works


CLAUDE_CMD = _resolve_claude_cmd()


# Modern Claude Code (>=2.1.140) terminates OSC sequences with ST (\x1b\\),
# not BEL (\x07). The old OSC branch (`\x1b\][^\x07]*\x07`) would greedily
# eat the entire TUI buffer hunting for a BEL that never arrives, deleting
# the human-readable text the scraper needs to detect. This regex handles
# both terminators, plus CSI with private-mode params (<, >, =, space) and
# 7-bit DCS/PM/APC sequences.
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"             # CSI
    r"|\x1b\][^\x1b\x07]*(?:\x07|\x1b\\)"  # OSC, terminated by BEL or ST
    r"|\x1b[PX^_][^\x1b]*\x1b\\"            # DCS / SOS / PM / APC
    r"|\x1b[=>NMcDEHM78]"                   # 2-byte ESC sequences
)

# The /usage dialog renders with TUI spacing quirks ("16%used" / "16% used")
# so detect with a tolerant regex that matches what the parser will accept.
DIALOG_RENDERED_RE = re.compile(r"\d+(?:\.\d+)?\s*%\s*used", re.IGNORECASE)

# Multiple signals that Claude Code's TUI is fully rendered and ready for
# input. We don't rely solely on "❯" because ANSI cursor positioning can
# interleave the prompt char with escape sequences in ways that don't survive
# ANSI stripping. Match any of these in the compacted (whitespace-collapsed,
# lowercased) buffer — TUI rendering eats spaces unpredictably.
READY_MARKERS = ["❯", "automode", "shift+tab", "/effort", "/help"]


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _is_ready(buf: str) -> bool:
    plain = strip_ansi(buf).lower()
    compact = re.sub(r"\s+", "", plain)
    return any(m in compact for m in (m.lower() for m in READY_MARKERS))


def parse_dialog(text: str) -> dict[str, Any]:
    plain = strip_ansi(text)
    out: dict[str, Any] = {}

    sections = [
        ("five_hour",   r"current\s*session"),
        ("week_all",    r"current\s*week\s*\(?\s*all\s*models?\s*\)?"),
        ("week_sonnet", r"current\s*week\s*\(?\s*son+et\s*[oa0]?n?ly\s*\)?"),
    ]
    for key, header_re in sections:
        m = re.search(header_re, plain, re.IGNORECASE)
        if not m:
            continue
        rest = plain[m.end():]
        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%\s*used", rest, re.IGNORECASE)
        if m_pct and m_pct.start() < 400:
            out[f"{key}_pct"] = float(m_pct.group(1))
        m_reset = re.search(r"resets\s*([0-9a-zA-Z:,\s/()\-+]+)", rest[:600], re.IGNORECASE)
        if m_reset:
            reset_str = re.split(r"\s{2,}|\n", m_reset.group(1).strip())[0].strip()
            out[f"{key}_reset"] = reset_str

    out["__raw_len"] = len(text)
    out["__captured_at"] = time.time()
    return out


def _reader(proc, q: queue.Queue, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            data = proc.read(4096)
        except EOFError:
            q.put(None)
            return
        except Exception:
            q.put(None)
            return
        if data:
            q.put(data)


def scrape(timeout: float = 45.0, debug_path: str | None = None) -> dict[str, Any]:
    if PtyProcess is None:
        return {"error": "pywinpty not installed", "error_kind": "missing_dep"}

    proc = None
    try:
        spawn_env = dict(os.environ)
        spawn_env["DISABLE_AUTOUPDATER"] = "1"
        proc = PtyProcess.spawn(CLAUDE_CMD, dimensions=(40, 140), env=spawn_env)
    except Exception as e:
        return {"error": f"spawn failed: {e}", "error_kind": "spawn_failed"}

    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(target=_reader, args=(proc, q, stop), daemon=True)
    reader.start()

    start = time.time()
    deadline = start + timeout
    buf = ""
    saw_dialog = False
    state = "wait_ready"   # wait_ready -> typed -> entered -> done

    def drain(seconds: float = 0.0) -> str:
        end = time.time() + seconds
        out = ""
        while time.time() < end or not q.empty():
            try:
                c = q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                if time.time() >= end:
                    return out
                continue
            if isinstance(c, str):
                out += c
            else:
                return out
        return out

    try:
        # 1. Wait for any signal that the TUI is fully rendered and ready for
        # input. See READY_MARKERS — multi-signal detection because the "❯"
        # prompt char doesn't always survive ANSI stripping. 20s budget covers
        # claude.cmd cold-start (npm wrapper + node boot + TUI init).
        ready_deadline = time.time() + 20.0
        ready = False
        while time.time() < ready_deadline:
            buf += drain(0.4)
            if _is_ready(buf):
                ready = True
                # Let any remaining welcome render finish
                buf += drain(0.6)
                break

        if not ready:
            if debug_path:
                try:
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(buf)
                except OSError:
                    pass
            return {
                "error": "claude prompt never appeared",
                "error_kind": "prompt_timeout",
                "raw_len": len(buf),
                "tail": strip_ansi(buf)[-300:],
            }

        # 2. Send /usage. Try a few Enter variants if the first doesn't take.
        for attempt in range(3):
            try:
                if attempt == 0:
                    proc.write("/usage\r")
                elif attempt == 1:
                    # Maybe Enter wasn't recognized; send another \r
                    proc.write("\r")
                else:
                    # Last resort: try newline
                    proc.write("\n")
            except Exception:
                pass

            # Wait up to 8s for the dialog to render. We match a tolerant regex
            # because TUI rendering can collapse spaces ("16%used" vs "16% used"),
            # and the word "Resets" can briefly appear partially redrawn.
            stage_deadline = time.time() + 8.0
            while time.time() < stage_deadline:
                buf += drain(0.4)
                plain_check = strip_ansi(buf)
                if DIALOG_RENDERED_RE.search(plain_check) and re.search(r"[Rr]eset", plain_check):
                    saw_dialog = True
                    buf += drain(1.0)
                    break
            if saw_dialog:
                break
    finally:
        stop.set()
        try:
            proc.write("\x03")
            time.sleep(0.1)
            proc.write("\x03")
            time.sleep(0.1)
        except Exception:
            pass
        try:
            proc.terminate(force=True)
        except Exception:
            pass

    if debug_path:
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(buf)
        except OSError:
            pass

    if not saw_dialog:
        return {
            "error": "dialog not seen",
            "error_kind": "dialog_timeout",
            "raw_len": len(buf),
            "tail": strip_ansi(buf)[-300:] if buf else "",
        }

    parsed = parse_dialog(buf)
    if "five_hour_pct" not in parsed:
        parsed["error"] = "parse failed"
        parsed["error_kind"] = "parse_failed"
        parsed["tail"] = strip_ansi(buf)[-300:]
    return parsed


if __name__ == "__main__":
    import json
    import sys
    debug = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(scrape(debug_path=debug), indent=2))
