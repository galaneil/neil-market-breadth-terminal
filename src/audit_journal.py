"""Audit the whole trade journal for rows that cannot be true.

Checks every row (all accounts), then compares NG-IBKR against IBKR's own fills:
  - closed before it was opened, or hold days negative
  - closed with no exit price / an exit price on an open row / missing entry or shares
  - a pyramid add opened after its core closed, or closed on a different day than its core
  - entry or exit price outside that day's high-low range in the stored price history (a price from the wrong trade)
  - rows inside the report window whose open/close dates or exit price disagree with the real fills
Run:  python src/audit_journal.py
"""
import json
import os
import sys

import config
import notion_sync


def _bars(country, symbol, cache={}):
    key = (country, symbol)
    if key not in cache:
        sub = config.COUNTRIES[country].get("docs_subdir", "")
        path = os.path.join(config.DOCS_DIR, sub, config.TICKER_DIR_NAME, symbol + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            cache[key] = {dt: (h, l) for dt, h, l in zip(d["dates"], d["high"], d["low"]) if h and l}
        except Exception:
            cache[key] = None
    return cache[key]


def rows_check(trades):
    issues = []
    by_id = {t["pageId"]: t for t in trades}

    def add(t, kind, detail):
        issues.append({"ticker": t["ticker"], "account": t["account"], "opened": t.get("dateOpened"), "closed": t.get("dateClosed"),
                       "pageId": t["pageId"], "kind": kind, "detail": detail})

    for t in trades:
        o, c = t.get("dateOpened"), t.get("dateClosed")
        if not t.get("entryPrice") or not t.get("shares"):
            add(t, "missing entry/shares", f"entry {t.get('entryPrice')} shares {t.get('shares')}")
        if c and o and c < o:
            add(t, "closed before opened", f"{o} -> {c}")
        if c and not t.get("exitPrice"):
            add(t, "closed without exit price", "")
        if not c and t.get("exitPrice"):
            add(t, "open row has an exit price", str(t.get("exitPrice")))
        p = by_id.get(t.get("parentId") or "")
        if p:
            if p.get("dateClosed") and o and o > p["dateClosed"]:
                add(t, "add opened after its core closed", f"add {o}, core closed {p['dateClosed']}")
            elif p.get("dateClosed") and c and c != p["dateClosed"]:
                add(t, "add closed on a different day than its core", f"add {c}, core {p['dateClosed']}")
            elif c is None and p.get("dateClosed"):
                add(t, "add still open but its core is closed", "")
        # prices against that day's range (US only: the India files are NSE-keyed differently)
        country = t.get("country") or "US"
        bars = _bars(country, (t.get("ticker") or "").upper()) if t.get("ticker") else None
        if bars:
            for label, dt, px in (("entry", o, t.get("entryPrice")), ("exit", c, t.get("exitPrice"))):
                if dt and px and dt in bars:
                    hi, lo = bars[dt]
                    if px > hi * 1.03 or px < lo * 0.97:
                        add(t, f"{label} price outside that day's range", f"{px} on {dt} (low {lo}, high {hi})")
    return issues


def fills_check(trades, log=print):
    """NG-IBKR rows inside the report window against the real fills."""
    import broker_sync as bs
    import ibkr_flex
    st = ibkr_flex.parse(ibkr_flex.fetch_statement(log=lambda *a: None))
    fills = [{"symbol": (f["symbol"] or "").upper(), "quantity": abs(f["quantity"]), "price": f["price"],
              "side": "BUY" if f["side"] == "BUY" else "SELL", "datetime": f["datetime"]} for f in st["trades"]]
    legs = bs._legs_from_fills(fills)
    issues = []
    ib = [t for t in trades if t["account"] == "NG-IBKR" and not t.get("parentId")]
    for sym, ls in legs.items():
        rows = [{"id": t["pageId"], "ticker": sym, "opened": (t["dateOpened"] or "")[:10], "closed": (t["dateClosed"] or "")[:10] or None,
                 "entry": t["entryPrice"], "exit": t.get("exitPrice")} for t in ib if (t["ticker"] or "").upper() == sym]
        matched = bs._assign(ls, rows)
        for i, leg in enumerate(ls):
            row = matched.get(i)
            if row is None:
                continue
            lc = (leg["closed"] or "")[:10] or None
            if lc and row["closed"] != lc:
                issues.append({"ticker": sym, "pageId": row["id"], "kind": "close date differs from the fills",
                               "detail": f"row {row['closed']} vs fills {lc} @ {leg['exitPrice']:.2f}", "fix": {"close": lc, "exit": leg["exitPrice"]}})
            elif lc and row["exit"] and leg["exitPrice"] and abs(row["exit"] - leg["exitPrice"]) / leg["exitPrice"] > 0.01 and leg["sellShares"] <= leg["shares"] * 1.001:
                issues.append({"ticker": sym, "pageId": row["id"], "kind": "exit price differs from the fills",
                               "detail": f"row {row['exit']} vs fills {leg['exitPrice']:.2f}", "fix": {"close": lc, "exit": leg["exitPrice"]}})
            elif not lc and row["closed"]:
                issues.append({"ticker": sym, "pageId": row["id"], "kind": "closed in the journal, still open in the fills", "detail": f"closed {row['closed']}"})
    return issues


if __name__ == "__main__":
    trades = notion_sync.fetch_trades(log=lambda *a: None)
    found = rows_check(trades)
    try:
        found += fills_check(trades)
    except Exception as error:
        print("fills check skipped:", error)
    for i in sorted(found, key=lambda x: (x["kind"], x["ticker"])):
        print(f"{i['kind']:48} {i['ticker']:6} {i.get('opened') or ''} {i.get('closed') or ''}  {i['detail']}  {i['pageId'][:8]}")
    print(len(found), "issues across", len(trades), "rows")
    sys.exit(0)
