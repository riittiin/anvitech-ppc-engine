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

let currentTrace = null;
let currentGantt = null;
let currentOrders = null;     // {columns, rows} from /run or /orders
let ITEMS = null;
let ganttDayWidth = 200;   // px per day column (Gantt is day-level, no hour detail)
let activeTab = "orders";
let currentRole = "user";   // set from /me; default to the least-privileged role

const $ = (id) => document.getElementById(id);
const setStatus = (m) => { $("status").textContent = m; };
const setDatasetStatus = (m) => { $("dataset-status").innerHTML = m; };

function readConfig() {
  return {
    consolidation_window_days: Number($("cfg-window").value),
    setup_time_min: Number($("cfg-setup").value),
    overlap_mode: "overlap",   // sequential mode retired — always overlap
    overlap_percent: Number($("cfg-overlap-pct").value),
    priority_metric: $("cfg-priority-metric").value,
    priority_window_days: $("cfg-priority-window").value,
    apply_downtime_to_plan: $("cfg-apply-downtime").checked,
    apply_operator_logic: $("cfg-operator-logic").checked,
    split_parallel: $("cfg-split-parallel").checked,
  };
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

// Reflect the (server) effective plan config back into the admin's config bar so
// it shows the last-saved plan settings.
function applyConfig(cfg) {
  if (!cfg) return;
  const setVal = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  const setSel = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = v; };
  setVal("cfg-window", cfg.consolidation_window_days);
  setVal("cfg-setup", cfg.setup_time_min);
  setVal("cfg-overlap-pct", cfg.overlap_percent);
  setSel("cfg-priority-metric", cfg.priority_metric);
  const pw = $("cfg-priority-window");
  if (pw) pw.value = (cfg.priority_window_days === null || cfg.priority_window_days === undefined)
    ? "" : String(cfg.priority_window_days);
  const dt = $("cfg-apply-downtime");
  if (dt) dt.checked = !!cfg.apply_downtime_to_plan;
  const ol = $("cfg-operator-logic");
  if (ol) ol.checked = !!cfg.apply_operator_logic;
  const sp = $("cfg-split-parallel");
  if (sp) sp.checked = !!cfg.split_parallel;
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
    if (currentRole === "admin" && data.config) applyConfig(data.config);
    renderReport(data.report);
    renderTabs();
    renderTab(activeTab);
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
      const detail = d.flagged.map((f) => `${escapeHtml(f.so_no)} (${escapeHtml(f.reason)})`).join("; ");
      msg += ` · <span class="pill-pending">${d.flagged.length} flagged</span>: ${detail}`;
    }
    if (d.masters_updated) msg += " · masters updated";
    setDatasetStatus(msg);
    await runPlan();           // refresh the book + schedule
    activeTab = "orders"; renderTabs(); renderTab("orders");
  } catch (e) { setDatasetStatus("Upload error: " + e.message); }
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

// ---- Tabs ----
function renderTabs() {
  const nav = $("tabs"); nav.innerHTML = "";
  // Orders tab first — the order book is the home view.
  const ot = document.createElement("div");
  ot.className = "tab tab-orders" + (activeTab === "orders" ? " active" : "");
  ot.textContent = "📋 Orders";
  ot.onclick = () => { activeTab = "orders"; renderTabs(); renderTab("orders"); };
  nav.appendChild(ot);

  ORDER.forEach((key) => {
    const entry = (currentTrace && currentTrace[key]) || {};
    const meta = RULES[key];
    const el = document.createElement("div");
    el.className = "tab" + (key === activeTab ? " active" : "")
      + (entry.error ? " error" : "") + (entry.reached === false ? " not-reached" : "");
    el.textContent = `${meta.n}. ${meta.title}` + (entry.error ? " ⚠" : "");
    el.onclick = () => { activeTab = key; renderTabs(); renderTab(key); };
    nav.appendChild(el);
  });

  const g = document.createElement("div");
  g.className = "tab tab-gantt" + (activeTab === "gantt" ? " active" : "");
  g.textContent = "📊 Gantt";
  g.onclick = () => { activeTab = "gantt"; renderTabs(); renderTab("gantt"); };
  nav.appendChild(g);
}

function renderTab(key) {
  if (key === "orders") { renderOrders(); return; }
  if (key === "gantt") { renderGantt(); return; }
  const entry = currentTrace ? currentTrace[key] : null;
  const meta = RULES[key];
  const root = $("tab-content");
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
  }
}

