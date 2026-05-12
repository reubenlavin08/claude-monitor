// claude-monitor frontend — CRT terminal view, 800×480 fixed stage.
// Wires the existing WebSocket snapshot to the new DOM.

const $ = (id) => document.getElementById(id);
const els = {
  model:        $("model"),
  project:      $("project"),
  mode:         $("mode"),
  live:         $("live"),
  fiveNum:      $("five-num"),
  weeklyPct:    $("weekly-pct"),
  cdLabel:      $("cd-label"),
  cd:           $("cd"),
  cdNum:        $("cd-num"),
  cdUnit:       $("cd-unit"),
  cdFootLeft:   $("cd-foot-left"),
  cdFootRight:  $("cd-foot-right"),
  ctxNum:       $("ctx-num"),
  ctxTok:       $("ctx-tok"),
  ctxFill:      $("ctx-fill"),
  sessionsList: $("sessions-list"),
  cost:         $("cost"),
  msgs:         $("msgs"),
  scrapeOk:     $("scrape-ok"),
  updated:      $("updated"),
  grassGate:    $("grass-gate"),
  grassStream:  $("grass-gate-stream"),
  grassConf:    $("grass-gate-conf"),
  grassBar:     $("grass-gate-bar"),
  grassStatus:  $("grass-gate-status"),
};

let lastSnapshot = null;
let lastMtime = 0;
// Local-only dismiss: hides overlay in this tab without clearing server state.
// Auto-expires after LOCAL_DISMISS_MS so a stray G press doesn't permanently
// suppress the lockout. Also resets when server flips grass_required to false.
const LOCAL_DISMISS_MS = 30_000;
let grassDismissedUntil = 0;
let resetEpoch = null;       // recomputed when we receive a new five_hour_reset string
let resetSource = "";        // the string the epoch came from (so we don't reparse needlessly)

// ---------- helpers ----------

const classForPct = (p) => p >= 85 ? "bad" : p >= 60 ? "warn" : "";
const sessionPctClass = (p) => p >= 85 ? "bad" : p >= 60 ? "warn" : "cool";

const fmtTokens = (n) => {
  n = Number(n) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + "k";
  return String(n);
};
const fmtCost = (c) => Number(c || 0).toFixed(2);
const fmtAgo = (sec) => {
  if (sec < 1)    return "just now";
  if (sec < 60)   return Math.round(sec) + "s ago";
  if (sec < 3600) return Math.round(sec/60) + "m ago";
  return Math.round(sec/3600) + "h ago";
};

// Parse "3:10pm (America/Tijuana)" or "1:30 am (America/Tijuana)"
// or "May 16, 6am (America/Tijuana)" -> epoch seconds of the next occurrence.
function parseResetToEpoch(s) {
  if (!s) return null;

  // Pull off the timezone.
  const tzMatch = s.match(/\(([^)]+)\)/);
  const tz = tzMatch ? tzMatch[1].trim() : null;
  let head = tzMatch ? s.slice(0, tzMatch.index).trim() : s.trim();
  head = head.replace(/,\s*$/, "");

  // Day-of-month form: "May 16, 6am" or "May 16 6am"
  const monthRe = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})[,\s]+(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)/i;
  // Time-only form: "3:10pm", "1:30 am"
  const timeRe = /^(\d{1,2})(?::(\d{1,2}))?\s*(am|pm)$/i;

  let hour = null, minute = 0, month = null, day = null;
  let m;
  if ((m = head.match(monthRe))) {
    const months = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
    month = months.indexOf(m[1].slice(0,3).toLowerCase()) + 1;
    day = +m[2];
    hour = +m[3];
    minute = m[4] ? +m[4] : 0;
    if (m[5].toLowerCase() === "pm" && hour !== 12) hour += 12;
    if (m[5].toLowerCase() === "am" && hour === 12) hour = 0;
  } else if ((m = head.match(timeRe))) {
    hour = +m[1];
    minute = m[2] ? +m[2] : 0;
    if (m[3].toLowerCase() === "pm" && hour !== 12) hour += 12;
    if (m[3].toLowerCase() === "am" && hour === 12) hour = 0;
  } else {
    return null;
  }

  // Get "now" expressed in the target tz so we can do date math without
  // wrestling with timezone offsets.
  const now = new Date();
  let parts;
  try {
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: tz || undefined,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
    parts = Object.fromEntries(fmt.formatToParts(now).filter(p => p.type !== "literal").map(p => [p.type, p.value]));
  } catch {
    parts = null;
  }
  if (!parts) return null;

  const nowY = +parts.year, nowM = +parts.month, nowD = +parts.day;
  const nowH = +parts.hour === 24 ? 0 : +parts.hour;
  const nowMin = +parts.minute, nowSec = +parts.second;

  // Determine target Y/M/D in tz.
  let targetY = nowY, targetM = month || nowM, targetD = day || nowD;

  // Construct seconds-of-day for "now" and "target".
  const nowSod = nowH*3600 + nowMin*60 + nowSec;
  const tgtSod = hour*3600 + minute*60;

  // If we only had a time (no date), and target time has already passed today
  // in tz, the reset must be tomorrow.
  let diffSec;
  if (!month) {
    diffSec = tgtSod - nowSod;
    if (diffSec <= 0) diffSec += 86400;
  } else {
    // We have a specific calendar day. Compute days between today-in-tz
    // and target-in-tz using UTC to dodge DST issues (we only need a day delta).
    const a = Date.UTC(nowY, nowM - 1, nowD);
    const b = Date.UTC(targetY, targetM - 1, targetD);
    const dayDelta = Math.round((b - a) / 86400000);
    diffSec = dayDelta * 86400 + (tgtSod - nowSod);
    if (diffSec < 0) {
      // The month/day already passed this year — assume same date next year.
      diffSec += 365 * 86400;
    }
  }

  return Math.floor(Date.now()/1000) + diffSec;
}

