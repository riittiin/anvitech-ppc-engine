"use strict";

// Per-rule tab metadata.
const RULES = {
  rule1: { n: 1, title: "Consolidate" },
  rule2: { n: 2, title: "Sort by delivery date" },
  rule3: { n: 3, title: "Smart priority (slack)" },
  rule4: { n: 4, title: "Setup time" },
  rule5: { n: 5, title: "Overlap mode" },
  rule6: { n: 6, title: "Allocate to machines" },
  rule7: { n: 7, title: "Capture actuals" },
  rule8: { n: 8, title: "Plan (book → remaining qty)" },
};
const ORDER = ["rule1","rule2","rule3","rule4","rule5","rule6","rule7","rule8"];
// Only two per-rule tabs are shown: Rule 6 (Allocate to machines — where the user
// downloads the schedule) and Rule 7 (Capture actuals — the feedback entry). Rules
// 1–5 and Rule 8 still run in the backend (the trace computes them; Rule 8 = the Plan
// over the order book, also shown on the Orders tab + triggered by the Plan button)
// but are HIDDEN from the UI. Add a key back here to re-show its tab. Frontend-only.
const VISIBLE_TABS = ["rule6", "rule7"];

let currentTrace = null;
let currentGantt = null;
let currentOrders = null;     // {columns, rows} from /run or /orders
let currentExpected = null;   // {"SO\x1fITEM": "YYYY-MM-DD"} from /run's expected_end (Task 9)
let optimizeMeta = null;      // /run's optimize_meta: applied optimization + staleness
let autoNote = null;          // /run's auto_note: scheduled optimization's latest note ({text, at} or null)
let optimizePollTimer = null; // /optimize/status polling handle
let ITEMS = null;
let ganttDayWidth = 200;   // px per day column (Gantt is day-level, no hour detail)
let currentRole = "user";   // set from /me; default to the least-privileged role
let currentConfig = null;   // /run's config (both roles) — feeds the status strip's plan basis
let nextRotation = null;    // /operators next_rotation (admin only) — status strip segment

// ---- View router ----
// Six destinations, one visible at a time. Each maps to an existing render call
// (the old per-rule tab machinery, absorbed into a fixed nav). `mountFor` returns
// the per-view content div so the unchanged render functions write to the right spot.
const VIEWS = ["orders", "schedule", "gantt", "entry", "analytics", "settings"];
let activeView = "orders";

const $ = (id) => document.getElementById(id);
const setStatus = (m) => { $("status").textContent = m; };
const setDatasetStatus = (m) => { $("dataset-status").innerHTML = m; };

function mountFor(key) {
  if (key === "orders") return $("orders-mount");
  if (key === "gantt") return $("gantt-mount");
  if (key === "analytics") return $("analytics-mount");
  if (key === "rule6" || key === "schedule") return $("schedule-mount");
  if (key === "rule7" || key === "entry") return $("entry-mount");
  return $("tab-content");
}

// Render the content for a view using the existing render functions.
function renderView(v) {
  if (v === "orders") renderOrders();
  else if (v === "schedule") renderTab("rule6");
  else if (v === "gantt") renderGantt();
  else if (v === "entry") renderTab("rule7");
  else if (v === "analytics") renderAnalytics();
  // "settings" is static markup (cards wired once at boot) — nothing to render.
}

function showView(v, push) {
  if (!VIEWS.includes(v)) v = "orders";
  if (v === "settings" && currentRole !== "admin") v = "orders";   // never land users on admin-only
  activeView = v;
  try { localStorage.setItem("anvitech-view", v); } catch (e) { /* private mode */ }
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === v));
  renderView(v);
  if (push && location.hash.replace(/^#/, "") !== v) location.hash = v;
}

function lastView() {
  let v;
  try { v = localStorage.getItem("anvitech-view"); } catch (e) { v = null; }
  return VIEWS.includes(v) ? v : "orders";
}

function onHashChange() {
  const h = location.hash.replace(/^#/, "");
  showView(VIEWS.includes(h) ? h : lastView());
}

function readConfig() {
  const cfgObj = {
    consolidation_window_days: Number($("cfg-window").value),
    setup_time_min: Number($("cfg-setup").value),
    overlap_mode: "overlap",   // sequential mode retired — always overlap
    overlap_percent: Number($("cfg-overlap-pct").value),
    priority_metric: $("cfg-priority-metric").value,
    priority_window_days: $("cfg-priority-window").value,
    apply_operator_logic: $("cfg-operator-logic").checked,
    split_parallel: $("cfg-split-parallel").checked,
    // Tick mark → a 45-min expedite window; unticked → 0 (legacy non-delay).
    expedite_window_min: $("cfg-expedite") && $("cfg-expedite").checked ? 45 : 0,
    balance_operator_load: !!($("cfg-balance-ops") && $("cfg-balance-ops").checked),
  };
  // Plan start date (the day scheduling begins). "Start from today" (auto) sends
  // null — the server resolves it to the real current date (IST) on every plan,
  // so a live shop never drifts to a stale date. Unchecked → send the picked ISO
  // date (omitted if blank so the saved/default date is kept). Daily punches still
  // advance the clock past it either way.
  const auto = $("cfg-start-auto");
  if (auto && auto.checked) {
    cfgObj.plan_start_date = null;
  } else {
    const ps = $("cfg-plan-start") && $("cfg-plan-start").value;
    if (ps) cfgObj.plan_start_date = ps;
  }
  return cfgObj;
}

// Today's date as ISO (browser local) — a FALLBACK for the disabled auto-mode
// display only; the authoritative start is the server-resolved IST date carried
// in the /run response (resolved_plan_start, cached below).
function todayIso() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
let lastResolvedStart = null;   // resolved_plan_start from the latest /run response

function updatePlanStartEcho() {
  const auto = $("cfg-start-auto"), inp = $("cfg-plan-start"), e = $("cfg-plan-start-echo");
  const isAuto = !!(auto && auto.checked);
  if (inp) {
    inp.disabled = isAuto;             // auto mode: the picker is display-only
    if (isAuto) inp.value = lastResolvedStart || todayIso();  // server IST today
  }
  const v = inp && inp.value;
  if (e) e.textContent = v ? "= " + isoToDdmmyyyy(v) : "";
}

// ---- Session / role ----
// Learn who we are. Default to the least-privileged role on any failure, and
// send the browser to the login page if the session is gone.
async function initSession() {
  try {
    const res = await fetch("/me");
    if (res.status === 401) { window.location = "/login"; return; }
    const me = await res.json();
    currentRole = me.role === "admin" ? "admin" : "user";
    renderSessionInfo(me.username, currentRole);
  } catch (e) {
    currentRole = "user";
  }
  document.body.classList.toggle("role-user", currentRole !== "admin");
}

function renderSessionInfo(username, role) {
  const el = $("session-info");
  if (!el) return;
  const label = role === "admin" ? "Admin" : "User";
  el.innerHTML = `Signed in as <strong>${escapeHtml(username || "")}</strong> · ${escapeHtml(label)} `
    + `<form method="post" action="/logout" class="logout-form"><button type="submit">Logout</button></form>`;
}

// Reflect the (server) SAVED plan config back into the admin's config bar so it
// shows the last-saved plan settings. `resolvedStart` (response key
// resolved_plan_start) = the ISO date the server's plan clock actually started
// from — shown in the disabled picker when auto ("start from today") is on.
function applyConfig(cfg, resolvedStart) {
  if (!cfg) return;
  const setVal = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  const setSel = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  // null plan_start_date = auto ("start from today"): tick the box, show the
  // server-resolved start (IST today; browser-local today as a fallback).
  const _auto = $("cfg-start-auto");
  const _isAuto = (cfg.plan_start_date === null || cfg.plan_start_date === undefined);
  if (_auto) _auto.checked = _isAuto;
  if (resolvedStart) lastResolvedStart = resolvedStart;
  setVal("cfg-plan-start", _isAuto ? (resolvedStart || todayIso()) : cfg.plan_start_date);
  updatePlanStartEcho();
  setVal("cfg-window", cfg.consolidation_window_days);
  setVal("cfg-setup", cfg.setup_time_min);
  setVal("cfg-overlap-pct", cfg.overlap_percent);
  setSel("cfg-priority-metric", cfg.priority_metric);
  const pw = $("cfg-priority-window");
  if (pw) pw.value = (cfg.priority_window_days === null || cfg.priority_window_days === undefined)
    ? "" : String(cfg.priority_window_days);
  const ol = $("cfg-operator-logic");
  if (ol) ol.checked = !!cfg.apply_operator_logic;
  const sp = $("cfg-split-parallel");
  if (sp) sp.checked = !!cfg.split_parallel;
  const ex = $("cfg-expedite");
  if (ex) ex.checked = Number(cfg.expedite_window_min) > 0;
  const bo = $("cfg-balance-ops");
  if (bo) bo.checked = !!cfg.balance_operator_load;
}

// ---- Plan (Run + Rerun unified) ----
// persist=true only on the admin's explicit Plan click (saves the config so every
// login sees the same plan). Auto-load and the user role pass persist=false.
async function runPlan(persist = false) {
  setStatus("Planning…");
  try {
    const res = await fetch("/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: readConfig(), persist: !!persist }),
    });
    if (res.status === 401) { window.location = "/login"; return; }
    if (!res.ok) { setStatus("Error: " + (await res.text())); return; }
    const data = await res.json();
    currentTrace = data.trace;
    currentGantt = data.gantt || null;
    currentOrders = data.orders || null;
    currentExpected = data.expected_end || null;
    optimizeMeta = data.optimize_meta || null;
    autoNote = data.auto_note || null;
    currentConfig = data.config || null;
    if (data.resolved_plan_start) lastResolvedStart = data.resolved_plan_start;
    if (currentRole === "admin" && data.config) applyConfig(data.config, data.resolved_plan_start);
    renderReport(data.report);
    renderOptimizeBanner();
    renderAutoNote();
    renderStatusStrip();
    renderView(activeView);
    setStatus("Plan " + data.run_id + " complete.");
  } catch (e) { setStatus("Request failed: " + e.message); }
}

// ---- Upload & merge into the order book ----
async function uploadExcel() {
  const f = $("xlsx-file").files[0];
  if (!f) { setDatasetStatus("Choose an .xlsx file first."); return; }
  const fd = new FormData(); fd.append("file", f);
  setDatasetStatus("Uploading & merging…");
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    if (!res.ok) { setDatasetStatus("Upload failed: " + (await res.text())); return; }
    const d = await res.json();
    ITEMS = null;  // item metadata may have changed
    let msg = `<strong>${escapeHtml(d.name)}</strong>: ${d.added} new order(s) added`;
    if (d.flagged && d.flagged.length) {
      const detail = d.flagged.map((f) => `${escapeHtml(f.so_no)}/${escapeHtml(f.item_code || "")} (${escapeHtml(f.reason)})`).join("; ");
      msg += ` · <span class="pill-pending">${d.flagged.length} flagged</span>: ${detail}`;
    }
    if (d.masters_updated) msg += " · masters updated";
    setDatasetStatus(msg);
    await runPlan();           // refresh the book + schedule
    showView("orders", true);
  } catch (e) { setDatasetStatus("Upload error: " + e.message); }
}

