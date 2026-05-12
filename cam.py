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
import numpy as np
import requests

# Source: integer for a local device (USB cam), or a URL for a network stream
# (e.g. iPhone MJPEG via DroidCam / IP Camera Lite). Examples:
#   CAM_SOURCE=0                                   # default USB cam
#   CAM_SOURCE=1                                   # second USB cam
#   CAM_SOURCE=http://192.168.1.7:4747/video       # DroidCam MJPEG on iPhone
#   CAM_SOURCE=http://192.168.1.7:8080/video       # IP Camera Lite on iPhone
CAM_SOURCE_RAW = os.environ.get("CAM_SOURCE", os.environ.get("CAM_INDEX", "0"))
try:
    CAM_SOURCE: int | str = int(CAM_SOURCE_RAW)
except ValueError:
    CAM_SOURCE = CAM_SOURCE_RAW
IS_URL = isinstance(CAM_SOURCE, str)

CAM_WIDTH  = int(os.environ.get("CAM_WIDTH", "640"))
CAM_HEIGHT = int(os.environ.get("CAM_HEIGHT", "480"))
PORT       = int(os.environ.get("CAM_PORT", "8767"))
JPEG_QUALITY = 80

# Rotation applied to every frame after capture. Useful when the phone app
# broadcasts in the wrong orientation. Valid: 0, 90, 180, 270 (clockwise).
CAM_ROTATE = int(os.environ.get("CAM_ROTATE", "0"))
_ROTATE_MAP = {
    90:  cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
CAM_ROTATE_CV = _ROTATE_MAP.get(CAM_ROTATE)

# Touch-Grass detector wiring
GRASS_DETECT       = os.environ.get("GRASS_DETECT", "1") not in ("0", "false", "False", "")
GRASS_THRESHOLD    = float(os.environ.get("GRASS_THRESHOLD", "0.85"))
GRASS_SUSTAIN_SEC  = float(os.environ.get("GRASS_SUSTAIN_SEC", "1.5"))
GRASS_DISMISS_URL  = os.environ.get("GRASS_DISMISS_URL", "http://localhost:8765/api/grass/dismiss")
DISMISS_COOLDOWN_S = 4.0   # avoid spamming the server with dismiss calls

# ---------- shared state ----------

_frame_lock = threading.Lock()
_latest_jpeg: bytes | None = None
_latest_bgr: np.ndarray | None = None     # raw BGR for the detector (no JPEG round-trip)
_stats = {
    "cam_index":  CAM_SOURCE if not IS_URL else -1,
    "source":     str(CAM_SOURCE),
    "is_url":     IS_URL,
    "width":      0,
    "height":     0,
    "fps":        0.0,
    "frames":     0,
    "started_at": time.time(),
    "error":      "",
    "backend":    "",
}

# Lockout/server state. Mirrors the main dashboard's `grass_required` flag
# so the cam dashboard can show whether the user actually needs to touch grass.
_server_lock = threading.Lock()
_server_state = {
    "grass_required":  False,
    "five_hour_pct":   None,
    "last_polled_at":  0.0,
    "error":           "",
}

# Detector state. Updated in _detector_loop, read by /api/stats.
_det_lock = threading.Lock()
_det_state = {
    "enabled":           GRASS_DETECT,
    "status":            "off" if not GRASS_DETECT else "loading",
    "confidence":        None,
    "threshold":         GRASS_THRESHOLD,
    "sustained_sec":     0.0,
    "sustain_target":    GRASS_SUSTAIN_SEC,
    "latency_ms":        0.0,
    "last_score_at":     0.0,
    "last_dismissed_at": 0.0,
    "dismiss_count":     0,
    "error":             "",
}


def _det_update(**kw) -> None:
    with _det_lock:
        _det_state.update(kw)


# ---------- camera capture ----------

# Order matters per source type:
#   USB device → DSHOW > MSMF > ANY (DSHOW is the reliable Windows path)
#   Network URL → FFMPEG > ANY (FFmpeg is the one that speaks MJPEG/RTSP)
_BACKENDS_DEVICE = [
    ("CAP_DSHOW", cv2.CAP_DSHOW),
    ("CAP_MSMF",  cv2.CAP_MSMF),
    ("CAP_ANY",   cv2.CAP_ANY),
]
_BACKENDS_URL = [
    ("CAP_FFMPEG", cv2.CAP_FFMPEG),
    ("CAP_ANY",    cv2.CAP_ANY),
]


def _open_camera() -> tuple[cv2.VideoCapture | None, str]:
    backends = _BACKENDS_URL if IS_URL else _BACKENDS_DEVICE
    for name, backend in backends:
        cap = cv2.VideoCapture(CAM_SOURCE, backend)
        if cap.isOpened():
            if not IS_URL:
                # Resolution hints are advisory and only meaningful for local
                # devices; network streams have their own fixed resolution.
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_HEIGHT)
            # Discard the first frame — some sources return black on first read.
            cap.read()
            return cap, name
        cap.release()
    return None, ""


