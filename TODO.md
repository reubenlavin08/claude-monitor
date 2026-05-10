# claude-monitor — accuracy hardening TODO

**Goal:** Dashboard shows real `/usage` numbers 100% of the time. No more
approximations. No more divergence between the wall display and what
Claude Code's own `/usage` command says.

**Why this list exists:** the dashboard was silently falling back to a
prompt-count approximation (`five_hour_msgs / 171`, `week_msgs / 8209`)
whenever the PTY scraper failed — and the scraper has been failing on
every cold-start because its 8-second "ready" window can't accommodate
`claude.cmd` boot time. Net effect: weekly window has been showing 3.8%
when reality is 16%. (Captured at 2026-05-10 ~11:00.)

---

## The plan

### 1. Bulletproof the scraper

- [x] **Task 2 — Harden `scrape_usage.py` timeouts.** Bumped
  `ready_deadline` 8s → 20s, per-attempt dialog wait 6s → 8s, default
  `timeout` 18s → 45s. Added `error_kind` field to every failure path
  (`missing_dep` / `spawn_failed` / `prompt_timeout` / `dialog_timeout`
  / `parse_failed`). Also fixed a deeper bug: the gate that decided
  "dialog rendered" required literal `"% used"` but the TUI renders
  `"16%used"` (no space). Replaced with a tolerant regex
  `DIALOG_RENDERED_RE` that matches the same pattern the parser will
  accept.
- [x] **Task 3 — Fast-retry on failure.** Added `SCRAPE_RETRY_SEC = 15`
  (env-overridable). `ScrapeState.maybe_scrape()` detects whether the
  last attempt failed (`last_attempt_at > last_scrape_at`) and uses 15s
  cadence if so, 90s if the last scrape succeeded.

### 2. Kill the approximation entirely

- [x] **Task 5 — Server: stop emitting fake numbers.** Removed `CAP_5H`,
  `CAP_WEEK`, `FIVE_HOUR_SEC`, `WEEK_SEC` constants and their env vars.
  Removed `TranscriptCache.windowed()` (~40 lines). Dropped 10 fields
  from the WebSocket snapshot (the `five_hour_*` and `week_*` prompt-
  count approximations). Updated the `history` deque to track real
  scraped `five_hour_pct` / `week_all_pct` instead of fake `msgs5h`.
  Updated fingerprint to include scraper health so broadcasts fire when
  state changes. Replaced the startup `Caps:` banner with one showing
  the scrape cadence.
- [x] **Task 4 — Frontend: no more fake numbers.** Removed `setBar()`
  approximation helper. Added `setBarNoData()` that shows `—` for the
  percentage and applies a `.nodata` class (faint outline). Foot text
  under each bar shows scraper state explicitly: "waiting for first
  /usage scrape…" before first attempt, or "scraper failing (N
  attempts)" with the actual error string after. Dropped the
  reset-countdown ticker (referenced deleted server fields). Bonus:
  fixed pre-existing dead `els.ctxTok` reference that was silently
  throwing every snapshot and being swallowed by the WS try/catch.

### 3. Remove dead code

- [x] **Task 6 — Rip out `DebugLogWatcher`.** Removed the class (~55
  lines), its `debug_watcher` instance, the `DEBUG_LOG_DIR` config
  block + env var, the `scan()` call in `state.refresh()`, the
  `live_limits` field from snapshot + fingerprint, and the
  `/api/raw-limits` endpoint.
- [x] **Task 7 — Strip `--debug=api` wrapper.** Removed the `claude`
  function from the PowerShell profile. Typing `claude` now calls
  `claude.cmd` directly. `monitor` and `monitor-stop` helpers kept.
  (Reload existing PS sessions with `. $PROFILE` to pick up the
  change.)

---

## Out of scope (intentionally)

- **Persistent warm `claude.cmd` PTY** for sub-second scrapes. Considered
  but rejected: long-lived hidden claude processes have many failure
  modes (model switch, modal dialogs, version updates), and re-spawning
  every 90s is good enough once the timeouts are fixed.
- **Token-weighted approximation** as a more accurate fallback. Not
  needed — if we make the scraper reliable, we don't need any fallback.

---

## Outcome

**All 7 tasks complete.** Server restarted; first scrape after restart
succeeded on the first attempt in 6.5 seconds (vs. previously failing
every cold-start). Real numbers now flowing: 5h=21%, weekly=17%,
weekly sonnet=0%. Snapshot is cleaned of all approximation fields and
all dead `live_limits` / debug-watcher fields.

To verify on the Pi: the dashboard should now show "—" briefly on
fresh server boot until the first scrape lands (~7s), then real
numbers. If the scraper ever fails, the bars will show "—" and the
foot text will explain why instead of silently showing fake numbers.