// ---- Optimize (admin: search for a better job sequence; banner for everyone) ----
function fmtMetrics(m) {
  if (!m) return "—";
  return `${m.makespan_days} days · ${m.late_orders}/${m.orders} late · ${m.total_late_days} late-days`;
}

function isoToDisplayDateTime(iso) {       // "2026-07-13T10:04:00" -> "13-07-2026 10:04"
  const s = String(iso || "");
  const [d, t] = s.split("T");
  return d ? isoToDdmmyyyy(d) + (t ? " " + t.slice(0, 5) : "") : s;
}

function renderOptimizeBanner() {
  const b = $("optimize-banner");
  if (!b) return;
  if (!optimizeMeta || !optimizeMeta.active) { b.classList.add("hidden"); b.innerHTML = ""; return; }
  b.classList.remove("hidden");
  let h = `Optimized plan active (saved ${escapeHtml(isoToDisplayDateTime(optimizeMeta.saved_at))})`;
  if (optimizeMeta.uncovered > 0) {
    h += ` · <span class="pill-pending">${optimizeMeta.uncovered} order(s) added since — ` +
         `run Optimize again for the best plan</span>`;
  }
  if (optimizeMeta.inputs_changed) {
    h += ` · <span class="pill-pending">Settings or masters have changed since this ` +
         `optimization was computed — its numbers will differ; run Optimize again</span>`;
  }
  if (currentRole === "admin") {
    h += ` <button id="optimize-clear-btn" class="ghost-btn small">Remove optimization</button>`;
  }
  b.innerHTML = h;
  const clr = $("optimize-clear-btn");
  if (clr) clr.onclick = async () => {
    if (!window.confirm("Remove the optimized order and go back to the standard plan?")) return;
    const res = await fetch("/optimize/clear", { method: "POST" });
    if (res.ok) { setStatus("Optimization removed."); await runPlan(false); }
    else setStatus("Could not remove optimization: " + (await res.text()));
  };
}

// Self-tuning contest's latest note (auto-triggered runs only) — informational,
// no control.
function renderAutoNote() {
  const el = $("auto-note");
  if (!el) return;
  const n = autoNote;
  if (n && n.text) { el.classList.remove("hidden"); el.textContent = n.text; }
  else { el.classList.add("hidden"); el.textContent = ""; }
}

// ---- Status strip (both roles) ----
// One calm line, entirely from data already fetched: order/late counts, the plan
// basis, next scheduled optimization, next shift rotation (admin only), the latest
// auto-note (collapsible), and a colored warning chip for staleness / unstaffed hrs.
function countLateOrders() {
  if (!currentOrders || !currentOrders.rows || !currentExpected) return 0;
  const cols = currentOrders.columns;
  const soI = cols.indexOf("SO No"), itI = cols.indexOf("Item Code");
  const ddI = cols.indexOf("SO Delivery Date"), stI = cols.indexOf("Status");
  if (soI < 0 || ddI < 0) return 0;
  let late = 0;
  currentOrders.rows.forEach((r) => {
    if (stI >= 0 && String(r[stI]) === "Complete") return;   // archived — not "late"
    const key = String(r[soI]) + "\x1f" + String(itI >= 0 ? r[itI] : "");
    const exp = isoToDate(currentExpected[key]);
    const due = ddmmyyyyToDate(r[ddI]);
    if (exp && due && exp > due) late++;
  });
  return late;
}

function renderStatusStrip() {
  const el = $("status-strip");
  if (!el) return;
  const nOrders = currentOrders && currentOrders.rows ? currentOrders.rows.length : 0;
  const nLate = countLateOrders();

  const segs = [];
  segs.push(`<span class="ss-seg"><span class="ss-dot"></span>${nOrders} order${nOrders === 1 ? "" : "s"}</span>`);
  segs.push(`<span class="ss-seg${nLate ? " ss-late" : ""}">${nLate} late</span>`);

  // Plan basis: null start date = "follows today" (server-resolved); else a fixed date.
  const isAuto = currentConfig ? (currentConfig.plan_start_date === null || currentConfig.plan_start_date === undefined) : true;
  const startDisp = isoToDdmmyyyy(lastResolvedStart || todayIso());
  segs.push(isAuto
    ? `<span class="ss-seg">Plan follows today (${escapeHtml(startDisp)})</span>`
    : `<span class="ss-seg">Plan starts ${escapeHtml(isoToDdmmyyyy(currentConfig.plan_start_date))}</span>`);

  segs.push(`<span class="ss-seg">Next optimization: ${escapeHtml(nextScheduledOptimize())}</span>`);
  if (currentRole === "admin" && nextRotation) {
    segs.push(`<span class="ss-seg">Next rotation: ${escapeHtml(isoToDdmmyyyy(nextRotation))}</span>`);
  }

  // Latest auto-note — collapsible.
  if (autoNote && autoNote.text) {
    segs.push(`<button type="button" class="ss-note" id="ss-note-toggle">▸ note</button>`
      + `<span class="ss-note-text hidden" id="ss-note-text">${escapeHtml(autoNote.text)}</span>`);
  }

  // Warning chip: staleness and/or unstaffed hours (both from the /run response).
  const warns = [];
  if (optimizeMeta && optimizeMeta.inputs_changed) {
    warns.push("Settings or masters changed since the applied optimization — its numbers will differ; run Optimize again.");
  }
  const unstaffed = currentTrace && currentTrace.analytics && currentTrace.analytics.headline
    ? currentTrace.analytics.headline.unstaffed_hrs : 0;
  if (unstaffed && unstaffed > 0) {
    warns.push(`${Math.round(unstaffed)} hrs of scheduled work fall in shifts with no free qualified operator — those shifts need more crew.`);
  }
  let chip = "";
  if (warns.length) {
    chip = `<button type="button" class="ss-chip" id="ss-chip">⚠ ${warns.length} warning${warns.length === 1 ? "" : "s"}</button>`
      + `<div class="ss-chip-detail hidden" id="ss-chip-detail"><ul>${warns.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul></div>`;
  }

  el.innerHTML = `<div class="ss-line">${segs.join("")}${chip}</div>`;

  const nt = $("ss-note-toggle");
  if (nt) nt.onclick = () => {
    const t = $("ss-note-text");
    if (t) { const open = t.classList.toggle("hidden") === false; nt.textContent = (open ? "▾ " : "▸ ") + "note"; }
  };
  const cb = $("ss-chip");
  if (cb) cb.onclick = () => { const d = $("ss-chip-detail"); if (d) d.classList.toggle("hidden"); };
}

function optimizeProgressLine(st) {
  const where = st.mode === "cloud" ? " in the cloud" : "";
  const tried = `tried ${st.evals} of ${st.budget_evals} plans${where}`;
  const best = st.best ? ` — best so far: ${fmtMetrics(st.best)}` : "";
  return tried + best;
}

// Poll the server for progress. CRUCIAL: this must survive a failed poll — Render's
// free tier drops the odd request, and if we stopped polling on the first blip the
// counter would freeze while the search kept running underneath. So on ANY failure we
// simply schedule another attempt (a slightly longer gap) instead of giving up.
function _schedulePoll(ms) {
  clearTimeout(optimizePollTimer);
  optimizePollTimer = setTimeout(pollOptimizeStatus, ms);
}

function _showRunningControls(running) {
  const startBtn = $("optimize-start"), stopBtn = $("optimize-stop");
  if (startBtn) startBtn.disabled = running;
  if (stopBtn) stopBtn.classList.toggle("hidden", !running);
}

async function pollOptimizeStatus() {
  let st;
  try {
    const res = await fetch("/optimize/status");
    if (!res.ok) { _schedulePoll(5000); return; }   // transient — try again, don't die
    st = await res.json();
  } catch (e) {
    _schedulePoll(5000);                              // network blip — try again, don't die
    return;
  }
  const prog = $("optimize-progress");
  if (st.state === "running") {
    _showRunningControls(true);
    const tail = st.stopping ? " — stopping…" : "…";
    if (prog) prog.textContent = optimizeProgressLine(st) + tail;
    _schedulePoll(3000);
    return;
  }
  clearTimeout(optimizePollTimer); optimizePollTimer = null;
  _showRunningControls(false);
  if (st.state === "failed") {
    if (prog) prog.textContent = "Optimization failed: " + (st.error || "unknown error");
    return;
  }
  if (st.state === "done") {
    const how = st.cancelled ? "stopped early" : "done";
    if (prog) prog.textContent = `${how} — ${st.evals} plans tried in ${st.elapsed_s}s`;
    renderOptimizeResult(st);
  }
}

