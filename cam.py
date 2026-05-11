"""Live USB webcam dashboard — the 'eyes' for the touch-grass detector.

Serves an MJPEG stream + a status page at http://localhost:8767 so you can
see what the camera sees. This is the preview UI; the CLIP grass detector
will hook into the same capture loop later and POST /api/grass/dismiss
on the main dashboard when real grass is shown.

Env vars:
  CAM_INDEX   — which camera (default 0; try 1, 2 if you have a built-in too)
  CAM_WIDTH   — capture width  (default 640)
  CAM_HEIGHT  — capture height (default 480)
  CAM_PORT    — http port      (default 8767)

Run:  .venv\\Scripts\\python.exe cam.py
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import time

import cv2

CAM_INDEX  = int(os.environ.get("CAM_INDEX", "0"))
CAM_WIDTH  = int(os.environ.get("CAM_WIDTH", "640"))
CAM_HEIGHT = int(os.environ.get("CAM_HEIGHT", "480"))
PORT       = int(os.environ.get("CAM_PORT", "8767"))
JPEG_QUALITY = 80

# ---------- shared state ----------

_frame_lock = threading.Lock()
_latest_jpeg: bytes | None = None
_stats = {
    "cam_index":  CAM_INDEX,
    "width":      0,
    "height":     0,
    "fps":        0.0,
    "frames":     0,
    "started_at": time.time(),
    "error":      "",
    "backend":    "",
}


# ---------- camera capture ----------

# Order matters: DirectShow is the most reliable on Windows for USB cams;
# MSMF is finicky with some devices; CAP_ANY is the last-resort fallback.
_BACKENDS = [
    ("CAP_DSHOW", cv2.CAP_DSHOW),
    ("CAP_MSMF",  cv2.CAP_MSMF),
    ("CAP_ANY",   cv2.CAP_ANY),
]


def _open_camera() -> tuple[cv2.VideoCapture | None, str]:
    for name, backend in _BACKENDS:
        cap = cv2.VideoCapture(CAM_INDEX, backend)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
            # Discard the first frame — some cams return black on first read.
            cap.read()
            return cap, name
        cap.release()
    return None, ""


def _capture_loop() -> None:
    global _latest_jpeg
    cap, backend_name = _open_camera()
    if cap is None:
        _stats["error"] = f"could not open camera index {CAM_INDEX}"
        print(f"[cam] {_stats['error']}")
        return
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    _stats["width"], _stats["height"] = actual_w, actual_h
    _stats["backend"] = backend_name
    print(f"[cam] index={CAM_INDEX} backend={backend_name} {actual_w}x{actual_h}")

    fps_t0 = time.time()
    fps_n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            _stats["error"] = "cap.read() returned false"
            time.sleep(0.2)
            continue
        if _stats["error"]:
            _stats["error"] = ""
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ok:
            with _frame_lock:
                _latest_jpeg = buf.tobytes()
            _stats["frames"] += 1
            fps_n += 1
        now = time.time()
        if now - fps_t0 >= 1.0:
            _stats["fps"] = round(fps_n / (now - fps_t0), 1)
            fps_t0 = now
            fps_n = 0


# ---------- http server ----------

INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>claude eyes</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:wght@400;500;700&display=swap">
<style>
:root {
  --bg:#000; --fg:#33ff66; --fg-hi:#a8ffb8;
  --fg-dim:#1f9a40; --fg-faint:#0d4a1c; --bad:#ff4455;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--fg);
  font-family: 'VT323', 'IBM Plex Mono', monospace;
  margin: 0; padding: 18px 22px;
  min-height: 100vh;
}
.top { display: flex; align-items: baseline; gap: 18px; margin-bottom: 14px; flex-wrap: wrap; }
h1 {
  font-size: 46px; margin: 0; letter-spacing: 2px;
  color: var(--fg-hi); line-height: 1;
}
.sub {
  color: var(--fg-dim); font-family: 'IBM Plex Mono', monospace;
  font-size: 13px; letter-spacing: 0.22em; text-transform: uppercase;
}
.layout { display: grid; grid-template-columns: minmax(320px, 1fr) 280px; gap: 18px; align-items: start; }
@media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
.frame {
  border: 1px solid var(--fg-dim);
  padding: 6px;
  position: relative;
  background: #050a06;
  display: inline-block;
  max-width: 100%;
  line-height: 0;
}
.frame img {
  display: block;
  max-width: 100%;
  max-height: 62vh;
  width: auto;
  height: auto;
}
.frame::after {
  content: ''; position: absolute; inset: 0; pointer-events: none;
  background: repeating-linear-gradient(0deg, rgba(51,255,102,0.05) 0 1px, transparent 1px 3px);
}
.placeholder {
  display: grid; place-items: center;
  width: auto; aspect-ratio: 4 / 3;
  max-height: 62vh;
  color: var(--fg-faint); border: 1px dashed var(--fg-faint);
  font-size: 22px;
  padding: 0 32px;
}
.panel {
  border: 1px solid var(--fg-dim);
  padding: 12px 14px;
  background: rgba(51, 255, 102, 0.02);
}
.panel + .panel { margin-top: 14px; }
.panel h2 {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px; letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--fg-dim); margin: 0 0 8px 0; font-weight: 700;
}
dl { margin: 0; font-family: 'IBM Plex Mono', monospace; font-size: 14px; }
dt { color: var(--fg-dim); display: inline-block; min-width: 78px; }
dd { display: inline; margin: 0; color: var(--fg); }
dd.hi { color: var(--fg-hi); font-weight: 700; }
.row { padding: 3px 0; border-bottom: 1px dashed var(--fg-faint); }
.row:last-child { border-bottom: 0; }
.err { color: var(--bad); margin-top: 12px; font-family: 'IBM Plex Mono', monospace; font-size: 14px; }
.hint {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px; color: var(--fg-faint); letter-spacing: 0.06em;
  margin-top: 10px;
}
.badge {
  display: inline-block; padding: 2px 8px;
  font-size: 14px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--fg-dim); border: 1px solid var(--fg-faint);
}
.badge.live { color: var(--fg); border-color: var(--fg); }
.badge.dead { color: var(--bad); border-color: var(--bad); }
</style>
</head><body>

<div class="top">
  <h1>claude eyes</h1>
  <span class="sub">grass detector · feed preview</span>
  <span id="badge" class="badge dead">offline</span>
</div>

<div class="layout">
  <div>
    <div class="frame" id="frame-wrap">
      <img id="stream" src="/stream" alt="camera feed"
           onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'placeholder',textContent:'no signal'}))">
    </div>
    <div class="err" id="err"></div>
    <div class="hint">stream: <code>/stream</code> · single frame: <code>/snapshot.jpg</code> · stats: <code>/api/stats</code></div>
  </div>

  <div>
    <div class="panel">
      <h2>capture</h2>
      <dl>
        <div class="row"><dt>resolution</dt><dd id="res" class="hi">— × —</dd></div>
        <div class="row"><dt>fps</dt><dd id="fps" class="hi">—</dd></div>
        <div class="row"><dt>frames</dt><dd id="frames">0</dd></div>
        <div class="row"><dt>cam idx</dt><dd id="cam-index">—</dd></div>
        <div class="row"><dt>backend</dt><dd id="backend">—</dd></div>
        <div class="row"><dt>uptime</dt><dd id="uptime">0s</dd></div>
      </dl>
    </div>

    <div class="panel">
      <h2>detection</h2>
      <dl>
        <div class="row"><dt>state</dt><dd class="hi">— not wired —</dd></div>
        <div class="row"><dt>confidence</dt><dd>—</dd></div>
        <div class="row"><dt>threshold</dt><dd>0.85</dd></div>
      </dl>
      <div class="hint">CLIP scoring lands in the next step.</div>
    </div>
  </div>
</div>

<script>
function fmtUp(s){
  if (s < 60)   return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + Math.round(s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
async function poll(){
  try {
    const r = await fetch('/api/stats', { cache: 'no-store' });
    const s = await r.json();
    document.getElementById('res').textContent = s.width + ' × ' + s.height;
    document.getElementById('fps').textContent = s.fps;
    document.getElementById('frames').textContent = s.frames.toLocaleString();
    document.getElementById('cam-index').textContent = s.cam_index;
    document.getElementById('backend').textContent = s.backend || '—';
    document.getElementById('uptime').textContent = fmtUp((Date.now()/1000) - s.started_at);
    document.getElementById('err').textContent = s.error || '';
    const live = s.fps > 0 && !s.error;
    const badge = document.getElementById('badge');
    badge.textContent = live ? 'live' : (s.error ? 'error' : 'starting');
    badge.className = 'badge ' + (live ? 'live' : 'dead');
  } catch (e) {
    document.getElementById('badge').textContent = 'offline';
    document.getElementById('badge').className = 'badge dead';
  }
}
poll(); setInterval(poll, 500);
</script>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/stats":
            self._send(200, json.dumps(_stats).encode(), "application/json")
        elif self.path == "/snapshot.jpg":
            with _frame_lock:
                jpg = _latest_jpeg
            if jpg is None:
                self.send_error(503, "no frame yet")
                return
            self._send(200, jpg, "image/jpeg")
        elif self.path == "/stream":
            self._stream_mjpeg()
        else:
            self.send_error(404)

    def _stream_mjpeg(self) -> None:
        boundary = "frame"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.end_headers()
        last_sent: bytes | None = None
        try:
            while True:
                with _frame_lock:
                    jpg = _latest_jpeg
                if jpg is None or jpg is last_sent:
                    time.sleep(0.03)
                    continue
                last_sent = jpg
                try:
                    self.wfile.write(b"--" + boundary.encode() + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.03)  # ~30 fps cap
        except Exception:
            return


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    threading.Thread(target=_capture_loop, daemon=True).start()
    print(f"claude eyes on http://localhost:{PORT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
