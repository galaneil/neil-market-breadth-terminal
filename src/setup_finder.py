"""Setup finder: big up-moves in the stored price history, with a likely entry point, for building a discretionary
setups list by hand.

How a move is found (all on the per-ticker price files the terminal already keeps, 2020 to now):
  1. For every day i, look at the next W sessions (the "window") and take the highest close. The day with the best
     gain from its own close to that high is the start of a move; the high is the peak. Moves for one stock cannot overlap.
  2. The entry candidate is the first close after the start that clears the previous 40 sessions' high while above
     its 50-day average (a breakout out of a base). If there is none before the peak, the start itself is the entry.
  3. Everything that can be filtered on (price, dollar volume, ADR) is measured at the entry, from the sessions before it.

Nothing here classifies a setup; that is the reader's call (and an agent's suggestion, stored separately).
Stocks that are no longer listed are not in the price files, so results lean toward survivors.
"""
import json
import os
import threading
from datetime import date

import numpy as np
import pandas as pd

import config

_LOCK = threading.Lock()
_TABLE = {}          # country -> {"stamp": float, "names": [...]}
_CACHE = {}          # (country, params-key) -> rows
STORE = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local", "setup_finder.json")
MAX_LEGS = 3         # moves kept per stock
PIVOT_DAYS = 40      # a breakout clears the prior 40 sessions' high


def _folder(country):
    return os.path.join(config.docs_dir(country), config.TICKER_DIR_NAME)


def _stamp(country):
    newest = 0.0
    try:
        with os.scandir(_folder(country)) as it:
            for e in it:
                if e.name.endswith(".json") and not e.name.startswith("_"):
                    newest = max(newest, e.stat().st_mtime)
    except OSError:
        pass
    return newest


