"""Daily breadth internals, computed from the per-ticker price files.

The table traders read every morning: how many stocks are up or down 4% today, the 5- and 10-day
ratios of those, how many are up or down 25% in a quarter, 25% / 50% in a month, 13% in 34 days,
how many are stretched 10 ATR above their 50-day average, and the share of stocks above the 50-day
average. Plus a snapshot of advances/declines, new highs/lows and %-above-averages for the indices
and every sector, and the time series behind the charts (including McClellan from the stored daily
advancers/decliners).

Counted over a liquid universe, not every ticker, so a thin or dead name cannot move a breadth number.
India's price files carry no usable volume, so there the universe is a price floor only.
"""
import json
import os

import numpy as np
import pandas as pd

import config

NB = 330         # bars kept per ticker (enough for a 200-day average plus the chart window)
ROWS = 30        # sessions in the daily table
SERIES = 130     # sessions in the charts
# country -> (minimum price, minimum 20-day average dollar volume or None when volume is unreliable)
UNIVERSE = {"US": (5.0, 5e6), "IN": (20.0, None)}


def _load(country):
    """{ticker: {date: (o, h, l, c, v)}} trimmed to the last NB bars, plus the master date list."""
    folder = config.ticker_dir(country)
    raw, last_dates = {}, {}
    for name in os.listdir(folder):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        dates = d.get("dates") or []
        if len(dates) < 60:
            continue
        sl = slice(-NB, None)
        raw[name[:-5]] = (dates[sl], [d.get(k, [None] * len(dates))[sl] for k in ("open", "high", "low", "close", "volume")])
        last_dates[dates[-1]] = last_dates.get(dates[-1], 0) + 1
    if not raw:
        return None, None
    latest = max(last_dates)
    master = max((v[0] for v in raw.values() if v[0][-1] == latest), key=len)
    return raw, master


