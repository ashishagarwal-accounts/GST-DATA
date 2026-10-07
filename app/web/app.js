"use strict";

// ---------------------------------------------------------------- helpers

const $ = (sel, root = document) => root.querySelector(sel);
const main = $("#main");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (v) => inr.format(Number(v || 0));
const rupees = (v) => "₹" + money(v);
const dateFmt = (iso) => (iso ? new Date(iso + (iso.length === 10 ? "T00:00:00" : "")).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "");
const timeFmt = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "");
const periodLabel = (p) => new Date(p + "-01T00:00:00").toLocaleDateString("en-IN", { month: "long", year: "numeric" });
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;

const KINDS = {
  sales_register: { label: "Sales register", accept: ".xlsx,.xls,.csv", register: true },
  purchase_register: { label: "Purchase register", accept: ".xlsx,.xls,.csv", register: true },
  gstr2b: { label: "GSTR-2B (JSON)", accept: ".json", register: false },
  gstr2a: { label: "GSTR-2A (JSON)", accept: ".json", register: false },
  advance_register: { label: "Advance register (bank book)", accept: ".xlsx,.xls,.csv", register: true, advance: true },
  advance_opening: { label: "Advance opening balances", accept: ".xlsx,.xls,.csv", register: true, advance: true },
};
const TEMPLATES = { sales_register: true, advance_register: true, advance_opening: true };
const DOC_TYPES = { INV: "Invoice", CRN: "Credit note", DBN: "Debit note" };
const CATEGORIES = {
  taxable: "", export_wpay: "Export (with tax)", export_wopay: "Export (LUT)", sez_wpay: "SEZ (with tax)",
  sez_wopay: "SEZ (LUT)", deemed_export: "Deemed export", nil_rated: "Nil-rated", exempt: "Exempt", non_gst: "Non-GST",
};

class ApiError extends Error {
  constructor(status, body) {
    super(ApiError.message(body) || `Request failed (${status})`);
    this.status = status;
    this.body = body;
  }
  static message(body) {
    if (!body) return "";
    if (typeof body.detail === "string") return body.errors?.[0]?.message || body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d) => String(d.msg).replace(/^Value error, /, "")).join("; ");
    return "";
  }
}

async function api(path, options = {}) {
  const res = await fetch("/api" + path, options);
  let body = null;
  if (res.status !== 204) {
    try { body = await res.json(); } catch { body = null; }
  }
  if (res.status === 401 && !path.startsWith("/auth/")) {
    showAuth("login", "Your session has ended. Sign in again to continue.");
    throw new ApiError(401, body);
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}
const postJson = (path, data, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 3500);
}

function store(key, value) {
  try { value === undefined ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}
function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

const guard = (fn) => async (e) => {
  e?.preventDefault?.();
  try { await fn(e); } catch (err) { alert(err.message); }
};

// ---------------------------------------------------------------- global state: period + organisation (GSTIN)

// period: the single return month used by return pages (GSTR-1, imports, advances).
// range:  the from-to months used by analysis pages (dashboard, transactions, cost centre report).
const state = { period: null, range: null, gstins: [], gstin: "", user: null };

const ym = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
function addMonths(period, n) {
  const [y, m] = period.split("-").map(Number);
  return ym(new Date(y, m - 1 + n, 1));
}
const fyStart = (period) => { const [y, m] = period.split("-").map(Number); return `${m >= 4 ? y : y - 1}-04`; };
const quarterStart = (period) => { const fy = fyStart(period); let q = fy; while (addMonths(q, 3) <= period) q = addMonths(q, 3); return q; };
const shortMonth = (p) => new Date(p + "-01T00:00:00").toLocaleDateString("en-IN", { month: "short", year: "numeric" });

const RANGE_PRESETS = {
  this_month: ["This Month", (now) => [now, now]],
  last_month: ["Last Month", (now) => [addMonths(now, -1), addMonths(now, -1)]],
  this_quarter: ["This Quarter", (now) => [quarterStart(now), addMonths(quarterStart(now), 2)]],
  last_quarter: ["Last Quarter", (now) => [addMonths(quarterStart(now), -3), addMonths(quarterStart(now), -1)]],
  this_fy: ["This Financial Year", (now) => [fyStart(now), addMonths(fyStart(now), 11)]],
  last_fy: ["Last Financial Year", (now) => [addMonths(fyStart(now), -12), addMonths(fyStart(now), -1)]],
};
function presetRange(key) {
  const [from, to] = RANGE_PRESETS[key][1](ym(new Date()));
  return { preset: key, from, to };
}
const rangeLabel = (r) => (r.from === r.to ? periodLabel(r.from) : `${shortMonth(r.from)} - ${shortMonth(r.to)}`);
const rangeQuery = () => `from=${state.range.from}&to=${state.range.to}`;

function rangePicker() {
  const r = state.range;
  const name = r.preset && RANGE_PRESETS[r.preset] ? RANGE_PRESETS[r.preset][0] : "Custom";
  return `<div class="dropdown" id="range-picker">
    <button class="btn secondary" type="button" data-toggle="range-menu" aria-haspopup="true">${icon("calendar")}<span>${esc(name)}</span><span class="muted">${esc(rangeLabel(r))}</span>&#9662;</button>
    <div class="menu" id="range-menu" hidden>
      ${Object.entries(RANGE_PRESETS).map(([k, [label]]) => {
        const p = presetRange(k);
        return `<button type="button" class="menu-item ${k === r.preset ? "on" : ""}" data-preset="${k}">${label}<span class="muted">${esc(rangeLabel(p))}</span></button>`;
      }).join("")}
      <div class="menu-custom">
        <label>From<input type="month" id="range-from" value="${r.from}"></label>
        <label>To<input type="month" id="range-to" value="${r.to}"></label>
        <button type="button" class="btn small" id="range-apply">Apply</button>
      </div>
    </div>
  </div>`;
}

function returnMonths() {
  // Return periods from the start of last financial year up to the current month, newest first.
  const now = ym(new Date());
  const months = [];
  for (let p = addMonths(fyStart(now), -12); p <= now; p = addMonths(p, 1)) months.push(p);
  if (!months.includes(state.period)) months.push(state.period);
  return months.sort().reverse();
}
const monthPicker = () => `<label class="period-select">Return period
  <select id="month-select">${returnMonths().map((m) => `<option value="${m}" ${m === state.period ? "selected" : ""}>${esc(periodLabel(m))}</option>`).join("")}</select></label>`;

function bindPickers() {
  $("#month-select")?.addEventListener("change", (e) => { state.period = e.target.value; store("period", state.period); route(); });
  const setRange = (r) => { state.range = r; store("range", JSON.stringify(r)); route(); };
  main.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => setRange(presetRange(b.dataset.preset))));
  $("#range-apply")?.addEventListener("click", () => {
    const from = $("#range-from").value, to = $("#range-to").value;
    if (!from || !to) return;
    if (from > to) { alert("The From month is after the To month."); return; }
    setRange({ preset: "", from, to });
  });
}

// Dropdown menus (period, user): one open at a time, closed by clicking elsewhere or Escape.
document.addEventListener("click", (e) => {
  if (e.target.closest(".menu") && !e.target.closest(".menu-item, [data-preset]")) return;
  const toggle = e.target.closest("[data-toggle]");
  document.querySelectorAll(".menu").forEach((m) => { if (!toggle || m.id !== toggle.dataset.toggle) m.hidden = true; });
  if (toggle) { const m = document.getElementById(toggle.dataset.toggle); m.hidden = !m.hidden; }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll(".menu").forEach((m) => (m.hidden = true)); });

