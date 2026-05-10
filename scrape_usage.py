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

import queue
import re
import threading
import time
from typing import Any

try:
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# The /usage dialog renders with TUI spacing quirks ("16%used" / "16% used")
# so detect with a tolerant regex that matches what the parser will accept.
DIALOG_RENDERED_RE = re.compile(r"\d+(?:\.\d+)?\s*%\s*used", re.IGNORECASE)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


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
        proc = PtyProcess.spawn("claude.cmd", dimensions=(40, 140))
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
        # 1. Wait for the prompt indicator "❯" — that's claude saying it's ready for input.
        # 20s budget: claude.cmd cold-start can take 10-15s on first launch (npm wrapper,
        # node boot, TUI init), and a tight 8s deadline was causing every first scrape
        # after server boot to fail.
        ready_deadline = time.time() + 20.0
        ready = False
        while time.time() < ready_deadline:
            buf += drain(0.4)
            if "❯" in buf:
                ready = True
                # Let any remaining welcome render finish
                buf += drain(0.6)
                break

        if not ready and "❯" not in strip_ansi(buf):
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