function renderOptimizeResult(st) {
  const box = $("optimize-result");
  if (!box) return;
  if (st.auto) {
    // An auto-triggered contest already applied itself (or found nothing better) —
    // never show a stale Apply/Discard panel for a decision that's already made.
    box.classList.remove("hidden");
    box.innerHTML = "<p>Auto-optimize finished — the plan updated itself if a better one was found.</p>";
    runPlan(false);
    return;
  }
  box.classList.remove("hidden");
  const rows = [
    ["Total days to finish", st.baseline ? st.baseline.makespan_days : "—",
     st.best ? st.best.makespan_days : "—"],
    ["Late orders", st.baseline ? `${st.baseline.late_orders} / ${st.baseline.orders}` : "—",
     st.best ? `${st.best.late_orders} / ${st.best.orders}` : "—"],
    ["Total late-days (sum of delivery gaps)", st.baseline ? st.baseline.total_late_days : "—",
     st.best ? st.best.total_late_days : "—"],
    ["Worst order lateness (days)", st.baseline ? st.baseline.max_late_days : "—",
     st.best ? st.best.max_late_days : "—"],
  ];
  const stoppedNote = st.cancelled ? " (stopped early — this is the best of the plans tried so far)" : "";
  let h = st.improved
    ? `<p><strong>A better plan was found${stoppedNote}.</strong> Apply it to use this order in every plan from now on.</p>`
    : `<p><strong>No improvement found</strong> — the current plan already matches the best sequence tried.</p>`;
  // Settings sweep: Optimize also tried the overlap % values.
  if (st.best_overlap != null && st.current_overlap != null) {
    h += st.best_overlap !== st.current_overlap
      ? `<p>Best setting found: <strong>Overlap ${st.best_overlap}%</strong> (currently ${st.current_overlap}%). Applying this plan also sets Overlap to ${st.best_overlap}% in Settings.</p>`
      : `<p>Your Overlap setting (${st.current_overlap}%) was also tested against ${escapeHtml("50–100%")} — it is already the best.</p>`;
  }
  h += '<div class="table-wrap"><table><thead><tr><th></th><th>Standard plan</th><th>Optimized</th></tr></thead><tbody>';
  rows.forEach((r) => {
    h += `<tr><td>${escapeHtml(String(r[0]))}</td><td>${escapeHtml(String(r[1]))}</td><td><strong>${escapeHtml(String(r[2]))}</strong></td></tr>`;
  });
  h += "</tbody></table></div>";
  h += `<div class="optimize-actions">
          <button id="optimize-apply-btn" class="primary">Apply this plan</button>
          <button id="optimize-discard-btn" class="ghost-btn">Discard</button>
        </div>`;
  box.innerHTML = h;
  $("optimize-apply-btn").onclick = async () => {
    const res = await fetch("/optimize/apply", { method: "POST" });
    if (!res.ok) { setStatus("Apply failed: " + (await res.text())); return; }
    box.classList.add("hidden"); box.innerHTML = "";
    setStatus("Optimized order applied — replanning…");
    await runPlan(false);
  };
  $("optimize-discard-btn").onclick = () => { box.classList.add("hidden"); box.innerHTML = ""; };
}

async function startOptimize() {
  const budget = "deep";   // one option (owner decision): ~1,000 plans total
  const prog = $("optimize-progress");
  const box = $("optimize-result");
  if (box) { box.classList.add("hidden"); box.innerHTML = ""; }
  _showRunningControls(true);
  if (prog) prog.textContent = "starting…";
  try {
    const res = await fetch("/optimize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ budget }),
    });
    if (!res.ok) {
      if (prog) prog.textContent = "Could not start: " + (await res.text());
      _showRunningControls(false);
      return;
    }
    pollOptimizeStatus();
  } catch (e) {
    if (prog) prog.textContent = "Request failed: " + e.message;
    _showRunningControls(false);
  }
}

async function stopOptimize() {
  const stopBtn = $("optimize-stop");
  if (stopBtn) stopBtn.disabled = true;
  try {
    await fetch("/optimize/cancel", { method: "POST" });   // keeps best-so-far
  } catch (e) { /* the poll will still pick up the done state */ }
  if (stopBtn) stopBtn.disabled = false;
  pollOptimizeStatus();   // reflect "stopping…" / result promptly
}

// ---- Loader report (collapsible) ----
const REPORT_LABELS = {
  PENDING_MASTER_DATA: "provisional machines", NO_ROUTING: "orders without routing",
  TIME_COERCION: "time coercions", BAD_DELIVERY_DATE: "bad delivery dates", MISSING_SHEET: "missing sheets",
};
function renderReport(report) {
  const panel = $("report-panel"), toggle = $("report-toggle"), detail = $("report-detail");
  if (!report || !report.rows || report.rows.length === 0) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const counts = {};
  report.rows.forEach((r) => { counts[r[0]] = (counts[r[0]] || 0) + 1; });
  const parts = Object.entries(counts).map(([k, v]) => `${v} ${REPORT_LABELS[k] || k}`).join(", ");
  toggle.innerHTML = `⚠ ${parts} (data gaps) — click to view <span class="chev">▸</span>`;
  detail.innerHTML = tableHtml(report, true);
  detail.classList.add("hidden"); toggle.classList.remove("open");
  toggle.onclick = () => { const open = !detail.classList.toggle("hidden"); toggle.classList.toggle("open", open); };
}

// ---- Per-view content (rule6 = Schedule, rule7 = Daily Entry) ----
// The fixed nav (renderView/showView) drives which view is visible; this renders
// the dynamic content for the rule-backed views into their per-view mount.
function renderTab(key) {
  if (key === "orders") { renderOrders(); return; }
  if (key === "gantt") { renderGantt(); return; }
  if (key === "analytics") { renderAnalytics(); return; }
  const entry = currentTrace ? currentTrace[key] : null;
  const meta = RULES[key];
  const root = mountFor(key);
  if (!entry) { root.innerHTML = '<p class="placeholder">Click <strong>Plan</strong> to run the rules.</p>'; return; }

  let html = `<div class="rule-header"><h2>Rule ${meta.n} — ${meta.title}</h2></div>`;
  if (entry.reached === false) {
    html += '<div class="not-reached-box">Not reached — a previous rule stopped the chain.</div>';
    root.innerHTML = html; return;
  }
  if (entry.error) {
    html += `<div class="error-box"><strong>Rule error</strong> — record `
      + `<code>${escapeHtml(String(entry.error.record_id))}</code>: ${escapeHtml(entry.error.message)}</div>`;
  }
  if (key === "rule7") html += actualsFormHtml();

  // Task 3: let operators download the machine schedule to print and follow.
  if (key === "rule6" && entry.output && entry.output.rows && entry.output.rows.length) {
    html += '<div class="dl-toolbar">'
      + '<button id="dl-schedule" class="primary">⬇ Download schedule (CSV)</button>'
      + '<button id="dl-machine">⬇ Download machine-wise view</button>'
      + '<button id="dl-shiftwise">⬇ Download shift-wise schedule</button>'
      + '<span class="muted"> — opens in Excel; print it for the floor</span></div>';
  }

  html += '<div class="io">';
  html += `<div class="panel"><h3>Input</h3>${tableHtml(entry.input)}</div>`;
  if (key === "rule7") {
    html += `<div class="panel"><h3>Saved entries</h3>${actualsOutputHtml(entry.output, entry.actuals_ids)}</div>`;
  } else {
    html += `<div class="panel"><h3>Output</h3>${tableHtml(entry.output)}</div>`;
  }
  html += "</div>";
  if (entry.tables && entry.tables.length) {
    entry.tables.forEach((t) => {
      html += `<div class="panel extra-table"><h3>${escapeHtml(t.title)}</h3>${tableHtml(t.table)}</div>`;
    });
  }
  if (entry.notes && entry.notes.length) {
    html += '<div class="notes"><h3>Decision notes</h3><ul>';
    entry.notes.forEach((n) => (html += `<li>${escapeHtml(n)}</li>`));
    html += "</ul></div>";
  }
  root.innerHTML = html;
  if (key === "rule7") { wireActualsForm(); wireRollback(); }
  if (key === "rule6") {
    const sched = $("dl-schedule");
    if (sched) sched.onclick = () => downloadCsv(`anvitech-schedule-${todayStamp()}.csv`, entry.output);
    const mach = $("dl-machine");
    const mView = entry.tables && entry.tables[0] && entry.tables[0].table;
    if (mach && mView) mach.onclick = () => downloadCsv(`anvitech-machine-schedule-${todayStamp()}.csv`, mView);
    else if (mach) mach.style.display = "none";
    const shift = $("dl-shiftwise");
    if (shift && entry.shiftwise && entry.shiftwise.rows && entry.shiftwise.rows.length) {
      shift.onclick = () => downloadCsv(`anvitech-shiftwise-schedule-${todayStamp()}.csv`, entry.shiftwise);
    } else if (shift) shift.style.display = "none";   // hidden when operator logic is off
  }
}

// ---- CSV download (for printing schedules) ----
function formatDdmmyyyy(d) {
  return `${String(d.getDate()).padStart(2, "0")}-${String(d.getMonth() + 1).padStart(2, "0")}-${d.getFullYear()}`;
}
function todayStamp() {
  return formatDdmmyyyy(new Date());
}

