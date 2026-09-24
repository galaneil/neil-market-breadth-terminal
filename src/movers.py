"""Movers scan: which names moved at least X% over a chosen period.

Reads the per-ticker price files the terminal already keeps (docs/tickers, or
docs/in/tickers), condenses each to the few numbers the scan needs, and keeps
that table in memory. It rebuilds only when the price files change, so a scan
after the first is instant.
"""
import json
import os
import threading

import config

_LOCK = threading.Lock()
_TABLE = {}          # country -> {"stamp": float, "rows": [...]}
KEEP = 127           # closes kept per name: enough for a 126-session move


def _tickers_dir(country):
    return os.path.join(config.docs_dir(country), config.TICKER_DIR_NAME)


def _stamp(country):
    """Newest price-file modification time: changes whenever any file is rewritten."""
    folder = _tickers_dir(country)
    newest = 0.0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.endswith(".json") and not e.name.startswith("_"):
                    newest = max(newest, e.stat().st_mtime)
    except OSError:
        pass
    return newest


def _cache_file(country):
    out = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, "movers_cache_" + country + ".json")


def _classification(country):
    path = os.path.join(config.data_dir(country), "classification.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _build(country):
    cls = _classification(country)
    folder = _tickers_dir(country)
    rows = []
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json") and not n.startswith("_")]
    except OSError:
        return rows
    for name in names:
        symbol = name[:-5]
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                d = json.load(f)
            close, high, low, vol, dates = d["close"], d["high"], d["low"], d.get("volume") or [], d["dates"]
        except (OSError, ValueError, KeyError):
            continue
        if len(close) < 3 or close[-1] is None:
            continue
        tail = [c for c in close[-KEEP:]]
        adr = [h / l - 1 for h, l in zip(high[-20:], low[-20:]) if h and l]
        dv = [c * v for c, v in zip(close[-20:], vol[-20:]) if c and v]
        hi52 = max((h for h in high[-252:] if h), default=None)
        info = cls.get(symbol) or []
        rows.append({
            "t": symbol, "sector": info[0] if len(info) > 0 else None,
            "industry": info[1] if len(info) > 1 else None,
            "logo": info[2] if len(info) > 2 else None,
            "date": dates[-1], "close": close[-1], "c": tail,
            "adr": round(sum(adr) / len(adr) * 100, 2) if adr else None,
            "dv": sum(dv) / len(dv) if dv else None,
            "off": round((close[-1] / hi52 - 1) * 100, 1) if hi52 else None,
        })
    return rows


_BUILDING = set()


def _refresh(country, stamp):
    try:
        rows = _build(country)
        with _LOCK:
            _TABLE[country] = {"stamp": stamp, "rows": rows}
        with open(_cache_file(country), "w", encoding="utf-8") as f:
            json.dump({"stamp": stamp, "rows": rows}, f, separators=(",", ":"))
    finally:
        _BUILDING.discard(country)


def table(country):
    """The condensed table, or None while the very first build is still running.
    A stale table keeps being served while a fresh one builds in the background."""
    stamp = _stamp(country)
    with _LOCK:
        hit = _TABLE.get(country)
        if hit is None:
            try:
                with open(_cache_file(country), encoding="utf-8") as f:
                    hit = json.load(f)
                _TABLE[country] = hit
            except (OSError, ValueError):
                hit = None
        if (hit is None or hit["stamp"] != stamp) and country not in _BUILDING:
            _BUILDING.add(country)
            threading.Thread(target=_refresh, args=(country, stamp), daemon=True).start()
    return hit["rows"] if hit else None


def warm():
    for code in config.COUNTRIES:
        table(code)


def sectors(country):
    return sorted({r["sector"] for r in (table(country) or []) if r["sector"]})


def scan(country, days=21, pct=20.0, direction="up", picked=None, min_price=0.0, min_dv=0.0, unclassified=False):
    days = max(1, min(int(days), KEEP - 1))
    rows = table(country)
    if rows is None:
        return {"building": True}
    out = []
    for r in rows:
        c = r["c"]
        if len(c) <= days or not c[-1] or not c[-1 - days]:
            continue
        move = (c[-1] / c[-1 - days] - 1) * 100
        if direction == "up" and move < pct:
            continue
        if direction == "down" and move > -pct:
            continue
        if direction == "either" and abs(move) < pct:
            continue
        if not r["sector"] and not unclassified:
            continue
        if picked and r["sector"] not in picked:
            continue
        if r["close"] < min_price or (min_dv and (r["dv"] or 0) < min_dv):
            continue
        out.append({"t": r["t"], "sector": r["sector"], "industry": r["industry"], "logo": r["logo"],
                    "close": round(r["close"], 2), "move": round(move, 1),
                    "d1": round((c[-1] / c[-2] - 1) * 100, 1) if c[-2] else None,
                    "adr": r["adr"], "dv": round((r["dv"] or 0) / 1e6, 1), "off": r["off"]})
    out.sort(key=lambda x: -abs(x["move"]))
    return {"asOf": max((r["date"] for r in rows), default=None),
            "universe": len(rows), "days": days, "rows": out, "building": country in _BUILDING}