// ---- CSV download (for printing schedules) ----
function todayStamp() {
  const d = new Date();
  return `${String(d.getDate()).padStart(2, "0")}-${String(d.getMonth() + 1).padStart(2, "0")}-${d.getFullYear()}`;
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
  const root = $("tab-content");
  if (!currentOrders) {
    try { currentOrders = (await (await fetch("/orders")).json()).orders; } catch (e) { currentOrders = null; }
  }
  const isAdmin = currentRole === "admin";
  let html = '<div class="rule-header"><h2>Order book</h2></div>';
  if (!currentOrders || !currentOrders.rows.length) {
    html += isAdmin
      ? '<p class="placeholder">No orders yet. Upload your Excel above to add them, then click <strong>Plan</strong>.</p>'
      : '<p class="placeholder">No orders yet.</p>';
    root.innerHTML = html; return;
  }
  // Delete controls are admin-only (server enforces this too).
  if (isAdmin) {
    html += '<div class="ord-toolbar">'
      + '<button id="ord-del-sel">🗑 Delete selected</button> '
      + '<button id="ord-del-all" class="danger">Delete ALL data</button>'
      + '<span class="muted"> · deletes permanently from the database (and their actuals)</span></div>';
  }
  html += orderTableHtml(currentOrders, isAdmin);
  html += '<p class="g-note">Pending = not started · Running = production logged · Complete = marked complete on a Rule 7 entry (archived). Plan schedules every active order by its <strong>remaining</strong> qty.</p>';
  root.innerHTML = html;
  if (isAdmin) wireOrdersDelete();
}