function renderCountdown() {
  if (!resetEpoch) {
    els.cd.classList.add("nodata");
    els.cdNum.textContent = "—";
    els.cdUnit.textContent = "";
    return;
  }
  let sec = resetEpoch - Math.floor(Date.now()/1000);
  if (sec < 0) sec = 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  els.cd.classList.remove("nodata");
  let main, unit;
  if (h > 0) {
    main = `${h}<span class="colon">:</span>${String(m).padStart(2,"0")}`;
    unit = "hm";
  } else if (m > 0) {
    main = `${m}<span class="colon">:</span>${String(s).padStart(2,"0")}`;
    unit = "ms";
  } else {
    main = `0<span class="colon">:</span>${String(s).padStart(2,"0")}`;
    unit = "s";
  }
  els.cdNum.innerHTML = main;
  els.cdUnit.textContent = unit;
}

function setBatteryPct(pct, isNoData) {
  els.ctxFill.classList.remove("warn", "bad");
  els.ctxNum.classList.remove("warn", "bad", "nodata");
  if (isNoData) {
    els.ctxFill.style.width = "0%";
    els.ctxNum.firstChild.nodeValue = "—";
    els.ctxNum.classList.add("nodata");
    return;
  }
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  els.ctxFill.style.width = pct.toFixed(1) + "%";
  const cls = classForPct(pct);
  if (cls) els.ctxFill.classList.add(cls);
  els.ctxNum.firstChild.nodeValue = String(Math.round(pct));
  if (cls) els.ctxNum.classList.add(cls);
}