def _capture_loop() -> None:
    """Open the camera, pump frames forever. If the source goes away
    (network stream disconnects, phone app put to sleep, etc.) keep
    retrying with backoff so the dashboard auto-recovers."""
    global _latest_jpeg, _latest_bgr
    cap: cv2.VideoCapture | None = None
    backend_name = ""
    fps_t0 = time.time()
    fps_n = 0
    consecutive_fails = 0

    while True:
        if cap is None:
            cap, backend_name = _open_camera()
            if cap is None:
                _stats["error"] = f"could not open camera source: {CAM_SOURCE!r} (retrying)"
                _stats["backend"] = ""
                time.sleep(2.0)
                continue
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            _stats["width"], _stats["height"] = actual_w, actual_h
            _stats["backend"] = backend_name
            consecutive_fails = 0
            print(f"[cam] source={CAM_SOURCE!r} backend={backend_name} {actual_w}x{actual_h}")

        ok, frame = cap.read()
        if not ok:
            consecutive_fails += 1
            _stats["error"] = f"cap.read() returned false ({consecutive_fails})"
            # Network streams (URL sources) get torn down and re-opened on
            # repeated failure; USB cams usually just hiccup once or twice.
            if consecutive_fails >= (5 if IS_URL else 25):
                try: cap.release()
                except Exception: pass
                cap = None
            time.sleep(0.2)
            continue
        if _stats["error"]:
            _stats["error"] = ""
        consecutive_fails = 0

        if CAM_ROTATE_CV is not None:
            frame = cv2.rotate(frame, CAM_ROTATE_CV)
            # Keep stats accurate after rotation
            h, w = frame.shape[:2]
            _stats["width"], _stats["height"] = w, h
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ok:
            with _frame_lock:
                _latest_jpeg = buf.tobytes()
                _latest_bgr = frame
            _stats["frames"] += 1
            fps_n += 1
        now = time.time()
        if now - fps_t0 >= 1.0:
            _stats["fps"] = round(fps_n / (now - fps_t0), 1)
            fps_t0 = now
            fps_n = 0


# ---------- main-dashboard state poller ----------

SERVER_STATE_URL = os.environ.get("SERVER_STATE_URL", "http://localhost:8765/api/state")


def _server_state_poll() -> None:
    """Mirror the main dashboard's grass_required flag so the cam UI can
    show whether a lockout is currently active."""
    while True:
        try:
            r = requests.get(SERVER_STATE_URL, timeout=3)
            d = r.json()
            ru = d.get("real_usage") or {}
            with _server_lock:
                _server_state["grass_required"] = bool(d.get("grass_required"))
                _server_state["five_hour_pct"]  = ru.get("five_hour_pct")
                _server_state["last_polled_at"] = time.time()
                _server_state["error"] = ""
        except Exception as e:
            with _server_lock:
                _server_state["error"] = str(e)
        time.sleep(1.0)


# ---------- SigLIP 2 detector loop ----------