// Next scheduled cloud optimize run: Mon or Fri at 11:00 local (the GitHub cron
// fires Mon/Fri 05:30 UTC = 11:00 IST, after the ~10:00 feedback entry window).
// If today IS a Mon/Fri and it's still before 11:00, today counts; otherwise the
// next Mon/Fri. `now` is optional (defaults to the real clock) so this is
// testable-by-reading without any mocking.
function nextScheduledOptimize(now) {
  const base = now instanceof Date ? new Date(now.getTime()) : new Date();
  const isSchedDay = (d) => d.getDay() === 1 || d.getDay() === 5;   // Mon=1, Fri=5
  const at11 = (d) => { const t = new Date(d.getTime()); t.setHours(11, 0, 0, 0); return t; };
  if (isSchedDay(base) && base < at11(base)) return formatDdmmyyyy(at11(base));
  const next = new Date(base.getTime());
  do { next.setDate(next.getDate() + 1); } while (!isSchedDay(next));
  return formatDdmmyyyy(at11(next));
}
function tableToCsv(table) {
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [table.columns.map(esc).join(",")];
  table.rows.forEach((r) => lines.push(r.map(esc).join(",")));
  return lines.join("\r\n");
}
function downloadCsv(filename, table) {
  if (!table || !table.columns) return;
  const blob = new Blob(["﻿" + tableToCsv(table)], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

// ---- Orders dashboard ----
async function renderOrders() {
  const root = mountFor("orders");
  if (!currentOrders) {
    try { currentOrders = (await (await fetch("/orders")).json()).orders; } catch (e) { currentOrders = null; }
  }
  const isAdmin = currentRole === "admin";
  let html = "";
  if (!currentOrders || !currentOrders.rows.length) {
    html += isAdmin
      ? '<p class="placeholder">No orders yet — upload your Excel to begin.</p>'
      : '<p class="placeholder">No orders yet.</p>';
    root.innerHTML = html; return;
  }
  // Commitment controls are admin-only (server enforces this too). Danger actions
  // (delete) live in a separate strip at the bottom, visually set apart.
  if (isAdmin) {
    html += '<div class="ord-toolbar">'
      + '<button id="ord-commit-sel">Commit selected</button> '
      + '<button id="ord-urgent-sel">Mark Urgent</button> '
      + '<button id="ord-uncommit-sel">Uncommit selected</button>'
      + '</div>';
  }
  html += orderTableHtml(currentOrders, isAdmin);
  html += '<p class="g-note">Pending = not started · Running = production logged · Complete = marked complete on a Rule 7 entry (archived). Plan schedules every active order by its <strong>remaining</strong> qty. '
    + 'Lane: status labels only (no scheduling effect) — <strong>open</strong> = newly arrived · <strong>committed</strong> = released, promised date snapshotted · <strong>urgent</strong> = flagged, promised = delivery date. The plan sequences all orders together by delivery date.</p>';
  if (isAdmin) {
    html += '<div class="danger-strip">'
      + '<span class="danger-label">Danger zone</span>'
      + '<button id="ord-del-sel">Delete selected</button> '
      + '<button id="ord-del-all" class="danger">Delete ALL data</button>'
      + '<span class="muted"> · deletes permanently from the database (and their actuals)</span></div>';
  }
  root.innerHTML = html;
  if (isAdmin) { wireOrdersDelete(); wireOrdersCommit(); }
}

// "DD-MM-YYYY" -> Date, or null if unparseable (used to compare Promised vs
// Current expected for the slip highlight).
function ddmmyyyyToDate(s) {
  const p = String(s || "").split("-");
  if (p.length !== 3) return null;
  const d = new Date(+p[2], +p[1] - 1, +p[0]);
  return isNaN(d.getTime()) ? null : d;
}
// ISO "YYYY-MM-DD" -> Date, or null.
function isoToDate(s) {
  if (!s) return null;
  const d = new Date(String(s) + "T00:00:00");
  return isNaN(d.getTime()) ? null : d;
}

function orderTableHtml(table, showSelect) {
  const sIdx = table.columns.indexOf("Status");
  const soIdx = table.columns.indexOf("SO No");
  const itemIdx = table.columns.indexOf("Item Code");
  const laneIdx = table.columns.indexOf("Lane");
  const promIdx = table.columns.indexOf("Promised");
  let h = '<div class="table-wrap"><table><thead><tr>';
  if (showSelect) h += '<th><input type="checkbox" id="ord-all-check" title="select all"></th>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += '<th>Current expected</th>';
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    const so = soIdx >= 0 ? String(row[soIdx]) : "";
    const item = itemIdx >= 0 ? String(row[itemIdx]) : "";
    h += "<tr>";
    // An order is the (SO#, item) pair — carry both so delete targets the exact line.
    if (showSelect) h += `<td><input type="checkbox" class="ordsel" data-so="${escapeHtml(so)}" data-item="${escapeHtml(item)}"></td>`;
    row.forEach((cell, i) => {
      const v = cell === null || cell === undefined ? "" : String(cell);
      if (i === sIdx) h += `<td><span class="status-pill status-${v.toLowerCase()}">${escapeHtml(v)}</span></td>`;
      else if (i === laneIdx) h += `<td><span class="lane-badge lane-${v.toLowerCase()}">${escapeHtml(v)}</span></td>`;
      else h += `<td>${escapeHtml(v)}</td>`;
    });
    // Current expected: look up the plan's expected completion for this (SO, item);
    // only populated after a Plan run (currentExpected comes from /run, not /orders).
    const key = so + "\x1f" + item;
    const expIso = currentExpected ? currentExpected[key] : null;
    const expDisp = expIso ? isoToDdmmyyyy(expIso) : "";
    const promised = promIdx >= 0 ? String(row[promIdx] || "") : "";
    const promD = promised ? ddmmyyyyToDate(promised) : null;
    const expD = expIso ? isoToDate(expIso) : null;
    const slip = promD && expD && expD > promD;
    h += `<td class="${slip ? "slip" : ""}">${escapeHtml(expDisp)}</td>`;
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

// A destructive action guarded by re-entering the admin password. ``doFetch(pw)``
// must return the fetch Response; on 403 the modal stays open with an error and
// lets the user retry; Cancel/Escape aborts. Resolves to true on success.
function deleteWithPassword(message, doFetch) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.innerHTML = `
      <div class="modal">
        <h3>⚠ Confirm deletion</h3>
        <p>${escapeHtml(message)}</p>
        <p class="muted">This cannot be undone. Enter your password to continue.</p>
        <input id="pw-confirm" type="password" placeholder="Password" autocomplete="off" />
        <div class="modal-err" id="pw-err"></div>
        <div class="modal-actions">
          <button id="pw-cancel">Cancel</button>
          <button id="pw-ok" class="danger">Delete</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const input = ov.querySelector("#pw-confirm");
    const err = ov.querySelector("#pw-err");
    const ok = ov.querySelector("#pw-ok");
    const done = (v) => { ov.remove(); resolve(v); };
    setTimeout(() => input.focus(), 0);

    const submit = async () => {
      const pw = input.value;
      if (!pw) { err.textContent = "Enter your password."; return; }
      ok.disabled = true; err.textContent = "Checking…";
      try {
        const res = await doFetch(pw);
        if (res.status === 403) { err.textContent = "Password incorrect — try again."; ok.disabled = false; input.select(); return; }
        if (!res.ok) { err.textContent = "Failed: " + (await res.text()); ok.disabled = false; return; }
        done(true);
      } catch (e) { err.textContent = "Error: " + e.message; ok.disabled = false; }
    };
    ov.querySelector("#pw-cancel").onclick = () => done(false);
    ok.onclick = submit;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit();
      if (e.key === "Escape") done(false);
    });
    ov.addEventListener("click", (e) => { if (e.target === ov) done(false); });
  });
}

function wireOrdersDelete() {
  const allCheck = $("ord-all-check");
  if (allCheck) allCheck.onclick = () => {
    document.querySelectorAll(".ordsel").forEach((c) => (c.checked = allCheck.checked));
  };
  const delSel = $("ord-del-sel");
  if (delSel) delSel.onclick = async () => {
    const sel = [...document.querySelectorAll(".ordsel:checked")].map((c) => [c.dataset.so, c.dataset.item]);
    if (!sel.length) { setStatus("No rows selected to delete."); return; }
    const okd = await deleteWithPassword(
      `Permanently delete ${sel.length} order(s) and their production data?`,
      (pw) => fetch("/orders/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel, password: pw }),
      }));
    if (!okd) { setStatus("Delete cancelled."); return; }
    setStatus(`Deleted ${sel.length} order(s).`);
    currentOrders = null; await runPlan();
  };
  const delAll = $("ord-del-all");
  if (delAll) delAll.onclick = async () => {
    const okd = await deleteWithPassword(
      "Permanently delete ALL orders and production data from the database?",
      (pw) => fetch("/orders/clear", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      }));
    if (!okd) { setStatus("Delete cancelled."); return; }
    setStatus("All orders deleted.");
    currentOrders = null; await runPlan();
  };
}

// Selected (SO, item) pairs from the order-book checkboxes.
function selectedOrderPairs() {
  return [...document.querySelectorAll(".ordsel:checked")].map((c) => [c.dataset.so, c.dataset.item]);
}

// Wire the Commit / Mark Urgent / Uncommit toolbar buttons (admin only).
function wireOrdersCommit() {
  const commitBtn = $("ord-commit-sel");
  if (commitBtn) commitBtn.onclick = async () => {
    const sel = selectedOrderPairs();
    if (!sel.length) { setStatus("Select orders to commit."); return; }
    try {
      const res = await fetch("/orders/commit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel }),
      });
      if (!res.ok) { setStatus("Commit failed: " + (await res.text())); return; }
      setStatus(`Committed ${sel.length} order(s).`);
      currentOrders = null; await runPlan();
    } catch (e) { setStatus("Commit error: " + e.message); }
  };

  const uncommitBtn = $("ord-uncommit-sel");
  if (uncommitBtn) uncommitBtn.onclick = async () => {
    const sel = selectedOrderPairs();
    if (!sel.length) { setStatus("Select orders to uncommit."); return; }
    try {
      const res = await fetch("/orders/uncommit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orders: sel }),
      });
      if (!res.ok) { setStatus("Uncommit failed: " + (await res.text())); return; }
      setStatus(`Uncommitted ${sel.length} order(s).`);
      currentOrders = null; await runPlan();
    } catch (e) { setStatus("Uncommit error: " + e.message); }
  };

  const urgentBtn = $("ord-urgent-sel");
  if (urgentBtn) urgentBtn.onclick = async () => {
    const sel = selectedOrderPairs();
    if (sel.length !== 1) { setStatus("Select exactly one order to mark urgent."); return; }
    const [so, item] = sel[0];
    try {
      const res = await fetch("/orders/urgent", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ so, item }),
      });
      if (!res.ok) { setStatus("Mark urgent failed: " + (await res.text())); return; }
      setStatus(`Marked ${so} (${item}) urgent.`);
      currentOrders = null; await runPlan();
    } catch (e) { setStatus("Mark urgent error: " + e.message); }
  };
}

// ---- Gantt ----
function renderGantt() {
  const root = mountFor("gantt");
  if (!currentGantt) { root.innerHTML = '<p class="placeholder">No schedule yet — add orders on the Orders view to build the Gantt.</p>'; return; }
  const g = currentGantt;
  if (!g.rows || !g.rows.length) {
    root.innerHTML = '<p class="placeholder">No schedule to chart — add orders on the Orders view.</p>';
    return;
  }
  const DAYW = ganttDayWidth, axisW = g.num_days * DAYW;
  const monthCells = g.months.map((m) => `<div class="g-month" style="width:${m.days * DAYW}px">${escapeHtml(m.label)}</div>`).join("");
  const dayCells = g.days.map((d) => `<div class="g-day${d.working ? "" : " g-off"}" style="width:${DAYW}px">${d.day} ${escapeHtml(d.month.slice(0, 3))}</div>`).join("");
  // Day-level chart: one column per day, no hour ruler. Day gridlines only,
  // with non-working days (Thursdays / holidays) shaded behind the bars.
  const grid = `repeating-linear-gradient(to right, transparent 0, transparent ${DAYW - 1}px, var(--border) ${DAYW - 1}px, var(--border) ${DAYW}px)`;
  const offDays = g.days.map((d, i) => d.working ? "" : `<div class="g-offday" style="left:${i * DAYW}px;width:${DAYW}px"></div>`).join("");
  const dateOnly = (s) => String(s).split(" ")[0];   // "DD-MM-YYYY HH:MM" -> "DD-MM-YYYY"

  const rowsHtml = g.rows.map((r) => {
    const lanes = r.bars.map((b) => {
      const left = b.offset_days * DAYW, w = Math.max(b.duration_days * DAYW, 8);
      const d0 = dateOnly(b.start), d1 = dateOnly(b.end);
      const dates = (d1 === d0) ? d0 : `${d0} → ${d1}`;
      const op = b.operator ? ` · ${b.operator}` : "";
      const tip = `${b.process} · ${b.machine}${op} · ${d0}${d1 !== d0 ? " → " + d1 : ""} · qty ${b.qty}`;
      // Wide bars (multi-day, e.g. OS turnaround) show the dates INSIDE at their start
      // so they stay on-screen; narrow bars show the dates just after the bar.
      const wide = w >= 220;
      const label = wide ? `${b.process} · ${dates}` : b.process;
      const after = wide ? "" : `<div class="g-bar-dates" style="left:${left + w + 6}px">${escapeHtml(dates)}</div>`;
      return `<div class="g-lane">`
        + `<div class="g-bar" style="left:${left}px;width:${w}px;background:${b.color}" title="${escapeHtml(tip)}">${escapeHtml(label)}</div>`
        + after
        + `</div>`;
    }).join("");
    const st = r.status || "";
    // Flag orders whose expected completion falls after the SO delivery date (late).
    const toDate = (s) => { const p = String(s).split("-"); return p.length === 3 ? new Date(+p[2], +p[1] - 1, +p[0]) : null; };
    const dd = toDate(r.so_delivery_date), cc = toDate(r.completion);
    const late = dd && cc && cc > dd;
    return `<tr>
      <td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(r.item_code)}</td>
      <td>${escapeHtml(r.so_no)}</td><td>${r.so_qty}</td><td>${escapeHtml(r.so_delivery_date)}</td>
      <td class="${late ? "g-late" : ""}" title="${late ? "Finishes after the SO delivery date" : "On time"}">${escapeHtml(r.completion || "")}</td>
      <td>${st ? `<span class="status-pill status-${st.toLowerCase()}">${escapeHtml(st)}</span>` : ""}</td>
      <td class="g-timeline"><div class="g-lanes" style="width:${axisW}px;background-image:${grid}">${offDays}${lanes}</div></td>
    </tr>`;
  }).join("");

  const legend = Object.entries(g.machine_colors).map(([m, c]) => `<span class="g-leg"><span class="g-chip" style="background:${c}"></span>${escapeHtml(m)}</span>`).join("");
  root.innerHTML = `
    <div class="g-toolbar">Zoom (day width) <button id="g-zoom-out">−</button> <button id="g-zoom-in">+</button>
      <span class="muted">· one column per day; shaded = non-working day</span></div>
    <div class="g-scroll"><table class="g-table"><thead>
      <tr><th class="g-corner" colspan="7" rowspan="2">Dates →</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px">${monthCells}</div></th></tr>
      <tr><th class="g-axis"><div class="g-band" style="width:${axisW}px">${dayCells}</div></th></tr>
      <tr><th>Item name</th><th>Item Code</th><th>SO No</th><th>SO Qty</th><th>SO Del date</th><th>Expected completion</th><th>Status</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px"></div></th></tr>
    </thead><tbody>${rowsHtml}</tbody></table></div>
    <div class="g-legend"><strong>Machines (bar colour):</strong> ${legend}</div>
    <p class="g-note">Each process sits on its own line, coloured by machine, placed on the day(s) it runs, with its start → end date shown. Hover a bar for machine · operator · time · qty. Status = Pending/Running per order.</p>`;
  $("g-zoom-in").onclick = () => { ganttDayWidth = Math.min(ganttDayWidth + 40, 560); renderGantt(); };
  $("g-zoom-out").onclick = () => { ganttDayWidth = Math.max(ganttDayWidth - 40, 80); renderGantt(); };
}

// ---- Analytics ----
function renderAnalytics() {
  const root = mountFor("analytics");
  const a = currentTrace && currentTrace.analytics;
  if (!a || !a.machines || !a.machines.length) {
    root.innerHTML = '<p class="placeholder">No analytics yet — add orders on the Orders view to see utilization &amp; bottlenecks.</p>';
    return;
  }
  const h = a.headline || {};
  const pct = (v) => v == null ? "—" : v + "%";
  const cls = (s) => s === "bottleneck" ? "hot" : s === "under-used" ? "cold" : s === "process" ? "proc" : "ok";

  // one utilization row: status dot · name (+ muted sub) · track/fill bar · value
  const row = (label, sub, v, s) => {
    const c = cls(s);
    const w = Math.max(Math.min(v == null ? 0 : v, 100), v ? 2 : 0);
    return `<div class="au-row">
      <span class="au-dot au-${c}"></span>
      <div class="au-name" title="${escapeHtml(label)}">${escapeHtml(label)}${sub ? `<span>${escapeHtml(sub)}</span>` : ""}</div>
      <div class="au-track"><div class="au-fill au-${c}" style="width:${w}%"></div></div>
      <div class="au-val au-${c}">${pct(v)}</div>
    </div>`;
  };

  // collapsible raw-numbers table (declutters — bars are the primary view)
  const table = (rows) => {
    if (!rows.length) return "";
    const cols = Object.keys(rows[0]).filter((c) => c !== "Status");
    const th = cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
    const tr = rows.map((r) => "<tr>" + cols.map((c) =>
      `<td>${escapeHtml(String(r[c] == null ? "—" : r[c]))}</td>`).join("") + "</tr>").join("");
    return `<details class="a-more"><summary>Show numbers</summary>
      <div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div></details>`;
  };

  // --- KPI cards ---
  const b = h.bottleneck;
  const nMach = a.machines.length;
  const kpis = `<div class="a-kpis">
    <div class="a-kpi hot">
      <span class="a-kpi-lab">Bottleneck</span>
      <span class="a-kpi-val">${b ? escapeHtml(b.Machine) : "—"}</span>
      <span class="a-kpi-sub">${b ? pct(b["Utilization %"]) + " used · your tightest resource" : "no machines"}</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Avg machine use</span>
      <span class="a-kpi-val">${pct(h.avg_machine_util)}</span>
      <span class="a-kpi-sub">across ${nMach} machine${nMach === 1 ? "" : "s"}</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Scheduled work</span>
      <span class="a-kpi-val">${Math.round(h.total_busy_hrs || 0)}<span class="a-kpi-unit">hrs</span></span>
      <span class="a-kpi-sub">total machine time</span>
    </div>
    <div class="a-kpi">
      <span class="a-kpi-lab">Makespan</span>
      <span class="a-kpi-val">${h.makespan_days || "?"}<span class="a-kpi-unit">days</span></span>
      <span class="a-kpi-sub">${a.window ? escapeHtml(a.window.start + " → " + a.window.end) : ""}</span>
    </div>
  </div>`;

  const legend = `<span class="a-legend">
    <span class="au-dot au-hot"></span>bottleneck ≥85%
    <span class="au-dot au-ok"></span>healthy
    <span class="au-dot au-cold"></span>under-used ≤30%</span>`;

  const machineBars = a.machines.map((m) => row(m.Machine, m.Type, m["Utilization %"], m.Status)).join("");
  const groupBars = a.machine_groups.map((g) =>
    row(g.Type, g.Machines + (g.Machines === 1 ? " machine" : " machines"), g["Utilization %"], g.Status)).join("");
  const opBars = a.operators.length
    ? a.operators.map((o) => row(o.Operator, "", o["Utilization %"], o.Status)).join("")
    : '<p class="a-empty">Operator logic is off. Turn it on in <strong>Settings</strong> to see per-operator load.</p>';
  // Honest capacity gap: shift segments no qualified operator was free to man
  // (never billed to a person, so nobody can show over 100%).
  const unstaffedNote = (h.unstaffed_hrs || 0) > 0
    ? `<p class="a-empty">⚠ <strong>${Math.round(h.unstaffed_hrs)} hrs</strong> of scheduled machine time fall in shifts where every qualified operator is already busy on another machine — that work needs more crew on those shifts.</p>`
    : "";
  const procBars = a.processes.map((p) => row(p.Process, p.Machines, p["Share %"], "process")).join("");

  root.innerHTML = `
    <div class="a-head">
      <span class="a-window">${a.window ? "Current plan · " + escapeHtml(a.window.start + " → " + a.window.end) : ""}</span>
    </div>
    ${kpis}
    <div class="a-grid">
      <section class="a-card">
        <div class="a-card-head"><h3>Machine utilization</h3>${legend}</div>
        <div class="au-list">${machineBars}</div>
        <div class="a-subhead">By machine type</div>
        <div class="au-list">${groupBars}</div>
        ${table(a.machines)}
      </section>
      <section class="a-card">
        <div class="a-card-head"><h3>Operator load</h3></div>
        <div class="au-list">${opBars}</div>
        ${unstaffedNote}
        ${a.operators.length ? table(a.operators) : ""}
      </section>
      <section class="a-card a-span">
        <div class="a-card-head"><h3>Where the work goes</h3><span class="muted">share of total machine-hours</span></div>
        <div class="au-list au-cols">${procBars}</div>
        ${table(a.processes)}
      </section>
    </div>
    <p class="a-foot"><strong>How to read this.</strong> Utilization = busy time ÷ that resource's <em>own</em> available time in the plan window (Thursdays &amp; holidays excluded), so a single-shift manual station and a two-shift CNC are judged fairly — 60% means the same for both. A <span class="au-t hot">bottleneck</span> is your constraint to relieve; <span class="au-t cold">under-used</span> resources have slack to take on more. Process share = each operation's cut of total machine-hours. Operator capacity assumes the standard shift length.</p>`;
}

// ---- Rule 8 daily entry form ----
async function ensureItems() {
  if (ITEMS) return ITEMS;
  try { ITEMS = await (await fetch("/items")).json(); } catch (e) { ITEMS = { items: {} }; }
  return ITEMS;
}
function fy(label, inner) { return `<label class="efield fy">${label}${inner}</label>`; }
function fr(label, inner) { return `<label class="efield fr">${label}${inner}</label>`; }

function actualsFormHtml() {
  const today = new Date().toISOString().slice(0, 10);
  const left =
    fy("Date", `<input id="a-date" type="date" value="${today}" /> <span id="a-date-echo" class="date-echo"></span>`) +
    fy("Shift", `<select id="a-shift"><option value="1st shift">1st shift</option><option value="2nd shift">2nd shift</option></select>`) +
    fr("Operator <span class=auto>(required)</span>", `<select id="a-operator"><option value="">— select operator —</option></select>`) +
    fr("SO No <span class=auto>(step 1 — pick from orders)</span>", `<select id="a-so"><option value="">— select SO No —</option></select>`) +
    fr("Item Code <span class=auto>(step 2 — pick this SO's item)</span>", `<select id="a-item"><option value="">— select SO No first —</option></select>`) +
    fr("Item Name <span class=auto>(auto)</span>", `<input id="a-itemname" readonly />`) +
    fr("Process <span class=auto>(dropdown)</span>", `<select id="a-process"></select>`);
  const right =
    fy("Qty Produced", `<input id="a-prod" type="number" min="0" value="" />`) +
    fy("Qty Rejected", `<input id="a-rej" type="number" min="0" value="" />`) +
    fy("Actual Setting Time (min)", `<input id="a-setup" type="number" min="0" value="" />`) +
    fy("No Power (min)", `<input id="a-nopower" type="number" min="0" value="" />`) +
    fy("No Operator (min)", `<input id="a-noop" type="number" min="0" value="" />`) +
    fy("Tool Problem (min)", `<input id="a-tool" type="number" min="0" value="" />`) +
    fy("Machine Breakdown (min)", `<input id="a-mbd" type="number" min="0" value="" />`) +
    fy("No Load (min)", `<input id="a-noload" type="number" min="0" value="" />`) +
    fy("Other Work (min)", `<input id="a-other" type="number" min="0" value="" />`) +
    fy("Remarks", `<textarea id="a-remarks" rows="2"></textarea>`);
  return `
    <div class="entry-legend"><span class="lg lg-y">Manual entry</span><span class="lg lg-r">Auto-prompt / dropdown</span></div>
    <div class="entry-grid"><div class="entry-col">${left}</div><div class="entry-col">${right}</div></div>
    <label class="complete-check"><input id="a-complete" type="checkbox" /> <strong>Mark this order (this SO No + item) complete</strong> — archives just this item line; the engine never auto-completes.</label>
    <button id="a-save" class="primary">Save daily entry</button>
    <div class="optimize-done-row">
      <button id="optimize-done" class="primary">Done entering — update plan</button>
      <span id="optimize-done-status" class="status"></span>
    </div>`;
}

// Populate the Capture Actuals Operator dropdown from the app-owned operator
// table (GET /operators — role-open, same list either role sees on Settings).
async function fillOperatorDropdown() {
  const sel = $("a-operator");
  if (!sel) return;
  try {
    const res = await fetch("/operators");
    if (!res.ok) return;
    const data = await res.json();
    const rows = data.operators || [];
    const keep = sel.value;
    sel.innerHTML = `<option value="">— select operator —</option>`
      + rows.map((o) => `<option value="${escapeHtml(o.name)}">${escapeHtml(o.name)}</option>`).join("");
    if (keep) sel.value = keep;
  } catch (e) { /* dropdown stays empty; required-field check + server 400 are the backstop */ }
}

// Populate the Shift dropdown with exactly the values /items advertises (falls
// back to the current two-shift vocabulary if the fetch hasn't landed yet), so
// the operator can no longer free-type shift text the report can't recognize.
function fillShiftDropdown() {
  const sel = $("a-shift");
  if (!sel) return;
  const list = (ITEMS && ITEMS.shifts && ITEMS.shifts.length) ? ITEMS.shifts : ["1st shift", "2nd shift"];
  const keep = sel.value;
  sel.innerHTML = list.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
  sel.value = list.includes(keep) ? keep : list[0];
}

// Populate the SO No dropdown with every SO from the orders tab.
function fillSoDropdown() {
  const sel = $("a-so");
  if (!sel) return;
  // Only ACTIVE (open) orders — you don't punch production for a completed/archived
  // order (and a stray punch on one would wrongly advance the plan clock).
  const list = (ITEMS && ITEMS.open_so_nos) ? ITEMS.open_so_nos
             : (ITEMS && ITEMS.so_nos) ? ITEMS.so_nos : [];
  sel.innerHTML = `<option value="">— select SO No —</option>`
    + list.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
}

// Step 2 of the picker: selecting an SO No fills the Item Code dropdown with THAT
// SO's open item lines (an SO number can carry several items, so the operator picks
// which one). A lone item auto-selects; then item name + process dropdown follow.
function fillItemFromSO() {
  const so = $("a-so").value.trim();
  const map = ITEMS && ITEMS.so_to_items ? ITEMS.so_to_items : {};
  const lines = map[so] || [];
  const sel = $("a-item");
  if (!lines.length) {
    sel.innerHTML = `<option value="">— select SO No first —</option>`;
  } else {
    const head = lines.length > 1 ? `<option value="">— select item —</option>` : "";
    sel.innerHTML = head + lines.map((l) =>
      `<option value="${escapeHtml(l.item_code)}">${escapeHtml(l.item_code)}${l.item_name ? " — " + escapeHtml(l.item_name) : ""}</option>`
    ).join("");
    if (lines.length === 1) sel.value = lines[0].item_code;   // only one → pick it
  }
  fillItemMeta();
}

function fillItemMeta() {
  const code = $("a-item").value.trim();
  if (!code) {
    $("a-itemname").value = "";
    $("a-process").innerHTML = `<option value="">—</option>`;
    return;
  }
  const meta = ITEMS && ITEMS.items ? ITEMS.items[code] : null;
  if (meta) {
    $("a-itemname").value = meta.item_name || "";
    $("a-process").innerHTML = meta.processes.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
  } else {
    $("a-itemname").value = "(unknown item code)";
    $("a-process").innerHTML = `<option value="">—</option>`;
  }
}

// True if an identical entry (same date / SO / item / process / produced / rejected)
// is already on record — used to warn before saving an accidental duplicate.
function isoToDdmmyyyy(iso) {            // "2025-08-01" -> "01-08-2025" (display format)
  const p = String(iso).split("-");
  return p.length === 3 ? `${p[2]}-${p[1]}-${p[0]}` : String(iso);
}
function actualIsDuplicate(body) {
  const t = currentTrace && currentTrace.rule7 && currentTrace.rule7.output;
  if (!t || !t.rows || !t.rows.length) return false;
  const ix = (n) => t.columns.indexOf(n);
  const [di, si, ii, pi, qp, qr] =
    ["Date", "SO No", "Item Code", "Process", "Qty Produced", "Qty Rejected"].map(ix);
  const dateCell = isoToDdmmyyyy(body.entry_date);   // stored Date column is DD-MM-YYYY
  return t.rows.some((r) =>
    String(r[di]) === dateCell && String(r[si]) === String(body.so_no) &&
    String(r[ii]) === String(body.item_code) && String(r[pi]) === String(body.process) &&
    Number(r[qp]) === Number(body.qty_produced) && Number(r[qr]) === Number(body.qty_rejected));
}

async function wireActualsForm() {
  ITEMS = null;                 // refetch so the SO dropdown reflects the latest orders
  await ensureItems();
  fillShiftDropdown();
  fillSoDropdown();
  await fillOperatorDropdown();
  $("a-so").addEventListener("change", fillItemFromSO);   // step 1: pick SO No -> fill Item dropdown
  $("a-item").addEventListener("change", fillItemMeta);   // step 2: pick Item -> name + processes
  fillItemMeta();
  // The native date picker shows the browser locale (MM/DD/YYYY on US machines);
  // echo the chosen date in DD-MM-YYYY so it always matches the rest of the app.
  const dateEcho = () => {
    const e = $("a-date-echo"), v = $("a-date") && $("a-date").value;
    if (e) e.textContent = v ? "= " + isoToDdmmyyyy(v) : "";
  };
  dateEcho();
  $("a-date").addEventListener("change", dateEcho);
  $("a-date").addEventListener("input", dateEcho);
  $("a-save").onclick = async () => {
    const btn = $("a-save");
    const num = (id) => Number($(id).value) || 0;
    const body = {
      entry_date: $("a-date").value, shift: $("a-shift").value,
      so_no: $("a-so").value.trim(), item_code: $("a-item").value.trim(),
      item_name: $("a-itemname").value, process: $("a-process").value,
      operator: $("a-operator").value.trim(),
      qty_produced: num("a-prod"), qty_rejected: num("a-rej"),
      actual_setup_min: num("a-setup"), no_power_min: num("a-nopower"),
      no_operator_min: num("a-noop"), tool_problem_min: num("a-tool"),
      machine_breakdown_min: num("a-mbd"), no_load_min: num("a-noload"),
      other_work_min: num("a-other"), remarks: $("a-remarks").value,
      mark_complete: $("a-complete").checked,
    };
    if (!body.operator || !body.so_no || !body.item_code) {
      setStatus("⚠ Select Operator, SO No, and Item Code before saving.");
      $(!body.operator ? "a-operator" : body.so_no ? "a-item" : "a-so").focus();
      return;
    }
    // Guard against re-saving the exact same entry (the #1 cause of duplicates).
    if (actualIsDuplicate(body) && !confirm(
        `An identical entry is already saved (SO ${body.so_no}, ${body.process || "—"}, `
        + `${body.qty_produced} produced on ${body.entry_date}). Save another?`)) {
      return;
    }
    // Immediate feedback so you know the click registered.
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Saving…";
    setStatus("Saving…");
    try {
      const res = await fetch("/actuals", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) {
        setStatus("Save failed: " + (await res.text()));
        btn.disabled = false; btn.textContent = label; return;
      }
      const d = await res.json();
      // Close the feedback loop on the punch: immediately re-plan so the schedule,
      // machine allotment, Gantt and Orders all reflect what the floor just reported
      // (per-process quantity produced/rejected). This is what makes the plan dynamic.
      setStatus("✓ Saved — re-planning from the new actuals…");
      await runPlan(false);                    // refreshes currentTrace/gantt/orders/report
      if (d.completed_order) {
        // The order was archived — show the result on the Orders view.
        setStatus(`✓ Saved & re-planned. Order ${body.so_no} marked complete and archived.`);
        showView("orders", true);
      } else {
        setStatus(`✓ Saved & re-planned — schedule, Gantt and Orders updated from the new actuals.`);
        renderTab("rule7");   // fresh blank form + updated output (stays on Daily Entry)
      }
    } catch (e) {
      setStatus("Save error: " + e.message); btn.disabled = false; btn.textContent = label;
    }
  };
  // Clerk's "Done entering" — refreshes the plan from the day's punches right
  // away; the sequence re-optimization itself now runs on a schedule (GitHub
  // Actions, Mon & Fri) rather than on this click. Available to both roles.
  const doneBtn = $("optimize-done");
  if (doneBtn) {
    doneBtn.onclick = async () => {
      const st = $("optimize-done-status");
      st.textContent = "Updating plan…";
      await runPlan(false);
      st.textContent = `Entries saved — plan updated. Next optimization: ${nextScheduledOptimize()}.`;
    };
  }
}

// Capture-actuals table with a per-row Rollback button (uses the parallel ids).
function actualsOutputHtml(table, ids) {
  if (!table || !table.columns || !table.columns.length) return '<div class="empty">— no entries —</div>';
  // Rollback is the FIRST column so it's always visible (the table is wide).
  let h = '<div class="table-wrap"><table><thead><tr><th>Rollback</th>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row, i) => {
    const id = ids && ids[i] ? ids[i] : "";
    h += "<tr>";
    h += id
      ? `<td><button class="rollback-btn danger" data-id="${escapeHtml(id)}" title="Delete this entry and return the order to normal">Rollback</button></td>`
      : "<td></td>";
    row.forEach((cell) => (h += `<td>${cell === null || cell === undefined ? "" : escapeHtml(String(cell))}</td>`));
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

// Roll back one saved actual: delete it and return that order to normal.
function wireRollback() {
  document.querySelectorAll(".rollback-btn").forEach((b) => {
    b.onclick = async () => {
      const id = b.getAttribute("data-id");
      if (!id) return;
      if (!confirm("Roll back this entry? It will be permanently deleted and the order returns to normal (if it was marked complete, it reopens).")) return;
      b.disabled = true; setStatus("Rolling back…");
      try {
        const res = await fetch("/actuals/rollback", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
        });
        if (!res.ok) { setStatus("Rollback failed: " + (await res.text())); b.disabled = false; return; }
        const d = await res.json();
        if (currentTrace && currentTrace.rule7) {
          currentTrace.rule7.output = d.actuals;
          currentTrace.rule7.actuals_ids = d.actuals_ids;
          currentTrace.rule7.tables = [{
            title: "Per item code — output & downtime rollup (minutes summed across entries)",
            table: d.by_item,
          }];
        }
        if (d.orders) currentOrders = d.orders;
        setStatus("✓ Entry rolled back." + (d.uncompleted_order ? " Order reopened (it was marked complete)." : "") + " Click Plan to refresh the schedule.");
        renderTab("rule7");
      } catch (e) { setStatus("Rollback error: " + e.message); b.disabled = false; }
    };
  });
}

function tableHtml(table, withClasses) {
  if (!table || !table.columns || table.columns.length === 0) return '<div class="empty">— no rows —</div>';
  let h = '<div class="table-wrap"><table><thead><tr>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    h += "<tr>";
    row.forEach((cell) => {
      let cls = "";
      if (withClasses && String(cell).includes("PENDING")) cls = ' class="pill-pending"';
      h += `<td${cls}>${cell === null || cell === undefined ? "" : escapeHtml(String(cell))}</td>`;
    });
    h += "</tr>";
  });
  return h + "</tbody></table></div>";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- Operator efficiency (Settings-area block; admin-only — pure reporting,
// never touches the plan). GET /efficiency for the on-screen preview,
// GET /efficiency.csv for the download; both admin-only server-side. ----
function effDefaultMonth() {
  // Previous calendar month, "YYYY-MM" (the value a <input type="month"> wants).
  const d = new Date();
  d.setDate(1);          // avoid rolling into the wrong month on short months
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function effYearMonth() {
  const el = $("eff-month");
  const v = el && el.value;
  if (!v) return null;
  const [y, m] = v.split("-").map(Number);
  return { year: y, month: m };
}

async function previewEfficiency() {
  const ym = effYearMonth();
  const el = $("eff-table");
  if (!ym) { setStatus("Pick a month first."); return; }
  try {
    const res = await fetch(`/efficiency?year=${ym.year}&month=${ym.month}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { setStatus("Efficiency error: " + (body.detail || res.status)); return; }
    renderEfficiencyTable(body.rows, el);
  } catch (e) { setStatus("Efficiency error: " + e.message); }
}

