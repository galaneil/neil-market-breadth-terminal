"""Two Signals screens that look at the trend rather than a single trigger day.

New highs   -- "ath": the high of one of the last 5 sessions reached the all-time high (TradingView's full history);
               "52w": a new 52-week high in the last 5 sessions, still under the all-time high;
               "near": closing 0-20% under the all-time high with no new high this week.
EMA bounce  -- an uptrend that has respected its 20 or 50 EMA for the whole move: no close more than 2% (20) / 3% (50)
               under it and never two closes running under it, for at least 25 / 40 sessions, a move of 25% or more,
               the average rising and price above the 200 EMA, the peak within the last 40 sessions.
               "at": within 4% of the average; "near": 4-8% above it and 3%+ off the peak; "ext": respecting, not pulled back.

Read from the same per-ticker price files the other scans use (docs/[in/]tickers). Agreed with Neil on Nov 25 2026
against DOCU, BOX, HNGE, INTA and CRL.
"""
import json
import os

import config

TOL = {20: 0.02, 50: 0.03}      # a close this far under the average is a slight undercut, not a break
MIN_RUN = {20: 25, 50: 40}      # sessions the move must have lasted above the average


def _ema(c, n):
    k, e, out = 2 / (n + 1), None, []
    for v in c:
        if v is not None:
            e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _tickers_dir(code):
    sub = config.COUNTRIES[code].get("docs_subdir", "")
    return os.path.join(config.DOCS_DIR, sub, "tickers") if sub else os.path.join(config.DOCS_DIR, "tickers")


def _load(code, t):
    try:
        with open(os.path.join(_tickers_dir(code), t + ".json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not d.get("dates") or len(d["dates"]) < 260 or not d.get("close") or d["close"][-1] is None:
        return None
    return d


def _respect(d, n):
    """Facts about the current move above the n EMA, or None when it has no qualifying move.
    The closeness to the average is reported, not required: the page groups by it."""
    c, h, l = d["close"], d["high"], d["low"]
    e, e200 = _ema(c, n), _ema(c, 200)
    N, tol = len(c) - 1, TOL[n]
    v = None                     # the last real break of the average
    for i in range(N, 220, -1):
        if c[i] is None or e[i] is None:
            continue
        if c[i] < e[i] * (1 - tol) or (i < N and c[i] < e[i] and c[i - 1] is not None and e[i - 1] and c[i - 1] < e[i - 1]):
            v = i
            break
    if v is None or N - v < MIN_RUN[n]:
        return None
    rng = range(max(0, v - 5), N + 1)
    lo_i = min(rng, key=lambda i: l[i] if l[i] is not None else 1e18)
    hi_i = max(range(lo_i, N + 1), key=lambda i: h[i] if h[i] is not None else -1)
    gain = (h[hi_i] / l[lo_i] - 1) * 100
    if gain < 25 or not e[N] > e[max(0, N - 20)] or not c[N] > e200[N] or N - hi_i > 40:
        return None
    touches, under, last_t = 0, 0, -99
    for i in range(v + 1, N + 1):
        if l[i] is not None and e[i] and l[i] <= e[i] * 1.01:
            if i - last_t > 5:
                touches += 1
            last_t = i
        if c[i] is not None and e[i] and c[i] < e[i]:
            under += 1
    return {"ema": n, "run": N - v, "start": d["dates"][lo_i], "gain": round(gain, 1), "peak": d["dates"][hi_i],
            "pull": round((c[N] / h[hi_i] - 1) * 100, 1), "dist": round((c[N] / e[N] - 1) * 100, 1),
            "touches": touches, "undercuts": under}


def _bounce(d):
    """The 20 EMA when it has held and price is near it, otherwise the 50."""
    r20, r50 = _respect(d, 20), _respect(d, 50)
    if r20 and r20["dist"] <= 8:
        return r20
    return r50 or r20


def _all_time_highs(code):
    """{ticker: all-time high} from TradingView's full history; empty when it cannot be reached."""
    try:
        from tradingview_screener import Query, col
        cfg = config.COUNTRIES[code]
        _, df = (Query().set_markets(cfg["tv_market"]).select("name", "all_time_high", "exchange")
                 .where(col("exchange").isin(cfg["exchanges"])).limit(9000).get_scanner_data())
        return {r["name"]: float(r["all_time_high"]) for _, r in df.iterrows() if r["all_time_high"] == r["all_time_high"]}
    except Exception:
        return {}


def scan(code, log=print):
    """{"highs": [...], "bounce": [...]} for one market."""
    ath = _all_time_highs(code)
    if not ath:
        log("  trend scans: TradingView all-time highs unavailable, using the local history (since 2021) instead")
    try:
        with open(os.path.join(config.data_dir(code), "classification.json"), encoding="utf-8") as f:
            cls = json.load(f)
    except (OSError, ValueError):
        return {"highs": [], "bounce": []}
    highs, bounce = [], []
    for t, row in cls.items():
        d = _load(code, t)
        if not d:
            continue
        c, h, l, v = d["close"], d["high"], d["low"], d.get("volume") or []
        N = len(c) - 1
        hs = [x for x in h[N - 19:N + 1] if x]
        adr = sum((h[i] / l[i] - 1) for i in range(N - 19, N + 1) if h[i] and l[i]) / 20 * 100
        turn = sum((c[i] or 0) * (v[i] or 0) for i in range(N - 19, N + 1)) / 20 if len(v) > N else 0
        base = {"sym": t, "sector": row[0] if row else None, "industry": row[1] if len(row) > 1 else None,
                "adr": round(adr, 2), "turnover": round(turn), "marketCap": row[3] if len(row) > 3 else None,
                "price": c[N], "chg": round((c[N] / c[N - 1] - 1) * 100, 2) if c[N - 1] else None}
        if not hs:
            continue
        # new highs
        a = ath.get(t) or max(x for x in h if x is not None)
        hi52 = max(x for x in h[N - 251:N + 1] if x is not None)
        days = next((k for k in range(5) if h[N - k] is not None
                     and h[N - k] >= max(x for x in h[N - k - 251:N - k + 1] if x is not None)), None)
        at_ath = max(x for x in h[N - 4:N + 1] if x is not None) >= a * 0.999
        off = round((c[N] / a - 1) * 100, 1)
        if at_ath or days is not None or off >= -20:
            seg = "ath" if at_ath else "52w" if days is not None else "near"
            highs.append(dict(base, seg=seg, ath=round(a, 2), hi52=hi52, offAth=off, athLocal=t not in ath,
                              daysAgo=days, firedDate=d["dates"][N - days] if days is not None else None))
        # EMA bounce
        b = _bounce(d)
        if b:
            seg = "at" if b["dist"] <= 4 else "near" if b["dist"] <= 8 and b["pull"] <= -3 else "ext"
            bounce.append(dict(base, seg=seg, **b))
    return {"highs": highs, "bounce": bounce}