function orderTableHtml(table, showSelect) {
  const sIdx = table.columns.indexOf("Status");
  const soIdx = table.columns.indexOf("SO No");
  let h = '<div class="table-wrap"><table><thead><tr>';
  if (showSelect) h += '<th><input type="checkbox" id="ord-all-check" title="select all"></th>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    const so = soIdx >= 0 ? String(row[soIdx]) : "";
    h += "<tr>";
    if (showSelect) h += `<td><input type="checkbox" class="ordsel" value="${escapeHtml(so)}"></td>`;
    row.forEach((cell, i) => {
      const v = cell === null || cell === undefined ? "" : String(cell);
      if (i === sIdx) h += `<td><span class="status-pill status-${v.toLowerCase()}">${escapeHtml(v)}</span></td>`;
      else h += `<td>${escapeHtml(v)}</td>`;
    });
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
    const sel = [...document.querySelectorAll(".ordsel:checked")].map((c) => c.value);
    if (!sel.length) { setStatus("No rows selected to delete."); return; }
    const okd = await deleteWithPassword(
      `Permanently delete ${sel.length} order(s) and their production data?`,
      (pw) => fetch("/orders/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ so_nos: sel, password: pw }),
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

// ---- Gantt ----
function renderGantt() {
  const root = $("tab-content");
  if (!currentGantt) { root.innerHTML = '<p class="placeholder">Click <strong>Plan</strong> to build the Gantt.</p>'; return; }
  const g = currentGantt;
  if (!g.rows || !g.rows.length) {
    root.innerHTML = '<div class="rule-header"><h2>Production Planning — Gantt</h2></div>'
      + '<p class="placeholder">No schedule to chart. Upload orders and click Plan.</p>';
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
    const bars = r.bars.map((b) => {
      const left = b.offset_days * DAYW, w = Math.max(b.duration_days * DAYW, 8);
      const d0 = dateOnly(b.start), d1 = dateOnly(b.end);
      const op = b.operator ? ` · 👤 ${b.operator}` : "";
      const tip = `${b.process} · ${b.machine}${op} · ${d0}${d1 !== d0 ? " → " + d1 : ""} · qty ${b.qty}`;
      return `<div class="g-bar" style="left:${left}px;width:${w}px;background:${b.color}" title="${escapeHtml(tip)}">${escapeHtml(b.process)}</div>`;
    }).join("");
    const st = r.status || "";
    return `<tr>
      <td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(r.item_code)}</td>
      <td>${escapeHtml(r.so_no)}</td><td>${r.so_qty}</td><td>${escapeHtml(r.so_delivery_date)}</td>
      <td>${st ? `<span class="status-pill status-${st.toLowerCase()}">${escapeHtml(st)}</span>` : ""}</td>
      <td class="g-timeline"><div class="g-track" style="width:${axisW}px;background-image:${grid}">${offDays}${bars}</div></td>
    </tr>`;
  }).join("");

  const legend = Object.entries(g.machine_colors).map(([m, c]) => `<span class="g-leg"><span class="g-chip" style="background:${c}"></span>${escapeHtml(m)}</span>`).join("");
  root.innerHTML = `
    <div class="rule-header"><h2>Production Planning — Gantt</h2></div>
    <div class="g-toolbar">Zoom (day width) <button id="g-zoom-out">−</button> <button id="g-zoom-in">+</button>
      <span class="muted">· one column per day; shaded = non-working day</span></div>
    <div class="g-scroll"><table class="g-table"><thead>
      <tr><th class="g-corner" colspan="6" rowspan="2">Dates →</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px">${monthCells}</div></th></tr>
      <tr><th class="g-axis"><div class="g-band" style="width:${axisW}px">${dayCells}</div></th></tr>
      <tr><th>Item name</th><th>Item Code</th><th>SO No</th><th>SO Qty</th><th>SO Del date</th><th>Status</th>
          <th class="g-axis"><div class="g-band" style="width:${axisW}px"></div></th></tr>
    </thead><tbody>${rowsHtml}</tbody></table></div>
    <div class="g-legend"><strong>Machines (bar colour):</strong> ${legend}</div>
    <p class="g-note">Each bar = one process, coloured by machine, placed on the day(s) it runs. Hover a bar for machine · operator · time · qty. Status = Pending/Running per order.</p>`;
  $("g-zoom-in").onclick = () => { ganttDayWidth = Math.min(ganttDayWidth + 40, 560); renderGantt(); };
  $("g-zoom-out").onclick = () => { ganttDayWidth = Math.max(ganttDayWidth - 40, 80); renderGantt(); };
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
    fy("Date", `<input id="a-date" type="date" value="${today}" />`) +
    fy("Shift", `<input id="a-shift" value="1st shift" />`) +
    fr("SO No <span class=auto>(pick from orders)</span>", `<select id="a-so"><option value="">— select SO No —</option></select>`) +
    fr("Item Code <span class=auto>(auto from SO No)</span>", `<input id="a-item" value="" placeholder="auto-fills from SO No" readonly />`) +
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
    <label class="complete-check"><input id="a-complete" type="checkbox" /> <strong>Mark this order (SO No) complete</strong> — archives it; the engine never auto-completes.</label>
    <button id="a-save" class="primary">Save daily entry</button>`;
}

// Populate the SO No dropdown with every SO from the orders tab.
function fillSoDropdown() {
  const sel = $("a-so");
  if (!sel) return;
  const list = (ITEMS && ITEMS.so_nos) ? ITEMS.so_nos : [];
  sel.innerHTML = `<option value="">— select SO No —</option>`
    + list.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join("");
}

// Selecting an SO No auto-fills its Item Code (from the order book), then the
// item name + process dropdown follow.
function fillItemFromSO() {
  const so = $("a-so").value.trim();
  const map = ITEMS && ITEMS.so_to_item ? ITEMS.so_to_item : {};
  $("a-item").value = map[so] || "";
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
  fillSoDropdown();
  $("a-so").addEventListener("change", fillItemFromSO);  // pick SO No -> Item Code (auto)
  fillItemMeta();
  $("a-save").onclick = async () => {
    const btn = $("a-save");
    const num = (id) => Number($(id).value) || 0;
    const body = {
      entry_date: $("a-date").value, shift: $("a-shift").value,
      so_no: $("a-so").value.trim(), item_code: $("a-item").value.trim(),
      item_name: $("a-itemname").value, process: $("a-process").value,
      qty_produced: num("a-prod"), qty_rejected: num("a-rej"),
      actual_setup_min: num("a-setup"), no_power_min: num("a-nopower"),
      no_operator_min: num("a-noop"), tool_problem_min: num("a-tool"),
      machine_breakdown_min: num("a-mbd"), no_load_min: num("a-noload"),
      other_work_min: num("a-other"), remarks: $("a-remarks").value,
      mark_complete: $("a-complete").checked,
    };
    if (!body.so_no || !body.item_code) {
      setStatus("⚠ Enter SO No and Item Code before saving.");
      $(body.so_no ? "a-item" : "a-so").focus();
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
      // (per-process remaining + downtime). This is what makes the plan dynamic.
      setStatus("✓ Saved — re-planning from the new actuals…");
      await runPlan(false);                    // refreshes currentTrace/gantt/orders/report
      if (d.completed_order) {
        // The order was archived — show the result on the Orders tab.
        setStatus(`✓ Saved & re-planned. Order ${body.so_no} marked complete and archived.`);
        activeTab = "orders"; renderTabs(); renderTab("orders");
      } else {
        setStatus(`✓ Saved & re-planned — schedule, Gantt and Orders updated from the new actuals.`);
        activeTab = "rule7"; renderTabs(); renderTab("rule7");   // fresh blank form + updated output
      }
    } catch (e) {
      setStatus("Save error: " + e.message); btn.disabled = false; btn.textContent = label;
    }
  };
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
      ? `<td><button class="rollback-btn danger" data-id="${escapeHtml(id)}" title="Delete this entry and return the order to normal">↺ Rollback</button></td>`
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
        setStatus("✓ Entry rolled back." + (d.uncompleted_order ? " Order reopened (it was marked complete)." : "") + " Click ▶ Plan to refresh the schedule.");
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

// Wire the admin controls (null-guarded — they're absent/hidden for the user role).
const _runBtn = $("run-btn");
if (_runBtn) _runBtn.onclick = () => runPlan(true);   // explicit admin Plan → persist
const _upBtn = $("upload-btn");
if (_upBtn) _upBtn.onclick = uploadExcel;

// Boot: learn the role, render the shell, then auto-load the current plan (no
// persist) so the schedule/Gantt/rule tabs populate without a Plan click.
(async function boot() {
  await initSession();
  renderTabs();
  renderTab(activeTab);
  await runPlan(false);
})();