function renderEfficiencyTable(rows, el) {
  if (!el) return;
  if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">— no rows —</div>'; return; }
  const columns = Object.keys(rows[0]);
  const tableRows = rows.map((r) => columns.map((c) => (r[c] === null || r[c] === undefined ? "—" : r[c])));
  el.innerHTML = tableHtml({ columns, rows: tableRows });
}

function downloadEfficiencyCsv() {
  const ym = effYearMonth();
  if (!ym) { setStatus("Pick a month first."); return; }
  window.location.href = `/efficiency.csv?year=${ym.year}&month=${ym.month}`;
}

// ---- Operator absences (Settings-area block; list is visible to both roles,
// add/remove controls are admin-only via CSS). GET /absences is role-open. ----
function renderAbsenceList(absences) {
  const ul = $("absence-list");
  if (!ul) return;
  if (!absences || absences.length === 0) {
    ul.innerHTML = '<li class="muted">No operators currently marked absent.</li>';
    return;
  }
  ul.innerHTML = absences.map((a) => `
    <li>
      <span>${escapeHtml(a.operator)} — ${isoToDdmmyyyy(a.from_date)} to ${isoToDdmmyyyy(a.to_date)}</span>
      <button type="button" class="ghost-btn absence-remove admin-only" data-id="${escapeHtml(a.id)}">✕</button>
    </li>`).join("");
  ul.querySelectorAll(".absence-remove").forEach((btn) => {
    btn.onclick = () => removeAbsence(btn.dataset.id);
  });
}

