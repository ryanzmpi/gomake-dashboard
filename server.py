"""
Marathon Printing Orders Dashboard Server
Serves the dashboard and proxies GoMake API calls server-side.

Setup:
    pip install fastapi uvicorn httpx python-dotenv

Run:
    python3 server.py

Then open http://localhost:8000 in your browser.
"""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

GOMAKE_BASE   = "https://api.gomake.net/api"
CLIENT_ID     = os.getenv("GOMAKE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOMAKE_CLIENT_SECRET", "")
PAGE_SIZE     = 100

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Orders Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; color: #1a202c; min-height: 100vh; }
    header { background: #22475A; color: white; padding: 16px 32px; display: flex; align-items: center; justify-content: space-between; }
    header .header-left { display: flex; align-items: center; gap: 20px; }
    header img.logo { height: 70px; width: auto; }
    header h1 { font-size: 36px; font-weight: 600; }
    .status-bar { font-size: 22px; opacity: 0.85; text-align: right; }
    nav { background: #1a3648; display: flex; gap: 4px; padding: 0 24px; }
    nav a { color: rgba(255,255,255,0.7); text-decoration: none; font-size: 23px; font-weight: 600; padding: 12px 20px; display: inline-block; border-bottom: 3px solid transparent; transition: color 0.15s, border-color 0.15s; }
    nav a:hover { color: white; }
    nav a.active { color: white; border-bottom-color: #E8572A; }
    main { width: 100%; margin: 32px auto; padding: 0 24px; box-sizing: border-box; }
    .card { background: white; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 24px; }
    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; justify-content: space-between; }
    .toolbar-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .badge { background: #ede9fe; color: #22475A; font-size: 22px; font-weight: 600; padding: 2px 8px; border-radius: 999px; margin-left: 6px; }
    .field-sm { display: flex; flex-direction: column; }
    .field-sm label { font-size: 22px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
    .field-sm select { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 23px; background: white; outline: none; }
    button { padding: 8px 18px; border-radius: 6px; font-size: 23px; font-weight: 600; cursor: pointer; border: none; transition: background 0.2s, opacity 0.2s; }
    .btn-primary { background: #22475A; color: white; }
    .btn-primary:hover { background: #1a3648; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-secondary { background: #e5e7eb; color: #374151; }
    .btn-secondary:hover { background: #d1d5db; }
    .search-box { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 23px; width: 220px; outline: none; }
    .search-box:focus { border-color: #22475A; }
    table { width: 100%; border-collapse: collapse; }
    thead th { text-align: left; font-size: 22px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; padding: 10px 16px; border-bottom: 2px solid #e5e7eb; cursor: pointer; user-select: none; }
    thead th:hover { color: #22475A; }
    thead th.no-sort { cursor: default; }
    thead th.no-sort:hover { color: #6b7280; }
    .section-header td { font-size: 22px; font-weight: 700; padding: 10px 16px; letter-spacing: 0.04em; text-transform: uppercase; border-bottom: none; }
    .section-overdue  td { background: #fee2e2; color: #dc2626; }
    .section-today    td { background: #fef3c7; color: #b45309; }
    .section-future   td { background: #dcfce7; color: #15803d; }
    tbody tr { border-bottom: 1px solid #f3f4f6; transition: background 0.1s; }
    tbody tr:hover { background: #f8faff; }
    tbody td { padding: 12px 16px; font-size: 25px; vertical-align: top; }
    .order-num { font-weight: 600; color: #22475A; font-family: monospace; }
    .due-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 22px; font-weight: 600; }
    .due-overdue { background: #fee2e2; color: #dc2626; }
    .due-soon    { background: #fef3c7; color: #d97706; }
    .due-ok      { background: #dcfce7; color: #16a34a; }
    .due-none    { background: #f3f4f6; color: #6b7280; }
    .state { text-align: center; padding: 48px 24px; color: #6b7280; font-size: 25px; }
    .state .icon { font-size: 29px; margin-bottom: 8px; }
    .error-msg { color: #dc2626; }
    .spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid #e5e7eb; border-top-color: #22475A; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .legend { margin-top: 14px; font-size: 22px; color: #9ca3af; padding: 0 4px; }
    .summary { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
    .stat-card { background: white; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 20px 28px; flex: 1; min-width: 200px; border-top: 4px solid #e5e7eb; }
    .stat-card.due { border-top-color: #d97706; }
    .stat-card.done { border-top-color: #16a34a; }
    .stat-label { font-size: 22px; color: #6b7280; font-weight: 600; margin-bottom: 6px; }
    .stat-value { font-size: 43px; font-weight: 700; color: #1a202c; }
    .stat-card.due .stat-value { color: #d97706; }
    .stat-card.done .stat-value { color: #16a34a; }
    .job-item { margin-bottom: 6px; }
    .job-item:last-child { margin-bottom: 0; }
    .job-item strong { display: block; font-size: 22px; color: #1a202c; margin-bottom: 3px; }
    .job-status-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 20px; font-weight: 600; margin-right: 4px; margin-bottom: 2px; }
    .status-loading { color: #9ca3af; font-size: 20px; font-style: italic; }
  </style>
</head>
<body>
<header>
  <div class="header-left">
    <img class="logo" src="/static/logo.png" alt="Logo" />
    <h1>Orders Dashboard</h1>
  </div>
  <div class="status-bar" id="statusBar">Loading&hellip;</div>
</header>
<nav>
  <a href="/" class="active">All Orders</a>
  <a href="/press">Press</a>
</nav>
<main>
  <div class="summary">
    <div class="stat-card due">
      <div class="stat-label">Due Today</div>
      <div class="stat-value" id="dueTodayCount">&mdash;</div>
    </div>
    <div class="stat-card done">
      <div class="stat-label">Closed / Shipped Today</div>
      <div class="stat-value" id="doneTodayCount">&mdash;</div>
    </div>
  </div>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong style="font-size: 27px">Sales Orders <span class="badge" id="orderCount">0</span></strong>
        <input class="search-box" type="search" id="searchBox" placeholder="Search orders&hellip;" oninput="renderTable()" />
      </div>
      <div class="toolbar-left">
        <div class="field-sm">
          <label for="refreshInterval">Auto-refresh</label>
          <select id="refreshInterval" onchange="scheduleRefresh()">
            <option value="0">Off</option>
            <option value="60" selected>Every 1 min</option>
            <option value="300">Every 5 min</option>
            <option value="600">Every 10 min</option>
            <option value="1800">Every 30 min</option>
          </select>
        </div>
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()" style="margin-top:18px">&#8635; Refresh</button>
        <button class="btn-secondary" onclick="downloadCSV()" style="margin-top:18px">&#8595; CSV</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let allOrders = [];
  let sortCol = "due_date";
  let sortAsc = true;
  let refreshTimer = null;
  let renderPending = false;

  async function fetchPage(page) {
    const res = await fetch('/api/orders?page=' + page);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    return res.json();
  }

  function extractOrders(data) {
    if (data && data.data && Array.isArray(data.data.data)) return data.data.data;
    if (data && Array.isArray(data.data)) return data.data;
    if (data && Array.isArray(data.orders)) return data.orders;
    if (Array.isArray(data)) return data;
    return [];
  }

  function extractFields(order) {
    const orderId = order.id || "";
    const orderNumber = order.number || order.orderNumber || order.orderId || "";
    const rawDate = order.dueDate || order.due_date || order.deliveryDate || order.requiredDate || "";
    let dueDate = String(rawDate).substring(0, 10);
    const worksNames = String(order.worksNames || "").replace(/,\\s*$/, "").trim();
    const notes = String(order.notes || "").trim();
    return { id: orderId, order_number: String(orderNumber), due_date: dueDate, works_names: worksNames, notes: notes, jobStatuses: null };
  }

  async function loadOrders() {
    setLoading(true);
    allOrders = [];
    const rawOrders = [];
    try {
      let page = 0;
      while (true) {
        const data = await fetchPage(page);
        const orders = extractOrders(data);
        if (!orders.length) break;
        rawOrders.push(...orders);
        const open = orders.filter(o =>
          !String(o.statusTitleText || "").toLowerCase().includes("closed") &&
          o.deliveryStatus !== 3
        );
        allOrders.push(...open.map(extractFields));
        if (orders.length < 100) break;
        page++;
      }

      const todayStr = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Los_Angeles'}).format(new Date());
      const dueToday = allOrders.filter(r => r.due_date === todayStr).length;
      const doneToday = rawOrders.filter(o => {
        const isClosed = String(o.statusTitleText || "").toLowerCase().includes("closed");
        const isDelivered = o.deliveryStatus === 3;
        const readyDate = String(o.readyDate || "").split("T")[0];
        return (isClosed || isDelivered) && readyDate === todayStr;
      }).length;
      document.getElementById("dueTodayCount").textContent = dueToday;
      document.getElementById("doneTodayCount").textContent = doneToday;

      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; ' + allOrders.length + ' orders &nbsp;&middot;&nbsp; Updated ' + now;

      renderTable();
      scheduleRefresh();
      loadJobStatuses();
    } catch (err) {
      showError("Error: " + err.message);
      document.getElementById("statusBar").textContent = "Error";
    } finally {
      setLoading(false);
    }
  }

  async function loadJobStatuses() {
    if (!allOrders.length) return;
    await Promise.all(allOrders.map(async (order, i) => {
      if (!order.id) return;
      try {
        const res = await fetch('/api/orders/' + order.id + '/jobs');
        if (res.ok) {
          allOrders[i].jobStatuses = await res.json();
          debouncedRender();
        }
      } catch (e) { /* ignore */ }
    }));
  }

  function debouncedRender() {
    if (renderPending) return;
    renderPending = true;
    setTimeout(() => { renderPending = false; renderTable(); }, 250);
  }

  function renderJobStatuses(jobStatuses) {
    if (jobStatuses === null) return '<span class="status-loading">Loading&hellip;</span>';
    if (!jobStatuses.length) return '<span style="color:#9ca3af">&#8212;</span>';
    return jobStatuses.map(function(job) {
      const badges = (job.jobStatuses || []).map(function(s) {
        const c = statusColor(s.status, s.currentStation);
        const label = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + label + '</span>';
      }).join('');
      return '<div class="job-item"><strong>' + esc(job.productName || '') + (job.quantity ? ' &times;' + job.quantity : '') + '</strong>' + (badges || '<span style="color:#9ca3af">no status</span>') + '</div>';
    }).join('');
  }

  function statusColor(status, station) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('wait'))                                   return {bg:'#f3f4f6', fg:'#6b7280'};
    if (s.includes('progress')||s.includes('active')||s.includes('run')) return {bg:'#dbeafe', fg:'#1d4ed8'};
    if (s.includes('done')||s.includes('complet')||s.includes('finish')) return {bg:'#dcfce7', fg:'#16a34a'};
    if (s.includes('hold')||s.includes('pause'))              return {bg:'#fef3c7', fg:'#d97706'};
    if (s.includes('cancel')||s.includes('error'))            return {bg:'#fee2e2', fg:'#dc2626'};
    return {bg:'#f3f4f6', fg:'#6b7280'};
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = allOrders.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      r.works_names.toLowerCase().includes(query) ||
      r.notes.toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const dateCmp = (a.due_date || "").localeCompare(b.due_date || "");
      if (dateCmp !== 0) return dateCmp;
      return (a.notes || "").localeCompare(b.notes || "");
    });
    document.getElementById("orderCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No orders found.</div>';
      return;
    }
    const todayPT = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Los_Angeles'}).format(new Date());
    const today = new Date(todayPT + 'T00:00:00');
    const soon = new Date(today); soon.setDate(soon.getDate() + 3);

    function rowDate(r) {
      if (!r.due_date) return null;
      const p = r.due_date.split('-');
      return new Date(parseInt(p[0]), parseInt(p[1])-1, parseInt(p[2]));
    }

    const overdue = rows.filter(r => { const d = rowDate(r); return d && d < today; });
    const todayRows = rows.filter(r => { const d = rowDate(r); return d && d.getTime() === today.getTime(); });
    const future = rows.filter(r => { const d = rowDate(r); return !d || d > today; });

    function buildRow(r) {
      let cls = "due-none", label = "&#8212;";
      if (r.due_date) {
        const parts = r.due_date.split('-');
        const displayDate = parts[1] + '-' + parts[2] + '-' + parts[0];
        label = displayDate;
        const d = rowDate(r);
        if (!isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.works_names) + '</td>' +
        '<td style="white-space:pre-line;color:#4a5568">' + esc(r.notes) + '</td>' +
        '<td>' + renderJobStatuses(r.jobStatuses) + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="5">' + label + ' (' + count + ')</td></tr>';
    }

    let tBody = '';
    if (overdue.length)   tBody += sectionHeader('section-overdue', '&#9888; Late / Past Due', overdue.length)   + overdue.map(buildRow).join('');
    if (todayRows.length) tBody += sectionHeader('section-today',   '&#128197; Due Today',     todayRows.length) + todayRows.map(buildRow).join('');
    if (future.length)    tBody += sectionHeader('section-future',  '&#128336; Upcoming',       future.length)    + future.map(buildRow).join('');
    document.getElementById("tableContainer").innerHTML =
      '<table><thead><tr>' +
      '<th data-col="order_number" onclick="sortBy(this.dataset.col)">Order # ' + sortIcon("order_number") + '</th>' +
      '<th data-col="due_date" onclick="sortBy(this.dataset.col)">Due Date ' + sortIcon("due_date") + '</th>' +
      '<th data-col="works_names" onclick="sortBy(this.dataset.col)">Jobs ' + sortIcon("works_names") + '</th>' +
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Job Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>' +
      '<div class="legend">&#127002; On time &nbsp; &#127601; Due within 3 days &nbsp; &#128308; Overdue &nbsp; &#11036; No date</div>';
  }

  function sortBy(col) {
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    renderTable();
  }
  function sortIcon(col) { return sortCol !== col ? "&#8597;" : (sortAsc ? "&#8593;" : "&#8595;"); }

  function downloadCSV() {
    if (!allOrders.length) { alert("No orders loaded yet."); return; }
    const csv = [["Order Number","Due Date","Jobs","Notes"]].concat(
      allOrders.map(r => [r.order_number, r.due_date, r.works_names, r.notes])
    ).map(r => r.map(v => '"' + String(v).replace(/"/g,'""') + '"').join(",")).join("\\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "gomake_orders_" + new Date().toISOString().split("T")[0] + ".csv";
    a.click();
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    const interval = parseInt(document.getElementById("refreshInterval").value);
    if (!interval) return;
    refreshTimer = setTimeout(loadOrders, interval * 1000);
  }

  function setLoading(on) {
    const btn = document.getElementById("refreshBtn");
    btn.disabled = on;
    btn.textContent = on ? "Loading…" : "↻ Refresh";
    if (on) document.getElementById("tableContainer").innerHTML =
      '<div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>';
  }

  function showError(msg) {
    document.getElementById("tableContainer").innerHTML =
      '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(msg) + '</div>';
  }

  function esc(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  loadOrders();
</script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not CLIENT_ID or not CLIENT_SECRET:
        print("\n⚠️  Warning: GOMAKE_CLIENT_ID or GOMAKE_CLIENT_SECRET not set.")
        print("   Make sure your .env file has both values.\n")
    else:
        print(f"\n✓ GoMake credentials loaded (client ID: {CLIENT_ID[:6]}…)\n")
    yield


app = FastAPI(title="Marathon Printing Orders Dashboard", lifespan=lifespan)

# Serve logo and other static assets if the static folder exists
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


PRESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Press Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; color: #1a202c; min-height: 100vh; }
    header { background: #22475A; color: white; padding: 16px 32px; display: flex; align-items: center; justify-content: space-between; }
    header .header-left { display: flex; align-items: center; gap: 20px; }
    header img.logo { height: 70px; width: auto; }
    header h1 { font-size: 36px; font-weight: 600; }
    .status-bar { font-size: 22px; opacity: 0.85; text-align: right; }
    nav { background: #1a3648; display: flex; gap: 4px; padding: 0 24px; }
    nav a { color: rgba(255,255,255,0.7); text-decoration: none; font-size: 23px; font-weight: 600; padding: 12px 20px; display: inline-block; border-bottom: 3px solid transparent; transition: color 0.15s, border-color 0.15s; }
    nav a:hover { color: white; }
    nav a.active { color: white; border-bottom-color: #E8572A; }
    main { width: 100%; margin: 32px auto; padding: 0 24px; box-sizing: border-box; }
    .card { background: white; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 24px; }
    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; justify-content: space-between; }
    .toolbar-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .badge { background: #ede9fe; color: #22475A; font-size: 22px; font-weight: 600; padding: 2px 8px; border-radius: 999px; margin-left: 6px; }
    .field-sm { display: flex; flex-direction: column; }
    .field-sm label { font-size: 22px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
    .field-sm select { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 23px; background: white; outline: none; }
    button { padding: 8px 18px; border-radius: 6px; font-size: 23px; font-weight: 600; cursor: pointer; border: none; transition: background 0.2s, opacity 0.2s; }
    .btn-primary { background: #22475A; color: white; }
    .btn-primary:hover { background: #1a3648; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
    .search-box { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 23px; width: 220px; outline: none; }
    .search-box:focus { border-color: #22475A; }
    table { width: 100%; border-collapse: collapse; }
    thead th { text-align: left; font-size: 22px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; padding: 10px 16px; border-bottom: 2px solid #e5e7eb; cursor: pointer; user-select: none; }
    thead th:hover { color: #22475A; }
    thead th.no-sort { cursor: default; }
    thead th.no-sort:hover { color: #6b7280; }
    tbody tr { border-bottom: 1px solid #f3f4f6; transition: background 0.1s; }
    tbody tr:hover { background: #f8faff; }
    tbody td { padding: 12px 16px; font-size: 25px; vertical-align: top; }
    .order-num { font-weight: 600; color: #22475A; font-family: monospace; }
    .due-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 22px; font-weight: 600; }
    .due-overdue { background: #fee2e2; color: #dc2626; }
    .due-soon    { background: #fef3c7; color: #d97706; }
    .due-ok      { background: #dcfce7; color: #16a34a; }
    .due-none    { background: #f3f4f6; color: #6b7280; }
    .state { text-align: center; padding: 48px 24px; color: #6b7280; font-size: 25px; }
    .state .icon { font-size: 29px; margin-bottom: 8px; }
    .error-msg { color: #dc2626; }
    .spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid #e5e7eb; border-top-color: #22475A; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .job-status-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 20px; font-weight: 600; }
    .section-header td { font-size: 22px; font-weight: 700; padding: 10px 16px; letter-spacing: 0.04em; text-transform: uppercase; border-bottom: none; }
    .section-overdue td { background: #fee2e2; color: #dc2626; }
    .section-today   td { background: #fef3c7; color: #b45309; }
    .section-future  td { background: #dcfce7; color: #15803d; }
  </style>
</head>
<body>
<header>
  <div class="header-left">
    <img class="logo" src="/static/logo.png" alt="Logo" />
    <h1>Orders Dashboard</h1>
  </div>
  <div class="status-bar" id="statusBar">Loading&hellip;</div>
</header>
<nav>
  <a href="/">All Orders</a>
  <a href="/press" class="active">Press</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong style="font-size: 27px">Press Jobs <span class="badge" id="jobCount">0</span></strong>
        <input class="search-box" type="search" id="searchBox" placeholder="Search&hellip;" oninput="renderTable()" />
      </div>
      <div class="toolbar-left">
        <div class="field-sm">
          <label for="refreshInterval">Auto-refresh</label>
          <select id="refreshInterval" onchange="scheduleRefresh()">
            <option value="0">Off</option>
            <option value="60" selected>Every 1 min</option>
            <option value="300">Every 5 min</option>
            <option value="600">Every 10 min</option>
            <option value="1800">Every 30 min</option>
          </select>
        </div>
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()" style="margin-top:18px">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let pressJobs = [];
  let sortCol = "due_date";
  let sortAsc = true;
  let refreshTimer = null;

  async function fetchPage(page) {
    const res = await fetch('/api/orders?page=' + page);
    if (!res.ok) throw new Error((await res.json().catch(() => ({detail: res.statusText}))).detail);
    return res.json();
  }

  function extractOrders(data) {
    if (data && data.data && Array.isArray(data.data.data)) return data.data.data;
    if (data && Array.isArray(data.data)) return data.data;
    if (Array.isArray(data)) return data;
    return [];
  }

  function isPrinting(jobStatuses) {
    return (jobStatuses || []).some(s =>
      String(s.status || '').toLowerCase().includes('printing') ||
      String(s.currentStation || '').toLowerCase().includes('printing')
    );
  }

  async function loadOrders() {
    setLoading(true);
    pressJobs = [];
    const openOrders = [];
    try {
      let page = 0;
      while (true) {
        const data = await fetchPage(page);
        const orders = extractOrders(data);
        if (!orders.length) break;
        const open = orders.filter(o =>
          !String(o.statusTitleText || '').toLowerCase().includes('closed') &&
          o.deliveryStatus !== 3
        );
        openOrders.push(...open);
        if (orders.length < 100) break;
        page++;
      }

      document.getElementById("statusBar").textContent = 'Loading job statuses (0/' + openOrders.length + ')...';

      let loaded = 0;
      await Promise.all(openOrders.map(async (order) => {
        if (!order.id) return;
        try {
          const res = await fetch('/api/orders/' + order.id + '/jobs');
          if (res.ok) {
            const jobs = await res.json();
            // Collect only jobs that are currently printing
            for (const job of jobs) {
              if (isPrinting(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                pressJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = "Error";
    } finally {
      setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = pressJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Press.</div>';
      return;
    }

    const todayPT = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Los_Angeles'}).format(new Date());
    const today = new Date(todayPT + 'T00:00:00');
    const soon = new Date(today); soon.setDate(soon.getDate() + 3);

    function rowDate(r) {
      if (!r.due_date) return null;
      const p = r.due_date.split('-');
      return new Date(parseInt(p[0]), parseInt(p[1])-1, parseInt(p[2]));
    }

    const overdue   = rows.filter(r => { const d = rowDate(r); return d && d < today; });
    const todayRows = rows.filter(r => { const d = rowDate(r); return d && d.getTime() === today.getTime(); });
    const future    = rows.filter(r => { const d = rowDate(r); return !d || d > today; });

    function buildRow(r) {
      let cls = "due-none", label = "&#8212;";
      if (r.due_date) {
        const parts = r.due_date.split('-');
        const displayDate = parts[1] + '-' + parts[2] + '-' + parts[0];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="5">' + label + ' (' + count + ')</td></tr>';
    }

    let tBody = '';
    if (overdue.length)   tBody += sectionHeader('section-overdue', '&#9888; Late / Past Due', overdue.length)   + overdue.map(buildRow).join('');
    if (todayRows.length) tBody += sectionHeader('section-today',   '&#128197; Due Today',     todayRows.length) + todayRows.map(buildRow).join('');
    if (future.length)    tBody += sectionHeader('section-future',  '&#128336; Upcoming',       future.length)    + future.map(buildRow).join('');

    document.getElementById("tableContainer").innerHTML =
      '<table><thead><tr>' +
      '<th data-col="order_number" onclick="sortBy(this.dataset.col)">Order # ' + sortIcon("order_number") + '</th>' +
      '<th data-col="due_date" onclick="sortBy(this.dataset.col)">Due Date ' + sortIcon("due_date") + '</th>' +
      '<th data-col="product_name" onclick="sortBy(this.dataset.col)">Job Name ' + sortIcon("product_name") + '</th>' +
      '<th data-col="quantity" onclick="sortBy(this.dataset.col)">Qty ' + sortIcon("quantity") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('wait'))                                   return {bg:'#f3f4f6', fg:'#6b7280'};
    if (s.includes('progress')||s.includes('active')||s.includes('run')) return {bg:'#dbeafe', fg:'#1d4ed8'};
    if (s.includes('done')||s.includes('complet')||s.includes('finish')) return {bg:'#dcfce7', fg:'#16a34a'};
    if (s.includes('hold')||s.includes('pause'))              return {bg:'#fef3c7', fg:'#d97706'};
    if (s.includes('cancel')||s.includes('error'))            return {bg:'#fee2e2', fg:'#dc2626'};
    return {bg:'#f3f4f6', fg:'#6b7280'};
  }

  function sortBy(col) {
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    renderTable();
  }
  function sortIcon(col) { return sortCol !== col ? "&#8597;" : (sortAsc ? "&#8593;" : "&#8595;"); }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    const interval = parseInt(document.getElementById("refreshInterval").value);
    if (!interval) return;
    refreshTimer = setTimeout(loadOrders, interval * 1000);
  }

  function setLoading(on) {
    const btn = document.getElementById("refreshBtn");
    btn.disabled = on;
    btn.textContent = on ? "Loading…" : "↻ Refresh";
    if (on) document.getElementById("tableContainer").innerHTML =
      '<div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>';
  }

  function esc(s) {
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }

  loadOrders();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/press", response_class=HTMLResponse)
async def serve_press():
    return HTMLResponse(content=PRESS_HTML)


@app.get("/api/orders")
async def get_orders(page: int = Query(0, ge=0)):
    """Proxy a page of orders from GoMake (page is 0-based)."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")

    url = f"{GOMAKE_BASE}/sales-orders/list"
    headers = {
        "X-Client-Id": CLIENT_ID,
        "X-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    body = {
        "model": {
            "pageSize": PAGE_SIZE,
            "pageNumber": page + 1,
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"GoMake API error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")


@app.get("/api/orders/{order_id}/jobs")
async def get_order_jobs(order_id: str):
    """Fetch all job statuses for an order's document items."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")

    headers = {
        "X-Client-Id": CLIENT_ID,
        "X-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: get order detail to find documentItems
        try:
            resp = await client.get(f"{GOMAKE_BASE}/sales-orders/{order_id}", headers=headers)
            resp.raise_for_status()
            order_data = resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"GoMake API error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")

        document_items = order_data.get("data", {}).get("documentItems", [])

        # Step 2: fetch job statuses in parallel
        async def fetch_job_status(item):
            item_id = item.get("id")
            product_name = item.get("productName", "")
            quantity = item.get("quantity")
            if not item_id:
                return {"productName": product_name, "quantity": quantity, "jobStatuses": []}
            try:
                r = await client.get(f"{GOMAKE_BASE}/jobs/get-job-status/{item_id}", headers=headers)
                r.raise_for_status()
                data = r.json()
                return {
                    "productName": product_name,
                    "quantity": quantity,
                    "jobStatuses": data.get("data", {}).get("jobStatus", [])
                }
            except Exception:
                return {"productName": product_name, "quantity": quantity, "jobStatuses": []}

        results = await asyncio.gather(*[fetch_job_status(item) for item in document_items])
        return list(results)


@app.get("/api/debug/order/{order_number}")
async def debug_order(order_number: str):
    """Returns the full raw GoMake payload for a specific order number."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")

    url = f"{GOMAKE_BASE}/sales-orders/list"
    headers = {
        "X-Client-Id": CLIENT_ID,
        "X-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }

    for page_num in range(1, 20):
        body = {"model": {"pageSize": PAGE_SIZE, "pageNumber": page_num}}
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                orders = data.get("data", {}).get("data", [])
                if not orders:
                    break
                for o in orders:
                    num = str(o.get("number") or o.get("orderNumber") or o.get("id") or "")
                    if num == order_number:
                        return o
                if len(orders) < PAGE_SIZE:
                    break
            except httpx.HTTPStatusError as e:
                raise HTTPException(status_code=e.response.status_code,
                                    detail=f"GoMake API error: {e.response.text}")

    raise HTTPException(status_code=404, detail=f"Order {order_number} not found.")


@app.get("/api/debug/dates")
async def debug_dates():
    """Returns raw dueDate values for the first page of orders."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")

    url = f"{GOMAKE_BASE}/sales-orders/list"
    headers = {
        "X-Client-Id": CLIENT_ID,
        "X-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }
    body = {"model": {"pageSize": PAGE_SIZE, "pageNumber": 1}}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            orders = data.get("data", {}).get("data", [])
            return [
                {
                    "number": o.get("number") or o.get("orderNumber") or o.get("id"),
                    "rawDueDate": o.get("dueDate") or o.get("due_date") or o.get("deliveryDate"),
                    "status": o.get("statusTitleText"),
                    "deliveryStatus": o.get("deliveryStatus"),
                }
                for o in orders
            ]
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"GoMake API error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
