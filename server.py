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
import base64
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()

GOMAKE_BASE   = "https://api.gomake.net/api"
CLIENT_ID     = os.getenv("GOMAKE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOMAKE_CLIENT_SECRET", "")
PAGE_SIZE     = 100

GOMAKE_HEADERS = {
    "X-Client-Id":     CLIENT_ID,
    "X-Client-Secret": CLIENT_SECRET,
    "Content-Type":    "application/json",
}

# ── ShipStation credentials ───────────────────────────────────────────────────
SS_API_KEY    = os.getenv("SHIPSTATION_API_KEY", "")
SS_API_SECRET = os.getenv("SHIPSTATION_API_SECRET", "")
SS_BASE       = "https://ssapi.shipstation.com"
_ss_token     = base64.b64encode(f"{SS_API_KEY}:{SS_API_SECRET}".encode()).decode()
SS_HEADERS    = {
    "Authorization": f"Basic {_ss_token}",
    "Content-Type":  "application/json",
}

SYNC_INTERVAL = 5 * 60  # seconds between auto-syncs
_sync_state   = {"running": False, "last_run": None, "last_ok": 0, "last_skip": 0, "last_err": 0}

# ── Lookup tables ─────────────────────────────────────────────────────────────

COUNTRY_MAP = {
    "united states": "US", "united states of america": "US",
    "usa": "US", "u.s.a.": "US", "u.s.": "US",
    "canada": "CA", "mexico": "MX",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "australia": "AU", "new zealand": "NZ",
    "germany": "DE", "france": "FR",
}

STATE_MAP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


# ── Address helpers ───────────────────────────────────────────────────────────

def _country_code(raw: str) -> str:
    cleaned = (raw or "").strip()
    if len(cleaned) == 2:
        return cleaned.upper()
    return COUNTRY_MAP.get(cleaned.lower(), "US")


def _state_code(raw: str) -> str:
    cleaned = (raw or "").strip()
    if len(cleaned) == 2:
        return cleaned.upper()
    return STATE_MAP.get(cleaned.lower(), cleaned)


def _is_valid_street(s: str) -> bool:
    """Real street addresses contain at least one digit (house number)."""
    return bool(s) and any(c.isdigit() for c in s)


def _normalize_addr(addr: dict, fallback_name: str) -> dict:
    """Map GoMake address fields to ShipStation shipTo shape."""
    street1 = (addr.get("address1") or addr.get("street") or "").strip()
    street2 = (addr.get("address2") or addr.get("apartment") or addr.get("entry") or "").strip()
    return {
        "name":       (addr.get("name") or addr.get("addressName") or fallback_name).strip(),
        "street1":    street1,
        "street2":    street2,
        "city":       (addr.get("city") or "").strip(),
        "state":      _state_code(addr.get("state") or addr.get("stateCode") or ""),
        "postalCode": (addr.get("zipCode") or addr.get("zip") or addr.get("postalCode") or "").strip(),
        "country":    _country_code(addr.get("country") or addr.get("countryCode") or "US"),
        "phone":      (addr.get("phone") or addr.get("tel1") or "").strip(),
    }


def extract_shipping_address(customer: dict) -> Optional[dict]:
    """Returns the best shipping address from a GoMake customer record (type=0 preferred)."""
    customer_name = (customer.get("name") or customer.get("mainContactName") or "").strip()
    addresses = [a for a in (customer.get("addresses") or []) if a and a.get("isActive") is not False]
    shipping = [a for a in addresses if a.get("type") == 0]
    billing  = [a for a in addresses if a.get("type") == 1]
    other    = [a for a in addresses if a.get("type") not in (0, 1)]
    for group in (shipping, other, billing):
        for addr in group:
            if _is_valid_street(addr.get("address1") or ""):
                return _normalize_addr(addr, customer_name)
            if _is_valid_street(addr.get("address2") or ""):
                patched = dict(addr)
                patched["address1"] = addr.get("address2")
                patched["address2"] = addr.get("address3") or ""
                return _normalize_addr(patched, customer_name)
    for contact in customer.get("contacts") or []:
        raw = contact.get("address")
        if not raw:
            continue
        contact_name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip() or customer_name
        if isinstance(raw, str) and _is_valid_street(raw):
            return {"name": contact_name, "street1": raw.strip(), "street2": "",
                    "city": "", "state": "", "postalCode": "", "country": "US", "phone": ""}
        if isinstance(raw, dict) and _is_valid_street(raw.get("address1") or raw.get("street") or ""):
            return _normalize_addr(raw, contact_name)
    return None


def extract_order_delivery_address(order_detail: dict, contact_name: str) -> Optional[dict]:
    """Pull the ship-to address from documentDeliveryAddresses on the order detail."""
    for addr in order_detail.get("documentDeliveryAddresses") or []:
        if not addr:
            continue
        street1 = (addr.get("address1") or addr.get("street") or "").strip()
        street2 = (addr.get("address2") or addr.get("apartment") or "").strip()
        if not _is_valid_street(street1) and _is_valid_street(street2):
            street1, street2 = street2, (addr.get("address3") or "").strip()
        if not street1:
            continue
        return {
            "name":       contact_name,
            "street1":    street1,
            "street2":    street2,
            "city":       (addr.get("city") or "").strip(),
            "state":      _state_code(addr.get("state") or addr.get("stateCode") or ""),
            "postalCode": (addr.get("zipCode") or addr.get("zip") or addr.get("postalCode") or "").strip(),
            "country":    _country_code(addr.get("country") or "US"),
            "phone":      "",
        }
    return None


# ── Date helper ───────────────────────────────────────────────────────────────