function setFivePct(pct, isNoData) {
  els.fiveNum.classList.remove("warn", "bad", "nodata");
  if (isNoData) {
    els.fiveNum.classList.add("nodata");
    els.fiveNum.innerHTML = `—<span class="pct">%</span>`;
    return;
  }
  const p = Math.round(Number(pct) || 0);
  const cls = classForPct(p);
  if (cls) els.fiveNum.classList.add(cls);
  els.fiveNum.innerHTML = `${p}<span class="pct">%</span>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  }[c]));
}

// ---------- main snapshot handler ----------

function applySnapshot(s) {
  if (!s) return;
  lastSnapshot = s;

  // header
  const hasSession = !!s.session_id;
  els.model.textContent = (s.model_name || "—").toLowerCase();
  if (hasSession) {
    const cwd   = s.cwd_leaf || s.project || "—";
    const title = s.title || "(no prompts yet)";
    els.project.textContent = `${cwd} · "${title}"`;
  } else {
    els.project.textContent = "no active session";
  }

  if (s.pinned) {
    els.mode.textContent = "pinned";
    els.mode.classList.add("pinned");
  } else {
    els.mode.textContent = "auto";
    els.mode.classList.remove("pinned");
  }

  // scraper-driven values
  const real = s.real_usage || {};
  const haveReal = real.__has_data === true;

  if (haveReal) {
    setFivePct(real.five_hour_pct, false);
    els.weeklyPct.textContent = (Math.round(Number(real.week_all_pct) || 0)) + "%";

    // Countdown source
    const resetStr = real.five_hour_reset || "";
    if (resetStr && resetStr !== resetSource) {
      resetSource = resetStr;
      resetEpoch = parseResetToEpoch(resetStr);
    }
    els.cdLabel.textContent = "resets in";
    els.cdFootLeft.textContent  = (resetStr.split("(")[0] || "").trim() || "—";
    els.cdFootRight.textContent = (real.five_hour_reset && real.five_hour_reset.match(/\(([^)]+)\)/) || ["",""])[1].split("/").pop().toLowerCase() || "—";
  } else {
    setFivePct(0, true);
    els.weeklyPct.textContent = "—";
    resetEpoch = null;
    resetSource = "";
    els.cdLabel.textContent = real.__attempts > 0 ? "scraper failing" : "scraper starting";
    els.cdFootLeft.textContent  = real.__last_error || "—";
    els.cdFootRight.textContent = "";
  }

  // context battery
  if (hasSession) {
    const maxC = s.max_context || 200_000;
    const ctxPct = maxC > 0 ? (s.context_tokens || 0) / maxC * 100 : 0;
    setBatteryPct(ctxPct, false);
    els.ctxTok.textContent = `${fmtTokens(s.context_tokens || 0)} / ${fmtTokens(maxC)} tok`;
  } else {
    setBatteryPct(0, true);
    els.ctxTok.textContent = "— / —";
  }

  // sessions sidebar
  renderSessions(s.other_sessions || []);

  // footer
  els.cost.textContent = fmtCost(s.cost_usd);
  els.msgs.textContent = s.messages || 0;
  const a = real.__attempts || 0, ok = real.__successes || 0;
  els.scrapeOk.textContent = `${ok}/${a}`;
  lastMtime = s.mtime || 0;

  // countdown immediately (re-render the cell with the parsed value)
  renderCountdown();

  // touch-grass lockout overlay
  if (!s.grass_required) grassDismissedUntil = 0;
  const stillDismissed = Date.now() < grassDismissedUntil;
  const grassActive = !!s.grass_required && !stillDismissed;
  if (els.grassGate) {
    const was = !els.grassGate.hidden;
    els.grassGate.hidden = !grassActive;
    if (grassActive && !was) startGrassCam();
    if (!grassActive && was) stopGrassCam();
  }
}

// ---------- embedded cam feed + live confidence on the lockout overlay ----------

let grassCamFramePoll = null;   // 8fps snapshot loop driving the <img>
let grassCamStatsPoll = null;   // slower poll for confidence/sustained/etc.

function camBaseUrl() {
  // Use whatever host the dashboard was loaded from so it works from the Pi
  // (192.168.1.x) as well as localhost — the cam runs on the same machine.
  return `${location.protocol}//${location.hostname}:8767`;
}

function startGrassCam() {
  if (!els.grassStream) return;
  stopGrassCam();   // ensure clean state regardless of where we came from
  // Snapshot polling. We deliberately do NOT use the MJPEG /stream endpoint
  // here — Chromium's MJPEG renderer is flaky across cycles (the socket
  // sometimes wedges after the first lockout). Polling /snapshot.jpg gives
  // an independent HTTP GET per frame; if one fails the next still works.
  const refreshFrame = () => {
    if (!els.grassGate || els.grassGate.hidden) return;
    els.grassStream.src = `${camBaseUrl()}/snapshot.jpg?t=${Date.now()}`;
  };
  refreshFrame();
  grassCamFramePoll = setInterval(refreshFrame, 120);   // ~8 fps
  pollGrassCam();
  grassCamStatsPoll = setInterval(pollGrassCam, 400);
}

function stopGrassCam() {
  if (grassCamFramePoll) { clearInterval(grassCamFramePoll); grassCamFramePoll = null; }
  if (grassCamStatsPoll) { clearInterval(grassCamStatsPoll); grassCamStatsPoll = null; }
  if (els.grassStream) els.grassStream.removeAttribute("src");
  if (els.grassConf)   els.grassConf.textContent = "—";
  if (els.grassBar)    els.grassBar.style.width = "0%";
  if (els.grassStatus) { els.grassStatus.textContent = "—"; els.grassStatus.classList.remove("hit"); }
}

