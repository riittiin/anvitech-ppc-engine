"use strict";

// Rule metadata for the tab headers (titles + one-line descriptions).
const RULES = {
  rule1: { n: 1, title: "Consolidate", desc: "Group same-item SO lines whose delivery dates fall within the window into one batch." },
  rule2: { n: 2, title: "Sort by delivery date", desc: "Order batches by earliest delivery date (primary priority)." },
  rule3: { n: 3, title: "Smart priority (slack)", desc: "Workload-aware: least slack (time-to-due − work-needed) first; reduces to 'more process time' on equal dates." },
  rule4: { n: 4, title: "Setup time", desc: "Helper: occupancy = cycle × qty + setup (90 min). Consumed inside Rule 6." },
  rule5: { n: 5, title: "Overlap mode", desc: "Helper: when the next process may start — sequential vs 50% overlap." },
  rule6: { n: 6, title: "Allocate to machines", desc: "Place each process on its earliest-available preferred machine; respect calendar + shifts." },
  rule7: { n: 7, title: "Parallel machine", desc: "Helper: batch > trigger → a separate preferred machine for the next CNC setup." },
  rule8: { n: 8, title: "Capture actuals", desc: "Record daily production → data/actuals.json (the only writable data)." },
  rule9: { n: 9, title: "Rerun MRP", desc: "Re-plan from balance (SO qty − produced) by re-calling Rules 1–7." },
};
const ORDER = ["rule1","rule2","rule3","rule4","rule5","rule6","rule7","rule8","rule9"];

let currentTrace = null;
let currentGantt = null;
let currentDataset = null;  // uploaded workbook id; null = bundled test file
let activeTab = "rule1";
let ITEMS = null;  // item metadata for the Rule 8 form (name auto-prompt + process dropdown)
let ganttHourWidth = 20;  // px per HOUR column (zoomable); day = 24 × this

function dsQuery() { return currentDataset ? `?dataset_id=${currentDataset}` : ""; }

async function ensureItems() {
  if (ITEMS) return ITEMS;
  try { ITEMS = await (await fetch("/items" + dsQuery())).json(); } catch (e) { ITEMS = { items: {} }; }
  return ITEMS;
}

const $ = (id) => document.getElementById(id);

function readConfig() {
  return {
    consolidation_window_days: Number($("cfg-window").value),
    setup_time_min: Number($("cfg-setup").value),
    overlap_mode: $("cfg-overlap").value,
    overlap_percent: Number($("cfg-overlap-pct").value),
    parallel_trigger_qty: Number($("cfg-parallel").value),
    priority_metric: $("cfg-priority-metric").value,
    priority_window_days: $("cfg-priority-window").value,  // "" => no limit
  };
}

function setStatus(msg) { $("status").textContent = msg; }
function setDatasetStatus(msg) { $("dataset-status").textContent = msg; }

async function uploadExcel() {
  const f = $("xlsx-file").files[0];
  if (!f) { setDatasetStatus("Choose an .xlsx file first."); return; }
  const fd = new FormData();
  fd.append("file", f);
  setDatasetStatus("Uploading & parsing…");
  try {
    const res = await fetch("/upload", { method: "POST", body: fd });
    if (!res.ok) { setDatasetStatus("Upload failed: " + (await res.text())); return; }
    const d = await res.json();
    currentDataset = d.dataset_id;
    ITEMS = null;  // refetch item metadata for the new dataset
    setDatasetStatus(`Data: ${d.name} — ${d.summary.items} items, ${d.summary.so_lines} SO lines, ${d.summary.machines} machines. Click Run plan.`);
  } catch (e) { setDatasetStatus("Upload error: " + e.message); }
}

async function runPlan() {
  setStatus("Running…");
  try {
    const res = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: readConfig(), dataset_id: currentDataset }),
    });
    if (!res.ok) { setStatus("Error: " + (await res.text())); return; }
    const data = await res.json();
    currentTrace = data.trace;
    currentGantt = data.gantt || null;
    renderReport(data.report);
    renderTabs();
    renderTab(activeTab);
    setStatus("Run " + data.run_id + " complete.");
  } catch (e) { setStatus("Request failed: " + e.message); }
}

async function rerunMRP() {
  setStatus("Re-planning from actuals…");
  try {
    const res = await fetch("/rerun", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: readConfig(), dataset_id: currentDataset }),
    });
    if (!res.ok) { setStatus("Error: " + (await res.text())); return; }
    const data = await res.json();
    currentTrace = data.trace;
    currentGantt = data.gantt || null;
    renderReport(data.report);
    renderTabs();
    activeTab = "rule9";
    renderTab(activeTab);
    setStatus("Rerun " + data.run_id + " complete.");
  } catch (e) { setStatus("Request failed: " + e.message); }
}