function defaultPeriod() {
  // Returns are prepared for the month just ended.
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

const org = () => state.gstins.find((g) => g.gstin === state.gstin);
const enc = () => encodeURIComponent(state.gstin);

async function loadGstins() {
  const entities = await api("/entities");
  state.gstins = entities.flatMap((e) => e.gstins.filter((g) => g.active).map((g) => ({ ...g, entity_name: e.name })));
  const wanted = state.gstin || recall("gstin");
  state.gstin = state.gstins.some((g) => g.gstin === wanted) ? wanted : state.gstins[0]?.gstin || "";
  drawOrgSwitch();
}

function setGstin(gstin) {
  if (!state.gstins.some((g) => g.gstin === gstin)) return;
  state.gstin = gstin;
  store("gstin", gstin);
  drawOrgSwitch();
}

function drawOrgSwitch() {
  const sel = $("#org");
  sel.innerHTML = state.gstins.length
    ? state.gstins.map((g) => `<option value="${esc(g.gstin)}" ${g.gstin === state.gstin ? "selected" : ""}>${esc(g.entity_name)} · ${esc(g.gstin)}</option>`).join("")
    : `<option value="">No GSTINs - add one under Entities</option>`;
}

const needOrg = (title) => (org() ? null : `${pageHead(title)}<div class="page-body"><div class="card empty">Add an entity and its GSTIN under <a href="#/entities">Entities &amp; GSTINs</a> to get started.</div></div>`);

// ---------------------------------------------------------------- page chrome

function pageHead(title, { sub = "", actions = "", crumbs = "" } = {}) {
  return `<div class="page-head">
    <div>${crumbs ? `<div class="crumbs">${crumbs}</div>` : ""}<h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ""}</div>
    ${actions ? `<div class="actions">${actions}</div>` : ""}
  </div>`;
}

const orgLine = (span = periodLabel(state.period)) => (org() ? `${esc(org().entity_name)} · <span class="mono">${esc(state.gstin)}</span> · ${esc(span)}` : "");

function warningsCard(warnings) {
  if (!warnings.length) return "";
  return `<div class="card result warn"><h2><span class="st st-warning">${plural(warnings.length, "item")} to check</span></h2>
    <ul class="issues">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
}

// ---------------------------------------------------------------- router

const routes = {
  home: renderHome,
  dashboard: renderDashboard,
  imports: renderImports,
  invoices: renderInvoices,
  advances: renderAdvances,
  entities: renderEntities,
  reports: renderReports,
  "reports/gstr1": renderGstr1Report,
  "reports/advance-tax": renderAdvanceTaxReport,
  "reports/cost-centres": renderCostCentreReport,
  users: renderUsers,
  account: renderAccount,
};

function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  return { name: routes[path] ? path : "home", params: Object.fromEntries(new URLSearchParams(query)) };
}

async function route() {
  const { name, params } = parseHash();
  if (params.gstin) setGstin(params.gstin);
  const section = name.split("/")[0];
  document.querySelectorAll("#rail a").forEach((a) => a.classList.toggle("active", a.dataset.route === section));
  $("#rail").classList.remove("open");
  $("#main").scrollTop = 0;
  main.innerHTML = `<div class="page-body muted">Loading...</div>`;
  state.prevRoute = state.route;
  state.route = name;
  try {
    await routes[name](params);
    bindPickers();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    main.innerHTML = `<div class="page-body"><div class="card result err"><h2>Something went wrong</h2><p>${esc(err.message)}</p></div></div>`;
    console.error(err);
  }
}

// ---------------------------------------------------------------- home: getting started

async function renderHome(params) {
  const g = org();
  const [services, allImports, periodImports, centres] = g ? await Promise.all([
    api(`/gstins/${g.id}/service-types`),
    api(`/imports?gstin=${enc()}`),
    api(`/imports?gstin=${enc()}&period=${state.period}`),
    api(`/gstins/${g.id}/cost-centres`),
  ]) : [[], [], [], []];
  const has = (kind, list = periodImports) => list.some((b) => b.kind === kind);
  const advOn = !!g?.advance_tax_enabled;

  const groups = {
    organisation: { title: "Organisation", steps: [
      { icon: "building", done: !!g, title: "Add Entity and GSTIN", text: "Add each Alcove entity and its GST registrations.", href: "#/entities", cta: "Configure" },
      { icon: "building", done: centres.length > 0, title: "Set Up Cost Centres", text: "Branches / projects under this GSTIN (e.g. Mall Road, Tower A) for cost-centre-wise GST.", href: "#/entities", cta: "Configure" },
      { icon: "doc", done: services.length > 0, title: "Set Up Service Types", text: "Flat sale, CAM, rent ... with SAC, GST rate and land deduction.", href: "#/advances", cta: "Configure" },
      { icon: "wallet", done: advOn, title: "Enable GST on Advances", text: "Charge GST on advances received and set it off on adjustment (Tables 11A / 11B).", href: "#/advances", cta: "Configure" },
      ...(advOn ? [{ icon: "upload", done: has("advance_opening", allImports), title: "Load Opening Advance Balances", text: "Unadjusted advances at go-live, for existing projects. Skip for a fresh project.", href: "#/imports?kind=advance_opening", cta: "Upload" }] : []),
    ] },
    monthly: { title: `Monthly Filing - ${periodLabel(state.period)}`, steps: [
      { icon: "upload", done: has("sales_register"), title: "Import Sales Register", text: "Tally export or Excel, with the Service Type column.", href: "#/imports?kind=sales_register", cta: "Upload" },
      { icon: "upload", done: has("purchase_register"), title: "Import Purchase Register", text: "Supplier invoice numbers are used for reconciliation.", href: "#/imports?kind=purchase_register", cta: "Upload" },
      { icon: "upload", done: has("gstr2b"), title: "Import GSTR-2B", text: "JSON downloaded from the GST portal.", href: "#/imports?kind=gstr2b", cta: "Upload" },
      ...(advOn ? [{ icon: "wallet", done: has("advance_register"), title: "Import Bank Book (Advances)", text: "Advances received and refunded this month.", href: "#/imports?kind=advance_register", cta: "Upload" }] : []),
      { icon: "chart", title: "Review and Export GSTR-1", text: "Check the tables and download the offline-tool Excel.", href: "#/reports/gstr1", cta: "Open", untracked: true },
    ] },
  };
  const tracked = Object.values(groups).flatMap((grp) => grp.steps.filter((s) => !s.untracked));
  const done = tracked.filter((s) => s.done).length;
  const tab = groups[params.tab] ? params.tab : (groups.organisation.steps.every((s) => s.done) ? "monthly" : "organisation");
  const count = (grp) => { const t = grp.steps.filter((s) => !s.untracked); return `${t.filter((s) => s.done).length}/${t.length}`; };

  main.innerHTML = `
    <div class="welcome">
      <h1>Welcome to Alcove GST</h1>
      <p>Complete the following steps to get your GST returns prepared for ${g ? esc(g.entity_name) : "your organisation"}.</p>
    </div>
    <div class="home-wrap">
      <div class="intro">
        <div>
          <span class="badge">${icon("play")}</span>
          <div><h3>How the monthly cycle works</h3>
            <p>Import your Tally registers, bank book and GSTR-2B, review the warnings, then export GSTR-1 for the GST offline tool.</p>
            <a class="btn secondary small" href="#/imports">Open Imports</a></div>
        </div>
        <div>
          <span style="color:var(--critical)">${icon("help")}</span>
          <div><h3>Upload templates</h3>
            <p>Use these layouts when exporting from Tally so every column is picked up on day one.</p>
            <div class="actions">
              <a class="btn secondary small" href="/api/templates/sales_register.xlsx">Sales register</a>
              <a class="btn secondary small" href="/api/templates/advance_register.xlsx">Bank book</a>
              <a class="btn secondary small" href="/api/templates/advance_opening.xlsx">Opening advances</a>
            </div></div>
        </div>
        <div class="intro-note">${icon("chart")}&nbsp; Reports: GSTR-1 and Advance Tax are ready. GSTR-3B and GSTR-2B reconciliation are next. &nbsp;<a href="#/reports">Open Reports &rarr;</a></div>
      </div>

      <div class="setup-head">
        <h2>${icon("rocket")}First-Time Setup: recommended actions to prepare your returns</h2>
        ${monthPicker()}
        <div class="progress">Completion progress <span class="bar"><span style="width:${tracked.length ? Math.round((done / tracked.length) * 100) : 0}%"></span></span></div>
      </div>
      <section class="card flush">
        <div class="tabs">${Object.entries(groups).map(([k, grp]) => `<a href="#/home?tab=${k}" class="${k === tab ? "on" : ""}">${esc(grp.title)} <span class="count">(${count(grp)})</span></a>`).join("")}</div>
        <ul class="steps">${groups[tab].steps.map((s) => `<li>
          <span class="step-icon ${s.done ? "done" : ""}">${icon(s.done ? "check" : s.icon)}</span>
          <div class="step-text"><b>${esc(s.title)}</b><span>${esc(s.text)}</span></div>
          ${s.done ? `<span class="completed">${icon("check")}Completed</span>` : `<a class="btn secondary" href="${s.href}">${s.cta}</a>`}
        </li>`).join("")}</ul>
      </section>
    </div>`;
}

// ---------------------------------------------------------------- dashboard

function importCell(row, kind) {
  const imp = row.imports[kind];
  const href = `#/imports?gstin=${encodeURIComponent(row.gstin)}&kind=${kind}`;
  if (!imp) {
    const optional = kind === "gstr2a";
    return `<a class="st st-missing" href="${href}" title="Click to upload"><span>${optional ? "Optional" : "Not imported"}</span></a>`;
  }
  const title = `${imp.file_name} - imported ${timeFmt(imp.created_at)}`;
  let docs = plural(imp.record_count, "doc");
  if (imp.of > 1) {
    docs = `${imp.months} of ${imp.of} months · ${docs}`;
    if (imp.months < imp.of) return `<a class="st st-warning" href="${href}" title="Some months in the range are not imported"><span>${docs}</span></a>`;
  }
  if (imp.warning_count) {
    return `<a class="st st-warning" href="#/imports?gstin=${encodeURIComponent(row.gstin)}&batch=${imp.batch_id}" title="${esc(title)}"><span>${docs} · ${plural(imp.warning_count, "warning")}</span></a>`;
  }
  return `<span class="st st-good" title="${esc(title)}">${docs}</span>`;
}

const REQUIRED = ["sales_register", "purchase_register", "gstr2b"];
const complete = (imp) => imp && imp.months === imp.of;  // imported for every month of the range

async function renderDashboard(params) {
  const data = await api(`/dashboard?${rangeQuery()}`);
  const span = rangeLabel(state.range);
  const { totals, gstins } = data;
  const ready = gstins.filter((g) => REQUIRED.every((k) => complete(g.imports[k]))).length;
  const gap = Number(totals.itc_books.tax) - Number(totals.itc_2b.tax);

  let gapNote;
  if (!gstins.some((g) => g.imports.gstr2b)) gapNote = `<span class="st st-missing">GSTR-2B not imported yet</span>`;
  else if (Math.abs(gap) < 1) gapNote = `<span class="st st-good">Books and 2B agree</span>`;
  else if (gap > 0) gapNote = `<span class="st st-warning">Books claim more than 2B shows</span>`;
  else gapNote = `<span class="st st-info">2B shows ITC not in books</span>`;

  const tab = ["overview", "imports", "advances"].includes(params.tab) ? params.tab : "overview";
  const tabs = [["overview", "Overview"], ["imports", "Import Status"], ...(data.advances ? [["advances", "Advances"]] : [])];
  const head = `${pageHead("GST Overview", { sub: `All GSTINs · ${esc(span)}`,
      actions: `${rangePicker()}<a class="btn secondary" href="#/reports">${icon("chart")}Reports</a><a class="btn" href="#/imports">${icon("upload")}Import data</a>` })}
    <div class="tabs">${tabs.map(([k, label]) => `<a href="#/dashboard?tab=${k}" class="${k === tab ? "on" : ""}">${label}</a>`).join("")}</div>`;

  if (tab === "overview") {
    main.innerHTML = `${head}
      <div class="page-body">
        ${gstins.length ? "" : `<div class="banner blue">No active GSTINs yet. <a href="#/entities">Add an entity and its GSTINs</a> to get started.</div>`}
        <div class="grid-2">
          <section class="card flush">
            <div class="card-head"><h2>Tax Position</h2><span class="actions muted">${esc(span)}</span></div>
            <div class="card-body"><div class="kpis">
              <div class="tile"><div class="label">Output tax</div><div class="value">${rupees(totals.outward.tax)}</div>
                <div class="note">on taxable ${rupees(totals.outward.taxable_value)}, net of credit notes</div></div>
              ${data.advances ? `<div class="tile"><div class="label">Net tax on advances</div><div class="value">${rupees(data.advances.net_tax)}</div>
                <div class="note">11A ${rupees(data.advances.tax_11a)} − 11B ${rupees(data.advances.tax_11b)}</div></div>` : ""}
            </div></div>
          </section>
          <section class="card flush">
            <div class="card-head"><h2>Input Tax Credit</h2><span class="actions muted">${esc(span)}</span></div>
            <div class="card-body"><div class="kpis">
              <div class="tile"><div class="label">ITC per books</div><div class="value">${rupees(totals.itc_books.tax)}</div>
                <div class="note">eligible purchases only</div></div>
              <div class="tile"><div class="label">ITC per GSTR-2B</div><div class="value">${rupees(totals.itc_2b.tax)}</div>
                <div class="note">${gapNote} ${Math.abs(gap) >= 1 ? `(${rupees(Math.abs(gap))})` : ""}</div></div>
            </div></div>
          </section>
          <section class="card flush">
            <div class="card-head"><h2>Filing Readiness</h2><span class="actions"><a href="#/dashboard?tab=imports">View all</a></span></div>
            <div class="card-body">
              <p style="margin:0 0 10px"><strong>${ready} of ${plural(gstins.length, "GSTIN")}</strong> have sales, purchases and GSTR-2B imported for every month of ${esc(span)}.</p>
              ${gstins.filter((g) => !REQUIRED.every((k) => complete(g.imports[k]))).slice(0, 5).map((g) => `<div style="padding:4px 0">
                <a href="#/imports?gstin=${encodeURIComponent(g.gstin)}">${esc(g.entity_name)}</a> <span class="muted mono">${esc(g.gstin)}</span> -
                <span class="st st-missing"><span>missing ${REQUIRED.filter((k) => !complete(g.imports[k])).map((k) => ({ sales_register: "sales", purchase_register: "purchases", gstr2b: "GSTR-2B" }[k])).join(", ")}</span></span></div>`).join("")}
            </div>
          </section>
          <section class="card flush">
            <div class="card-head"><h2>Returns</h2><span class="actions"><a href="#/reports">All reports</a></span></div>
            <div class="card-body">
              <div style="padding:4px 0"><a href="#/reports/gstr1">GSTR-1</a> <span class="muted">- outward supplies, ready for the offline tool</span></div>
              <div style="padding:4px 0" class="muted">GSTR-3B <span class="pill">Coming soon</span></div>
              <div style="padding:4px 0" class="muted">GSTR-2B Reconciliation <span class="pill">Coming soon</span></div>
            </div>
          </section>
        </div>
        <div class="banner blue" style="margin-top:4px">Figures are preliminary sums for orientation. The GSTR-3B report will compute actual liability, RCM and ITC cross-utilisation.</div>
      </div>`;
    return;
  }

  if (tab === "advances") {
    const rows = gstins.filter((g) => g.advances);
    main.innerHTML = `${head}
      <div class="page-body"><section class="card flush">
        <div class="card-head"><h2>Advances by GSTIN</h2></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Entity / GSTIN</th><th class="num">11A tax</th><th class="num">11B tax set off</th><th class="num">Net tax</th><th class="num">Unadjusted at month-end</th></tr></thead>
          <tbody>${rows.map((g) => `<tr>
            <td><a href="#/reports/advance-tax?gstin=${encodeURIComponent(g.gstin)}">${esc(g.entity_name)}</a><span class="small mono">${esc(g.gstin)}</span></td>
            <td class="num">${money(g.advances.tax_11a)}</td><td class="num">${money(g.advances.tax_11b)}</td>
            <td class="num">${money(g.advances.net_tax)}</td><td class="num">${money(g.advances.closing_amount)}</td></tr>`).join("")}</tbody>
        </table></div>
      </section></div>`;
    return;
  }

  main.innerHTML = `${head}
    <div class="page-body">
      <section class="card flush">
        <div class="card-head"><h2>Import status by GSTIN</h2><span class="actions muted">${ready} of ${plural(gstins.length, "GSTIN")} ready</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Entity / GSTIN</th><th>State</th><th>Sales</th><th>Purchases</th><th>GSTR-2B</th><th>GSTR-2A</th><th>Advances</th>
            <th class="num">Output tax</th><th class="num">ITC (books)</th><th class="num">ITC (2B)</th><th class="num">Advance tax (net)</th></tr></thead>
          <tbody>${gstins.map((g) => `
            <tr>
              <td><a href="#/reports/gstr1?gstin=${encodeURIComponent(g.gstin)}">${esc(g.entity_name)}</a><span class="small mono">${esc(g.gstin)}</span></td>
              <td>${esc(g.state_name || g.state_code)}</td>
              ${["sales_register", "purchase_register", "gstr2b", "gstr2a"].map((k) => `<td>${importCell(g, k)}</td>`).join("")}
              <td>${g.advances ? importCell(g, "advance_register") : `<a class="st st-missing" href="#/advances?gstin=${encodeURIComponent(g.gstin)}"><span>Not enabled</span></a>`}</td>
              <td class="num">${money(g.outward.tax)}</td>
              <td class="num">${money(g.itc_books.tax)}</td>
              <td class="num">${money(g.itc_2b.tax)}</td>
              <td class="num">${g.advances ? `<a href="#/reports/advance-tax?gstin=${encodeURIComponent(g.gstin)}">${money(g.advances.net_tax)}</a>` : `<span class="muted">-</span>`}</td>
            </tr>`).join("") || `<tr><td colspan="11" class="empty">No GSTINs</td></tr>`}
          </tbody>
          ${gstins.length > 1 ? `<tfoot><tr><td colspan="7">Total</td><td class="num">${money(totals.outward.tax)}</td>
            <td class="num">${money(totals.itc_books.tax)}</td><td class="num">${money(totals.itc_2b.tax)}</td>
            <td class="num">${data.advances ? money(data.advances.net_tax) : "-"}</td></tr></tfoot>` : ""}
        </table></div>
      </section>
    </div>`;
}

// ---------------------------------------------------------------- imports

function issueList(issues) {
  return `<ul class="issues">${issues.map((i) => `<li><span class="row">${i.row ? "Row " + i.row : ""}</span>${esc(i.message)}</li>`).join("")}</ul>`;
}

function renderResult(kind, outcome) {
  if (outcome.ok) {
    const { batch, replaced_batch_ids: replaced, details } = outcome.body;
    const w = batch.warnings;
    return `<div class="card result ${w.length ? "warn" : "ok"}">
      <h2><span class="st ${w.length ? "st-warning" : "st-good"}">Imported ${plural(batch.record_count, "document")}</span></h2>
      <p class="muted">${esc(batch.file_name)}${replaced.length ? " - replaced the previous import" : ""}${details?.header_row ? ` - header found on row ${details.header_row} of sheet "${esc(details.sheet)}"` : ""}</p>
      ${details?.unmapped_columns?.length ? `<p class="muted">Columns not used: ${details.unmapped_columns.map(esc).join(", ")}</p>` : ""}
      ${w.length ? `<strong>${plural(w.length, "warning")} to review</strong>${issueList(w)}` : ""}
    </div>`;
  }
  const { status, body } = outcome;
  const errors = body?.errors || [{ message: ApiError.message(body) || `Upload failed (${status})` }];
  const alreadyImported = status === 409 && /replace=true/.test(errors[0].message);
  return `<div class="card result err">
    <h2><span class="st st-critical">${alreadyImported ? "Already imported" : "Import rejected - nothing was saved"}</span></h2>
    ${alreadyImported
      ? `<p>${KINDS[kind].label} for this GSTIN and period already exists.</p>
         <div class="actions"><button class="btn" id="retry-replace">Replace the existing import</button></div>`
      : `<p class="muted">Fix these in the file and upload it again.</p>${issueList(errors)}`}
    ${body?.warnings?.length ? `<p><strong>Also ${plural(body.warnings.length, "warning")}:</strong></p>${issueList(body.warnings)}` : ""}
  </div>`;
}

async function renderImports(params) {
  const blocked = needOrg("Imports");
  if (blocked) { main.innerHTML = blocked; return; }
  const kind = KINDS[params.kind] ? params.kind : recall("kind") || "sales_register";
  main.innerHTML = `
    ${pageHead("Imports", { sub: orgLine(), actions: `${monthPicker()}<span id="template-link"></span>` })}
    <div class="page-body">
      <form class="card" id="upload-form">
        <h2>Upload a file</h2>
        <div class="form-grid">
          <label class="field">File type<select name="kind">${Object.entries(KINDS).map(([k, v]) => `<option value="${k}" ${k === kind ? "selected" : ""}>${v.label}</option>`).join("")}</select></label>
          <label class="field" id="source-field">Source<select name="source"><option value="tally">Tally export</option><option value="excel">Manual Excel / CSV</option></select></label>
          <label class="field">File<input type="file" name="file" required></label>
          <div class="actions"><button class="btn" type="submit">${icon("upload")}Upload</button></div>
        </div>
        <p class="muted" id="kind-hint" style="margin:10px 0 0"></p>
        <label class="check" style="margin-top:10px"><input type="checkbox" name="replace"> Replace an existing import for this period</label>
      </form>
      <div id="upload-result"></div>
      <section class="card flush"><div class="card-head"><h2>Import history</h2><span class="muted">${esc(periodLabel(state.period))}</span></div><div id="history"></div></section>
    </div>`;

  const form = $("#upload-form");
  const syncKind = () => {
    const k = KINDS[form.kind.value];
    $("#source-field").style.display = k.register ? "" : "none";
    form.file.accept = k.accept;
    store("kind", form.kind.value);
    $("#template-link").innerHTML = TEMPLATES[form.kind.value]
      ? `<a class="btn secondary" href="/api/templates/${form.kind.value}.xlsx">${icon("download")}Download template</a>` : "";
    const hints = [];
    if (form.kind.value === "advance_opening") hints.push("Uploaded once per GSTIN. The selected return period is the go-live month; every row must be dated before it.");
    if (k.advance) hints.push(`GST on advances must be enabled for this GSTIN on the <a href="#/advances">Advances</a> page.`);
    $("#kind-hint").innerHTML = hints.join(" ");
  };
  form.kind.addEventListener("change", syncKind);
  syncKind();

  const submit = async (forceReplace = false) => {
    const k = form.kind.value;
    const fd = new FormData();
    fd.append("gstin", state.gstin);
    fd.append("period", state.period);
    fd.append("kind", k);
    if (KINDS[k].register) fd.append("source", form.source.value);
    fd.append("replace", String(forceReplace || form.replace.checked));
    fd.append("file", form.file.files[0]);
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    let outcome;
    try {
      outcome = { ok: true, body: await api(`/imports/${KINDS[k].register ? "register" : "portal"}`, { method: "POST", body: fd }) };
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      outcome = { ok: false, status: err.status, body: err.body };
    } finally {
      button.disabled = false;
    }
    $("#upload-result").innerHTML = renderResult(k, outcome);
    $("#retry-replace")?.addEventListener("click", () => submit(true));
    if (outcome.ok) {
      form.file.value = "";
      form.replace.checked = false;
      loadHistory();
    }
  };
  form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  await loadHistory(params.batch);
}

async function loadHistory(openBatch) {
  const batches = await api(`/imports?gstin=${enc()}&period=${state.period}`);
  const box = $("#history");
  if (!batches.length) {
    box.innerHTML = `<p class="empty">Nothing imported for this period yet.</p>`;
    return;
  }
  box.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Type</th><th>File</th><th>Source</th><th class="num">Documents</th><th>Warnings</th><th>Imported</th><th></th></tr></thead>
    <tbody>${batches.map((b) => `
      <tr>
        <td>${KINDS[b.kind].label}</td>
        <td>${esc(b.file_name)}</td>
        <td>${b.source === "gst_portal" ? "GST portal" : b.source === "tally" ? "Tally" : "Excel"}</td>
        <td class="num">${b.record_count}</td>
        <td>${b.warnings.length
          ? `<details ${String(b.id) === String(openBatch) ? "open" : ""}><summary class="st st-warning"><span>${b.warnings.length}</span></summary>${issueList(b.warnings)}</details>`
          : `<span class="st st-good">None</span>`}</td>
        <td>${timeFmt(b.created_at)}</td>
        <td class="num"><button class="btn danger small" data-delete="${b.id}">Delete</button></td>
      </tr>`).join("")}
    </tbody></table></div>`;
  box.querySelectorAll("[data-delete]").forEach((btn) => btn.addEventListener("click", guard(async () => {
    const b = batches.find((x) => String(x.id) === btn.dataset.delete);
    if (!confirm(`Delete the ${KINDS[b.kind].label} import "${b.file_name}" and its ${plural(b.record_count, "document")}?`)) return;
    await api(`/imports/${b.id}`, { method: "DELETE" });
    toast("Import deleted");
    loadHistory();
  })));
}

// ---------------------------------------------------------------- sales & purchases

const VIEWS = {
  outward: { label: "Sales", party: "Customer" },
  inward: { label: "Purchases", party: "Supplier" },
  gstr2b: { label: "GSTR-2B", party: "Supplier" },
  gstr2a: { label: "GSTR-2A", party: "Supplier" },
};

async function renderInvoices(params) {
  const blocked = needOrg("Sales &amp; Purchases");
  if (blocked) { main.innerHTML = blocked; return; }
  const view = VIEWS[params.view] ? params.view : "outward";
  const portal = view === "gstr2b" || view === "gstr2a";
  const docs = portal
    ? await api(`/portal-documents?gstin=${enc()}&${rangeQuery()}&kind=${view}`)
    : await api(`/invoices?gstin=${enc()}&${rangeQuery()}&direction=${view}`);
  const rows = docs.map((d) => ({
    ...d,
    party: portal ? d.supplier_name : d.counterparty_name,
    partyGstin: portal ? d.supplier_gstin : d.counterparty_gstin,
    sign: d.doc_type === "CRN" ? -1 : 1,
    centres: portal ? [] : [...new Set(d.items.map((i) => i.cost_centre_name || "Unassigned"))],
  }));
  const centreNames = [...new Set(rows.flatMap((r) => r.centres))].sort((a, b) => (a === "Unassigned") - (b === "Unassigned") || a.localeCompare(b));
  const ccFilter = centreNames.includes(params.cc) ? params.cc : "";
  // With a cost centre selected, amounts come from that cost centre's lines only (an invoice may span several).
  const lineView = (r) => {
    if (!ccFilter) return r;
    const lines = r.items.filter((i) => (i.cost_centre_name || "Unassigned") === ccFilter);
    if (!lines.length) return null;
    const s = (f) => lines.reduce((a, i) => a + Number(i[f]), 0);
    const tax = s("igst") + s("cgst") + s("sgst") + s("cess");
    return { ...r, taxable_value: s("taxable_value"), igst: s("igst"), cgst: s("cgst"), sgst: s("sgst"),
             invoice_value: s("taxable_value") + s("land_value") + tax };
  };

  main.innerHTML = `
    ${pageHead("Transactions", { sub: orgLine(rangeLabel(state.range)),
      actions: `${rangePicker()}${centreNames.length > 1 || (centreNames.length && centreNames[0] !== "Unassigned")
        ? `<label class="field" style="flex-direction:row;align-items:center;gap:8px">Cost centre<select id="f-cc"><option value="">All</option>${centreNames.map((c) => `<option ${c === ccFilter ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>` : ""}
        <label class="search">${icon("search")}<input id="f-search" type="search" placeholder="Search invoice no, party, GSTIN"></label>` })}
    <div class="tabs" role="tablist">${Object.entries(VIEWS).map(([k, v]) => `<button role="tab" data-view="${k}" class="${k === view ? "on" : ""}">${v.label}</button>`).join("")}</div>
    <div class="page-body">
      <section class="card flush" id="inv-table"></section>
    </div>`;
  main.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    const q = $("#f-search").value.trim();
    location.hash = `#/invoices?${new URLSearchParams({ view: b.dataset.view, ...(q ? { q } : {}) })}`;
  }));
  $("#f-cc")?.addEventListener("change", (e) => {
    const q = $("#f-search").value.trim();
    location.hash = `#/invoices?${new URLSearchParams({ view, ...(q ? { q } : {}), ...(e.target.value ? { cc: e.target.value } : {}) })}`;
  });

  const draw = (term) => {
    const t = term.trim().toLowerCase();
    const shown = rows.map(lineView).filter(Boolean)
      .filter((r) => !t || [r.invoice_no, r.party, r.partyGstin, ...r.centres].some((v) => (v || "").toLowerCase().includes(t)));
    const sum = (f) => shown.reduce((acc, r) => acc + r.sign * Number(r[f]), 0);
    const flags = (r) => [
      r.doc_type !== "INV" ? DOC_TYPES[r.doc_type] : "",
      r.reverse_charge ? "RCM" : "",
      CATEGORIES[r.supply_category] || "",
      r.itc_eligible === false ? "ITC ineligible" : "",
      r.itc_available === false ? `ITC not available${r.itc_unavailable_reason ? ": " + r.itc_unavailable_reason : ""}` : "",
      r.supplier_return_filed === false ? "Supplier GSTR-1 not filed" : "",
    ].filter(Boolean);
    $("#inv-table").innerHTML = !rows.length
      ? `<p class="empty">No ${VIEWS[view].label.toLowerCase()} documents for this period. <a href="#/imports">Import a file</a>.</p>`
      : `<div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Number</th><th>${VIEWS[view].party}</th>${portal ? "" : "<th>Cost centre</th>"}<th>POS</th><th>Notes</th>
          <th class="num">Taxable</th><th class="num">IGST</th><th class="num">CGST</th><th class="num">SGST</th><th class="num">Value</th></tr></thead>
        <tbody>${shown.map((r) => `
          <tr>
            <td>${dateFmt(r.invoice_date)}</td>
            <td class="mono">${esc(r.invoice_no)}</td>
            <td>${esc(r.party || "-")}<span class="small mono">${esc(r.partyGstin || "Unregistered")}</span></td>
            ${portal ? "" : `<td>${r.centres.map((c) => `<span class="pill ${c === "Unassigned" ? "" : "blue"}">${esc(c)}</span>`).join(" ")}</td>`}
            <td>${esc(r.place_of_supply || "")}</td>
            <td>${flags(r).map((f) => `<span class="pill">${esc(f)}</span>`).join(" ")}</td>
            <td class="num">${money(r.sign * r.taxable_value)}</td>
            <td class="num">${money(r.sign * r.igst)}</td>
            <td class="num">${money(r.sign * r.cgst)}</td>
            <td class="num">${money(r.sign * r.sgst)}</td>
            <td class="num">${money(r.sign * r.invoice_value)}</td>
          </tr>`).join("")}
        </tbody>
        <tfoot><tr><td colspan="${portal ? 5 : 6}">${shown.length} of ${plural(rows.length, "document")}${ccFilter ? ` - ${esc(ccFilter)} lines only` : ""} (credit notes negative)</td>
          <td class="num">${money(sum("taxable_value"))}</td><td class="num">${money(sum("igst"))}</td>
          <td class="num">${money(sum("cgst"))}</td><td class="num">${money(sum("sgst"))}</td><td class="num">${money(sum("invoice_value"))}</td></tr></tfoot>
      </table></div>`;
  };
  const search = $("#f-search");
  search.value = params.q || "";
  search.addEventListener("input", (e) => draw(e.target.value));
  draw(search.value);
}

// ---------------------------------------------------------------- advances (setup + ledger)

async function renderAdvances() {
  const blocked = needOrg("Advances");
  if (blocked) { main.innerHTML = blocked; return; }
  const g = org();
  const [services, report, ledger, rules] = await Promise.all([
    api(`/gstins/${g.id}/service-types`),
    g.advance_tax_enabled ? api(`/advances/report?gstin=${enc()}&period=${state.period}`) : null,
    g.advance_tax_enabled ? api(`/advances/ledger?gstin=${enc()}&period=${state.period}`) : [],
    g.advance_tax_enabled ? api(`/advances/rules?gstin=${enc()}`) : [],
  ]);
  const tax = (b) => Number(b.igst) + Number(b.cgst) + Number(b.sgst);
  const open = ledger.filter((r) => Number(r.balance) > 0);

  main.innerHTML = `
    ${pageHead("Advances", { sub: orgLine(),
      actions: monthPicker() + (g.advance_tax_enabled ? `<a class="btn secondary" href="#/reports/advance-tax">${icon("chart")}Advance Tax report (11A / 11B)</a>
        <a class="btn" href="#/imports?kind=advance_register">${icon("upload")}Import bank book</a>` : "") })}
    <div class="page-body">
      <form class="card" id="adv-settings">
        <h2>Preference</h2>
        <div class="actions">
          <label class="check"><input type="checkbox" name="enabled" ${g.advance_tax_enabled ? "checked" : ""}> Charge GST on advances received for this GSTIN</label>
          <label class="field" style="flex-direction:row;align-items:center;gap:8px">from<input type="date" name="from" value="${esc(g.advance_tax_from || "")}"></label>
          <button class="btn small" type="submit">Save</button>
        </div>
      </form>

      <section class="card flush">
        <div class="card-head"><h2>Service types</h2><span class="muted">Advances adjust only against invoice lines of the same service type</span></div>
        ${services.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Name (as in the upload)</th><th>SAC</th><th class="num">GST rate</th><th>Land deduction</th><th>Status</th><th></th></tr></thead>
          <tbody>${services.map((s) => `<tr>
            <td>${esc(s.name)}</td><td class="mono">${esc(s.sac || "")}</td><td class="num">${Number(s.tax_rate)}%</td>
            <td>${s.land_deduction ? "Yes - ⅓ deducted" : "No"}</td>
            <td>${s.active ? `<span class="st st-good">Active</span>` : `<span class="st st-missing">Inactive</span>`}</td>
            <td class="num"><button class="btn ghost small" data-toggle-service="${s.id}" data-active="${s.active}">${s.active ? "Deactivate" : "Activate"}</button></td>
          </tr>`).join("")}</tbody></table></div>` : `<p class="empty">No service types yet.</p>`}
        <div class="card-body">
          <details class="inline" ${services.length ? "" : "open"} style="margin:0"><summary style="margin:0">+ New service type</summary>
            <form id="service-form"><div class="form-grid">
              <label class="field">Name<input name="name" required placeholder="e.g. Flat Sale, CAM"></label>
              <label class="field">SAC<input name="sac" maxlength="8"></label>
              <label class="field">GST rate %<input name="tax_rate" type="number" step="0.01" min="0" max="40" required value="18"></label>
              <label class="check"><input type="checkbox" name="land_deduction"> Land deduction (⅓)</label>
              <div><button class="btn" type="submit">Save</button></div>
            </div></form>
          </details>
        </div>
      </section>

      ${!g.advance_tax_enabled ? `<div class="banner">GST on advances is off for this GSTIN. Turn it on above to upload the bank book and get Tables 11A / 11B.</div>` : `
      <section class="kpis" aria-label="Advance totals">
        <div class="tile"><div class="label">Received this month</div><div class="value">${rupees(report.received.amount)}</div>
          <div class="note">tax ${rupees(tax(report.received))}; ${rupees(report.adjusted_same_month.amount)} adjusted within the month</div></div>
        <div class="tile"><div class="label">Table 11A tax</div><div class="value">${rupees(report.tax_11a)}</div>
          <div class="note">this month's advances unadjusted at month-end</div></div>
        <div class="tile"><div class="label">Table 11B tax set off</div><div class="value">${rupees(report.tax_11b)}</div>
          <div class="note">earlier advances adjusted or refunded</div></div>
        <div class="tile"><div class="label">Unadjusted at month-end</div><div class="value">${rupees(report.closing_balance.amount)}</div>
          <div class="note">tax already paid ${rupees(tax(report.closing_balance))}</div></div>
      </section>
      ${warningsCard(report.warnings)}

      <section class="card flush">
        <div class="card-head"><h2>Advance ledger</h2><span class="muted">up to ${esc(periodLabel(state.period))}</span>
          <div class="actions"><label class="check"><input type="checkbox" id="only-open" checked> Open advances only (${open.length} of ${ledger.length})</label></div></div>
        <div id="ledger-table"></div>
      </section>

      <section class="card flush">
        <div class="card-head"><h2>Manual adjustments</h2><span class="muted">Override oldest-first: adjust a particular advance against a particular invoice</span></div>
        ${rules.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Invoice no</th><th>Advance voucher no</th><th class="num">Amount</th><th>Note</th><th></th></tr></thead>
          <tbody>${rules.map((r) => `<tr><td class="mono">${esc(r.invoice_no)}</td><td class="mono">${esc(r.advance_voucher_no)}</td>
            <td class="num">${r.amount ? money(r.amount) : "As much as possible"}</td><td>${esc(r.note || "")}</td>
            <td class="num"><button class="btn danger small" data-delete-rule="${r.id}">Remove</button></td></tr>`).join("")}</tbody></table></div>` : ""}
        <form id="rule-form" class="card-body"><div class="form-grid">
          <label class="field">Invoice no<input name="invoice_no" required></label>
          <label class="field">Advance voucher no<input name="advance_voucher_no" required list="open-vouchers"></label>
          <datalist id="open-vouchers">${open.map((r) => `<option value="${esc(r.voucher_no)}">${esc(r.customer_name)} · ${esc(r.service_type)} · ${money(r.balance)}</option>`).join("")}</datalist>
          <label class="field">Amount (optional)<input name="amount" type="number" step="0.01" min="0"></label>
          <label class="field">Note<input name="note"></label>
          <div><button class="btn" type="submit">Add rule</button></div>
        </div></form>
      </section>`}
    </div>`;

  const reload = async () => { await loadGstins(); await renderAdvances(); };
  $("#adv-settings").addEventListener("submit", guard(async (e) => {
    const f = e.target;
    await postJson(`/gstins/${g.id}`, { advance_tax_enabled: f.enabled.checked, advance_tax_from: f.from.value || null }, "PATCH");
    toast("Preference saved");
    await reload();
  }));
  $("#service-form").addEventListener("submit", guard(async (e) => {
    const f = e.target;
    await postJson(`/gstins/${g.id}/service-types`, { name: f.elements.name.value, sac: f.sac.value.trim() || null, tax_rate: f.tax_rate.value, land_deduction: f.land_deduction.checked });
    toast("Service type added");
    await reload();
  }));
  main.querySelectorAll("[data-toggle-service]").forEach((b) => b.addEventListener("click", guard(async () => {
    await postJson(`/service-types/${b.dataset.toggleService}`, { active: b.dataset.active !== "true" }, "PATCH");
    await reload();
  })));
  if (!g.advance_tax_enabled) return;

  const drawLedger = () => {
    const rows = $("#only-open").checked ? open : ledger;
    $("#ledger-table").innerHTML = !rows.length ? `<p class="empty">No advances.</p>` : `<div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Voucher</th><th>Customer</th><th>Service</th><th>Cost centre</th><th class="num">Received</th><th class="num">Tax paid</th>
        <th class="num">Adjusted</th><th class="num">Refunded</th><th class="num">Balance</th><th>Adjustments</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${dateFmt(r.entry_date)}${r.entry_type === "opening" ? `<span class="small">Opening</span>` : ""}</td>
        <td class="mono">${esc(r.voucher_no)}</td>
        <td>${esc(r.customer_name)}${r.customer_gstin ? `<span class="small mono">${esc(r.customer_gstin)}</span>` : ""}</td>
        <td>${esc(r.service_type)}</td>
        <td>${r.cost_centre ? esc(r.cost_centre) : `<span class="muted">-</span>`}</td>
        <td class="num">${money(r.amount)}</td><td class="num">${money(r.tax)}</td>
        <td class="num">${money(r.adjusted)}</td><td class="num">${money(r.refunded)}</td><td class="num">${money(r.balance)}</td>
        <td>${r.allocations.length ? `<details><summary class="muted">${r.allocations.length}</summary><ul class="issues">${r.allocations.map((a) =>
          `<li>${a.kind === "refund" ? "Refund" : "Invoice"} <span class="mono">${esc(a.reference)}</span> · ${dateFmt(a.date)} · ${money(a.amount)} (tax ${money(a.tax)})${a.manual ? ` <span class="pill blue">manual</span>` : ""}</li>`).join("")}</ul></details>` : `<span class="muted">-</span>`}</td>
      </tr>`).join("")}</tbody></table></div>`;
  };
  $("#only-open").addEventListener("change", drawLedger);
  drawLedger();

  $("#rule-form").addEventListener("submit", guard(async (e) => {
    const f = e.target;
    await postJson("/advances/rules", { gstin: state.gstin, invoice_no: f.invoice_no.value, advance_voucher_no: f.advance_voucher_no.value,
      amount: f.amount.value || null, note: f.note.value || null });
    toast("Rule added - Tables 11A / 11B recalculated");
    await reload();
  }));
  main.querySelectorAll("[data-delete-rule]").forEach((b) => b.addEventListener("click", guard(async () => {
    await api(`/advances/rules/${b.dataset.deleteRule}`, { method: "DELETE" });
    toast("Rule removed");
    await reload();
  })));
}

// ---------------------------------------------------------------- reports center

const REPORTS = [
  { category: "GST Returns", items: [
    { name: "GSTR-1", desc: "Outward supplies - all tables, with offline-tool Excel", href: "#/reports/gstr1" },
    { name: "GSTR-3B", desc: "Summary return - liability, ITC, RCM, cross-utilisation", soon: true },
    { name: "GSTR-2B Reconciliation", desc: "Purchase register vs GSTR-2B, vendor-wise", soon: true },
  ] },
  { category: "GSTR-1 Details", items: [
    { name: "B2B Invoices", desc: "Table 4A, 4B, 6B, 6C", href: "#/reports/gstr1?section=b2b" },
    { name: "B2C Summary", desc: "Table 7, net of B2C credit notes", href: "#/reports/gstr1?section=b2cs" },
    { name: "Credit / Debit Notes (Registered)", desc: "Table 9B CDNR", href: "#/reports/gstr1?section=cdnr" },
    { name: "HSN Summary", desc: "Table 12, B2B and B2C", href: "#/reports/gstr1?section=hsn_b2b" },
    { name: "Documents Issued", desc: "Table 13", href: "#/reports/gstr1?section=docs" },
  ] },
  { category: "Advances", items: [
    { name: "Advance Tax (11A / 11B)", desc: "Tax on advances received and set off on adjustment", href: "#/reports/advance-tax" },
    { name: "Advance Ledger", desc: "Every advance with adjustments, refunds and balance", href: "#/advances" },
  ] },
  { category: "Cost Centres", items: [
    { name: "GST by Cost Centre", desc: "Output tax, advance tax, ITC and net per branch / project", href: "#/reports/cost-centres" },
  ] },
  { category: "Data", items: [
    { name: "Import Status", desc: "What has been imported per GSTIN for the period", href: "#/dashboard" },
  ] },
];

async function renderReports() {
  main.innerHTML = `
    ${pageHead("Reports", { sub: orgLine(),
      actions: `<label class="search">${icon("search")}<input id="r-search" type="search" placeholder="Search reports"></label>` })}
    <div class="page-body"><div class="report-center" id="report-center"></div></div>`;
  const draw = (term) => {
    const t = term.trim().toLowerCase();
    $("#report-center").innerHTML = REPORTS.map((cat) => {
      const items = cat.items.filter((i) => !t || `${i.name} ${i.desc} ${cat.category}`.toLowerCase().includes(t));
      if (!items.length) return "";
      return `<section class="report-cat"><h3>${esc(cat.category)}</h3><ul>${items.map((i) => `<li>${i.soon
        ? `<span class="soon">${icon("chart")}<span>${esc(i.name)} <span class="pill">Coming soon</span><span class="desc">${esc(i.desc)}</span></span></span>`
        : `<a href="${i.href}">${icon("chart")}<span>${esc(i.name)}<span class="desc">${esc(i.desc)}</span></span></a>`}</li>`).join("")}</ul></section>`;
    }).join("") || `<p class="empty">No reports match "${esc(term)}".</p>`;
  };
  $("#r-search").addEventListener("input", (e) => draw(e.target.value));
  draw("");
}

function reportTitle(name, span = periodLabel(state.period)) {
  const g = org();
  return `<div class="report-title">
    <div class="org">${esc(g.entity_name)} · <span class="mono">${esc(g.gstin)}</span></div>
    <div class="name">${name}</div>
    <div class="for">For ${esc(span)}</div>
  </div>`;
}

const crumbs = (category) => `<a href="#/reports">Reports</a> › ${esc(category)}`;

// ---------------------------------------------------------------- GSTR-1 report

const TAX_COLS = [["Taxable", "txval"], ["IGST", "iamt"], ["CGST", "camt"], ["SGST", "samt"], ["Cess", "csamt"]];
const docCols = (num, date) => [["Number", (r) => `<span class="mono">${esc(r[num])}</span>`], ["Date", (r) => dateFmt(r[date])]];
const GSTR1_COLUMNS = {
  b2b: [["Recipient", (r) => `${esc(r.name)}<span class="small mono">${esc(r.ctin)}</span>`], ...docCols("inum", "idt"),
    ["POS", (r) => esc(r.pos)], ["Type", (r) => esc(r.inv_typ) + (r.rchrg === "Y" ? ` <span class="pill">RCM</span>` : "")],
    ["Value", "val"], ["Rate", "rt"]],
  b2cl: [...docCols("inum", "idt"), ["POS", (r) => esc(r.pos)], ["Value", "val"], ["Rate", "rt"]],
  b2cs: [["POS", (r) => esc(r.pos)], ["Supply", (r) => (r.sply_ty === "INTRA" ? "Intra-state" : "Inter-state")], ["Rate", "rt"]],
  exp: [["Export type", (r) => esc(r.exp_typ)], ...docCols("inum", "idt"), ["Value", "val"], ["Rate", "rt"]],
  cdnr: [["Recipient", (r) => `${esc(r.name)}<span class="small mono">${esc(r.ctin)}</span>`], ...docCols("nt_num", "nt_dt"),
    ["Note", (r) => (r.ntty === "C" ? "Credit" : "Debit")], ["POS", (r) => esc(r.pos)], ["Value", "val"], ["Rate", "rt"]],
  cdnur: [["UR type", (r) => esc(r.ur_typ)], ...docCols("nt_num", "nt_dt"), ["Note", (r) => (r.ntty === "C" ? "Credit" : "Debit")],
    ["POS", (r) => esc(r.pos)], ["Value", "val"], ["Rate", "rt"]],
  at: [["POS", (r) => esc(r.pos)], ["Rate", "rt"], ["Gross incl. GST", "gross"]],
  atadj: [["POS", (r) => esc(r.pos)], ["Rate", "rt"], ["Gross incl. GST", "gross"]],
  hsn_b2b: [["HSN/SAC", (r) => `<span class="mono">${esc(r.hsn || "(blank)")}</span>`], ["Description", (r) => esc(r.desc)], ["UQC", (r) => esc(r.uqc)],
    ["Qty", "qty"], ["Total value", "val"], ["Rate", "rt"]],
};
GSTR1_COLUMNS.hsn_b2c = GSTR1_COLUMNS.hsn_b2b;

function sectionTable(key, rows) {
  if (!rows.length) return `<p class="muted">Nothing in this table for the period.</p>`;
  if (key === "exemp") {
    return `<div class="table-wrap"><table><thead><tr><th>Description</th><th class="num">Nil rated</th><th class="num">Exempted</th><th class="num">Non-GST</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${esc(r.desc)}</td><td class="num">${money(r.nil)}</td><td class="num">${money(r.exempt)}</td><td class="num">${money(r.non_gst)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  if (key === "docs") {
    return `<div class="table-wrap"><table><thead><tr><th>Nature of document</th><th>From</th><th>To</th><th class="num">Total</th><th class="num">Cancelled</th></tr></thead>
      <tbody>${rows.map((r) => `<tr><td>${esc(r.nature)}</td><td class="mono">${esc(r.from)}</td><td class="mono">${esc(r.to)}</td><td class="num">${r.total}</td><td class="num">${r.cancelled}</td></tr>`).join("")}</tbody></table></div>`;
  }
  const cols = [...(GSTR1_COLUMNS[key] || []), ...TAX_COLS];
  const numeric = (c) => typeof c[1] === "string";
  const cell = (c, r) => (numeric(c) ? (c[1] === "rt" ? `${Number(r.rt)}%` : money(r[c[1]])) : c[1](r));
  return `<div class="table-wrap"><table>
    <thead><tr>${cols.map((c) => `<th class="${numeric(c) ? "num" : ""}">${c[0]}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${numeric(c) ? "num" : ""}">${cell(c, r)}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

async function renderGstr1Report(params) {
  const blocked = needOrg("GSTR-1");
  if (blocked) { main.innerHTML = blocked; return; }
  const g = await api(`/gstr1?gstin=${enc()}&period=${state.period}`);
  const open = g.sections[params.section] ? params.section : "";
  const L = g.liability;
  const current = g.summary.find((s) => s.key === open);

  main.innerHTML = `
    ${pageHead("GSTR-1", { crumbs: crumbs("GST Returns"), sub: orgLine(),
      actions: `${monthPicker()}<a class="btn secondary" href="/api/gstr1/export.xlsx?gstin=${enc()}&period=${state.period}">${icon("download")}Excel (offline tool)</a>
        <a class="btn" href="/api/gstr1/export.json?gstin=${enc()}&period=${state.period}" title="Upload on gst.gov.in: Returns > GSTR-1 > Prepare Offline > Upload">${icon("download")}JSON for GST portal</a>` })}
    <div class="page-body">
      <div class="banner blue">To upload: gst.gov.in &rarr; Returns &rarr; GSTR-1 for ${esc(periodLabel(state.period))} &rarr; <b>Prepare Offline</b> &rarr; Upload &rarr; choose the JSON.
        It loads as a draft only; review the summary on the portal and file from there.${g.warnings.length ? " <b>Resolve the items below first.</b>" : ""}</div>
      ${warningsCard(g.warnings)}
      <div class="report-paper">
        ${reportTitle("GSTR-1 - Details of Outward Supplies")}
        <div class="report-section">
          <section class="kpis" style="margin:0" aria-label="Tax from this return">
            <div class="tile"><div class="label">Tax payable from GSTR-1</div><div class="value">${rupees(L.tax)}</div>
              <div class="note">after credit notes and advance set-off; reverse charge excluded</div></div>
            <div class="tile"><div class="label">IGST</div><div class="value">${rupees(L.iamt)}</div></div>
            <div class="tile"><div class="label">CGST</div><div class="value">${rupees(L.camt)}</div></div>
            <div class="tile"><div class="label">SGST / UTGST</div><div class="value">${rupees(L.samt)}</div></div>
          </section>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Table</th><th>Section</th><th class="num">Records</th>${TAX_COLS.map(([h]) => `<th class="num">${h}</th>`).join("")}</tr></thead>
          <tbody>${g.summary.map((s) => `
            <tr class="${s.key === open ? "selected" : ""}">
              <td class="mono">${esc(s.table)}</td>
              <td><a href="#/reports/gstr1?section=${s.key}">${esc(s.title)}</a></td>
              <td class="num">${s.records || `<span class="muted">0</span>`}</td>
              ${TAX_COLS.map(([, k]) => `<td class="num">${Number(s[k]) ? money(s[k]) : `<span class="muted">-</span>`}</td>`).join("")}
            </tr>`).join("")}
          </tbody>
          <tfoot><tr><td colspan="3">Tax payable (net)</td><td class="num">${money(L.txval)}</td><td class="num">${money(L.iamt)}</td>
            <td class="num">${money(L.camt)}</td><td class="num">${money(L.samt)}</td><td class="num">${money(L.csamt)}</td></tr></tfoot>
        </table></div>
        ${current ? `<div class="report-section" id="section-detail">
          <h2>${esc(current.title)} <span class="muted">· Table ${esc(current.table)}</span></h2>
          ${sectionTable(open, g.sections[open])}
        </div>` : `<div class="report-section muted">Select a section to see its rows.</div>`}
      </div>
    </div>`;
  // Scroll to the rows only when a section was picked inside the report, not when arriving from elsewhere.
  if (state.prevRoute === "reports/gstr1") $("#section-detail")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// ---------------------------------------------------------------- Advance Tax report

function taxTable(rows) {
  if (!rows.length) return `<p class="muted">Nothing to report for this month.</p>`;
  const sum = (f) => rows.reduce((a, r) => a + Number(r[f]), 0);
  return `<div class="table-wrap"><table>
    <thead><tr><th>Place of supply</th><th>Supply</th><th class="num">Rate</th><th class="num">Gross (incl. GST)</th>
      <th class="num">Taxable value</th><th class="num">IGST</th><th class="num">CGST</th><th class="num">SGST</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td>${esc(r.place_of_supply)}</td><td>${r.supply_type === "INTRA" ? "Intra-state" : "Inter-state"}</td>
      <td class="num">${Number(r.rate)}%</td><td class="num">${money(r.amount)}</td><td class="num">${money(r.taxable_value)}</td>
      <td class="num">${money(r.igst)}</td><td class="num">${money(r.cgst)}</td><td class="num">${money(r.sgst)}</td></tr>`).join("")}
    </tbody>
    ${rows.length > 1 ? `<tfoot><tr><td colspan="3">Total</td><td class="num">${money(sum("amount"))}</td><td class="num">${money(sum("taxable_value"))}</td>
      <td class="num">${money(sum("igst"))}</td><td class="num">${money(sum("cgst"))}</td><td class="num">${money(sum("sgst"))}</td></tr></tfoot>` : ""}
  </table></div>`;
}

async function renderAdvanceTaxReport() {
  const blocked = needOrg("Advance Tax");
  if (blocked) { main.innerHTML = blocked; return; }
  const head = pageHead("Advance Tax (11A / 11B)", { crumbs: crumbs("Advances"), sub: orgLine(),
    actions: `${monthPicker()}<a class="btn secondary" href="#/advances">Advance ledger</a><a class="btn" href="/api/gstr1/export.xlsx?gstin=${enc()}&period=${state.period}">${icon("download")}Export GSTR-1 Excel</a>` });
  if (!org().advance_tax_enabled) {
    main.innerHTML = `${head}<div class="page-body"><div class="banner">GST on advances is off for this GSTIN. Turn it on under <a href="#/advances">Advances</a>.</div></div>`;
    return;
  }
  const r = await api(`/advances/report?gstin=${enc()}&period=${state.period}`);
  main.innerHTML = `${head}
    <div class="page-body">
      ${warningsCard(r.warnings)}
      <div class="report-paper">
        ${reportTitle("Advance Tax - GSTR-1 Tables 11A / 11B")}
        <div class="report-section">
          <div class="table-wrap"><table>
            <tbody>
              <tr><td>Advances received this month (incl. GST)</td><td class="num">${money(r.received.amount)}</td></tr>
              <tr><td>Less: adjusted against demands within the month</td><td class="num">${money(r.adjusted_same_month.amount)}</td></tr>
              <tr><td><strong>Table 11A - tax on advances unadjusted at month-end</strong></td><td class="num"><strong>${money(r.tax_11a)}</strong></td></tr>
              <tr><td><strong>Table 11B - tax set off on earlier advances adjusted / refunded</strong> (refunds ${money(r.refunded)})</td><td class="num"><strong>${money(r.tax_11b)}</strong></td></tr>
            </tbody>
            <tfoot><tr><td>Net tax on advances (11A - 11B)</td><td class="num">${money(r.net_tax)}</td></tr></tfoot>
          </table></div>
        </div>
        <div class="report-section"><h2>Table 11A(1), 11A(2) - Advances received, not adjusted</h2>${taxTable(r.table_11a)}</div>
        <div class="report-section"><h2>Table 11B(1), 11B(2) - Advances adjusted / refunded</h2>${taxTable(r.table_11b)}</div>
        <div class="report-section muted">Unadjusted advances carried forward: ${rupees(r.closing_balance.amount)} (tax already paid ${rupees(Number(r.closing_balance.igst) + Number(r.closing_balance.cgst) + Number(r.closing_balance.sgst))}).</div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- GST by Cost Centre report

async function renderCostCentreReport() {
  const blocked = needOrg("GST by Cost Centre");
  if (blocked) { main.innerHTML = blocked; return; }
  const r = await api(`/reports/cost-centres?gstin=${enc()}&${rangeQuery()}`);
  const span = rangeLabel(state.range);
  const adv = r.advances_enabled;
  const cols = [["Sales docs", "outward_docs", true], ["Outward taxable", "outward_taxable"], ["Output tax", "output_tax"],
    ...(adv ? [["Advance tax 11A", "advance_11a"], ["Advance set-off 11B", "advance_11b"]] : []),
    ["Purchase docs", "inward_docs", true], ["ITC (books)", "itc"], ["Net GST", "net"]];
  const cell = (row, [, k, count]) => (count ? (row[k] ?? "") : money(row[k]));
  main.innerHTML = `
    ${pageHead("GST by Cost Centre", { crumbs: crumbs("Cost Centres"), sub: orgLine(span),
      actions: `${rangePicker()}<a class="btn secondary" href="#/entities">Manage cost centres</a><a class="btn" href="/api/reports/cost-centres.xlsx?gstin=${enc()}&${rangeQuery()}">${icon("download")}Export to Excel</a>` })}
    <div class="page-body">
      ${r.rows.length <= 1 && r.rows[0]?.name === "Unassigned" ? `<div class="banner blue">No cost centres set up for this GSTIN yet. Add them under <a href="#/entities">Entities</a> and include a Cost Centre column in your uploads.</div>` : ""}
      <div class="report-paper">
        ${reportTitle("GST by Cost Centre", span)}
        <div class="table-wrap"><table>
          <thead><tr><th>Cost centre</th>${cols.map(([h]) => `<th class="num">${h}</th>`).join("")}</tr></thead>
          <tbody>${r.rows.map((row) => `<tr>
            <td>${row.name === "Unassigned" ? `<span class="muted">Unassigned</span>` : `<a href="#/invoices?cc=${encodeURIComponent(row.name)}">${esc(row.name)}</a>`}
              ${row.code ? `<span class="small">${esc(row.code)}</span>` : ""}${row.active ? "" : ` <span class="pill">Inactive</span>`}</td>
            ${cols.map((c) => `<td class="num">${cell(row, c)}</td>`).join("")}</tr>`).join("")}
          </tbody>
          <tfoot><tr><td>Total (GSTIN)</td>${cols.map(([, k, count]) => `<td class="num">${count ? "" : money(r.totals[k])}</td>`).join("")}</tr></tfoot>
        </table></div>
        <div class="report-section muted">Net GST = output tax${adv ? " + advance tax (11A) - advance set-off (11B)" : ""} - eligible ITC per books, per cost centre.
          Indicative only: the return and ITC cross-utilisation are at GSTIN level. Lines without a cost centre show as Unassigned, so rows add up to the GSTIN.</div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- entities

const REG_TYPES = { regular: "Regular", composition: "Composition", sez_unit: "SEZ unit", isd: "ISD", casual: "Casual" };

async function renderEntities(params) {
  const entities = await api("/entities?include_inactive=true");
  const allGstins = entities.flatMap((e) => e.gstins);
  const centreLists = await Promise.all(allGstins.map((g) => api(`/gstins/${g.id}/cost-centres`)));
  const centres = Object.fromEntries(allGstins.map((g, i) => [g.id, centreLists[i]]));
  main.innerHTML = `
    ${pageHead("Entities &amp; GSTINs", { sub: "Inactive entities and GSTINs are hidden from the dashboard, uploads and reports",
      actions: `<button class="btn" id="new-entity">+ New entity</button>` })}
    <div class="page-body">
      <form class="card" id="entity-form" ${params.new || !entities.length ? "" : "hidden"}>
        <h2>New entity</h2>
        <div class="form-grid">
          <label class="field">Name<input name="name" required placeholder="e.g. Alcove Mall Road LLP"></label>
          <label class="field">PAN (optional - taken from the first GSTIN)<input name="pan" maxlength="10" style="text-transform:uppercase"></label>
          <label class="field">Notes<input name="notes"></label>
          <div class="actions"><button class="btn" type="submit">Save</button><button class="btn secondary" type="button" id="cancel-entity">Cancel</button></div>
        </div>
      </form>
      ${entities.map((e) => entityCard(e, centres)).join("") || `<p class="empty">No entities yet.</p>`}
    </div>`;

  const form = $("#entity-form");
  $("#new-entity").addEventListener("click", () => { form.hidden = false; form.elements.name.focus(); });
  $("#cancel-entity").addEventListener("click", () => { form.hidden = true; });
  form.addEventListener("submit", guard(async () => {
    await postJson("/entities", { name: form.elements.name.value.trim(), pan: form.pan.value.trim() || null, notes: form.notes.value.trim() || null });
    toast("Entity added");
    await refreshEntities();
  }));
  main.querySelectorAll("form[data-gstin-for]").forEach((f) => f.addEventListener("submit", guard(async () => {
    await postJson(`/entities/${f.dataset.gstinFor}/gstins`, {
      gstin: f.gstin.value.trim(), trade_name: f.trade_name.value.trim() || null,
      legal_name: f.legal_name.value.trim() || null, registration_type: f.registration_type.value,
    });
    toast("GSTIN added");
    await refreshEntities();
  })));
  main.querySelectorAll("[data-toggle-entity]").forEach((b) => b.addEventListener("click", guard(async () => {
    await postJson(`/entities/${b.dataset.toggleEntity}`, { active: b.dataset.active !== "true" }, "PATCH");
    await refreshEntities();
  })));
  main.querySelectorAll("[data-toggle-gstin]").forEach((b) => b.addEventListener("click", guard(async () => {
    await postJson(`/gstins/${b.dataset.toggleGstin}`, { active: b.dataset.active !== "true" }, "PATCH");
    await refreshEntities();
  })));
  main.querySelectorAll("form[data-cc-for]").forEach((f) => f.addEventListener("submit", guard(async () => {
    await postJson(`/gstins/${f.dataset.ccFor}/cost-centres`, { name: f.elements.name.value, code: f.elements.code.value.trim() || null });
    toast("Cost centre added");
    await refreshEntities();
  })));
  main.querySelectorAll("[data-toggle-cc]").forEach((b) => b.addEventListener("click", guard(async () => {
    const on = b.dataset.active === "true";
    if (on && !confirm(`Deactivate cost centre "${b.dataset.name}"? Uploads naming it will be rejected; existing data is kept.`)) return;
    await postJson(`/cost-centres/${b.dataset.toggleCc}`, { active: !on }, "PATCH");
    await refreshEntities();
  })));
}

async function refreshEntities() {
  await loadGstins();
  await renderEntities({});
}

function entityCard(e, centres) {
  return `<section class="card flush">
    <div class="card-head">
      <div><h2>${esc(e.name)} ${e.active ? "" : `<span class="pill">Inactive</span>`}</h2>
        <span class="muted">PAN ${esc(e.pan || "not set")}${e.notes ? " · " + esc(e.notes) : ""}</span></div>
      <div class="actions"><button class="btn secondary small" data-toggle-entity="${e.id}" data-active="${e.active}">${e.active ? "Deactivate" : "Activate"}</button></div>
    </div>
    ${e.gstins.length ? `<div class="table-wrap"><table>
      <thead><tr><th>GSTIN</th><th>State</th><th>Trade name</th><th>Registration</th><th>GST on advances</th><th>Status</th><th></th></tr></thead>
      <tbody>${e.gstins.map((g) => `
        <tr>
          <td class="mono">${esc(g.gstin)}</td>
          <td>${esc(g.state_code)}</td>
          <td>${esc(g.trade_name || g.legal_name || "")}</td>
          <td>${REG_TYPES[g.registration_type] || esc(g.registration_type)}</td>
          <td>${g.advance_tax_enabled ? `<span class="st st-good">On</span>` : `<span class="st st-missing">Off</span>`}</td>
          <td>${g.active ? `<span class="st st-good">Active</span>` : `<span class="st st-missing">Inactive</span>`}</td>
          <td class="num"><button class="btn ghost small" data-toggle-gstin="${g.id}" data-active="${g.active}">${g.active ? "Deactivate" : "Activate"}</button></td>
        </tr>
        <tr class="sub-row"><td colspan="7"><div class="cc-line">
          <span class="muted">Cost centres / branches</span>
          ${(centres[g.id] || []).map((c) => `<button class="chip ${c.active ? "" : "off"}" data-toggle-cc="${c.id}" data-active="${c.active}" data-name="${esc(c.name)}"
              title="${c.active ? "Click to deactivate" : "Inactive - click to activate"}">${esc(c.name)}${c.code ? ` <span class="muted">${esc(c.code)}</span>` : ""}</button>`).join("")
            || `<span class="muted">none yet</span>`}
          <form class="cc-add" data-cc-for="${g.id}">
            <input name="name" required maxlength="100" placeholder="New cost centre, e.g. Tower A" aria-label="New cost centre name">
            <input name="code" maxlength="30" placeholder="Code" aria-label="Code" style="width:80px">
            <button class="btn secondary small" type="submit">Add</button>
          </form>
        </div></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="empty">No GSTINs yet.</p>`}
    <div class="card-body">
      <details class="inline" ${e.gstins.length ? "" : "open"} style="margin:0">
        <summary style="margin:0">+ Add a GSTIN</summary>
        <form data-gstin-for="${e.id}">
          <div class="form-grid">
            <label class="field">GSTIN<input name="gstin" required maxlength="15" style="text-transform:uppercase"></label>
            <label class="field">Trade name<input name="trade_name"></label>
            <label class="field">Legal name<input name="legal_name"></label>
            <label class="field">Registration<select name="registration_type">${Object.entries(REG_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
            <div><button class="btn" type="submit">Save</button></div>
          </div>
        </form>
      </details>
    </div>
  </section>`;
}

// ---------------------------------------------------------------- boot

// ---------------------------------------------------------------- sign-in, user menu, users, account

function showAuth(mode, message = "") {
  state.user = null;
  $("#shell").hidden = true;
  const box = $("#auth");
  box.hidden = false;
  const setup = mode === "setup";
  box.innerHTML = `<div class="auth-card">
    <div class="auth-brand"><span class="auth-logo">${icon("globe")}</span><span>Alcove <b>GST</b></span></div>
    <h1>${setup ? "Create the administrator account" : "Sign in"}</h1>
    <p class="auth-sub">${setup ? "This is the first sign-in on this installation. This account can add other users." : "to continue to Alcove GST"}</p>
    ${message ? `<div class="auth-msg">${esc(message)}</div>` : ""}
    <form id="auth-form" novalidate>
      ${setup ? `<label class="field">Full name<input name="name" required autocomplete="name"></label>` : ""}
      <label class="field">Email address<input name="email" type="email" required autocomplete="username"></label>
      <label class="field">Password<span class="pw"><input name="password" type="password" required autocomplete="${setup ? "new-password" : "current-password"}"><button type="button" class="pw-toggle">Show</button></span></label>
      ${setup ? `<label class="field">Confirm password<input name="confirm" type="password" required autocomplete="new-password"></label>
        <p class="auth-hint">At least 8 characters, mixing letters with numbers or symbols.</p>` : ""}
      <div class="auth-error" id="auth-error" role="alert"></div>
      <button class="btn auth-submit" type="submit">${setup ? "Create account and sign in" : "Sign in"}</button>
    </form>
    ${setup ? "" : `<p class="auth-hint auth-foot">Forgot your password? Ask an administrator to reset it.</p>`}
  </div>`;
  const form = $("#auth-form");
  (setup ? form.elements.name : form.elements.email).focus();
  form.querySelector(".pw-toggle").addEventListener("click", (e) => {
    const input = form.elements.password;
    input.type = input.type === "password" ? "text" : "password";
    e.target.textContent = input.type === "password" ? "Show" : "Hide";
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const fail = (msg) => { $("#auth-error").textContent = msg; };
    if (!f.email.value.trim() || !f.password.value) return fail("Enter your email address and password.");
    if (setup && f.password.value !== f.confirm.value) return fail("The two passwords do not match.");
    const button = form.querySelector(".auth-submit");
    button.disabled = true;
    try {
      const body = { email: f.email.value, password: f.password.value, ...(setup ? { name: f.name.value } : {}) };
      const user = await postJson(setup ? "/auth/setup" : "/auth/login", body);
      await enterApp(user);
    } catch (err) {
      fail(err.message.charAt(0).toUpperCase() + err.message.slice(1));
    } finally {
      button.disabled = false;
    }
  });
}

async function enterApp(user) {
  state.user = user;
  $("#auth").hidden = true;
  $("#auth").innerHTML = "";
  $("#shell").hidden = false;
  drawUserMenu();
  await loadGstins();
  route();
}

function drawUserMenu() {
  const u = state.user;
  const initials = u.name.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  $("#user-slot").innerHTML = `<div class="dropdown">
    <button class="avatar" type="button" data-toggle="user-menu" title="${esc(u.name)}" aria-label="Account menu">${esc(initials)}</button>
    <div class="menu menu-right" id="user-menu" hidden>
      <div class="menu-user"><b>${esc(u.name)}</b><span>${esc(u.email)}</span>${u.is_admin ? `<span class="pill blue">Administrator</span>` : ""}</div>
      <a class="menu-item" href="#/account">Change password</a>
      ${u.is_admin ? `<a class="menu-item" href="#/users">Users</a>` : ""}
      <button class="menu-item" type="button" id="sign-out">Sign out</button>
    </div>
  </div>`;
  $("#sign-out").addEventListener("click", async () => {
    try { await api("/auth/logout", { method: "POST" }); } catch { /* already signed out */ }
    showAuth("login", "You have signed out.");
  });
}

async function renderAccount() {
  main.innerHTML = `
    ${pageHead("Change password", { sub: `${esc(state.user.name)} · ${esc(state.user.email)}` })}
    <div class="page-body"><form class="card" id="pw-form" style="max-width:520px">
      <label class="field">Current password<input name="current" type="password" required autocomplete="current-password"></label>
      <label class="field" style="margin-top:12px">New password<input name="next" type="password" required autocomplete="new-password"></label>
      <label class="field" style="margin-top:12px">Confirm new password<input name="confirm" type="password" required autocomplete="new-password"></label>
      <p class="muted" style="font-size:12.5px">At least 8 characters, mixing letters with numbers or symbols. Other devices are signed out.</p>
      <button class="btn" type="submit">Change password</button>
    </form></div>`;
  $("#pw-form").addEventListener("submit", guard(async (e) => {
    const f = e.target.elements;
    if (f.next.value !== f.confirm.value) throw new Error("The two new passwords do not match.");
    await postJson("/auth/password", { current_password: f.current.value, new_password: f.next.value });
    e.target.reset();
    toast("Password changed");
  }));
}

async function renderUsers() {
  if (!state.user.is_admin) {
    main.innerHTML = `${pageHead("Users")}<div class="page-body"><div class="card empty">Only an administrator can manage users.</div></div>`;
    return;
  }
  const users = await api("/users");
  main.innerHTML = `
    ${pageHead("Users", { sub: "People who can sign in to Alcove GST", actions: `<button class="btn" id="new-user">+ New user</button>` })}
    <div class="page-body">
      <form class="card" id="user-form" hidden>
        <h2>New user</h2>
        <div class="form-grid">
          <label class="field">Full name<input name="name" required></label>
          <label class="field">Email address<input name="email" type="email" required></label>
          <label class="field">Temporary password<input name="password" type="text" required autocomplete="off"></label>
          <label class="check"><input type="checkbox" name="is_admin"> Administrator</label>
          <div class="actions"><button class="btn" type="submit">Save</button><button class="btn secondary" type="button" id="cancel-user">Cancel</button></div>
        </div>
      </form>
      <section class="card flush"><div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead>
        <tbody>${users.map((u) => `<tr>
          <td>${esc(u.name)}${u.id === state.user.id ? ` <span class="pill blue">You</span>` : ""}</td>
          <td>${esc(u.email)}</td>
          <td>${u.is_admin ? "Administrator" : "User"}</td>
          <td>${u.active ? `<span class="st st-good">Active</span>` : `<span class="st st-missing">Inactive</span>`}</td>
          <td>${u.last_login_at ? timeFmt(u.last_login_at) : `<span class="muted">Never</span>`}</td>
          <td class="num">${u.id === state.user.id ? "" : `
            <button class="btn ghost small" data-user="${u.id}" data-set='${JSON.stringify({ is_admin: !u.is_admin })}'>${u.is_admin ? "Remove admin" : "Make admin"}</button>
            <button class="btn ghost small" data-user="${u.id}" data-reset="${esc(u.email)}">Reset password</button>
            <button class="btn ${u.active ? "danger" : "ghost"} small" data-user="${u.id}" data-set='${JSON.stringify({ active: !u.active })}'>${u.active ? "Deactivate" : "Activate"}</button>`}</td>
        </tr>`).join("")}</tbody></table></div></section>
    </div>`;
  const form = $("#user-form");
  $("#new-user").addEventListener("click", () => { form.hidden = false; form.elements.name.focus(); });
  $("#cancel-user").addEventListener("click", () => { form.hidden = true; });
  form.addEventListener("submit", guard(async () => {
    const f = form.elements;
    await postJson("/users", { name: f.name.value, email: f.email.value, password: f.password.value, is_admin: f.is_admin.checked });
    toast("User added - share the temporary password with them");
    await renderUsers();
  }));
  main.querySelectorAll("[data-set]").forEach((b) => b.addEventListener("click", guard(async () => {
    await postJson(`/users/${b.dataset.user}`, JSON.parse(b.dataset.set), "PATCH");
    await renderUsers();
  })));
  main.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", guard(async () => {
    const pw = prompt(`New temporary password for ${b.dataset.reset} (at least 8 characters, letters and numbers):`);
    if (!pw) return;
    await postJson(`/users/${b.dataset.user}`, { password: pw }, "PATCH");
    toast("Password reset - the user's sessions were signed out");
  })));
}