def _detector_loop() -> None:
    """Background thread: score the latest frame, track sustained-above-threshold
    time, POST /api/grass/dismiss when grass is held long enough."""
    if not GRASS_DETECT:
        return

    # Lazy import — torch/transformers are heavy. Keeps cam.py runnable
    # without them when GRASS_DETECT=0.
    try:
        from PIL import Image
        from grass_detector import Detector
        det = Detector()
    except Exception as e:
        _det_update(status="error", error=f"load failed: {e}")
        print(f"[grass] load failed: {e}")
        return

    _det_update(status="idle", error="")
    print("[grass] detector ready")

    above_since: float | None = None
    last_post_at = 0.0

    while True:
        with _frame_lock:
            frame = _latest_bgr
        if frame is None:
            time.sleep(0.1)
            continue
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            r = det.score(img)
            now = time.time()
            is_above = r.confidence >= GRASS_THRESHOLD

            if is_above:
                if above_since is None:
                    above_since = now
                sustained = now - above_since
            else:
                above_since = None
                sustained = 0.0

            if sustained >= GRASS_SUSTAIN_SEC:
                status = "GRASS"
            elif is_above:
                status = "holding"
            else:
                status = "watching"

            _det_update(
                status=status,
                confidence=r.confidence,
                sustained_sec=sustained,
                latency_ms=r.latency_ms,
                last_score_at=now,
                error="",
            )

            # Fire the auto-dismiss once per qualifying hold, with a short
            # cooldown so we don't hammer the endpoint if grass stays in frame.
            if sustained >= GRASS_SUSTAIN_SEC and (now - last_post_at) > DISMISS_COOLDOWN_S:
                try:
                    requests.post(GRASS_DISMISS_URL, timeout=2)
                    last_post_at = now
                    above_since = None    # require fresh hold for next dismiss
                    with _det_lock:
                        _det_state["last_dismissed_at"] = now
                        _det_state["dismiss_count"] += 1
                    print(f"[grass] auto-dismiss POSTed (conf {r.confidence:.3f})")
                except Exception as e:
                    _det_update(error=f"dismiss POST: {e}")
        except Exception as e:
            _det_update(status="error", error=str(e))


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
  max-height: 92vh;
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
  max-height: 92vh;
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
.bar-wrap { margin-top: 10px; font-family: 'IBM Plex Mono', monospace; }
.bar-inner {
  position: relative;
  height: 10px;
  border: 1px solid var(--fg-faint);
  background: rgba(51, 255, 102, 0.04);
  overflow: hidden;
}
.bar-inner > div:first-child {
  height: 100%; width: 0%;
  background: var(--fg);
  transition: width 120ms linear, background-color 120ms;
}
#det-thresh-marker {
  position: absolute; top: -3px; bottom: -3px;
  width: 1px; background: var(--bad);
  left: 85%;
}
.bar-cap { color: var(--fg-faint); font-size: 11px; margin-top: 2px; letter-spacing: 0.1em; text-transform: uppercase; }
.det-status-GRASS    { color: var(--bad); font-weight: 700; animation: blink 0.4s steps(2, end) infinite; }
.det-status-holding  { color: #ffcc00; }
.det-status-watching { color: var(--fg); }
.det-status-loading  { color: var(--fg-faint); }
.det-status-idle     { color: var(--fg-dim); }
.det-status-error    { color: var(--bad); }
.det-status-off      { color: var(--fg-faint); }
@keyframes blink { 50% { opacity: 0.3; } }

/* big lockout / cleared banners at the top of the cam dashboard */
.lockout-banner {
  background: rgba(255, 68, 85, 0.95);
  color: #000;
  padding: 14px 24px;
  margin: 0 0 16px 0;
  text-align: center;
  border: 2px solid var(--bad);
  animation: lockout-pulse 0.9s steps(2, end) infinite;
}
.lockout-banner-title {
  font-family: 'VT323', monospace;
  font-size: 42px;
  font-weight: 800;
  letter-spacing: 6px;
  line-height: 1;
}
.lockout-banner-sub {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 14px;
  margin-top: 6px;
  letter-spacing: 2px;
  text-transform: uppercase;
  opacity: 0.85;
}
@keyframes lockout-pulse {
  50% { background: rgba(120, 0, 12, 0.95); }
}
.cleared-banner {
  background: rgba(51, 255, 102, 0.08);
  color: var(--fg);
  padding: 10px 18px;
  margin: 0 0 16px 0;
  text-align: center;
  border: 1px solid var(--fg-dim);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 18px;
  letter-spacing: 2px;
}
.cleared-banner-title { letter-spacing: 4px; }
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

<div id="lockout-banner" class="lockout-banner" hidden>
  <div class="lockout-banner-title">▌ TOUCH GRASS REQUIRED ▐</div>
  <div class="lockout-banner-sub">show real grass to the camera — lockout will clear automatically</div>
</div>
<div id="cleared-banner" class="cleared-banner" hidden>
  <div class="cleared-banner-title">✓ ALL CLEAR — no grass needed</div>
</div>

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
        <div class="row"><dt>source</dt><dd id="cam-source" style="word-break:break-all">—</dd></div>
        <div class="row"><dt>backend</dt><dd id="backend">—</dd></div>
        <div class="row"><dt>uptime</dt><dd id="uptime">0s</dd></div>
      </dl>
    </div>

    <div class="panel">
      <h2>detection · siglip 2</h2>
      <dl>
        <div class="row"><dt>status</dt><dd id="det-status" class="hi">—</dd></div>
        <div class="row"><dt>confidence</dt><dd id="det-conf" class="hi">—</dd></div>
        <div class="row"><dt>threshold</dt><dd id="det-thresh">—</dd></div>
        <div class="row"><dt>sustained</dt><dd id="det-sus">0.0 / —</dd></div>
        <div class="row"><dt>latency</dt><dd id="det-lat">—</dd></div>
        <div class="row"><dt>dismisses</dt><dd id="det-count">0</dd></div>
        <div class="row"><dt>last</dt><dd id="det-last">—</dd></div>
      </dl>
      <div class="bar-wrap">
        <div class="bar-inner">
          <div id="det-conf-bar"></div>
          <div id="det-thresh-marker"></div>
        </div>
        <div class="bar-cap">confidence</div>
      </div>
      <div class="bar-wrap">
        <div class="bar-inner"><div id="det-sus-bar"></div></div>
        <div class="bar-cap">grass-hold progress</div>
      </div>
      <div id="det-err" class="err" style="font-size:13px;margin-top:6px;"></div>
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
    document.getElementById('cam-source').textContent =
      s.is_url ? s.source : ('cam ' + s.cam_index + ' (USB)');
    document.getElementById('backend').textContent = s.backend || '—';
    document.getElementById('uptime').textContent = fmtUp((Date.now()/1000) - s.started_at);
    document.getElementById('err').textContent = s.error || '';
    const live = s.fps > 0 && !s.error;
    const badge = document.getElementById('badge');
    badge.textContent = live ? 'live' : (s.error ? 'error' : 'starting');
    badge.className = 'badge ' + (live ? 'live' : 'dead');

    // Lockout banner — mirrors the main dashboard's grass_required flag
    const srv = s.server || {};
    const locked = !!srv.grass_required;
    document.getElementById('lockout-banner').hidden = !locked;
    document.getElementById('cleared-banner').hidden = locked;

    // Detection panel
    const d = s.detect || {};
    const st = document.getElementById('det-status');
    st.textContent = (d.status || '—').toUpperCase();
    st.className = 'hi det-status-' + (d.status || 'idle');
    document.getElementById('det-conf').textContent  = (d.confidence == null) ? '—' : d.confidence.toFixed(3);
    document.getElementById('det-thresh').textContent = (d.threshold || 0.85).toFixed(2);
    document.getElementById('det-sus').textContent   =
      `${(d.sustained_sec||0).toFixed(1)} / ${(d.sustain_target||1.5).toFixed(1)}s`;
    document.getElementById('det-lat').textContent   = d.latency_ms ? `${d.latency_ms.toFixed(0)}ms` : '—';
    document.getElementById('det-count').textContent = d.dismiss_count || 0;
    document.getElementById('det-last').textContent  = d.last_dismissed_at
      ? fmtUp((Date.now()/1000) - d.last_dismissed_at) + ' ago' : '—';
    document.getElementById('det-err').textContent   = d.error || '';

    // bars
    const confBar = document.getElementById('det-conf-bar');
    const cPct = Math.round(((d.confidence || 0)) * 100);
    confBar.style.width = cPct + '%';
    confBar.style.background = (d.confidence >= (d.threshold || 0.85)) ? 'var(--bad)' : 'var(--fg)';
    document.getElementById('det-thresh-marker').style.left = ((d.threshold || 0.85) * 100) + '%';
    const susBar = document.getElementById('det-sus-bar');
    const sPct = Math.min(100, ((d.sustained_sec||0) / (d.sustain_target||1.5)) * 100);
    susBar.style.width = sPct + '%';
    susBar.style.background = (sPct >= 100) ? 'var(--bad)' : 'var(--fg)';
  } catch (e) {
    document.getElementById('badge').textContent = 'offline';
    document.getElementById('badge').className = 'badge dead';
  }
}
poll(); setInterval(poll, 250);