def compute(country, log=print):
    min_price, min_dv = UNIVERSE.get(country, (5.0, 5e6))
    raw, master = _load(country)
    if not raw:
        return None
    n = len(master)
    pos = {d: i for i, d in enumerate(master)}
    names = [t for t, v in raw.items() if v[0][-1] == master[-1]]
    arr = np.full((5, len(names), n), np.nan)
    for j, t in enumerate(names):
        dates, cols = raw[t]
        for k in range(5):
            for d, x in zip(dates, cols[k]):
                p = pos.get(d)
                if p is not None and x is not None:
                    arr[k, j, p] = x
    O, H, L, C, V = arr
    has_vol = min_dv is not None and np.isfinite(V[:, -20:]).any()
    dv20 = np.nanmean(C[:, -20:] * V[:, -20:], axis=1) if has_vol else np.full(len(names), np.nan)
    liquid = (C[:, -1] >= min_price) & ((dv20 >= min_dv) if has_vol else True)
    liq = [t for t, ok in zip(names, liquid) if ok]
    idx = {t: i for i, t in enumerate(names)}
    log(f"  breadth internals: {len(liq)} liquid of {len(names)} names")

    Cd = pd.DataFrame(C.T, index=master, columns=names)
    sma = lambda k: Cd.rolling(k, min_periods=k).mean()
    ema = lambda k: Cd.ewm(span=k, adjust=False).mean()
    sma5, sma50, sma200, ema10, ema21 = sma(5), sma(50), sma(200), ema(10), ema(21)
    ret = lambda k: Cd / Cd.shift(k) - 1
    r1, r21, r63, r24 = ret(1), ret(21), ret(63), ret(24)
    prevC = Cd.shift(1)
    Hd, Ld = pd.DataFrame(H.T, index=master, columns=names), pd.DataFrame(L.T, index=master, columns=names)
    tr = pd.concat([(Hd - Ld).stack(), (Hd - prevC).abs().stack(), (Ld - prevC).abs().stack()], axis=1).max(axis=1).unstack()
    ext = (Cd - sma50) / tr.rolling(14, min_periods=14).mean()
    sub = lambda df: df[liq]

    up4 = (sub(r1) >= .04).sum(axis=1)
    dn4 = (sub(r1) <= -.04).sum(axis=1)

    def ratio(sr_up, sr_dn, k, size):
        return round(float(sr_up.iloc[k - size + 1:k + 1].sum() / max(1, sr_dn.iloc[k - size + 1:k + 1].sum())), 2)

    rows = []
    for k in range(n - ROWS, n):
        row = {"d": master[k], "up4": int(up4.iloc[k]), "dn4": int(dn4.iloc[k]),
               "r5": ratio(up4, dn4, k, 5), "r10": ratio(up4, dn4, k, 10),
               "up25q": int((sub(r63).iloc[k] >= .25).sum()), "dn25q": int((sub(r63).iloc[k] <= -.25).sum()),
               "up25m": int((sub(r21).iloc[k] >= .25).sum()), "dn25m": int((sub(r21).iloc[k] <= -.25).sum()),
               "up50m": int((sub(r21).iloc[k] >= .50).sum()), "dn50m": int((sub(r21).iloc[k] <= -.50).sum()),
               "up13": int((sub(r24).iloc[k] >= .13).sum()), "dn13": int((sub(r24).iloc[k] <= -.13).sum()),
               "ext": int((sub(ext).iloc[k] >= 10).sum())}
        s50 = sub(sma50).iloc[k]
        row["a50"] = round(float((sub(Cd).iloc[k] > s50).sum() / max(1, s50.notna().sum()) * 100), 1)
        rows.append(row)

    def pct_above(df, k, cols):
        base = df.iloc[k][cols]
        return round(float((Cd.iloc[k][cols] > base).sum() / max(1, base.notna().sum()) * 100), 1)

    ks = range(n - SERIES, n)
    ts = {"d": master[n - SERIES:], "a10": [pct_above(ema10, k, liq) for k in ks], "a50": [pct_above(sma50, k, liq) for k in ks],
          "a200": [pct_above(sma200, k, liq) for k in ks], "up4": [int(up4.iloc[k]) for k in ks], "dn4": [int(dn4.iloc[k]) for k in ks],
          "r5": [ratio(up4, dn4, k, 5) for k in ks], "r10": [ratio(up4, dn4, k, 10) for k in ks]}

    # McClellan oscillator + summation index from the stored daily advancers/decliners
    path = os.path.join(config.data_dir(country), "breadth_adv_decl.jsonl")
    if os.path.exists(path):
        ad = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        ra = pd.Series([(a["advancers"] - a["decliners"]) / max(1, a["advancers"] + a["decliners"]) * 1000 for a in ad], index=[a["date"] for a in ad])
        osc = ra.ewm(span=19, adjust=False).mean() - ra.ewm(span=39, adjust=False).mean()
        summ = osc.cumsum()
        ts["mc_d"] = list(osc.index[-SERIES:]); ts["mc_osc"] = [round(v, 2) for v in osc.iloc[-SERIES:]]; ts["mc_sum"] = [round(v, 1) for v in summ.iloc[-SERIES:]]

    # snapshot of the latest session: indices on top, sectors below
    try:
        with open(os.path.join(config.data_dir(country), "hilo_names.json"), encoding="utf-8") as f:
            hn = json.load(f)[-1]
        hi52, lo52 = set(hn["w52"]["hi"]), set(hn["w52"]["lo"])
    except Exception:
        hi52, lo52 = set(), set()
    try:
        with open(os.path.join(config.data_dir(country), "index_membership.json"), encoding="utf-8") as f:
            mem = json.load(f)
        with open(os.path.join(config.data_dir(country), "classification.json"), encoding="utf-8") as f:
            cls = json.load(f)
    except Exception:
        mem, cls = {}, {}
    last = n - 1

    def snap(label, tickers, kind):
        cols = [t for t in tickers if t in idx and t in set(liq)]
        if len(cols) < 5:
            return None
        r = r1.iloc[last][cols]
        adv, dec = int((r > 0).sum()), int((r < 0).sum())
        nh, nl = len([t for t in cols if t in hi52]), len([t for t in cols if t in lo52])
        return {"k": kind, "n": label, "cnt": len(cols), "adv": adv, "dec": dec, "net": adv - dec,
                "nr": round((adv - dec) / max(1, adv + dec) * 100, 1), "hi": nh, "lo": nl, "hnet": nh - nl,
                "hnr": round((nh - nl) / max(1, nh + nl) * 100, 1),
                "a5": pct_above(sma5, last, cols), "a10": pct_above(ema10, last, cols), "a21": pct_above(ema21, last, cols),
                "a50": pct_above(sma50, last, cols), "a200": pct_above(sma200, last, cols)}

    snaps = [snap("All liquid stocks", names, "idx")]
    for code, label, _tv in config.SCREENER_INDEXES.get(country, []):
        snaps.append(snap(label, [t for t, v in mem.items() if code in v], "idx"))
    for sec in sorted({v[0] for v in cls.values() if v and v[0]}):
        snaps.append(snap(sec, [t for t, v in cls.items() if v and v[0] == sec], "sec"))
    snaps = [s for s in snaps if s]

    note = (f"Liquid universe: price ≥ {min_price:g} and 20-day average dollar volume ≥ ${min_dv / 1e6:g}M" if has_vol
            else f"Universe: price ≥ {min_price:g} (volume is not reliable for this market)")
    return {"rows": rows, "ts": ts, "snaps": snaps, "date": master[-1], "universe": len(liq), "note": note}


def write(country, log=print):
    data = compute(country, log)
    if data is None:
        log("  breadth internals: no price files, skipped")
        return None
    path = os.path.join(config.data_dir(country), "breadth_internals.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    log(f"  breadth internals: wrote {path} ({os.path.getsize(path) // 1024} KB)")
    return path
