"""Monthly review of the trade journal: numbers the Trade Journal's Monthly review tab draws.

Two sources:
  - the Notion journal (every closed position, pyramid adds folded into their core row), with each trade's best and worst
    price while it was held (journal_mfe), and
  - IBKR's own account value, day by day.

IBKR's Flex statement only reaches back as far as its query is set to (30 days today), so the account value is saved to
nav_history.jsonl every time a statement is read, and the file keeps growing from there. A month before the first saved day
has trades and no account curve; widen the Flex query's period in IBKR (Reports > Flex Queries) and the next read backfills it.
"""
import json
import os
import collections
from datetime import date, datetime

import config

OUT = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
NAV_FILE = os.path.join(OUT, "nav_history.jsonl")
DEFAULT_FX = 1.3974       # USD to CAD, only used before any statement has said otherwise


def _iso(d):
    d = str(d)
    return d if "-" in d else d[:4] + "-" + d[4:6] + "-" + d[6:]


def nav_rows():
    rows = {}
    try:
        with open(NAV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    rows[r["date"]] = r
    except OSError:
        pass
    return [rows[k] for k in sorted(rows)]


def record_nav(statement):
    """Keep every account-value day the statement carries; later reads overwrite the same day."""
    navs = (statement or {}).get("nav") or []
    if not navs:
        return 0
    fx = next((p.get("fx_to_base") for p in (statement.get("positions") or []) if p.get("currency") == "USD" and p.get("fx_to_base")), None)
    have = {r["date"]: r for r in nav_rows()}
    for n in navs:
        d = _iso(n["date"])
        have[d] = {"date": d, "total": round(n["total"], 2), "cash": round(n.get("cash") or 0, 2), "fx": fx or have.get(d, {}).get("fx")}
    os.makedirs(OUT, exist_ok=True)
    tmp = NAV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in sorted(have):
            f.write(json.dumps(have[k]) + "\n")
    os.replace(tmp, NAV_FILE)
    return len(navs)


def ensure_nav(log=lambda *a: None):
    """Read a statement when the saved history is stale; harmless if the broker is not reachable."""
    rows = nav_rows()
    if rows and (date.today() - date.fromisoformat(rows[-1]["date"])).days <= 3:
        return
    try:
        import ibkr_flex
        record_nav(ibkr_flex.parse(ibkr_flex.fetch_statement(log=log)))
    except Exception as error:
        log(f"  monthly review: could not refresh account history ({str(error)[:80]})")


def _index_closes(key):
    path = os.path.join(config.data_dir("US"), f"index_{key}.jsonl")
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    out[r["date"]] = r["close"]
    except OSError:
        pass
    return out


def available(account="NG-IBKR"):
    import notion_sync
    rows = [t for t in notion_sync.fetch_trades(log=lambda *a: None) if t["account"] == account and not t.get("parentId")]
    return months_available(rows)


def months_available(trades):
    c = collections.Counter((t.get("dateClosed") or "")[:7] for t in trades if t.get("dateClosed"))
    return [{"m": m, "n": n} for m, n in sorted(c.items()) if m]


def build(months, account="NG-IBKR"):
    """`months`: list of 'YYYY-MM'. The review covers those months' closed positions and the account curve between them."""
    import notion_sync
    import journal_mfe
    months = sorted(set(m for m in months if len(m) == 7))
    if not months:
        raise ValueError("pick at least one month")
    start = months[0] + "-01"
    y, m = int(months[-1][:4]), int(months[-1][5:])
    end = f"{y + (m == 12)}-{(m % 12) + 1:02d}-01"            # exclusive: first day of the month after the last one
    ensure_nav() if account == "NG-IBKR" else None
    all_rows = [t for t in notion_sync.fetch_trades(log=lambda *a: None) if t["account"] == account]
    byid = {t["pageId"]: t for t in all_rows}
    groups = collections.defaultdict(list)
    for t in all_rows:
        groups[(byid.get(t.get("parentId")) or t)["pageId"]].append(t)
    cur = (all_rows[0].get("currency") if all_rows else "USD") or "USD"
    navs = nav_rows() if account == "NG-IBKR" else []
    fx = next((r["fx"] for r in reversed(navs) if r.get("fx")), None) or (DEFAULT_FX if cur == "USD" and account == "NG-IBKR" else 1.0)
    out = []
    for cid, rows in groups.items():
        c = byid[cid]
        if not c.get("dateClosed") or c["dateClosed"][:7] not in months:
            continue
        opened = min(r["dateOpened"] for r in rows if r.get("dateOpened"))
        cost = sum((r.get("costValue") or 0) for r in rows)
        pnl = sum((r.get("pnl") or 0) for r in rows)
        mf = journal_mfe.trade_row(c) or {}
        out.append({"id": cid, "t": c["ticker"], "logoid": c.get("logoid"), "setup": c.get("entrySetup") or "", "opened": opened, "closed": c["dateClosed"],
                    "hold": c.get("holdDays") or 0, "pnl": round(pnl, 2), "cost": round(cost, 2), "pct": round(pnl / cost * 100, 2) if cost else 0,
                    "mfe": round((mf.get("mfe") or 0) * 100, 2), "mae": round((mf.get("mae") or 0) * 100, 2), "stop": c.get("initialStop"),
                    "adds": len(rows) - 1, "peak": mf.get("peak"), "peakDate": mf.get("peakDate")})
    ndx = _index_closes("nasdaq100")
    series = []
    for r in navs:
        if start <= r["date"] < end:
            series.append([r["date"], round(r["total"]), ndx.get(r["date"])])
    # Months picked can skip a month between them (September and November): the curve is drawn only inside each picked month.
    series = [s for s in series if s[0][:7] in months]
    # The close just before the first month is the base the month is measured from (30 Sept for October).
    before = [r for r in navs if r["date"] < start]
    if before and series and (date.fromisoformat(start) - date.fromisoformat(before[-1]["date"])).days <= 5:
        series.insert(0, [before[-1]["date"], round(before[-1]["total"]), ndx.get(before[-1]["date"])])
    return {"months": months, "start": start, "end": end, "account": account, "currency": cur, "fx": fx,
            "display": "C$" if (cur == "USD" and account == "NG-IBKR") else ("₹" if cur == "INR" else "$"),
            "trades": sorted(out, key=lambda r: r["closed"]), "nav": series, "navFrom": navs[0]["date"] if navs else None,
            "available": months_available([t for cid, rows in groups.items() for t in rows if t["pageId"] == cid])}