async function pollGrassCam() {
  try {
    const r = await fetch(`${camBaseUrl()}/api/stats`, { cache: "no-store" });
    const s = await r.json();
    const d = s.detect || {};
    const c = d.confidence;
    const thr = d.threshold || 0.85;
    const hit = (c != null) && c >= thr;
    if (els.grassConf) {
      els.grassConf.textContent = (c == null) ? "—" : c.toFixed(3);
      els.grassConf.classList.toggle("hit", hit);
    }
    if (els.grassBar) {
      els.grassBar.style.width = (Math.max(0, Math.min(1, c || 0)) * 100).toFixed(1) + "%";
      els.grassBar.classList.toggle("hit", hit);
    }
    if (els.grassStatus) {
      const sustained = (d.sustained_sec || 0).toFixed(1);
      const target    = (d.sustain_target || 1.5).toFixed(1);
      const stat      = (d.status || "—").toUpperCase();
      els.grassStatus.textContent = `${stat} · ${sustained} / ${target}s`;
      els.grassStatus.classList.toggle("hit", stat === "GRASS");
    }
  } catch (e) {
    if (els.grassStatus) els.grassStatus.textContent = "cam offline";
  }
}

function renderSessions(list) {
  if (!list.length) {
    els.sessionsList.innerHTML = '<div class="session-row" style="opacity:0.5;cursor:default"><span class="arrow">·</span><span class="name">— none —</span></div>';
    return;
  }
  els.sessionsList.innerHTML = "";
  for (const o of list) {
    const row = document.createElement("div");
    row.className = "session-row";
    const ctxPct = (o.max_context > 0)
      ? Math.round((o.context_tokens / o.max_context) * 100)
      : 0;
    const cls = sessionPctClass(ctxPct);
    const cwd = o.cwd_leaf || o.project || "";
    const title = o.title || "(no prompts yet)";
    const name = cwd ? `${cwd} · ${title}` : title;
    row.innerHTML = `
      <span class="arrow">·</span>
      <span class="name">${escapeHtml(name)}</span>
      <span class="pct ${cls}">${ctxPct}<span class="sym">%</span></span>
    `;
    row.addEventListener("click", () => focusSession(o.session_id));
    els.sessionsList.appendChild(row);
  }
}

// ---------- click actions ----------

function focusSession(sid) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "focus", session_id: sid }));
  }
}
function unpin() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: "auto" }));
  }
}
els.mode.addEventListener("click", () => {
  if (lastSnapshot && lastSnapshot.pinned) unpin();
});

// Touch-grass dismiss keys:
//   G              local-only (this tab) — Pi keeps showing the lockout
//   Shift+G or D   safety key — POST /api/grass/dismiss, clears for everyone
window.addEventListener("keydown", (e) => {
  if (!els.grassGate || els.grassGate.hidden) return;
  if (e.shiftKey && (e.key === "G" || e.key === "g")) {
    fetch("/api/grass/dismiss", { method: "POST" }).catch(() => {});
    return;
  }
  if (e.key === "d" || e.key === "D") {
    fetch("/api/grass/dismiss", { method: "POST" }).catch(() => {});
    return;
  }
  if (e.key === "g" || e.key === "G") {
    grassDismissedUntil = Date.now() + LOCAL_DISMISS_MS;
    els.grassGate.hidden = true;
  }
});

// ---------- per-second ticker ----------

setInterval(() => {
  // "X ago" + stale flag
  if (!lastMtime) {
    els.updated.textContent = "—";
  } else {
    const ago = (Date.now() / 1000) - lastMtime;
    els.updated.textContent = fmtAgo(ago);
    els.live.classList.toggle("stale", ago > 30);
  }
  renderCountdown();
}, 1000);

// ---------- scale-to-fit ----------

function fit() {
  const s = $("screen");
  if (!s) return;
  const sx = window.innerWidth / 800;
  const sy = window.innerHeight / 480;
  s.style.transform = `scale(${Math.min(sx, sy)})`;
}
window.addEventListener("resize", fit);
requestAnimationFrame(fit);

// ---------- WebSocket ----------

let ws = null;
let backoff = 500;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { backoff = 500; els.live.classList.remove("stale"); };
  ws.onmessage = (ev) => {
    try { applySnapshot(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  ws.onclose = () => {
    els.live.classList.add("stale");
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 1.6, 5000);
  };
  ws.onerror = () => { try { ws.close(); } catch {} };
}
connect();