// ---------------------------------------------------------------- boot

async function boot() {
  // A tab opened before an update can hold the old page with this newer script; reload it once.
  if (!$("#shell") || !$("#auth")) {
    try {
      if (!sessionStorage.getItem("alcove-reloaded")) {
        sessionStorage.setItem("alcove-reloaded", "1");
        location.reload();
      }
    } catch { /* storage blocked: don't risk a reload loop; F5 fixes it */ }
    return;
  }
  try { sessionStorage.removeItem("alcove-reloaded"); } catch { /* storage unavailable */ }
  state.period = recall("period") || defaultPeriod();
  try { state.range = JSON.parse(recall("range") || "null"); } catch { state.range = null; }
  if (!state.range?.from) state.range = presetRange("last_month");
  else if (RANGE_PRESETS[state.range.preset]) state.range = presetRange(state.range.preset);  // keep "This Month" relative to today
  $("#org").addEventListener("change", (e) => {
    setGstin(e.target.value);
    const { name, params } = parseHash();
    delete params.gstin;  // drop any ?gstin= so the new choice sticks
    const next = `#/${name}${Object.keys(params).length ? "?" + new URLSearchParams(params) : ""}`;
    if (location.hash !== next) location.hash = next;  // hashchange re-renders
    else route();
  });
  $("#menu-btn").addEventListener("click", () => $("#rail").classList.toggle("open"));
  // Global search: open Transactions filtered by the term (current tab kept); "/" focuses it, as in Zoho.
  $("#global-search").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#global-q").value.trim();
    const { name, params } = parseHash();
    const view = name === "invoices" && params.view ? params.view : "outward";
    location.hash = `#/invoices?${new URLSearchParams({ view, ...(q ? { q } : {}) })}`;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !$("#shell").hidden && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName)) {
      e.preventDefault();
      $("#global-q").focus();
    }
  });
  window.addEventListener("hashchange", () => { if (state.user) route(); });

  let me;
  try {
    me = await api("/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      try {
        const status = await api("/auth/setup-status");
        showAuth(status.needs_setup ? "setup" : "login");
      } catch (e2) {
        showAuth("login", `Cannot reach the server: ${e2.message}`);
      }
      return;
    }
    $("#shell").hidden = false;
    main.innerHTML = `<div class="page-body"><div class="card result err"><h2>Cannot reach the server</h2><p>${esc(err.message)}</p></div></div>`;
    return;
  }
  await enterApp(me);
}

boot();