def _classification(country):
    try:
        with open(os.path.join(config.data_dir(country), "classification.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load(country):
    """Compact arrays per stock. Only names that ever traded real dollar volume are kept."""
    stamp = _stamp(country)
    with _LOCK:
        hit = _TABLE.get(country)
        if hit and hit["stamp"] == stamp:
            return hit["names"]
    cls = _classification(country)
    names = []
    for fn in os.listdir(_folder(country)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            with open(os.path.join(_folder(country), fn), encoding="utf-8") as f:
                d = json.load(f)
            dates = d["dates"]
            if len(dates) < 250:
                continue
            c = np.array([np.nan if x is None else x for x in d["close"]], dtype=np.float32)
            h = np.array([np.nan if x is None else x for x in d["high"]], dtype=np.float32)
            l = np.array([np.nan if x is None else x for x in d["low"]], dtype=np.float32)
            v = np.array([np.nan if x is None else x for x in (d.get("volume") or [None] * len(dates))], dtype=np.float32)
        except Exception:
            continue
        for arr in (c, h, l):
            arr[arr <= 0] = np.nan                    # a zero close is a missing bar, not a price
        dv = c * v
        if np.isfinite(dv).sum() and float(np.nanmedian(dv)) < 3e6:      # never liquid enough to matter
            continue
        t = fn[:-5]
        info = cls.get(t) or [None, None, None]
        names.append({"t": t, "sector": info[0] or "", "industry": info[1] or "", "d": dates, "c": c, "h": h, "l": l, "v": v})
    with _LOCK:
        _TABLE[country] = {"stamp": stamp, "names": names}
        _CACHE.clear()
    return names


def _legs(n, window, min_gain):
    """Up to MAX_LEGS non-overlapping (start, peak) index pairs for one stock."""
    c = n["c"].astype(np.float64)
    if len(c) < window + 60:
        return []
    ser = pd.Series(c[::-1])
    fut = ser.rolling(window, min_periods=1).max().values[::-1]          # highest close from i to i+window-1
    with np.errstate(invalid="ignore", divide="ignore"):
        gain = fut / c - 1
    gain[-5:] = -1                                                      # a start with no room to run
    gain[:55] = -1                                                      # need history for the 50-day average
    out = []
    for _ in range(MAX_LEGS):
        i = int(np.nanargmax(gain)) if np.isfinite(gain).any() else -1
        if i < 0 or not gain[i] >= min_gain:
            break
        j = i + int(np.nanargmax(c[i:i + window]))
        out.append((i, j))
        gain[max(0, i - window):j + 1] = -1
    return out


def _entry(n, i, j):
    """First breakout after the start: close above the prior 40 sessions' high and above the 50-day average."""
    c, h = n["c"].astype(np.float64), n["h"].astype(np.float64)
    sma = pd.Series(c).rolling(50, min_periods=50).mean().values
    for k in range(i + 1, j):
        if k < PIVOT_DAYS:
            continue
        hi = np.nanmax(h[k - PIVOT_DAYS:k])
        if c[k] > hi and c[k] > sma[k]:
            return k, "breakout"
    return i, "start"


BASE_LOOKBACK = 120      # the base is measured from the highest high in the 120 sessions before the entry


def base_stats(h, l, c, k):
    """Depth of the pullback before an entry: prior high, the lowest low since it, how many sessions ago the high was,
    and how far the entry close sits below that high. (The page repeats this rule when you change an entry.)"""
    lo = max(0, k - BASE_LOOKBACK)
    if k - lo < 5:
        return None
    seg = h[lo:k]
    if not np.isfinite(seg).any():
        return None
    hi_i = lo + int(np.nanargmax(seg))
    prior = float(h[hi_i])
    trough = float(np.nanmin(l[hi_i:k + 1]))
    if not (prior > 0 and trough > 0):
        return None
    return {"priorHigh": round(prior, 2), "depth": round((trough / prior - 1) * 100, 1), "baseDays": k - hi_i,
            "offHigh": round((float(c[k]) / prior - 1) * 100, 1)}


def _row(n, i, j, k, kind):
    c, h, l, v = n["c"], n["h"], n["l"], n["v"]
    lo20 = max(0, k - 20)
    with np.errstate(invalid="ignore", divide="ignore"):
        adr = float(np.nanmean((h[lo20:k] - l[lo20:k]) / l[lo20:k]) * 100) if k > lo20 else None
    dvs = (c[max(0, k - 50):k] * v[max(0, k - 50):k])
    dv = float(np.nanmean(dvs) / 1e6) if np.isfinite(dvs).any() else None
    sma = float(np.nanmean(c[max(0, k - 49):k + 1])) if k >= 49 else None
    d = n["d"]
    return {
        "key": n["t"] + "|" + d[i], "t": n["t"], "sector": n["sector"], "industry": n["industry"], "year": int(d[i][:4]),
        "start": d[i], "startPx": round(float(c[i]), 2), "entry": d[k], "entryPx": round(float(c[k]), 2), "entryType": kind,
        "peak": d[j], "peakPx": round(float(c[j]), 2), "days": j - i, "daysFromEntry": j - k,
        "gainStart": round((float(c[j]) / float(c[i]) - 1) * 100, 1), "gainEntry": round((float(c[j]) / float(c[k]) - 1) * 100, 1),
        "adr": None if adr is None or adr != adr else round(adr, 1), "dv": None if dv is None or dv != dv else round(dv, 1),
        "aboveSma50": None if sma is None else bool(c[k] > sma),
        **(base_stats(h, l, c, k) or {"priorHigh": None, "depth": None, "baseDays": None, "offHigh": None}),
    }


def scan(country, p):
    """Rows for the filter set `p`. Window and start-gain shape which moves exist; everything else filters them."""
    names = _load(country)
    window = max(10, min(252, int(p.get("window") or 126)))
    min_gain = float(p.get("minGainStart") or 60) / 100
    key = (country, window, round(min_gain, 3))
    with _LOCK:
        rows = _CACHE.get(key)
    if rows is None:
        rows = []
        for n in names:
            for i, j in _legs(n, window, min_gain):
                k, kind = _entry(n, i, j)
                if not (n["c"][i] > 0 and n["c"][k] > 0 and n["c"][j] > 0):
                    continue
                rows.append(_row(n, i, j, k, kind))
        with _LOCK:
            _CACHE[key] = rows
            if len(_CACHE) > 12:
                _CACHE.pop(next(iter(_CACHE)))
    years = {int(y) for y in (p.get("years") or [])}
    sectors = set(p.get("sectors") or [])
    q = (p.get("q") or "").strip().upper()
    num = lambda k, d=None: (float(p[k]) if p.get(k) not in (None, "") else d)
    min_gain_e, max_days, min_px, min_dv, min_adr, max_adr = num("minGainEntry"), num("maxDays"), num("minPrice"), num("minDv"), num("minAdr"), num("maxAdr")
    min_depth, max_depth, max_off = num("minDepth"), num("maxDepth"), num("maxOffHigh")
    out = []
    for r in rows:
        if years and r["year"] not in years:
            continue
        if sectors and r["sector"] not in sectors:
            continue
        if q and q not in r["t"]:
            continue
        if p.get("entryType") in ("breakout", "start") and r["entryType"] != p["entryType"]:
            continue
        if p.get("aboveSma50") and r["aboveSma50"] is not True:
            continue
        if min_gain_e is not None and r["gainEntry"] < min_gain_e:
            continue
        if max_days is not None and r["days"] > max_days:
            continue
        if min_px is not None and r["entryPx"] < min_px:
            continue
        if min_dv is not None and (r["dv"] is None or r["dv"] < min_dv):
            continue
        if min_depth is not None and (r["depth"] is None or -r["depth"] < min_depth):      # depth is negative: -35 = a 35% pullback
            continue
        if max_depth is not None and (r["depth"] is None or -r["depth"] > max_depth):
            continue
        if max_off is not None and (r["offHigh"] is None or -r["offHigh"] > max_off):
            continue
        if min_adr is not None and (r["adr"] is None or r["adr"] < min_adr):
            continue
        if max_adr is not None and (r["adr"] is None or r["adr"] > max_adr):
            continue
        out.append(r)
    sort = p.get("sort") or "gainEntry"
    out.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0)) if sort not in ("t", "start") else (False, r[sort]))
    per_year = int(num("perYear", 100) or 100)
    kept, seen = [], {}
    for r in out:
        seen[r["year"]] = seen.get(r["year"], 0) + 1
        if seen[r["year"]] <= per_year:
            kept.append(r)
    sectors_all = sorted({r["sector"] for r in rows if r["sector"]})
    return {"rows": kept, "matched": len(out), "moves": len(rows), "stocks": len(names), "sectors": sectors_all,
            "asOf": max((n["d"][-1] for n in names), default=None)}


# ---- your edits, saved filter sets, and the agents' suggestions (one small file) ----
def _read():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    for k in ("edits", "filters", "suggestions"):
        d.setdefault(k, {})
    return d


def _write(d):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, STORE)


def state():
    return _read()


def save_edit(key, fields):
    """Merge `fields` (entry date/price, setup, note, status) into one row's edits; an empty dict clears it."""
    d = _read()
    allowed = {"entry", "entryPx", "setup", "note", "status", "priorHigh", "depth", "baseDays", "offHigh", "savedPageId", "savedUrl", "savedSetup"}
    clean = {k: v for k, v in (fields or {}).items() if k in allowed}
    if clean:
        d["edits"][key] = {**d["edits"].get(key, {}), **clean, "editedAt": date.today().isoformat()}
    else:
        d["edits"].pop(key, None)
    _write(d)
    return d["edits"].get(key)


def save_filter(name, params, delete=False):
    d = _read()
    if delete:
        d["filters"].pop(name, None)
    else:
        d["filters"][name] = params
    _write(d)
    return d["filters"]


def warm(country="US"):
    try:
        _load(country)
    except Exception:
        pass