// Friendly one-word labels for the loader report kinds.
const REPORT_LABELS = {
  PENDING_MASTER_DATA: "provisional machines",
  NO_ROUTING: "orders without routing",
  TIME_COERCION: "time coercions",
  BAD_DELIVERY_DATE: "bad delivery dates",
  MISSING_SHEET: "missing sheets",
};

function renderReport(report) {
  const panel = $("report-panel");
  const toggle = $("report-toggle");
  const detail = $("report-detail");
  if (!report || !report.rows || report.rows.length === 0) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");

  // Summarize by kind (column 0). Collapsed by default; expand on click.
  const counts = {};
  report.rows.forEach((r) => { counts[r[0]] = (counts[r[0]] || 0) + 1; });
  const parts = Object.entries(counts)
    .map(([k, v]) => `${v} ${REPORT_LABELS[k] || k}`)
    .join(", ");
  toggle.innerHTML = `⚠ ${parts} (data gaps) — click to view <span class="chev">▸</span>`;
  detail.innerHTML = tableHtml(report, true);
  detail.classList.add("hidden");
  toggle.classList.remove("open");
  toggle.onclick = () => {
    const open = !detail.classList.toggle("hidden");
    toggle.classList.toggle("open", open);
  };
}

function renderTabs() {
  const nav = $("tabs");
  nav.innerHTML = "";
  ORDER.forEach((key) => {
    const entry = currentTrace[key] || {};
    const meta = RULES[key];
    const el = document.createElement("div");
    el.className = "tab" + (key === activeTab ? " active" : "")
      + (entry.error ? " error" : "")
      + (entry.reached === false ? " not-reached" : "");
    el.textContent = `${meta.n}. ${meta.title}` + (entry.error ? " ⚠" : "");
    el.onclick = () => { activeTab = key; renderTabs(); renderTab(key); };
    nav.appendChild(el);
  });
  // Separate, non-numbered tab for the worker-facing Gantt view.
  const g = document.createElement("div");
  g.className = "tab tab-gantt" + (activeTab === "gantt" ? " active" : "");
  g.textContent = "📊 Gantt";
  g.onclick = () => { activeTab = "gantt"; renderTabs(); renderTab("gantt"); };
  nav.appendChild(g);
}

function tableHtml(table, withClasses) {
  if (!table || !table.columns || table.columns.length === 0)
    return '<div class="empty">— no rows —</div>';
  let h = '<div class="table-wrap"><table><thead><tr>';
  table.columns.forEach((c) => (h += `<th>${escapeHtml(c)}</th>`));
  h += "</tr></thead><tbody>";
  table.rows.forEach((row) => {
    h += "<tr>";
    row.forEach((cell, i) => {
      let cls = "";
      if (withClasses && String(cell).includes("PENDING")) cls = ' class="pill-pending"';
      h += `<td${cls}>${cell === null || cell === undefined ? "" : escapeHtml(String(cell))}</td>`;
    });
    h += "</tr>";
  });
  h += "</tbody></table></div>";
  return h;
}

function renderTab(key) {
  if (key === "gantt") { renderGantt(); return; }
  const entry = currentTrace ? currentTrace[key] : null;
  const meta = RULES[key];
  const root = $("tab-content");
  if (!entry) { root.innerHTML = '<p class="placeholder">Run the plan first.</p>'; return; }

  let html = `<div class="rule-header"><h2>Rule ${meta.n} — ${meta.title}</h2></div>`;

  if (entry.reached === false) {
    html += '<div class="not-reached-box">Not reached — a previous rule stopped the chain.</div>';
    root.innerHTML = html;
    return;
  }

  if (entry.error) {
    html += `<div class="error-box"><strong>Rule error</strong> — record `
      + `<code>${escapeHtml(String(entry.error.record_id))}</code>: ${escapeHtml(entry.error.message)}</div>`;
  }

  // Special interactive form for Rule 8 (actuals entry).
  if (key === "rule8") html += actualsFormHtml();

  html += '<div class="io">';
  html += `<div class="panel"><h3>Input</h3>${tableHtml(entry.input)}</div>`;
  html += `<div class="panel"><h3>Output</h3>${tableHtml(entry.output)}</div>`;
  html += "</div>";

  // Extra full-width tables (e.g. Rule 6's machine-wise view).
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
  if (key === "rule8") wireActualsForm();
}

function fieldYellow(label, inner) {
  return `<label class="efield fy">${label}${inner}</label>`;
}
function fieldRed(label, inner) {
  return `<label class="efield fr">${label}${inner}</label>`;
}

