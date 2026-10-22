"""Accumulation / distribution, the O'Neil way, for the indices and for every stock.

Index (per index that has its own volume):
  distribution day  the index closes down at least 0.2% on volume above the prior day's
  accumulation day  the mirror image: up at least 0.2% on volume above the prior day's
  follow-through day (FTD)  day 4 or later of a rally attempt (the first up close after a low, when the index is at least
                    5% under its recent high) that closes up at least 1.25% on higher volume
  A distribution day stays live for 25 sessions. The page applies the thresholds, the window and the optional
  "drop it once the index is 5% above it" rule, so they can be changed there; this file carries the series and the FTDs.

Stock (last 25 sessions and 13 weeks):
  distribution days  down on volume above the prior day's;  accumulation days  up on volume above the prior day's
  up/down volume     volume on up days divided by volume on down days over 13 weeks
  A-E rating         the 13-week up/down volume ratio ranked across the universe in fifths (A = top fifth). This follows
                     IBD's A-E idea, but IBD's own formula is not published, so the letters will not match theirs.
"""
import json
import os

import numpy as np
import pandas as pd

import config
from metrics import breadth_internals as bi

INDEX_KEYS = {"US": ["nasdaq", "sp500", "russell2000"], "IN": []}


def _index_series(country, key):
    path = os.path.join(config.data_dir(country), f"index_{key}.jsonl")
    if not os.path.exists(path):
        return None
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()][-520:]
    recent = rows[-260:]
    if not recent or sum(1 for r in recent if r.get("volume")) < len(recent) * 0.6:
        return None                       # an index with no volume of its own cannot have distribution days
    d = [r["date"] for r in rows]
    c = [r["close"] for r in rows]
    v = [r.get("volume") or 0 for r in rows]
    n = len(c)
    ftd, att, events = [0] * n, None, []
    hi50 = pd.Series(c).rolling(50, min_periods=1).max().values
    for i in range(2, n):
        if att is None and c[i] > c[i - 1] and c[i - 1] <= min(c[max(0, i - 10):i]) and c[i - 1] <= hi50[i - 1] * 0.95:
            att = {"low": c[i - 1], "day1": i}
        elif att is not None:
            if c[i] < att["low"]:
                att = None
            elif i - att["day1"] + 1 >= 4 and v[i] and v[i - 1] and c[i] / c[i - 1] - 1 >= 0.0125 and v[i] > v[i - 1]:
                ftd[i] = 1
                events.append(d[i])
                att = None
    s = slice(-260, None)
    return {"d": d[s], "o": [round(r.get("open") or r["close"], 2) for r in rows][s], "h": [round(r["high"], 2) for r in rows][s],
            "l": [round(r["low"], 2) for r in rows][s], "c": [round(x, 2) for x in c][s], "v": v[s], "ftd": ftd[s]}


def compute_indices(country):
    labels = {"index_nasdaq": "Nasdaq Composite", "index_sp500": "S&P 500", "index_russell2000": "Russell 2000"}
    out = {}
    for key in INDEX_KEYS.get(country, []):
        s = _index_series(country, key)
        if s:
            out[key] = {"label": labels.get(f"index_{key}", key), "series": s}
    return out


def compute_stocks(country, log=print):
    min_price, min_dv = bi.UNIVERSE.get(country, (5.0, 5e6))
    raw, master = bi._load(country)
    if not raw:
        return None
    rows = {}
    for t, (dates, cols) in raw.items():
        if dates[-1] != master[-1]:
            continue
        close = np.array([x if x is not None else np.nan for x in cols[3]], float)
        vol = np.array([x if x is not None else np.nan for x in cols[4]], float)
        if len(close) < 66 or np.isnan(close[-1]) or close[-1] < min_price or np.isnan(vol[-25:]).all():
            continue
        if min_dv and np.nanmean((close * vol)[-20:]) < min_dv:
            continue
        ret = close[1:] / close[:-1] - 1
        vup = vol[1:] > vol[:-1]
        dist25 = int(np.sum((ret[-25:] < 0) & vup[-25:]))
        acc25 = int(np.sum((ret[-25:] > 0) & vup[-25:]))
        av50 = np.nanmean(vol[-50:])
        heavy = int(np.sum((ret[-25:] < -0.002) & (vol[1:][-25:] >= 1.4 * av50)))
        r13, v13 = ret[-65:], vol[1:][-65:]
        ud13 = float(np.nansum(v13[r13 > 0]) / max(1.0, np.nansum(v13[r13 < 0])))
        rows[t] = (ud13, dist25, acc25, heavy)
    if not rows:
        return None
    ud = np.array([v[0] for v in rows.values()])
    cuts = [float(x) for x in np.quantile(ud, [0.2, 0.4, 0.6, 0.8])]

    def rate(x):
        return "A" if x >= cuts[3] else "B" if x >= cuts[2] else "C" if x >= cuts[1] else "D" if x >= cuts[0] else "E"

    log(f"  accumulation/distribution: {len(rows)} stocks, A-E cuts {[round(c, 2) for c in cuts]}")
    return {"cuts": [round(c, 3) for c in cuts], "t": {t: [rate(v[0]), round(v[0], 2), v[1], v[2], v[3]] for t, v in rows.items()}}


def write(country, log=print):
    base = config.data_dir(country)
    idx = compute_indices(country)
    with open(os.path.join(base, "accdist_index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    st = compute_stocks(country, log)
    if st:
        with open(os.path.join(base, "accdist_stocks.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
    log(f"  accumulation/distribution: indices {list(idx)}, stocks {len((st or {}).get('t', {}))}")