async function loadAbsences() {
  try {
    const res = await fetch("/absences");
    if (!res.ok) return;
    const data = await res.json();
    const sel = $("absence-operator");
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = (data.operators || []).map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
      if (prev && (data.operators || []).includes(prev)) sel.value = prev;
    }
    renderAbsenceList(data.absences || []);
  } catch (e) { /* the list is a convenience view — a fetch hiccup shouldn't block the page */ }
}

async function addAbsence() {
  const op = $("absence-operator"), from = $("absence-from"), to = $("absence-to");
  if (!op || !op.value) { setStatus("Pick an operator to mark absent."); return; }
  if (!from.value || !to.value) { setStatus("Pick both absence dates."); return; }
  try {
    const res = await fetch("/absences", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: op.value, from_date: from.value, to_date: to.value }),
    });
    if (!res.ok) { setStatus("Mark absent failed: " + (await res.text())); return; }
    setStatus(`Marked ${op.value} absent.`);
    from.value = ""; to.value = "";
    await loadAbsences();
    await runPlan(false);
  } catch (e) { setStatus("Mark absent error: " + e.message); }
}

async function removeAbsence(id) {
  try {
    const res = await fetch(`/absences/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { setStatus("Remove absence failed: " + (await res.text())); return; }
    setStatus("Absence removed.");
    await loadAbsences();
    await runPlan(false);
  } catch (e) { setStatus("Remove absence error: " + e.message); }
}

// ---- Operators & shifts (Settings-area block). The list is visible to both
// roles, but rows render two different markups per role (rather than dual-DOM
// + CSS-hiding as the absence row's single delete button does): admins get
// editable inputs/select/checkbox/remove; users get plain text — same
// per-role conditional-render approach already used for the order book table
// (`renderOrders`'s `isAdmin` branch). Simplest fit here since every cell but
// the name needs an admin-only edit affordance, not just one trailing button. ----
function renderOperatorsTable(operators) {
  const tbody = $("operators-tbody");
  if (!tbody) return;
  const isAdmin = currentRole === "admin";
  if (!operators || operators.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${isAdmin ? 5 : 4}" class="empty">No operators yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = operators.map((o) => {
    const id = escapeHtml(o.id);
    if (!isAdmin) {
      const shiftLabel = o.shift ? escapeHtml(o.shift) : "Day (9–18)";
      return `<tr>
        <td>${escapeHtml(o.name)}</td>
        <td>${escapeHtml(o.machines_raw || "")}</td>
        <td>${shiftLabel}</td>
        <td>${o.pinned ? "Yes" : "—"}</td>
      </tr>`;
    }
    const shiftOpts = ["", "First shift", "Second shift"].map((v) =>
      `<option value="${escapeHtml(v)}"${o.shift === v ? " selected" : ""}>${v === "" ? "Day (9–18)" : escapeHtml(v)}</option>`
    ).join("");
    return `<tr data-id="${id}">
      <td>${escapeHtml(o.name)}</td>
      <td><input type="text" class="op-machines" data-id="${id}" value="${escapeHtml(o.machines_raw || "")}" /></td>
      <td><select class="op-shift" data-id="${id}">${shiftOpts}</select></td>
      <td><input type="checkbox" class="op-pinned" data-id="${id}" ${o.pinned ? "checked" : ""} /></td>
      <td class="admin-only"><button type="button" class="ghost-btn op-remove" data-id="${id}">✕</button></td>
    </tr>`;
  }).join("");
  if (isAdmin) {
    tbody.querySelectorAll(".op-machines").forEach((inp) => {
      inp.addEventListener("change", () => patchOperator(inp.dataset.id, { machines_raw: inp.value }));
    });
    tbody.querySelectorAll(".op-shift").forEach((sel) => {
      sel.addEventListener("change", () => patchOperator(sel.dataset.id, { shift: sel.value }));
    });
    tbody.querySelectorAll(".op-pinned").forEach((chk) => {
      chk.addEventListener("change", () => patchOperator(chk.dataset.id, { pinned: chk.checked }));
    });
    tbody.querySelectorAll(".op-remove").forEach((btn) => {
      btn.onclick = () => removeOperator(btn.dataset.id);
    });
  }
}

async function loadOperators() {
  try {
    const res = await fetch("/operators");
    if (!res.ok) return;
    const data = await res.json();
    nextRotation = data.next_rotation || null;   // status-strip "Next rotation" segment
    const header = $("operators-header");
    if (header) {
      header.textContent = data.next_rotation
        ? `Shifts rotate every Friday (effective from first shift). Next rotation: ${isoToDdmmyyyy(data.next_rotation)}.`
        : "Shifts rotate every Friday (effective from first shift).";
    }
    renderOperatorsTable(data.operators || []);
    renderStatusStrip();
  } catch (e) { /* the panel is a convenience view — a fetch hiccup shouldn't block the page */ }
}

async function patchOperator(id, body) {
  try {
    const res = await fetch(`/operators/${encodeURIComponent(id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) { setStatus("Update operator failed: " + (await res.text())); await loadOperators(); return; }
    setStatus("Operator updated.");
    await loadOperators();
    await runPlan(false);
  } catch (e) { setStatus("Update operator error: " + e.message); }
}

async function addOperator() {
  const name = $("op-add-name"), machines = $("op-add-machines"), shift = $("op-add-shift");
  if (!name || !name.value.trim()) { setStatus("Enter an operator name."); return; }
  try {
    const res = await fetch("/operators", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.value.trim(),
        machines_raw: machines ? machines.value : "",
        shift: shift ? shift.value : "",
      }),
    });
    if (!res.ok) { setStatus("Add operator failed: " + (await res.text())); return; }
    setStatus(`Added operator ${name.value.trim()}.`);
    name.value = ""; if (machines) machines.value = ""; if (shift) shift.value = "";
    await loadOperators();
    await runPlan(false);
  } catch (e) { setStatus("Add operator error: " + e.message); }
}