function actualsFormHtml() {
  // Daily Production Entry — yellow = manual entry, red = auto-prompt / dropdown.
  const left =
    fieldYellow("Date", `<input id="a-date" type="date" value="2025-08-01" />`) +
    fieldYellow("Shift", `<input id="a-shift" value="1st shift" />`) +
    fieldYellow("SO No", `<input id="a-so" value="24-25SO214" />`) +
    fieldYellow("Item Code", `<input id="a-item" value="61240807-01" />`) +
    fieldRed("Item Name <span class=auto>(auto)</span>", `<input id="a-itemname" readonly />`) +
    fieldRed("Process <span class=auto>(dropdown)</span>", `<select id="a-process"></select>`);
  const right =
    fieldYellow("Qty Produced", `<input id="a-prod" type="number" value="82" />`) +
    fieldYellow("Qty Rejected", `<input id="a-rej" type="number" value="3" />`) +
    fieldYellow("Actual Setting Time (min)", `<input id="a-setup" type="number" value="120" />`) +
    fieldYellow("No Power (min)", `<input id="a-nopower" type="number" value="80" />`) +
    fieldYellow("No Operator (min)", `<input id="a-noop" type="number" value="30" />`) +
    fieldYellow("Tool Problem (min)", `<input id="a-tool" type="number" value="15" />`) +
    fieldYellow("Machine Breakdown (min)", `<input id="a-mbd" type="number" value="0" />`) +
    fieldYellow("No Load (min)", `<input id="a-noload" type="number" value="0" />`) +
    fieldYellow("Other Work (min)", `<input id="a-other" type="number" value="0" />`) +
    fieldYellow("Remarks", `<textarea id="a-remarks" rows="2"></textarea>`);
  return `
    <div class="entry-legend">
      <span class="lg lg-y">Manual entry</span>
      <span class="lg lg-r">Auto-prompt / dropdown</span>
    </div>
    <div class="entry-grid">
      <div class="entry-col">${left}</div>
      <div class="entry-col">${right}</div>
    </div>
    <button id="a-save" class="primary">Save daily entry</button>`;
}

function fillItemMeta() {
  const code = $("a-item").value.trim();
  const meta = ITEMS && ITEMS.items ? ITEMS.items[code] : null;
  const nameEl = $("a-itemname"), procEl = $("a-process");
  if (meta) {
    nameEl.value = meta.item_name || "";
    procEl.innerHTML = meta.processes.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
  } else {
    nameEl.value = "(unknown item code)";
    procEl.innerHTML = `<option value="">—</option>`;
  }
}