def _to_iso(raw: str) -> Optional[str]:
    """Convert GoMake date strings to ISO 8601 for ShipStation."""
    if not raw:
        return None
    for fmt in (
        "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


# ── ShipStation sync helpers ──────────────────────────────────────────────────

def build_ss_order(order: dict, ship_to: dict, items: list, order_detail: dict = None) -> dict:
    order_number  = str(order.get("number") or order.get("orderNumber") or order.get("id") or "")
    order_date    = _to_iso(order.get("createdDate") or order.get("createDate") or "")
    company_name  = order.get("contactName") or order.get("customerName") or ""
    contact_phone = order.get("contactPhone") or ship_to.get("phone") or ""
    contact_email = order.get("contactEmail") or ""

    person_name = ""
    requested_service = ""
    if order_detail:
        contacts = order_detail.get("documentContacts") or []
        if contacts:
            person_name = (contacts[0].get("contactName") or "").strip()
            if not contact_phone:
                contact_phone = (contacts[0].get("contactPhone") or "").strip()
            if not contact_email:
                contact_email = (contacts[0].get("contactMail") or "").strip()

        # Extract carrier/service from the Delivery line item.
        # workName is cleanest; fall back to details (which may contain HTML).
        for doc_item in (order_detail.get("documentItems") or []):
            if str(doc_item.get("productName") or "").strip().lower() == "delivery":
                svc = (doc_item.get("workName") or "").strip()
                if not svc:
                    import re as _re
                    svc = _re.sub(r"<[^>]+>", "", doc_item.get("details") or "").strip()
                requested_service = svc
                break

    ship_to_final = dict(ship_to)
    ship_to_final["name"]    = person_name or company_name
    ship_to_final["company"] = company_name if person_name else ""

    ss_items = [
        {
            "lineItemKey": str(item.get("id") or ""),
            "name":        item.get("productName") or item.get("description") or "Print Job",
            "quantity":    int(item.get("quantity") or 1),
            "unitPrice":   float(item.get("unitPrice") or item.get("price") or 0),
        }
        for item in items
    ] or [
        {
            "lineItemKey": order_number,
            "name":        order.get("worksNames") or "Print Order",
            "quantity":    1,
            "unitPrice":   float(order.get("totalPrice") or 0),
        }
    ]

    address_block = {
        "name":       ship_to_final.get("name", ""),
        "company":    ship_to_final.get("company", ""),
        "street1":    ship_to_final.get("street1", ""),
        "street2":    ship_to_final.get("street2", ""),
        "city":       ship_to_final.get("city", ""),
        "state":      ship_to_final.get("state", ""),
        "postalCode": ship_to_final.get("postalCode", ""),
        "country":    ship_to_final.get("country", "US"),
        "phone":      contact_phone,
    }

    payload = {
        "orderNumber":   order_number,
        "orderKey":      order_number,
        "orderStatus":   "awaiting_shipment",
        "orderDate":     order_date,
        "customerEmail": contact_email,
        "billTo":        address_block,
        "shipTo":        address_block,
        "items":         ss_items,
        "internalNotes": order.get("notes") or "",
    }
    if requested_service:
        payload["requestedShippingService"] = requested_service
    return payload


async def _get_ss_tracking(client: httpx.AsyncClient, order_number: str) -> Optional[dict]:
    """Return the first ShipStation shipment with a tracking number for this order, or None."""
    try:
        r = await client.get(
            f"{SS_BASE}/shipments",
            headers=SS_HEADERS,
            params={"orderNumber": order_number},
            timeout=30,
        )
        r.raise_for_status()
        for s in r.json().get("shipments", []):
            tracking = (s.get("trackingNumber") or "").strip()
            if tracking:
                return {
                    "tracking":   tracking,
                    "carrier":    s.get("carrierCode") or "",
                    "ship_date":  (s.get("shipDate") or "")[:10],
                    "cost":       s.get("shipmentCost"),
                }
    except Exception:
        pass
    return None


async def _write_tracking_to_gomake(client: httpx.AsyncClient, order_id: str, current_notes: str, tracking: dict):
    """Append tracking info to GoMake order's internalNotes."""
    line = "Tracking: " + tracking["tracking"]
    if tracking.get("carrier"):
        line += " (" + tracking["carrier"] + ")"
    if tracking.get("ship_date"):
        line += " — shipped " + tracking["ship_date"]
    if tracking.get("cost") is not None:
        line += " — $" + f"{tracking['cost']:.2f}"
    new_notes = (current_notes.strip() + "\n" + line).strip() if current_notes.strip() else line
    await client.put(
        f"{GOMAKE_BASE}/sales-orders/{order_id}",
        headers=GOMAKE_HEADERS,
        json={"internalNotes": new_notes},
        timeout=30,
    )


async def _sync_all_orders() -> dict:
    """Fetch all open GoMake orders and upsert into ShipStation. Returns counts."""
    counts = {"ok": 0, "skipped": 0, "error": 0, "tracking_updated": 0}
    async with httpx.AsyncClient() as client:
        # Fetch open orders
        orders = []
        page = 1
        while True:
            resp = await client.post(
                f"{GOMAKE_BASE}/sales-orders/list",
                headers=GOMAKE_HEADERS,
                json={"model": {"pageSize": PAGE_SIZE, "pageNumber": page}},
                timeout=30,
            )
            resp.raise_for_status()
            page_data = resp.json().get("data", {}).get("data", [])
            if not page_data:
                break
            open_orders = [
                o for o in page_data
                if "closed" not in str(o.get("statusTitleText", "")).lower()
                and o.get("deliveryStatus") != 3
                and not o.get("readyDate")
            ]
            orders.extend(open_orders)
            if len(page_data) < PAGE_SIZE:
                break
            page += 1

        for order in orders:
            order_number = str(order.get("number") or order.get("orderNumber") or order.get("id") or "?")
            order_id     = order.get("id") or ""
            contact_name = order.get("contactName") or order.get("customerName") or ""

            order_detail: dict = {}
            items: list = []
            if order_id:
                try:
                    r = await client.get(
                        f"{GOMAKE_BASE}/sales-orders/{order_id}",
                        headers=GOMAKE_HEADERS, timeout=30,
                    )
                    r.raise_for_status()
                    order_detail = r.json().get("data", {})
                    items = order_detail.get("documentItems", [])
                except Exception:
                    pass

            # Skip Self Collection orders — only sync Delivery orders.
            # isHaveDeliveryItem can be unreliable; also check documentDeliveryAddresses
            # and whether any line item has productName "Delivery".
            has_delivery_flag = order_detail.get("isHaveDeliveryItem", False)
            has_delivery_address = bool(order_detail.get("documentDeliveryAddresses"))
            has_delivery_item = any(
                str(item.get("productName") or "").strip().lower() == "delivery"
                for item in (order_detail.get("documentItems") or [])
            )
            if not (has_delivery_flag or has_delivery_address or has_delivery_item):
                counts["skipped"] += 1
                continue

            ship_to = extract_order_delivery_address(order_detail, contact_name)
            if not ship_to:
                customer_id = order.get("customerId") or ""
                if customer_id:
                    try:
                        r = await client.get(
                            f"{GOMAKE_BASE}/customers/{customer_id}",
                            headers=GOMAKE_HEADERS, timeout=30,
                        )
                        r.raise_for_status()
                        customer = r.json().get("data", {})
                        ship_to  = extract_shipping_address(customer)
                    except Exception:
                        pass

            if not ship_to:
                counts["skipped"] += 1
                continue

            payload = build_ss_order(order, ship_to, items, order_detail=order_detail)
            try:
                r = await client.post(
                    f"{SS_BASE}/orders/createorder",
                    headers=SS_HEADERS, json=payload, timeout=30,
                )
                r.raise_for_status()
                counts["ok"] += 1
            except Exception:
                counts["error"] += 1

            # Reverse sync: pull tracking number from ShipStation → GoMake internalNotes
            if order_id:
                try:
                    tracking = await _get_ss_tracking(client, order_number)
                    if tracking:
                        current_notes = (order_detail.get("internalNotes") or "").strip()
                        if tracking["tracking"] not in current_notes:
                            await _write_tracking_to_gomake(client, order_id, current_notes, tracking)
                            counts["tracking_updated"] += 1
                except Exception:
                    pass

    return counts


async def _reverse_tracking_sync(client: httpx.AsyncClient) -> int:
    """
    Standalone reverse sync: query ShipStation for recent shipments and write
    tracking numbers back to GoMake internalNotes. Works on ALL GoMake orders
    (open and closed) so it catches orders that have already shipped.
    """
    updated = 0
    try:
        # Step 1: Build a map of GoMake order number → {id, internalNotes}
        # across ALL orders (no open/closed filter).
        gomake_map: dict = {}
        page = 1
        while True:
            resp = await client.post(
                f"{GOMAKE_BASE}/sales-orders/list",
                headers=GOMAKE_HEADERS,
                json={"model": {"pageSize": PAGE_SIZE, "pageNumber": page}},
                timeout=30,
            )
            resp.raise_for_status()
            orders = resp.json().get("data", {}).get("data", [])
            if not orders:
                break
            for o in orders:
                num = str(o.get("number") or o.get("orderNumber") or o.get("id") or "")
                if num:
                    gomake_map[num] = {
                        "id":    o.get("id") or "",
                        "notes": (o.get("internalNotes") or "").strip(),
                    }
            if len(orders) < PAGE_SIZE:
                break
            page += 1

        # Step 2: Fetch recent ShipStation shipments (last 30 days).
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        ss_page = 1
        while True:
            r = await client.get(
                f"{SS_BASE}/shipments",
                headers=SS_HEADERS,
                params={"shipDateStart": since, "page": ss_page, "pageSize": 100},
                timeout=30,
            )
            r.raise_for_status()
            shipments = r.json().get("shipments", [])
            if not shipments:
                break

            for s in shipments:
                tracking = (s.get("trackingNumber") or "").strip()
                order_number = str(s.get("orderNumber") or "").strip()
                if not tracking or not order_number:
                    continue
                gm = gomake_map.get(order_number)
                if not gm or not gm["id"]:
                    continue
                if tracking in gm["notes"]:
                    continue  # already written
                tracking_info = {
                    "tracking":  tracking,
                    "carrier":   s.get("carrierCode") or "",
                    "ship_date": (s.get("shipDate") or "")[:10],
                    "cost":      s.get("shipmentCost"),
                }
                try:
                    await _write_tracking_to_gomake(client, gm["id"], gm["notes"], tracking_info)
                    updated += 1
                except Exception:
                    pass

            if len(shipments) < 100:
                break
            ss_page += 1

    except Exception:
        pass
    return updated


async def run_sync():
    """Trigger a sync run, updating _sync_state."""
    if _sync_state["running"]:
        return {"status": "already_running"}
    _sync_state["running"] = True
    try:
        async with httpx.AsyncClient() as client:
            counts = await _sync_all_orders()
            tracking_updated = await _reverse_tracking_sync(client)
            counts["tracking_updated"] = counts.get("tracking_updated", 0) + tracking_updated
        _sync_state.update({
            "last_run":  datetime.utcnow().isoformat(),
            "last_ok":   counts["ok"],
            "last_skip": counts["skipped"],
            "last_err":  counts["error"],
            "last_tracking": counts.get("tracking_updated", 0),
        })
        return counts
    finally:
        _sync_state["running"] = False


async def _sync_loop():
    """Background task: sync every SYNC_INTERVAL seconds."""
    await asyncio.sleep(10)  # brief delay on startup
    while True:
        try:
            await run_sync()
        except Exception as e:
            print(f"[sync] error: {e}")
        await asyncio.sleep(SYNC_INTERVAL)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Orders Dashboard</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
  <style>:root { --td: 25px; }</style>
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="summary">
    <div class="stat-card created">
      <div class="stat-label">Created Today</div>
      <div class="stat-value" id="createdTodayCount">&mdash;</div>
      <div class="stat-sub" id="createdTodayTotal">&mdash;</div>
    </div>
    <div class="stat-card due">
      <div class="stat-label">Due Today</div>
      <div class="stat-value" id="dueTodayCount">&mdash;</div>
      <div class="stat-sub" id="dueTodayTotal">&mdash;</div>
    </div>
    <div class="stat-card done">
      <div class="stat-label">Closed / Shipped Today</div>
      <div class="stat-value" id="doneTodayCount">&mdash;</div>
      <div class="stat-sub" id="doneTodayTotal">&mdash;</div>
    </div>
  </div>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Sales Orders <span class="badge" id="orderCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
        <button class="btn-secondary" onclick="downloadCSV()">&#8595; CSV</button>
        <button class="btn-green" id="syncBtn" onclick="syncShipStation()">&#8644; Sync SS</button>
        <span id="syncStatus" class="sync-status"></span>
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

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    // Preserve existing job statuses so the column stays visible during silent refresh
    const savedStatuses = {};
    if (silent) {
      for (const o of allOrders) {
        if (o.id && o.jobStatuses !== null) savedStatuses[o.id] = o.jobStatuses;
      }
    }
    const newOrders = [];
    const rawOrders = [];
    try {
      const openCandidates = [];
      let page = 0;
      while (true) {
        const data = await fetchPage(page);
        const orders = extractOrders(data);
        if (!orders.length) break;
        rawOrders.push(...orders);
        const open = orders.filter(o =>
          !String(o.statusTitleText || "").toLowerCase().includes("closed") &&
          o.deliveryStatus !== 3 &&
          (o.dueDate || o.due_date || o.deliveryDate)
        );
        openCandidates.push(...open);
        if (orders.length < 100) break;
        page++;
      }

      // Orders with readyDate might be fully done — fetch their job statuses to check
      const definitelyOpen = openCandidates.filter(o => !o.readyDate);
      const mightBeDone   = openCandidates.filter(o =>  o.readyDate);

      const prefetchedStatuses = {};
      if (mightBeDone.length) {
        document.getElementById("statusBar").textContent = 'Verifying ' + mightBeDone.length + ' completed order(s)...';
        await Promise.all(mightBeDone.map(async (order) => {
          if (!order.id) return;
          try {
            const res = await fetch('/api/orders/' + order.id + '/jobs');
            if (res.ok) prefetchedStatuses[order.id] = await res.json();
          } catch (e) {}
        }));
      }

      function allJobsDone(jobs) {
        if (!jobs || !jobs.length) return false;
        return jobs.every(job => {
          const statuses = job.jobStatuses || [];
          if (!statuses.length) return true; // GoMake clears statuses when done
          return statuses.every(s => /done|complet|finish/i.test(String(s.status || '')));
        });
      }

      // Keep only mightBeDone orders where at least one job is still active
      const stillActive = mightBeDone.filter(o => !allJobsDone(prefetchedStatuses[o.id]));
      const finalOpen = [...definitelyOpen, ...stillActive];
      newOrders.push(...finalOpen.map(extractFields));

      // Pre-populate job statuses for orders we already fetched above
      for (const o of finalOpen) {
        if (prefetchedStatuses[o.id]) {
          const idx = newOrders.findIndex(no => no.id === o.id);
          if (idx >= 0) newOrders[idx].jobStatuses = prefetchedStatuses[o.id];
        }
      }

      const todayStr = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Los_Angeles'}).format(new Date());
      // createdDate from GoMake list API is "DD-MM-YYYY HH:MM" — reformat to YYYY-MM-DD for comparison
      function parseGoMakeCreatedDate(raw) {
        const s = String(raw || "").trim();
        if (!s) return "";
        const datePart = s.split(" ")[0];       // "29-07-2026"
        const p = datePart.split("-");           // ["29","07","2026"]
        if (p.length !== 3) return "";
        return p[2] + "-" + p[1] + "-" + p[0]; // "2026-07-29"
      }
      const createdTodayOrders = rawOrders.filter(o => parseGoMakeCreatedDate(o.createdDate) === todayStr);
      const createdToday = createdTodayOrders.length;
      const createdTodayTotal = createdTodayOrders.reduce((sum, o) => sum + parseFloat(o.totalPrice || o.totalPayment || 0), 0);
      const dueTodayOrders = rawOrders.filter(o => String(o.dueDate || o.due_date || "").substring(0, 10) === todayStr && !o.readyDate);
      const dueToday = dueTodayOrders.length;
      const dueTodayTotal = dueTodayOrders.reduce((sum, o) => sum + parseFloat(o.totalPrice || o.totalPayment || 0), 0);
      const doneTodayOrders = rawOrders.filter(o => {
        const readyDate = String(o.readyDate || "").split("T")[0];
        if (readyDate !== todayStr) return false;
        // If we fetched job statuses for this order, verify ALL jobs are truly done
        if (prefetchedStatuses[o.id] !== undefined) return allJobsDone(prefetchedStatuses[o.id]);
        return true; // formally closed orders without job status checks pass through
      });
      const doneToday = doneTodayOrders.length;
      const doneTodayTotal = doneTodayOrders.reduce((sum, o) => sum + parseFloat(o.totalPrice || o.totalPayment || 0), 0);
      document.getElementById("createdTodayCount").textContent = createdToday;
      document.getElementById("createdTodayTotal").textContent = '$' + createdTodayTotal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      document.getElementById("dueTodayCount").textContent = dueToday;
      document.getElementById("dueTodayTotal").textContent = '$' + dueTodayTotal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      document.getElementById("doneTodayCount").textContent = doneToday;
      document.getElementById("doneTodayTotal").textContent = '$' + doneTodayTotal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

      // Carry over saved job statuses so column stays populated during background refresh
      // (only where we don't already have freshly-fetched data)
      if (silent) {
        for (const o of newOrders) {
          if (o.id && o.jobStatuses === null && savedStatuses[o.id] !== undefined) {
            o.jobStatuses = savedStatuses[o.id];
          }
        }
      }
      allOrders = newOrders;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; ' + allOrders.length + ' orders &nbsp;&middot;&nbsp; Updated ' + now;

      renderTable();
      scheduleRefresh();
      loadJobStatuses();
      loadSyncStatus();
    } catch (err) {
      if (!silent) showError("Error: " + err.message);
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
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
    if (!jobStatuses.length) return '<span style="color:var(--muted)">&#8212;</span>';
    return jobStatuses.map(function(job) {
      const badges = (job.jobStatuses || []).map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const label = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + label + '</span>';
      }).join('');
      return '<div class="job-item"><strong>' + esc(job.productName || '') + (job.quantity ? ' &times;' + job.quantity : '') + '</strong>' + (badges || '<span style="color:var(--muted)">no status</span>') + '</div>';
    }).join('');
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
        const displayDate = parts[1] + '-' + parts[2];
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
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes) + '</td>' +
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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

  async function loadSyncStatus() {
    try {
      const res = await fetch('/api/sync/status');
      if (!res.ok) return;
      const s = await res.json();
      const el = document.getElementById("syncStatus");
      if (s.running) {
        el.textContent = "⟳ Syncing…";
      } else if (s.last_run) {
        const t = new Date(s.last_run + "Z").toLocaleTimeString();
        el.textContent = `Last sync ${t} · ✓${s.last_ok} ⊘${s.last_skip} ✗${s.last_err}`;
      } else {
        el.textContent = "Not synced yet";
      }
    } catch (e) { /* ignore */ }
  }

  async function syncShipStation() {
    const btn = document.getElementById("syncBtn");
    const el  = document.getElementById("syncStatus");
    btn.disabled = true;
    el.textContent = "⟳ Syncing…";
    try {
      const res = await fetch('/api/sync', { method: 'POST' });
      const data = await res.json();
      if (data.status === "already_running") {
        el.textContent = "Already running…";
      } else {
        el.textContent = `Done · ✓${data.ok} ⊘${data.skipped} ✗${data.error}`;
      }
    } catch (e) {
      el.textContent = "Sync error";
    } finally {
      btn.disabled = false;
    }
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
    if SS_API_KEY and SS_API_SECRET:
        print(f"✓ ShipStation credentials loaded — sync every {SYNC_INTERVAL//60} min\n")
        asyncio.create_task(_sync_loop())
    else:
        print("⚠️  SHIPSTATION_API_KEY / SHIPSTATION_API_SECRET not set — sync disabled\n")
    if CLIENT_ID and CLIENT_SECRET:
        asyncio.create_task(_cache_warm_loop())
        print(f"✓ Shared GoMake cache on — refresh every {CACHE_REFRESH} sec\n")
    yield


app = FastAPI(title="Marathon Printing Orders Dashboard", lifespan=lifespan)

# CORS: allow the apparel designer page (GitHub Pages) to call /api/quote
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your GitHub Pages URL if desired
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

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
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
  <style>:root { --td: 25px; }</style>
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press" class="active">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Press Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
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

  const EXCLUDED_MACHINES = ['hp latex', 'mimaki', 'screen printing'];
  function isExcludedMachine(jobStatuses) {
    return (jobStatuses || []).some(s =>
      EXCLUDED_MACHINES.some(m => String(s.machineName || '').toLowerCase().includes(m))
    );
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const savedJobs = silent ? pressJobs.slice() : [];
    const newJobs = [];
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
              if (isPrinting(job.jobStatuses) && !isExcludedMachine(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      pressJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = pressJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const machine = (r.jobStatuses.find(s => s.machineName) || {}).machineName || '';
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td>' + esc(machine) + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="7">' + label + ' (' + count + ')</td></tr>';
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
      '<th class="no-sort">Press</th>' +
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


PREPRESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Prepress / Graphics Jobs</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress" class="active">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Prepress / Graphics Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let prepressJobs = [];
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

  function isPrepress(jobStatuses) {
    return (jobStatuses || []).some(s =>
      String(s.status || '').toLowerCase().includes('prepress') ||
      String(s.currentStation || '').toLowerCase().includes('prepress') ||
      String(s.status || '').toLowerCase().includes('graphics') ||
      String(s.currentStation || '').toLowerCase().includes('graphics')
    );
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const newJobs = [];
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
            for (const job of jobs) {
              if (isPrepress(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      prepressJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = prepressJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Prepress / Graphics.</div>';
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="6">' + label + ' (' + count + ')</td></tr>';
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
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


WIDE_FORMAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Wide Format Jobs</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format" class="active">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Wide Format Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let wideFormatJobs = [];
  let sortCol = "due_date";
  let sortAsc = true;
  let refreshTimer = null;

  const WIDE_FORMAT_MACHINES = ['hp latex', 'mimaki'];

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

  function isWideFormat(jobStatuses) {
    return (jobStatuses || []).some(s =>
      WIDE_FORMAT_MACHINES.some(m => String(s.machineName || '').toLowerCase().includes(m)) ||
      String(s.currentStation || '').toLowerCase().includes('roll sheeter') ||
      String(s.currentStation || '').toLowerCase().includes('fabric folding') ||
      String(s.currentStation || '').toLowerCase().includes('rings') ||
      String(s.machineName || '').toLowerCase().includes('hand cutting') ||
      (String(s.currentStation || '').toLowerCase().includes('cutting') && String(s.machineName || '').toLowerCase().includes('cnc'))
    );
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const newJobs = [];
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
            for (const job of jobs) {
              if (isWideFormat(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      wideFormatJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = wideFormatJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Wide Format.</div>';
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const machine = (r.jobStatuses.find(s => s.machineName) || {}).machineName || '';
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td>' + esc(machine) + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="7">' + label + ' (' + count + ')</td></tr>';
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
      '<th class="no-sort">Device</th>' +
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


APP_CSS = r'''/* Marathon Printing – GoMake Orders Dashboard
   Shared stylesheet for every tab. Look and tokens follow the Printer's Plan dashboard. */
:root {
  color-scheme: light;
  --bg: #f4f3f0;
  --surface: #fcfcfb;
  --surface-2: #f0efec;
  --border: #e2e1dc;
  --text: #0b0b0b;
  --text-2: #52514e;
  --muted: #7a7974;
  --accent: #2a78d6;
  --accent-soft: #cde2fb;
  --critical: #d03b3b;
  --critical-ink: #b42828;
  --critical-soft: #fbe5e5;
  --warning: #fab219;
  --warning-ink: #8a5a00;
  --warning-soft: #fdf1d6;
  --good: #0ca30c;
  --good-ink: #0a7a0a;
  --good-soft: #e0f3e0;
  --radius: 10px;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --td: 22px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #2e2e2b;
    --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86;
    --accent: #3987e5; --accent-soft: #184f95;
    --critical-ink: #ff8a8a; --critical-soft: #3a1c1c;
    --warning-ink: #fab219; --warning-soft: #3a2e12;
    --good-ink: #6fdc6f; --good-soft: #15301a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #2e2e2b;
  --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86;
  --accent: #3987e5; --accent-soft: #184f95;
  --critical-ink: #ff8a8a; --critical-soft: #3a1c1c;
  --warning-ink: #fab219; --warning-soft: #3a2e12;
  --good-ink: #6fdc6f; --good-soft: #15301a;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; -webkit-font-smoothing: antialiased; }
button, input, select { font: inherit; color: inherit; }

/* ---------- header + nav ---------- */
header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 28px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
header .header-left { display: flex; align-items: center; gap: 16px; }
header img.logo { height: 56px; width: auto; border-radius: 8px; }
header h1 { font-size: 30px; font-weight: 650; letter-spacing: -0.01em; }
.status-bar { font-size: 20px; color: var(--text-2); background: var(--surface-2); border-radius: 999px; padding: 4px 16px; text-align: right; }
nav { background: var(--surface); border-bottom: 1px solid var(--border); display: flex; gap: 2px; padding: 0 20px; overflow-x: auto; }
nav a { color: var(--text-2); text-decoration: none; font-size: 22px; font-weight: 600; padding: 12px 18px; display: inline-block; white-space: nowrap; border-bottom: 3px solid transparent; transition: color 0.15s, border-color 0.15s; }
nav a:hover { color: var(--text); }
nav a.active { color: var(--text); border-bottom-color: var(--accent); }

/* ---------- layout + cards ---------- */
main { width: 100%; padding: 20px 28px 40px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px 22px; margin-bottom: 20px; min-width: 0; }
.toolbar { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }
.toolbar-left { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.toolbar strong { font-size: 27px; font-weight: 650; letter-spacing: -0.01em; }
.badge { display: inline-block; vertical-align: 2px; margin-left: 8px; background: var(--surface-2); color: var(--text-2); border: 1px solid var(--border); border-radius: 999px; padding: 1px 12px; font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }

/* ---------- controls ---------- */
.field-sm { display: flex; flex-direction: column; gap: 4px; font-size: 18px; color: var(--text-2); }
.field-sm label { font-size: 18px; font-weight: 600; color: var(--text-2); }
.field-sm select, .search-box { border: 1px solid var(--border); border-radius: 8px; background: var(--surface); padding: 7px 12px; font-size: 20px; outline: none; }
.search-box { width: 240px; }
.search-box:focus, .field-sm select:focus { border-color: var(--accent); }
button { padding: 8px 18px; border-radius: 8px; font-size: 20px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: background 0.15s, opacity 0.15s, border-color 0.15s; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { filter: brightness(1.08); }
.btn-secondary { background: var(--surface); color: var(--text); border-color: var(--border); }
.btn-secondary:hover { background: var(--surface-2); }
.btn-green { background: var(--good); color: #fff; }
.btn-green:hover { filter: brightness(1.08); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.sync-status { font-size: 18px; color: var(--muted); align-self: center; }

/* ---------- KPI tiles (Orders summary) ---------- */
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: var(--radius); padding: 14px 20px; min-width: 0; }
.stat-card.created { border-left-color: var(--accent); }
.stat-card.due { border-left-color: var(--warning); }
.stat-card.done { border-left-color: var(--good); }
.stat-label { font-size: 20px; color: var(--text-2); font-weight: 600; }
.stat-value { font-size: 43px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stat-card.created .stat-value { color: var(--accent); }
.stat-card.due .stat-value { color: var(--warning-ink); }
.stat-card.done .stat-value { color: var(--good-ink); }
.stat-sub { font-size: 20px; color: var(--muted); margin-top: 2px; font-weight: 600; font-variant-numeric: tabular-nums; }

/* ---------- table ---------- */
table { width: 100%; border-collapse: collapse; font-size: var(--td); }
th { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); color: var(--text-2); font-size: 20px; font-weight: 600; white-space: nowrap; cursor: pointer; user-select: none; }
th:hover { color: var(--text); }
th.no-sort, th.no-sort:hover { cursor: default; color: var(--text-2); }
td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr:hover td { background: var(--surface-2); }
.order-num { font-weight: 700; white-space: nowrap; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }

.due-badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 20px; font-weight: 650; white-space: nowrap; font-variant-numeric: tabular-nums; }
.due-overdue { background: var(--critical-soft); color: var(--critical-ink); }
.due-soon    { background: var(--warning-soft);  color: var(--warning-ink); }
.due-ok      { background: var(--good-soft);     color: var(--good-ink); }
.due-none    { background: var(--surface-2);     color: var(--muted); }

/* section headers: Late / Due Today / Upcoming */
.section-header td { font-size: 20px; font-weight: 700; padding: 9px 14px; letter-spacing: 0.03em; text-transform: uppercase; border-bottom: none; }
tbody tr.section-header:hover td { background: inherit; }
.section-overdue td { background: var(--critical-soft); color: var(--critical-ink); box-shadow: inset 5px 0 0 var(--critical); }
.section-today   td { background: var(--warning-soft);  color: var(--warning-ink);  box-shadow: inset 5px 0 0 var(--warning); }
.section-future  td { background: var(--surface-2);     color: var(--text-2);       box-shadow: inset 5px 0 0 var(--good); }
tbody tr.section-overdue:hover td { background: var(--critical-soft); }
tbody tr.section-today:hover td   { background: var(--warning-soft); }
tbody tr.section-future:hover td  { background: var(--surface-2); }

/* job status chips (colours come from the department rules in each page) */
.job-status-badge { display: inline-block; padding: 1px 9px; border-radius: 6px; font-size: 19px; font-weight: 650; margin: 0 4px 3px 0; }
.job-item { margin-bottom: 8px; }
.job-item:last-child { margin-bottom: 0; }
.job-item strong { display: block; font-size: 21px; font-weight: 650; color: var(--text); margin-bottom: 3px; }
.status-loading { color: var(--muted); font-size: 19px; font-style: italic; }

/* ---------- states ---------- */
.state { text-align: center; padding: 48px 24px; color: var(--muted); font-size: 25px; }
.state .icon { font-size: 29px; margin-bottom: 8px; }
.error-msg { color: var(--critical-ink); }
.spinner { display: inline-block; width: 16px; height: 16px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
.legend { margin-top: 14px; font-size: 19px; color: var(--muted); }

/* ---------- Completed tab ---------- */
.tracking-link { color: var(--accent); text-decoration: none; font-weight: 650; }
.tracking-link:hover { text-decoration: underline; }
.cost-cell { font-weight: 650; color: var(--good-ink); font-variant-numeric: tabular-nums; }

@media (max-width: 760px) {
  header { padding: 10px 16px; }
  header h1 { font-size: 22px; }
  main { padding: 14px 14px 32px; }
  .search-box { width: 100%; }
}
'''
import hashlib as _hashlib
CSS_VERSION = _hashlib.md5(APP_CSS.encode()).hexdigest()[:8]


def _page(html: str) -> HTMLResponse:
    """Serve a dashboard page, pointing it at the current shared stylesheet version."""
    return HTMLResponse(content=html.replace("__V__", CSS_VERSION))


@app.get("/app.css")
async def serve_css():
    return Response(content=APP_CSS, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return _page(DASHBOARD_HTML)


@app.get("/press", response_class=HTMLResponse)
async def serve_press():
    return _page(PRESS_HTML)


@app.get("/prepress", response_class=HTMLResponse)
async def serve_prepress():
    return _page(PREPRESS_HTML)


@app.get("/wide-format", response_class=HTMLResponse)
async def serve_wide_format():
    return _page(WIDE_FORMAT_HTML)


SCREEN_PRINTING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Screen Printing Jobs</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing" class="active">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Screen Printing Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let screenPrintingJobs = [];
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

  function isScreenPrinting(jobStatuses) {
    return (jobStatuses || []).some(s => {
      const station = String(s.currentStation || '').toLowerCase();
      const status  = String(s.status || '').toLowerCase();
      const machine = String(s.machineName || '').toLowerCase();
      return status.includes('exposure') ||
        station.includes('exposure') ||
        status.includes('stencil') ||
        station.includes('stencil') ||
        status.includes('mesh preparing') ||
        station.includes('mesh preparing') ||
        status.includes('film printing') ||
        station.includes('film printing') ||
        (machine.includes('screen printing') && (status.includes('printing') || station.includes('printing')));
    });
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const newJobs = [];
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
            for (const job of jobs) {
              if (isScreenPrinting(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      screenPrintingJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = screenPrintingJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Screen Printing.</div>';
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="6">' + label + ' (' + count + ')</td></tr>';
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
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


@app.get("/screen-printing", response_class=HTMLResponse)
async def serve_screen_printing():
    return _page(SCREEN_PRINTING_HTML)


FINISHING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Finishing Jobs</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing" class="active">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Finishing Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let finishingJobs = [];
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

  function isFinishing(jobStatuses) {
    return (jobStatuses || []).some(s => {
      const station = String(s.currentStation || '').toLowerCase();
      const status  = String(s.status || '').toLowerCase();
      const machine = String(s.machineName || '').toLowerCase();
      const isCutting = status.includes('cutting') || station.includes('cutting');
      if (isCutting && machine.includes('cnc')) return false; // CNC cutting → Wide Format, not Finishing
      return isCutting ||
        status.includes('folding') ||
        station.includes('folding') ||
        status.includes('lamination') ||
        station.includes('lamination') ||
        status.includes('scoring') ||
        station.includes('scoring');
    });
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const newJobs = [];
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
            for (const job of jobs) {
              if (isFinishing(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      finishingJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = finishingJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Finishing.</div>';
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="6">' + label + ' (' + count + ')</td></tr>';
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
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


@app.get("/finishing", response_class=HTMLResponse)
async def serve_finishing():
    return _page(FINISHING_HTML)


SHIPPING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Shipping Jobs</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping" class="active">Shipping</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Shipping Jobs <span class="badge" id="jobCount">0</span></strong>
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
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer">
      <div class="state"><span class="spinner"></span> Fetching orders&hellip;</div>
    </div>
  </div>
</main>
<script>
  let shippingJobs = [];
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

  function isShipping(jobStatuses) {
    return (jobStatuses || []).some(s =>
      String(s.status || '').toLowerCase().includes('packing') ||
      String(s.currentStation || '').toLowerCase().includes('packing')
    );
  }

  async function loadOrders(silent = false) {
    if (!silent) setLoading(true);
    else document.getElementById("statusBar").innerHTML = '&#8635; Refreshing&hellip;';
    const newJobs = [];
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
            for (const job of jobs) {
              if (isShipping(job.jobStatuses)) {
                const rawDate = order.dueDate || order.due_date || order.deliveryDate || '';
                const dueDate = String(rawDate).substring(0, 10);
                newJobs.push({
                  order_number: String(order.number || order.orderNumber || order.id || ''),
                  due_date: dueDate,
                  product_name: job.productName || '',
                  quantity: job.quantity || '',
                  notes: String(order.notes || '').trim(),
                  jobStatuses: job.jobStatuses || []
                });
              }
            }
          }
        } catch (e) { /* ignore */ }
        loaded++;
        document.getElementById("statusBar").textContent = 'Loading job statuses (' + loaded + '/' + openOrders.length + ')...';
      }));

      shippingJobs = newJobs;
      const now = new Date().toLocaleTimeString();
      document.getElementById("statusBar").innerHTML = '&#10003; Updated ' + now;
      renderTable();
      scheduleRefresh();
    } catch (err) {
      if (!silent) document.getElementById("tableContainer").innerHTML = '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById("statusBar").textContent = silent ? "Refresh failed" : "Error";
    } finally {
      if (!silent) setLoading(false);
    }
  }

  function renderTable() {
    const query = document.getElementById("searchBox").value.toLowerCase();
    let rows = shippingJobs.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      r.product_name.toLowerCase().includes(query) ||
      r.due_date.toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      const primary = sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      if (primary !== 0) return primary;
      return String(a.notes ?? '').localeCompare(String(b.notes ?? ''));
    });
    document.getElementById("jobCount").textContent = rows.length;
    if (!rows.length) {
      document.getElementById("tableContainer").innerHTML = '<div class="state"><div class="icon">&#128269;</div>No jobs currently at Shipping.</div>';
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
        const displayDate = parts[1] + '-' + parts[2];
        label = displayDate;
        const d = rowDate(r);
        if (d && !isNaN(d)) {
          if (d < today)      { cls = "due-overdue"; label = "&#9888; " + displayDate; }
          else if (d <= soon) { cls = "due-soon"; }
          else                { cls = "due-ok"; }
        }
      }
      const badges = r.jobStatuses.map(function(s) {
        const c = statusColor(s.status, s.currentStation, s.machineName);
        const lbl = esc(s.status) + (s.currentStation ? ' @ ' + esc(s.currentStation) : '');
        return '<span class="job-status-badge" style="background:' + c.bg + ';color:' + c.fg + '">' + lbl + '</span>';
      }).join(' ');
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td><span class="due-badge ' + cls + '">' + label + '</span></td>' +
        '<td>' + esc(r.product_name) + '</td>' +
        '<td>' + (r.quantity ? esc(String(r.quantity)) : '&#8212;') + '</td>' +
        '<td style="white-space:pre-line;color:var(--text-2)">' + esc(r.notes || '') + '</td>' +
        '<td>' + (badges || '&#8212;') + '</td>' +
        '</tr>';
    }

    function sectionHeader(cls, label, count) {
      return '<tr class="section-header ' + cls + '"><td colspan="6">' + label + ' (' + count + ')</td></tr>';
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
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon("notes") + '</th>' +
      '<th class="no-sort">Status</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  function statusColor(status, station, machine) {
    const s = String(status || '').toLowerCase();
    const st = String(station || '').toLowerCase();
    const m = String(machine || '').toLowerCase();
    if (s.includes('prepress') || st.includes('prepress') || s.includes('graphics') || st.includes('graphics')) return {bg:'#FFFF00', fg:'#1a202c'};
    if ((s.includes('printing') || st.includes('printing')) && m.includes('screen printing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('printing') || st.includes('printing'))           return {bg:'#30C5FF', fg:'#fff'};
    if (s.includes('gathering') || st.includes('gathering'))         return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('saddle') || st.includes('saddle') || s.includes('booklet saddle') || st.includes('booklet saddle')) return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('exposure') || st.includes('exposure') || s.includes('stencil') || st.includes('stencil') || s.includes('mesh preparing') || st.includes('mesh preparing')) return {bg:'#8B5CF6', fg:'#fff'};
    if (s.includes('scoring') || st.includes('scoring'))              return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('cutting') || st.includes('cutting'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('folding') || st.includes('folding'))             return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('lamination') || st.includes('lamination'))       return {bg:'#FF793B', fg:'#fff'};
    if (s.includes('packing') || st.includes('packing'))             return {bg:'#FA3BF0', fg:'#fff'};
    if (st.includes('roll sheeter') || st.includes('fabric folding') || st.includes('rings') || m.includes('hand cutting')) return {bg:'#1BFC06', fg:'#1a202c'};
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
    refreshTimer = setTimeout(() => loadOrders(true), interval * 1000);
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


@app.get("/shipping", response_class=HTMLResponse)
async def serve_shipping():
    return _page(SHIPPING_HTML)


@app.get("/api/completed-orders")
async def get_completed_orders():
    """Return ShipStation shipments joined with GoMake order data."""
    if not SS_API_KEY or not SS_API_SECRET:
        raise HTTPException(status_code=500, detail="ShipStation credentials not configured.")
    async with httpx.AsyncClient() as client:
        # Fetch GoMake orders (all pages) → build number→data map
        gomake_map: dict = {}
        gm_page = 1
        while True:
            try:
                resp = await client.post(
                    f"{GOMAKE_BASE}/sales-orders/list",
                    headers=GOMAKE_HEADERS,
                    json={"model": {"pageSize": PAGE_SIZE, "pageNumber": gm_page}},
                    timeout=30,
                )
                resp.raise_for_status()
                orders = resp.json().get("data", {}).get("data", [])
                if not orders:
                    break
                for o in orders:
                    num = str(o.get("number") or "")
                    if num:
                        gomake_map[num] = o
                if len(orders) < PAGE_SIZE:
                    break
                gm_page += 1
            except Exception:
                break

        # Fetch ShipStation shipments (last 60 days, skip voided)
        since = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        results = []
        ss_page = 1
        while True:
            try:
                r = await client.get(
                    f"{SS_BASE}/shipments",
                    headers=SS_HEADERS,
                    params={"shipDateStart": since, "page": ss_page, "pageSize": 100},
                    timeout=30,
                )
                r.raise_for_status()
                shipments = r.json().get("shipments", [])
                if not shipments:
                    break
                for s in shipments:
                    if s.get("voided"):
                        continue
                    order_number = str(s.get("orderNumber") or "")
                    gm = gomake_map.get(order_number)
                    if not gm:
                        continue
                    results.append({
                        "order_number":   order_number,
                        "customer_name":  gm.get("customerName") or gm.get("contactName") or "",
                        "due_date":       str(gm.get("dueDate") or "")[:10],
                        "notes":          gm.get("internalNotes") or gm.get("notes") or "",
                        "ship_date":      str(s.get("shipDate") or "")[:10],
                        "carrier_code":   s.get("carrierCode") or "",
                        "service_code":   s.get("serviceCode") or "",
                        "tracking_number": s.get("trackingNumber") or "",
                        "shipping_cost":  s.get("shipmentCost"),
                    })
                if len(shipments) < 100:
                    break
                ss_page += 1
            except Exception:
                break

        results.sort(key=lambda x: x["ship_date"], reverse=True)
        return JSONResponse(content=results)


COMPLETED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Completed Orders</title>
  <script>(function(){try{var q=new URLSearchParams(location.search).get('theme');if(q==='auto'){localStorage.removeItem('theme')}else if(q==='dark'||q==='light'){localStorage.setItem('theme',q)}var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})();</script>
  <link rel="stylesheet" href="/app.css?v=__V__" />
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
  <a href="/prepress">Prepress / Graphics</a>
  <a href="/press">Press</a>
  <a href="/wide-format">Wide Format</a>
  <a href="/screen-printing">Screen Printing</a>
  <a href="/finishing">Finishing</a>
  <a href="/shipping">Shipping</a>
  <a href="/completed" class="active">Completed</a>
</nav>
<main>
  <div class="card">
    <div class="toolbar">
      <div class="toolbar-left">
        <strong>Completed Orders <span class="badge" id="orderCount">0</span></strong>
        <input class="search-box" type="search" id="searchBox" placeholder="Search&hellip;" oninput="renderTable()" />
      </div>
      <div class="toolbar-left">
        <button class="btn-primary" id="refreshBtn" onclick="loadOrders()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="tableContainer"><div class="state"><span class="spinner"></span> Loading&hellip;</div></div>
  </div>
</main>
<script>
  let allOrders = [];
  let sortCol = 'ship_date', sortAsc = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function trackingUrl(carrier, tracking) {
    const c = (carrier || '').toLowerCase();
    const t = encodeURIComponent(tracking);
    if (c.includes('usps') || c.includes('stamps'))
      return 'https://tools.usps.com/go/TrackConfirmAction?tLabels=' + t;
    if (c.includes('ups'))
      return 'https://www.ups.com/track?tracknum=' + t;
    if (c.includes('fedex'))
      return 'https://www.fedex.com/fedextrack/?tracknumbers=' + t;
    return 'https://www.google.com/search?q=' + t + '+tracking';
  }

  function formatService(carrier, service) {
    const raw = service || carrier || '';
    return raw.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  function formatDate(d) {
    if (!d) return '—';
    const p = d.split('-');
    if (p.length < 3) return d;
    return p[1] + '-' + p[2]; // MM-DD
  }

  async function loadOrders() {
    const btn = document.getElementById('refreshBtn');
    btn.disabled = true; btn.textContent = 'Loading…';
    document.getElementById('statusBar').textContent = 'Loading…';
    try {
      const res = await fetch('/api/completed-orders');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      allOrders = await res.json();
      const now = new Date().toLocaleTimeString();
      document.getElementById('statusBar').innerHTML = '&#10003; Updated ' + now + ' &nbsp;(last 60 days)';
      renderTable();
    } catch (err) {
      document.getElementById('tableContainer').innerHTML =
        '<div class="state error-msg"><div class="icon">&#9888;</div>' + esc(err.message) + '</div>';
      document.getElementById('statusBar').textContent = 'Error loading';
    } finally {
      btn.disabled = false; btn.textContent = '↻ Refresh';
    }
  }

  function sortBy(col) {
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    renderTable();
  }
  function sortIcon(col) { return sortCol !== col ? '&#8597;' : (sortAsc ? '&#8593;' : '&#8595;'); }

  function renderTable() {
    const query = document.getElementById('searchBox').value.toLowerCase();
    let rows = allOrders.filter(r =>
      r.order_number.toLowerCase().includes(query) ||
      (r.customer_name || '').toLowerCase().includes(query) ||
      (r.tracking_number || '').toLowerCase().includes(query) ||
      (r.service_code || '').toLowerCase().includes(query) ||
      (r.notes || '').toLowerCase().includes(query)
    );
    rows.sort((a, b) => {
      const va = a[sortCol] ?? '', vb = b[sortCol] ?? '';
      return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
    document.getElementById('orderCount').textContent = rows.length;
    if (!rows.length) {
      document.getElementById('tableContainer').innerHTML =
        '<div class="state"><div class="icon">&#128269;</div>No completed orders found.</div>';
      return;
    }
    const tBody = rows.map(r => {
      const url = r.tracking_number ? trackingUrl(r.carrier_code, r.tracking_number) : '';
      const trackingCell = r.tracking_number
        ? '<a class="tracking-link" href="' + esc(url) + '" target="_blank">' + esc(r.tracking_number) + '</a>'
        : '&#8212;';
      const costCell = r.shipping_cost != null
        ? '$' + parseFloat(r.shipping_cost).toFixed(2)
        : '&#8212;';
      return '<tr>' +
        '<td class="order-num">' + esc(r.order_number) + '</td>' +
        '<td>' + esc(r.customer_name) + '</td>' +
        '<td>' + esc(r.notes) + '</td>' +
        '<td>' + formatDate(r.due_date) + '</td>' +
        '<td>' + formatDate(r.ship_date) + '</td>' +
        '<td>' + esc(formatService(r.carrier_code, r.service_code)) + '</td>' +
        '<td>' + trackingCell + '</td>' +
        '<td class="cost-cell">' + costCell + '</td>' +
        '</tr>';
    }).join('');
    document.getElementById('tableContainer').innerHTML =
      '<table><thead><tr>' +
      '<th data-col="order_number" onclick="sortBy(this.dataset.col)">Order # ' + sortIcon('order_number') + '</th>' +
      '<th data-col="customer_name" onclick="sortBy(this.dataset.col)">Customer ' + sortIcon('customer_name') + '</th>' +
      '<th data-col="notes" onclick="sortBy(this.dataset.col)">Notes ' + sortIcon('notes') + '</th>' +
      '<th data-col="due_date" onclick="sortBy(this.dataset.col)">Due Date ' + sortIcon('due_date') + '</th>' +
      '<th data-col="ship_date" onclick="sortBy(this.dataset.col)">Ship Date ' + sortIcon('ship_date') + '</th>' +
      '<th data-col="service_code" onclick="sortBy(this.dataset.col)">Service ' + sortIcon('service_code') + '</th>' +
      '<th class="no-sort">Tracking #</th>' +
      '<th data-col="shipping_cost" onclick="sortBy(this.dataset.col)">Ship Cost ' + sortIcon('shipping_cost') + '</th>' +
      '</tr></thead><tbody>' + tBody + '</tbody></table>';
  }

  loadOrders();
</script>
</body>
</html>"""


@app.get("/completed", response_class=HTMLResponse)
async def serve_completed():
    return _page(COMPLETED_HTML)


# ── Shared GoMake data cache ──────────────────────────────────────────────────
# All dashboard pages/screens read from this in-memory cache instead of calling
# GoMake directly. A background task refreshes it every CACHE_REFRESH seconds, and
# concurrent requests for the same key are coalesced into a single API call.

CACHE_REFRESH     = 150   # seconds between background refreshes (2.5 min)
CACHE_TTL         = 180   # a cached entry older than this is refetched on demand
WARM_CONCURRENCY  = 5     # max simultaneous GoMake calls from the background refresh

_cache: dict = {}         # key -> {"data": ..., "ts": epoch seconds}
_cache_locks: dict = {}   # key -> asyncio.Lock
_cache_state = {"last_warm": None, "last_warm_secs": None, "last_warm_orders": 0, "last_error": None}


def _gomake_headers() -> dict:
    return {
        "X-Client-Id": CLIENT_ID,
        "X-Client-Secret": CLIENT_SECRET,
        "Content-Type": "application/json",
    }


async def _cached(key: str, fetch, force: bool = False):
    """Return cached data for key, refetching (once, even under concurrency) if stale.
    If a refetch fails and older data exists, the stale data is served instead."""
    import time
    entry = _cache.get(key)
    if entry and not force and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    async with lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < (5 if force else CACHE_TTL):
            return entry["data"]   # another request just refreshed it
        try:
            data = await fetch()
        except Exception:
            if entry:
                return entry["data"]   # serve stale rather than fail
            raise
        _cache[key] = {"data": data, "ts": time.time()}
        return data


async def _fetch_orders_page(page: int):
    url = f"{GOMAKE_BASE}/sales-orders/list"
    body = {"model": {"pageSize": PAGE_SIZE, "pageNumber": page + 1}}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=_gomake_headers(), json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code,
                                detail=f"GoMake API error: {e.response.text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")


async def _fetch_order_jobs(order_id: str):
    headers = _gomake_headers()
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
        return [r for r in results if (r.get("productName") or "").strip().lower() != "delivery"]


async def _warm_cache():
    """Refresh every orders page, then job statuses for every open order."""
    import time
    t0 = time.time()
    page, open_ids = 0, []
    while True:
        data = await _cached(f"orders:{page}", lambda p=page: _fetch_orders_page(p), force=True)
        d = data.get("data") if isinstance(data, dict) else None
        orders = d.get("data", []) if isinstance(d, dict) else (d or [])
        if not orders:
            break
        for o in orders:
            if ("closed" not in str(o.get("statusTitleText", "")).lower()
                    and o.get("deliveryStatus") != 3 and o.get("id")):
                open_ids.append(str(o["id"]))
        if len(orders) < PAGE_SIZE:
            break
        page += 1

    sem = asyncio.Semaphore(WARM_CONCURRENCY)

    async def warm_one(oid):
        async with sem:
            try:
                await _cached(f"jobs:{oid}", lambda: _fetch_order_jobs(oid), force=True)
            except Exception:
                pass

    await asyncio.gather(*[warm_one(oid) for oid in open_ids])
    _cache_state.update({
        "last_warm": datetime.utcnow().isoformat(),
        "last_warm_secs": round(time.time() - t0, 1),
        "last_warm_orders": len(open_ids),
        "last_error": None,
    })


async def _cache_warm_loop():
    """Background task: keep the shared cache fresh so users never hit GoMake directly."""
    while True:
        try:
            await _warm_cache()
        except Exception as e:
            _cache_state["last_error"] = str(e)
            print(f"[cache] refresh error: {e}")
        await asyncio.sleep(CACHE_REFRESH)


@app.get("/api/orders")
async def get_orders(page: int = Query(0, ge=0)):
    """A page of orders from GoMake (page is 0-based), served from the shared cache."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")
    return await _cached(f"orders:{page}", lambda: _fetch_orders_page(page))


@app.get("/api/orders/{order_id}/jobs")
async def get_order_jobs(order_id: str):
    """Job statuses for an order's document items, served from the shared cache."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")
    return await _cached(f"jobs:{order_id}", lambda: _fetch_order_jobs(order_id))


@app.get("/api/cache/status")
async def cache_status():
    """When the shared cache was last refreshed (handy for checking it's working)."""
    return {**_cache_state, "entries": len(_cache), "refresh_every_secs": CACHE_REFRESH}


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


@app.get("/api/debug/order-detail/{order_number}")
async def debug_order_detail(order_number: str):
    """Returns the full GoMake order detail (documentDeliveryAddresses, documentItems, etc.)."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")
    headers = {"X-Client-Id": CLIENT_ID, "X-Client-Secret": CLIENT_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: find the order ID from the list
        order_id = None
        for page_num in range(1, 20):
            resp = await client.post(f"{GOMAKE_BASE}/sales-orders/list", headers=headers,
                                     json={"model": {"pageSize": PAGE_SIZE, "pageNumber": page_num}})
            resp.raise_for_status()
            orders = resp.json().get("data", {}).get("data", [])
            if not orders:
                break
            for o in orders:
                if str(o.get("number") or "") == order_number:
                    order_id = o.get("id")
                    break
            if order_id or len(orders) < PAGE_SIZE:
                break
        if not order_id:
            raise HTTPException(status_code=404, detail=f"Order {order_number} not found.")
        # Step 2: fetch full detail
        resp = await client.get(f"{GOMAKE_BASE}/sales-orders/{order_id}", headers=headers)
        resp.raise_for_status()
        return resp.json().get("data", {})


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


@app.get("/api/debug/shipments/{order_number}")
async def debug_shipments(order_number: str):
    """Returns ShipStation shipments for a given order number, plus the result of a GoMake internalNotes PUT attempt."""
    if not SS_API_KEY or not SS_API_SECRET:
        raise HTTPException(status_code=500, detail="ShipStation credentials not configured.")
    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch shipments from ShipStation
        r = await client.get(
            f"{SS_BASE}/shipments",
            headers=SS_HEADERS,
            params={"orderNumber": order_number},
        )
        ss_data = r.json() if r.status_code == 200 else {"error": r.text, "status": r.status_code}

        # Also try a test PUT to GoMake to see what the API returns
        # Find the GoMake order ID first
        gomake_id = None
        gomake_notes = None
        for page_num in range(1, 20):
            resp = await client.post(
                f"{GOMAKE_BASE}/sales-orders/list",
                headers=GOMAKE_HEADERS,
                json={"model": {"pageSize": PAGE_SIZE, "pageNumber": page_num}},
            )
            orders = resp.json().get("data", {}).get("data", [])
            if not orders:
                break
            for o in orders:
                if str(o.get("number") or "") == order_number:
                    gomake_id = o.get("id")
                    gomake_notes = o.get("internalNotes")
                    break
            if gomake_id or len(orders) < PAGE_SIZE:
                break

        probe_results = {}
        if gomake_id:
            test_note = (gomake_notes or "").strip()
            test_note = (test_note + "\nDEBUG_TEST").strip()
            body = {"internalNotes": test_note}
            for method, path in [
                ("POST",  f"{GOMAKE_BASE}/sales-orders/set-internal-notes"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/save-internal-notes"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/{gomake_id}/set-internal-notes"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/{gomake_id}/note"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/{gomake_id}/notes"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/save"),
                ("POST",  f"{GOMAKE_BASE}/documents/{gomake_id}"),
                ("PUT",   f"{GOMAKE_BASE}/documents/{gomake_id}"),
                ("POST",  f"{GOMAKE_BASE}/documents/update"),
                ("POST",  f"{GOMAKE_BASE}/sales-orders/{order_number}"),
                ("PUT",   f"{GOMAKE_BASE}/sales-orders/{order_number}"),
            ]:
                try:
                    r2 = await client.request(method, path, headers=GOMAKE_HEADERS, json=body)
                    probe_results[f"{method} {path.replace(GOMAKE_BASE,'')}"] = {
                        "status": r2.status_code, "body": r2.text[:300]
                    }
                except Exception as e:
                    probe_results[f"{method} {path.replace(GOMAKE_BASE,'')}"] = {"error": str(e)}

        return JSONResponse(content={
            "order_number": order_number,
            "gomake_id": gomake_id,
            "gomake_current_notes": gomake_notes,
            "shipstation_shipments": ss_data,
            "gomake_probe_results": probe_results,
        })


@app.get("/api/debug/order/{order_number}")
async def debug_order(order_number: str):
    """Dump the full raw GoMake order detail — shows every field GoMake returns."""
    async with httpx.AsyncClient(timeout=30) as client:
        # Search all pages for the order (includes closed/shipped)
        gomake_order = None
        for page_num in range(1, 50):
            try:
                resp = await client.post(
                    f"{GOMAKE_BASE}/sales-orders/list",
                    headers=GOMAKE_HEADERS,
                    json={"model": {"pageSize": PAGE_SIZE, "pageNumber": page_num}},
                )
                resp.raise_for_status()
                orders = resp.json().get("data", {}).get("data", [])
                if not orders:
                    break
                for o in orders:
                    if str(o.get("number") or "") == order_number:
                        gomake_order = o
                        break
                if gomake_order or len(orders) < PAGE_SIZE:
                    break
            except Exception as e:
                return JSONResponse(content={"error": f"List fetch failed on page {page_num}: {str(e)}"})

        if not gomake_order:
            return JSONResponse(status_code=404, content={"error": f"Order {order_number} not found in GoMake list."})

        gomake_id = gomake_order.get("id")

        # Fetch full order detail via GET /sales-orders/{id}
        detail_status = None
        detail_raw = None
        detail_error = None
        try:
            r = await client.get(
                f"{GOMAKE_BASE}/sales-orders/{gomake_id}",
                headers=GOMAKE_HEADERS,
            )
            detail_status = r.status_code
            try:
                detail_raw = r.json()
            except Exception:
                detail_raw = r.text
        except Exception as e:
            detail_error = str(e)

        # Unwrap {"data": {...}} if present
        detail_unwrapped = None
        if isinstance(detail_raw, dict):
            detail_unwrapped = detail_raw.get("data") or detail_raw

        # Pull out documentItems with all fields (including nulls so we can see the schema)
        items_full = []
        if isinstance(detail_unwrapped, dict):
            items_full = detail_unwrapped.get("documentItems") or []

        return JSONResponse(content={
            "order_number":      order_number,
            "gomake_id":         gomake_id,
            "detail_http_status": detail_status,
            "detail_error":      detail_error,
            "document_items":    items_full,       # full item data with all fields
            "detail_keys":       list(detail_unwrapped.keys()) if isinstance(detail_unwrapped, dict) else [],
            "detail_raw":        detail_raw,       # complete raw response
        })


@app.get("/api/debug/products")
async def debug_products():
    """Dump raw GoMake product SKUs to check for weight/dimension fields."""
    async with httpx.AsyncClient(timeout=30) as client:
        skus_status = None
        skus_raw = None
        skus_error = None
        try:
            r = await client.get(
                f"{GOMAKE_BASE}/products/skus",
                headers=GOMAKE_HEADERS,
            )
            skus_status = r.status_code
            try:
                skus_raw = r.json()
            except Exception:
                skus_raw = r.text
        except Exception as e:
            skus_error = str(e)

        products_status = None
        products_raw = None
        products_error = None
        try:
            r2 = await client.get(
                f"{GOMAKE_BASE}/products/list",
                headers=GOMAKE_HEADERS,
            )
            products_status = r2.status_code
            try:
                products_raw = r2.json()
            except Exception:
                products_raw = r2.text
        except Exception as e:
            products_error = str(e)

        # Summarise first SKU's keys so we can see the schema
        sku_keys = []
        sku_sample = None
        if isinstance(skus_raw, dict):
            items = skus_raw.get("data") or skus_raw.get("skus") or []
            if isinstance(items, list) and items:
                sku_sample = items[0]
                sku_keys = list(sku_sample.keys())

        return JSONResponse(content={
            "skus_http_status":     skus_status,
            "skus_error":           skus_error,
            "sku_keys":             sku_keys,
            "sku_sample":           sku_sample,
            "skus_raw":             skus_raw,
            "products_http_status": products_status,
            "products_error":       products_error,
            "products_raw":         products_raw,
        })


@app.api_route("/api/sync", methods=["GET", "POST"])
async def trigger_sync():
    """Manually trigger a GoMake → ShipStation sync."""
    if not SS_API_KEY or not SS_API_SECRET:
        raise HTTPException(status_code=500, detail="ShipStation credentials not configured.")
    result = await run_sync()
    return JSONResponse(content=result)


@app.get("/api/sync/status")
async def sync_status():
    """Return the current sync state."""
    return JSONResponse(content=_sync_state)


# ── Apparel designer: live quote via GoMake ──────────────────────────────────

from pydantic import BaseModel

APPAREL_CUSTOMER_CODE = "8066"
APPAREL_CUSTOMER_NAME = "Apparel Quotes"
QUOTE_POLL_TRIES      = 15   # x 2s = up to ~30s wait for pricing
QUOTE_POLL_DELAY      = 2


class ApparelQuoteRequest(BaseModel):
    product: str                 # e.g. "Softstyle CVC (Gildan 64000CVC · 60/40 ring-spun cotton/poly)"
    color: str
    quantity: int
    locations: list = []         # e.g. ["front", "back"]
    name: str = ""
    email: str = ""


@app.post("/api/quote")
async def create_apparel_quote(req: ApparelQuoteRequest):
    """Create a quote in GoMake for an apparel job and return the full price."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")
    if req.quantity < 1 or req.quantity > 100000:
        raise HTTPException(status_code=400, detail="Invalid quantity.")

    locations = [l for l in req.locations if l in ("front", "back")]
    if locations:
        print_desc = (f"screen printed in {len(locations)} location(s): "
                      + " and ".join(locations))
    else:
        print_desc = "blank, no printing"
    prompt = (f"{req.quantity} x {req.product} t-shirts/apparel, "
              f"color {req.color}, {print_desc}")

    payload = {
        "customerCode": APPAREL_CUSTOMER_CODE,
        "customerName": APPAREL_CUSTOMER_NAME,
        "quoteItemPrompts": [prompt],
    }
    if req.name or req.email:
        payload["quoteContacts"] = [{
            "contactName":  req.name or "Web Customer",
            "contactMail":  req.email,
            "contactPhone": "",
        }]

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{GOMAKE_BASE}/quotes/create-quote-using-ai",
                headers=GOMAKE_HEADERS, json=payload, timeout=60,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502,
                                detail=f"GoMake rejected the quote: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")

        created = resp.json().get("data") or {}
        quote_id  = created.get("QuoteID")
        quote_num = created.get("QuoteNumber")
        if not quote_id:
            raise HTTPException(status_code=502, detail="GoMake did not return a quote ID.")

    # Return immediately; the page polls /api/quote/status/{id} for pricing
    return JSONResponse(content={
        "success": True,
        "quoteId": quote_id,
        "quoteNumber": quote_num,
    })


@app.get("/api/quote/status/{quote_id}")
async def apparel_quote_status(quote_id: str, quantity: int = Query(1, ge=1)):
    """Check whether a GoMake quote has been priced yet."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Server credentials not configured.")
    async with httpx.AsyncClient() as client:
        try:
            g = await client.get(f"{GOMAKE_BASE}/quotes/{quote_id}",
                                 headers=GOMAKE_HEADERS, timeout=30)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach GoMake: {e}")
    if g.status_code != 200:
        return JSONResponse(content={"priced": False})
    data = g.json().get("data") or {}
    total = data.get("totalPrice")
    if not total:
        return JSONResponse(content={"priced": False})
    return JSONResponse(content={
        "priced": True,
        "quoteNumber": data.get("number"),
        "totalPrice": total,
        "unitPrice": round(total / max(quantity, 1), 2),
        "currency": data.get("currency") or "USD",
    })


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