async function removeOperator(id) {
  if (!confirm("Remove this operator? Any absences recorded against them are kept but will show as unknown.")) return;
  try {
    const res = await fetch(`/operators/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { setStatus("Remove operator failed: " + (await res.text())); return; }
    setStatus("Operator removed.");
    await loadOperators();
    await runPlan(false);
  } catch (e) { setStatus("Remove operator error: " + e.message); }
}

// Wire the admin controls (null-guarded — they're absent/hidden for the user role).
const _runBtn = $("run-btn");
if (_runBtn) _runBtn.onclick = () => runPlan(true);   // explicit admin Plan → persist
const _upBtn = $("upload-btn");
if (_upBtn) _upBtn.onclick = uploadExcel;
// Plan-start date: keep the DD-MM-YYYY echo in sync as the admin changes it.
// The "Start from today" checkbox disables the picker and echoes today (auto).
const _psField = $("cfg-plan-start");
if (_psField) { _psField.addEventListener("change", updatePlanStartEcho); }
const _autoField = $("cfg-start-auto");
if (_autoField) { _autoField.addEventListener("change", updatePlanStartEcho); }
updatePlanStartEcho();
// Settings & Optimize are now their own nav destinations (Settings view / Schedule
// view) — no disclosure toggles. Optimize Start/Stop wire directly.
const _optStart = $("optimize-start");
if (_optStart) _optStart.onclick = startOptimize;
const _optStop = $("optimize-stop");
if (_optStop) _optStop.onclick = stopOptimize;
// Operator absences: add is admin-only (button is CSS-hidden for the user role,
// but the handler is harmless to wire either way — the server enforces the role).
const _absAdd = $("absence-add");
if (_absAdd) _absAdd.onclick = addAbsence;
// Operators & shifts: add is admin-only (row wrapped in admin-only, so it's
// CSS-hidden for the user role too; the handler is harmless to wire either
// way — the server enforces the role).
const _opAdd = $("op-add-btn");
if (_opAdd) _opAdd.onclick = addOperator;
// Operator efficiency: month input defaults to last month; Preview/Download are
// admin-only (server enforces it; the fieldset is inside the admin-only Settings
// panel so the user role never sees it at all).
const _effMonthEl = $("eff-month");
if (_effMonthEl) _effMonthEl.value = effDefaultMonth();
const _effPreviewBtn = $("eff-preview-btn");
if (_effPreviewBtn) _effPreviewBtn.onclick = previewEfficiency;
const _effDownloadBtn = $("eff-download-btn");
if (_effDownloadBtn) _effDownloadBtn.onclick = downloadEfficiencyCsv;

// Boot: learn the role, restore the last view, then auto-load the current plan (no
// persist) so the schedule/Gantt/views populate without a Plan click.
(async function boot() {
  await initSession();
  window.addEventListener("hashchange", onHashChange);
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.addEventListener("click", (e) => { e.preventDefault(); showView(n.dataset.view, true); });
  });
  // Land on the hash's view, else the last view visited (defaults to Orders).
  const initial = VIEWS.includes(location.hash.replace(/^#/, "")) ? location.hash.replace(/^#/, "") : lastView();
  showView(initial, true);
  if (currentRole === "admin") {
    loadAbsences();   // Settings cards (admin-only): operator/absence masters
    loadOperators();
  }
  await runPlan(false);
  // A search may still be running (or have finished) from before a page reload.
  // Resume the progress display so a refresh mid-run never loses the work — the
  // Optimize card lives on the Schedule view and updates in place.
  if (currentRole === "admin") {
    try {
      const st = await (await fetch("/optimize/status")).json();
      if (st.state === "running" || (st.state === "done" && st.best)) pollOptimizeStatus();
    } catch (e) { /* status is cosmetic at boot */ }
  }
})();
