"""
Marathon Printing Orders Dashboard Server
Serves the dashboard and proxies GoMake API calls server-side.

Setup:
    pip install fastapi uvicorn httpx python-dotenv

Run:
    python3 server.py

Then open http://localhost:8000 in your browser.
"""

import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

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
  <title>Marathon Printing Orders Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; color: #1a202c; min-height: 100vh; }
    header { background: #2e3092; color: white; padding: 16px 32px; display: flex; align-items: center; justify-content: space-between; }
    header h1 { font-size: 40px; font-weight: 600; }
    header .subtitle { font-size: 26px; opacity: 0.75; margin-top: 2px; }
    .status-bar { font-size: 24px; opacity: 0.85; text-align: right; }
    main { width: 100%; margin: 32px auto; padding: 0 24px; box-sizing: border-box; }
    .card { background: white; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 24px; margin-bottom: 24px; }
    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; justify-content: space-between; }
    .toolbar-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .badge { background: #ede9fe; color: #2e3092; font-size: 24px; font-weight: 600; padding: 2px 8px; border-radius: 999px; margin-left: 6px; }
    .field-sm { display: flex; flex-direction: column; }
    .field-sm label { font-size: 24px; font-weight: 600; color: #4a5568; margin-bottom: 4px; }
    .field-sm select { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 26px; background: white; outline: none; }
    button { padding: 8px 18px; border-radius: 6px; font-size: 26px; font-weight: 600; cursor: pointer; border: none; transition: background 0.2s, opacity 0.2s; }
    .btn-primary { background: #2e3092; color: white; }
    .btn-primary:hover { background: #252880; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-secondary { background: #e5e7eb; color: #374151; }
    .btn-secondary:hover { background: #d1d5db; }
    .search-box { border: 1px solid #d1d5db; border-radius: 6px; padding: 7px 12px; font-size: 26px; width: 220px; outline: none; }
    .search-box:focus { border-color: #2e3092; }
    table { width: 100%; border-collapse: collapse; }
    thead th { text-align: left; font-size: 24px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; padding: 10px 16px; border-bottom: 2px solid #e5e7eb; cursor: pointer; user-select: none; }
    thead th:hover { color: #2e3092; }
    tbody tr { border-bottom: 1px solid #f3f4f6; transition: background 0.1s; }
    tbody tr:hover { background: #f8faff; }
    tbody td { padding: 12px 16px; font-size: 28px; }
    .order-num { font-weight: 600; color: #2e3092; font-family: monospace; }
    .due-badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 24px; font-weight: 600; }
    .due-overdue { background: #fee2e2; color: #dc2626; }
    .due-soon    { background: #fef3c7; color: #d97706; }
    .due-ok      { background: #dcfce7; color: #16a34a; }
    .due-none    { background: #f3f4f6; color: #6b7280; }
    .state { text-align: center; padding: 48px 24px; color: #6b7280; font-size: 28px; }
    .state .icon { font-size: 32px; margin-bottom: 8px; }
    .error-msg { color: #dc2626; }
    .spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid #e5e7eb; border-top-color: #2e3092; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .legend { margin-top: 14px; font-size: 24px; color: #9ca3af; padding: 0 4px; }
    .summary { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
    .stat-card { background: white; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 20px 28px; flex: 1; min-width: 200px; border-top: 4px solid #e5e7eb; }
    .stat-card.due { border-top-color: #d97706; }
    .stat-card.done { border-top-color: #16a34a; }
    .stat-label { font-size: 24px; color: #6b7280; font-weight: 600; margin-bottom: 6px; }
    .stat-value { font-size: 48px; font-weight: 700; color: #1a202c; }
    .stat-card.due .stat-value { color: #d97706; }
    .stat-card.done .stat-value { color: #16a34a; }
  </style>
</head>
<body>
<header>
  <div>
    <h1>Marathon Printing Orders Dashboard</h1>
    <div class="subtitle">Sales orders · order numbers &amp; due dates</div>
  </div>
  <div class="status-bar" id="statusBar">Loading…</div>
</header>
<main>
  <div class="summary">
    <div class="stat-card due">
      <div class="stat-label">Due Today</div>
      <div class="stat-value" id="dueTodayCount">—</div>
    </div>
    <div class="stat-card done">
      <div class="stat-label">Closed / Shipped Today</div>
      <div class="stat-value" id="doneTodayCount">—</div>
    </div>
  </div>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong style="font-size:30px">Sales Orders <span class="badge" id="orderCount">0</span></strong>
        <input class="search-box" type="search" id="searchBox" placeholder="Search orders…" oninput="renderTable()" />
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
      <div class="state"><span class="spinner"></span> Fetching orders…</div>
    </div>
  </div>
</main>
<script>
  let allOrders = [];
  let sortCol = "due_date";
  let sortAsc = true;
  let refreshTimer = null;

  async function fetchPage(page) {
    const res = await fetch('/api/orders?page=' + page);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || res.statusText);
    }
    return res.json();
  }

  function extractOrders(data) {
    // GoMake shape: { success, data: { data: [...orders], totalItems } }
    if (data && data.data && Array.isArray(data.data.data)) return data.data.data;
    if (data && Array.isArray(data.data)) return data.data;
    if (data && Array.isArray(data.orders)) return data.orders;
    if (Array.isArray(data)) return data;
    return [];
  }

  function extractFields(order) {
    const orderNumber = order.number || order.orderNumber || order.orderId || order.id || "";
    const rawDate = order.dueDate || order.due_date || order.deliveryDate || order.requiredDate || "";
    let dueDate = String(rawDate);
    if (dueDate.includes("T")) {
      try { dueDate = new Date(rawDate).toISOString().split("T")[0]; } catch(e) {}
    }
    const worksNames = String(order.worksNames || "").replace(/,\\s*$/,"").trim();
    const notes = String(order.notes || "").trim();
    return { order_number: String(orderNumber), due_date: dueDate, works_names: worksNames, notes: notes };
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

      // Summary counts
      const todayStr = new Date().toISOString().split("T")[0];
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
    } catch (err) {
      showError("Error: " + err.message);
      document.getElementById("statusBar").textContent = "Error";
    } finally {
      setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = allOrders.filter(r =>
      r.order_number.toLowerCase().includes(query) || r.due_date.toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      // Primary: due_date ascending
      const dateCmp = (a.due_date || "").localeCompare(b.due_date || "");
      if (dateCmp !== 0) return dateCmp;
      // Secondary: notes ascending
      return (a.notes || "").localeCompare(b.notes || "");
    });
    document.getElementById("orderCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No orders found.</div>';
      return;
    }
    const today = new Date(); today.setHours(0,0,0,0);
    const soon = new Date(today); soon.setDate(soon.getDate() + 3);
    const tBody = rows.map(r => {
      let cls = "due-none", label = r.due_date || "—";
      if (r.due_date) {
        const d = new Date(r.due_date);
        if (!isNaN(d)) {
          if (d < today)     { cls = "due-overdue"; label = "&#9888; " + r.due_date; }
          else if (d <= soon){ cls = "due-soon"; }
          else               { cls = "due-ok"; }
        }
      }
      return '<tr><td class="order-num">' + esc(r.order_number) + '</td><td><span class="due-badge ' + cls + '">' + label + '</span></td><td>' + esc(r.works_names) + '</td><td style="white-space:pre-line;color:#4a5568">' + esc(r.notes) + '</td></tr>';
    }).join("");
    document.getElementById("tableContainer").innerHTML =
      '<table><thead><tr>' +
      '<th data-col="order_number" onclick="sortBy(this.dataset.col)">Order Number ' + sortIcon("order_number") + '</th>' +
      '<th data-col="due_date" onclick="sortBy(this.dataset.col)">Due Date ' + sortIcon("due_date") + '</th>' +
      '<th data-col="works_names" onclick="sortBy(this.dataset.col)">Jobs ' + sortIcon("works_names") + '</th>' +
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
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
    const csv = [["Order Number","Due Date","Jobs","Notes"]].concat(allOrders.map(r => [r.order_number, r.due_date, r.works_names, r.notes]))
      .map(r => r.map(v => '"' + v + '"').join(",")).join("\\n");
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
      '<div class="state"><span class="spinner"></span> Fetching orders…</div>';
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


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


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
            "pageNumber": page + 1,  # GoMake uses 1-based page numbers
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
