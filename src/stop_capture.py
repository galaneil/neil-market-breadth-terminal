"""Capture the stop you placed at IBKR into the journal's Initial Stop $.

Why stops were missing: IBKR's end-of-day Flex report carries executed trades only, not the orders you place, so a stop
that was set and later cancelled, moved or hit never reaches us. Two sources can fix that:

  1. Live resting stops, from the Client Portal gateway (needs you logged in to the gateway). Written onto the open
     trade as soon as they are seen, and every sighting is kept in stop_history.jsonl so a stop that is later cancelled
     or filled is still remembered.
  2. The Flex report's ORDERS section, if the Flex query includes it (IBKR > Reports > Flex Queries > edit the query >
     add "Orders" with order type, stop price, date/time, symbol, side, quantity, status). Then the first stop order
     placed after each entry becomes that trade's initial stop, including for trades already closed.

Nothing here overwrites a stop you set yourself: only a blank Initial Stop $ is ever filled.
"""
import json
import os
import time
from datetime import date

import config
import notion_sync

OUT = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
HISTORY = os.path.join(OUT, "stop_history.jsonl")
STATUS = os.path.join(OUT, "stops_status.json")


def _save_status(update):
    cur = status()
    cur.update(update)
    cur["ts"] = time.time()
    os.makedirs(OUT, exist_ok=True)
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(cur, f)


def status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _blank_stop_rows(open_only):
    """Core NG-IBKR rows with no Initial Stop $ yet."""
    rows = []
    for t in notion_sync.fetch_trades(log=lambda *a: None):
        if t["account"] != "NG-IBKR" or t.get("parentId") or t.get("initialStop"):
            continue
        if open_only and t.get("dateClosed"):
            continue
        if t.get("ticker") and t.get("entryPrice") and t.get("dateOpened"):
            rows.append(t)
    return rows


def capture_live(log=print):
    """Resting stops from the gateway -> blank Initial Stop $ on open trades."""
    import ibkr_cp
    written = []
    try:
        stops = ibkr_cp.resting_stops(log=log)
        gateway = "connected"
    except Exception as error:
        stops, gateway = {}, f"not available: {str(error)[:90]}"
        log(f"  stop capture: gateway {gateway}")
    if stops:
        os.makedirs(OUT, exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as f:
            for tk, stop in stops.items():
                f.write(json.dumps({"ts": time.time(), "date": date.today().isoformat(), "ticker": tk, "stop": stop}) + "\n")
        for t in _blank_stop_rows(open_only=True):
            stop = stops.get(t["ticker"].upper())
            if stop and stop < t["entryPrice"]:
                notion_sync.update_stop(t["pageId"], stop, log=log)
                written.append({"ticker": t["ticker"], "stop": stop, "source": "gateway"})
    _save_status({"gateway": gateway, "liveWritten": len(written)})
    return written


def from_statement(statement, log=print):
    """Initial stops from the Flex ORDERS section (when the query has one)."""
    orders = [o for o in (statement.get("orders") or []) if o.get("stop") and (o.get("side") or "").upper().startswith("S")
              and (o.get("order_type") or "").upper() in ("STP", "STPLMT", "STP LMT", "TRAIL")]
    _save_status({"flexOrders": bool(statement.get("orders")), "flexStopOrders": len(orders)})
    if not orders:
        return []
    written = []
    for t in _blank_stop_rows(open_only=False):
        opened, closed = t["dateOpened"][:10], (t.get("dateClosed") or "9999-12-31")[:10]
        cands = sorted((o for o in orders if (o["symbol"] or "").upper() == t["ticker"].upper()
                        and opened <= (o["datetime"] or "")[:10] <= closed and o["stop"] < t["entryPrice"]),
                       key=lambda o: o["datetime"] or "")
        if cands:
            notion_sync.update_stop(t["pageId"], cands[0]["stop"], log=log)
            written.append({"ticker": t["ticker"], "stop": cands[0]["stop"], "source": "flex orders"})
    if written:
        log(f"  stop capture: {len(written)} initial stops filled from the Flex orders section")
    _save_status({"flexWritten": len(written)})
    return written


def summary():
    """For the dashboard: how many trades lack a stop, and why."""
    st = status()
    rows = [t for t in notion_sync.fetch_trades(log=lambda *a: None) if t["account"] == "NG-IBKR" and not t.get("parentId")]
    return {"open": sum(1 for t in rows if not t.get("dateClosed")), "openNoStop": sum(1 for t in rows if not t.get("dateClosed") and not t.get("initialStop")),
            "closed": sum(1 for t in rows if t.get("dateClosed")), "closedNoStop": sum(1 for t in rows if t.get("dateClosed") and not t.get("initialStop")),
            "status": st}