// Safety-key: D POSTs the server dismiss endpoint directly so you can clear
// the lockout for testing without actually finding real grass.
window.addEventListener('keydown', async (e) => {
  if (e.key === 'd' || e.key === 'D') {
    try {
      await fetch('http://localhost:8765/api/grass/dismiss', { method: 'POST' });
      console.log('[grass] safety dismiss POSTed');
    } catch (err) { console.error(err); }
  }
});
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
        # CORS — main dashboard at :8765 needs to fetch /api/stats and embed
        # /stream cross-origin. LAN-only so wildcard is fine.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        # CORS preflight (browser fires this for some fetch configurations)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/stats":
            with _det_lock:
                det = dict(_det_state)
            with _server_lock:
                server = dict(_server_state)
            payload = {**_stats, "detect": det, "server": server}
            self._send(200, json.dumps(payload).encode(), "application/json")
        elif self.path.startswith("/snapshot.jpg"):
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
        self.send_header("Access-Control-Allow-Origin", "*")
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
    threading.Thread(target=_server_state_poll, daemon=True).start()
    if GRASS_DETECT:
        threading.Thread(target=_detector_loop, daemon=True).start()
        print(f"[grass] detector: ON  threshold={GRASS_THRESHOLD}  sustain={GRASS_SUSTAIN_SEC}s")
    else:
        print("[grass] detector: OFF  (set GRASS_DETECT=1 to enable)")
    print(f"claude eyes on http://localhost:{PORT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