async function wireActualsForm() {
  await ensureItems();
  $("a-item").addEventListener("input", fillItemMeta);
  fillItemMeta();  // initial fill for the prefilled item code

  $("a-save").onclick = async () => {
    const num = (id) => Number($(id).value) || 0;
    const body = {
      entry_date: $("a-date").value,
      shift: $("a-shift").value,
      so_no: $("a-so").value,
      item_code: $("a-item").value,
      item_name: $("a-itemname").value,
      process: $("a-process").value,
      qty_produced: num("a-prod"),
      qty_rejected: num("a-rej"),
      actual_setup_min: num("a-setup"),
      no_power_min: num("a-nopower"),
      no_operator_min: num("a-noop"),
      tool_problem_min: num("a-tool"),
      machine_breakdown_min: num("a-mbd"),
      no_load_min: num("a-noload"),
      other_work_min: num("a-other"),
      remarks: $("a-remarks").value,
    };
    const res = await fetch("/actuals", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (res.ok) {
      const d = await res.json();
      setStatus(`Saved (${d.saved} actuals on record). Re-run plan or Rerun MRP to see the effect.`);
      runPlan();
    } else setStatus("Save failed: " + (await res.text()));
  };
}

async function fetchGantt() {
  try {
    currentGantt = await (await fetch("/gantt" + dsQuery())).json();
    if (activeTab === "gantt") renderGantt();
  } catch (e) {
    $("tab-content").innerHTML = '<p class="placeholder">Gantt failed to load — click Run plan.</p>';
  }
}

function renderGantt() {
  const root = $("tab-content");
  if (!currentGantt) {
    root.innerHTML = '<p class="placeholder">Loading Gantt…</p>';
    fetchGantt();
    return;
  }
  const g = currentGantt;
  if (!g.rows || !g.rows.length) {
    root.innerHTML = '<div class="rule-header"><h2>Production Planning — Gantt</h2></div>'
      + '<p class="placeholder">No schedule to chart. Click Run plan.</p>';
    return;
  }
  const HW = ganttHourWidth;       // px per hour
  const DAYW = 24 * HW;            // px per day
  const axisW = g.num_days * DAYW;

  // Is clock-hour h on day index di a working hour? Shifts run 08:00→05:00 next
  // day, so a day's window covers its own 08–23 plus 00–05 of the next morning.
  const isWorkingHour = (di, h) => {
    if (h >= 8) return !!g.days[di].working;                 // 08–23 current day
    if (h < 5) return di > 0 && !!g.days[di - 1].working;    // 00–05 = prev day's 2nd shift
    return false;                                            // 05–08 gap
  };

  const monthCells = g.months
    .map((m) => `<div class="g-month" style="width:${m.days * DAYW}px">${escapeHtml(m.label)}</div>`)
    .join("");
  const dayCells = g.days
    .map((d) => `<div class="g-day${d.working ? "" : " g-off"}" style="width:${DAYW}px">${d.day} ${escapeHtml(d.month.slice(0, 3))}</div>`)
    .join("");
  let hourCells = "";
  g.days.forEach((d, di) => {
    for (let h = 0; h < 24; h++) {
      hourCells += `<div class="g-hour${isWorkingHour(di, h) ? "" : " g-hoff"}" style="width:${HW}px">${h}</div>`;
    }
  });

  // Body grid: faint line every hour, stronger line every day.
  const grid =
    `repeating-linear-gradient(to right, transparent 0, transparent ${HW - 1}px, rgba(140,145,156,0.18) ${HW - 1}px, rgba(140,145,156,0.18) ${HW}px),` +
    `repeating-linear-gradient(to right, transparent 0, transparent ${DAYW - 1}px, var(--border) ${DAYW - 1}px, var(--border) ${DAYW}px)`;

  const rowsHtml = g.rows.map((r) => {
    const bars = r.bars.map((b) => {
      const left = b.offset_days * DAYW;
      const w = Math.max(b.duration_days * DAYW, 6);
      const tip = `${b.process} · ${b.machine} · ${b.start} → ${b.end} · qty ${b.qty}`;
      return `<div class="g-bar" style="left:${left}px;width:${w}px;background:${b.color}" title="${escapeHtml(tip)}">${escapeHtml(b.process)}</div>`;
    }).join("");
    return `<tr>
      <td>${escapeHtml(r.item_name)}</td>
      <td>${escapeHtml(r.item_code)}</td>
      <td>${escapeHtml(r.so_no)}</td>
      <td>${r.so_qty}</td>
      <td>${escapeHtml(r.so_delivery_date)}</td>
      <td class="g-timeline"><div class="g-track" style="width:${axisW}px;background-image:${grid}">${bars}</div></td>
    </tr>`;
  }).join("");

  const legend = Object.entries(g.machine_colors)
    .map(([m, c]) => `<span class="g-leg"><span class="g-chip" style="background:${c}"></span>${escapeHtml(m)}</span>`)
    .join("");

  root.innerHTML = `
    <div class="rule-header"><h2>Production Planning — Gantt</h2></div>
    <div class="g-toolbar">Zoom (hour width) <button id="g-zoom-out">−</button> <button id="g-zoom-in">+</button>
      <span class="muted">· hour columns; shaded = non-working (nights / Thu / holidays)</span></div>
    <div class="g-scroll">
      <table class="g-table">
        <thead>
          <tr>
            <th class="g-corner" colspan="5" rowspan="2">Dates / hours →</th>
            <th class="g-axis"><div class="g-band" style="width:${axisW}px">${monthCells}</div></th>
          </tr>
          <tr>
            <th class="g-axis"><div class="g-band" style="width:${axisW}px">${dayCells}</div></th>
          </tr>
          <tr>
            <th>Item name</th><th>Item Code</th><th>SO No</th><th>SO Qty</th><th>SO Del date</th>
            <th class="g-axis"><div class="g-band" style="width:${axisW}px">${hourCells}</div></th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
    <div class="g-legend"><strong>Machines (bar colour):</strong> ${legend}</div>
    <p class="g-note">Each bar = one process, positioned by its real start/end <strong>time</strong> (hour columns 0–23 per day) and coloured by the machine running it. Hover a bar for full detail.</p>`;

  $("g-zoom-in").onclick = () => { ganttHourWidth = Math.min(ganttHourWidth + 8, 80); renderGantt(); };
  $("g-zoom-out").onclick = () => { ganttHourWidth = Math.max(ganttHourWidth - 8, 8); renderGantt(); };
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("run-btn").onclick = runPlan;
$("rerun-btn").onclick = rerunMRP;
$("upload-btn").onclick = uploadExcel;
