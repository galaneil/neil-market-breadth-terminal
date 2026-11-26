"""IPOs screen (India): every NSE stock that listed in the last 4 years with a market cap of Rs 500 Cr or more, and the
setup it is in, read from its listing day. Agreed with Neil on Nov 26 2026 against DHOOTTRANS (flat base), INDOMIM (VCP,
base on base), ORIENTCABL (cup with handle), PROTEAN (reclaim after a 71% fall) and TECHNOCRAF (flat base).

Setups, checked in this order:
  breakout  a new post-IPO high after a base of 15+ sessions
  reclaim   fell 40%+ from the post-IPO high and is back above its 200 EMA (50 EMA under 200 sessions old), within 15%
  flat      40+ sessions with closes inside a 25% range, in its upper half
  vcp       3+ pullbacks from the post-IPO high, each smaller, the last 12% or less, within 15% of the high, above the 50
  cup       a first pullback of 15-40%, every later dip 15% or less, back within 15% of the high
  flat      15+ sessions in a 25% range (when nothing else fits)
  new       under 15 sessions old;  other: no clean setup yet
IPO dates and the list come from TradingView's screener (ipo_offer_date); prices are the terminal's own files, and Yahoo
(.NS) for listings the nightly universe does not cover, saved in the same format. The IPO index is our own equal-weight
index of these IPOs (NSE's Nifty IPO index is not on Yahoo).
"""
import json
import math
import os
import time

import config

MIN_CAP = 5e9            # Rs 500 Cr
YEARS = 4


def ema(c, n):
    k, e, out = 2 / (n + 1), None, []
    for v in c:
        if v is not None:
            e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def swings(h, l, th=0.05, start=1):
    """Zigzag on highs and lows: alternating swing highs (H) and lows (L) that each move at least th."""
    piv, mode, ext = [], "up", start
    for i in range(start + 1, len(h)):
        if mode == "up":
            if h[i] >= h[ext]: ext = i
            elif l[i] <= h[ext] * (1 - th): piv.append(("H", ext)); mode, ext = "down", i
        else:
            if l[i] <= l[ext]: ext = i
            elif h[i] >= l[ext] * (1 + th): piv.append(("L", ext)); mode, ext = "up", i
    piv.append(("H" if mode == "up" else "L", ext))
    return piv


def classify(d, meta):
    D, o, h, l, c, v = d["dates"], d["open"], d["high"], d["low"], d["close"], d.get("volume") or []
    # start at the listing: the first session on or after the IPO date (some files begin years earlier for old listings)
    k0 = next((i for i, x in enumerate(D) if x >= meta["ipoDate"]), None)
    if k0 is None:
        return None
    D, o, h, l, c, v = D[k0:], o[k0:], h[k0:], l[k0:], c[k0:], v[k0:]
    keep = [i for i in range(len(c)) if c[i] is not None and h[i] is not None and l[i] is not None]
    D = [D[i] for i in keep]; o = [o[i] for i in keep]; h = [h[i] for i in keep]; l = [l[i] for i in keep]; c = [c[i] for i in keep]; v = [v[i] if i < len(v) else 0 for i in keep]
    n = len(c)
    if n < 15:
        return {"seg": "new", "why": "listed %d sessions ago, too early for a base" % n, "age": n}
    N = n - 1
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    first = o[0] or c[0]
    hi_i = max(range(1, n), key=lambda i: h[i])
    H = h[hi_i]
    lo_after = min(l[hi_i:]) if hi_i < N else l[N]
    run1 = (max(h[:min(n, 10)]) / first - 1) * 100               # first two weeks
    out = {"age": n, "listed": D[0], "listPx": first, "postHigh": H, "postHighDate": D[hi_i], "offHigh": round((c[N] / H - 1) * 100, 1),
           "fromList": round((c[N] / first - 1) * 100, 1), "run1": round(run1, 1), "depth": round((lo_after / H - 1) * 100, 1)}
    piv = swings(h, l, 0.05, 1)
    # contractions: each swing high to the next swing low, after the post-IPO peak region
    contr = []
    for a, b in zip(piv, piv[1:]):
        if a[0] == "H" and b[0] == "L":
            contr.append((a[1], b[1], round((l[b[1]] / h[a[1]] - 1) * 100, 1)))
    out["contractions"] = [x[2] for x in contr[-4:]]
    above20, above50 = c[N] >= e20[N], c[N] >= e50[N]
    # 1. breakout to a new post-IPO high after at least 15 sessions of base
    prior_hi = max(h[:max(1, N - 4)])
    prior_i = max(range(max(1, N - 4)), key=lambda i: h[i])
    if max(h[N - 4:]) >= prior_hi and N - 4 - prior_i >= 15:
        out.update(seg="breakout", why="new post-IPO high after a %d-session base" % (N - 4 - prior_i))
        return out
    # deep decline then reclaim of the 200 (or the 50 for listings under 200 sessions)
    ref = e200 if n >= 200 else e50
    if out["depth"] <= -40 and c[N] > ref[N] and c[N] <= ref[N] * 1.15:
        out.update(seg="reclaim", why="fell %.0f%% from its post-IPO high, now back above the %d EMA" % (-out["depth"], 200 if n >= 200 else 50))
        return out
    # flat base: the longest stretch ending today whose closes stay within 25%, at least 15 sessions, after the first week
    lo_c = hi_c = c[N]; length = 0
    for i in range(N, 4, -1):
        lo_c, hi_c = min(lo_c, c[i]), max(hi_c, c[i])
        if hi_c / lo_c - 1 > 0.25:
            break
        length = N - i + 1
    if length >= 40 and c[N] >= lo_c + 0.5 * (hi_c - lo_c):
        out.update(seg="flat", why="%d sessions in a %.0f%% range, in its upper half" % (length, (hi_c / lo_c - 1) * 100), baseLen=length)
        return out
    # the base's contractions, measured the classic way: from the base high to the lowest low after it, then from the
    # highest high after that low to the lowest low after it, and so on, while each leg is at least 3% and 3 sessions
    base, top = [], hi_i
    while top < N - 2:
        lo = min(range(top, n), key=lambda i: l[i])
        dep = (l[lo] / h[top] - 1) * 100
        if dep > -3 or lo == top:
            break
        base.append((top, lo, round(dep, 1)))
        if lo >= N - 1:
            break
        # the next rally top: the first swing high that is followed by a drop of 3% or more (else the high so far)
        top, run = lo + 1, lo + 1
        for i in range(lo + 1, n):
            if h[i] >= h[run]: run = i
            elif l[i] <= h[run] * 0.97: break
        top = run
    out["contractions"] = [x[2] for x in base]
    near_high = out["offHigh"] >= -15
    bob = None
    for st in range(1, max(1, hi_i - 15)):
        seg = c[st:st + 15]
        if len(seg) == 15 and max(seg) / min(seg) - 1 <= 0.12 and max(h[st + 15:hi_i + 1] or [0]) >= max(seg) * 1.3:
            bob = (D[st], D[st + 14])
    if bob: out["bob"] = bob      # shown with VCP, cup and breakout setups only
    if len(base) >= 3 and all(abs(base[j][2]) < abs(base[j - 1][2]) for j in range(len(base) - 2, len(base))) and abs(base[-1][2]) <= 12 and near_high and above50:
        out.update(seg="vcp", why="pullbacks " + " \u2192 ".join("%.0f%%" % abs(x[2]) for x in base[-4:]) + ", each smaller" + (", base on base" if bob else ""))
        return out
    # cup with handle: the first leg is the cup (15-40%), every later dip is part of the handle (15% or less)
    if len(base) >= 2 and 15 <= abs(base[0][2]) <= 40 and all(abs(x[2]) <= 15 for x in base[1:]) and near_high:
        out.update(seg="cup", why="cup %.0f%% deep, handle %.0f%%" % (abs(base[0][2]), max(abs(x[2]) for x in base[1:])))
        return out
    # flat base: the longest stretch ending today whose closes stay within 25%, at least 15 sessions, after the first week
    lo_c = hi_c = c[N]; length = 0
    for i in range(N, 4, -1):
        lo_c, hi_c = min(lo_c, c[i]), max(hi_c, c[i])
        if hi_c / lo_c - 1 > 0.25:
            break
        length = N - i + 1
    if length >= 15 and c[N] >= lo_c + 0.5 * (hi_c - lo_c):
        out.update(seg="flat", why="%d sessions in a %.0f%% range, in its upper half" % (length, (hi_c / lo_c - 1) * 100), baseLen=length)
        return out
    out.update(seg="other", why="no clean setup yet" + ("" if above50 else ", under its 50 EMA"))
    return out




def ipo_list(country="IN", log=print):
    """{ticker: meta} for NSE-listed IPOs of the last YEARS years with a market cap of MIN_CAP or more."""
    from tradingview_screener import Query, col
    since = time.time() - YEARS * 365 * 86400
    _, df = (Query().set_markets("india").select("name", "ipo_offer_date")
             .where(col("ipo_offer_date") > since).limit(5000).get_scanner_data())
    names = sorted(set(df["name"]))
    _, nse = (Query().set_markets("india").select("name", "exchange", "market_cap_basic", "sector", "industry", "logoid", "description")
              .where(col("exchange") == "NSE", col("name").isin(names)).limit(5000).get_scanner_data())
    nse = {r["name"]: r for _, r in nse.iterrows()}
    out = {}
    for _, r in df.iterrows():
        t, d = r["name"], r["ipo_offer_date"]
        if t not in nse or d != d:
            continue
        cap = nse[t]["market_cap_basic"]
        if not cap or cap != cap or cap < MIN_CAP:
            continue
        if t in out and d >= out[t]["ipoTs"]:
            continue
        out[t] = {"sym": t, "name": nse[t]["description"], "ipoTs": float(d), "ipoDate": time.strftime("%Y-%m-%d", time.gmtime(d)),
                  "cap": float(cap), "sector": nse[t]["sector"], "industry": nse[t]["industry"], "logoid": nse[t]["logoid"] or None}
    return out


def _fetch_missing(tdir, tickers, log=print):
    """Yahoo prices for listings the nightly universe leaves out, written in the ticker-file format."""
    need = [t for t in tickers if not os.path.exists(os.path.join(tdir, t + ".json"))]
    if not need:
        return 0
    import yfinance as yf
    got = 0
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        try:
            df = yf.download([t + ".NS" for t in chunk], period="5y", interval="1d", group_by="ticker", progress=False, auto_adjust=False, threads=True)
        except Exception as error:
            log(f"  ipo prices: Yahoo failed ({error})")
            continue
        for t in chunk:
            try:
                x = df[t + ".NS"].dropna(how="all")
            except Exception:
                continue
            if len(x) < 5:
                continue
            rnd = lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)
            j = {"dates": [d.strftime("%Y-%m-%d") for d in x.index], "open": [rnd(v) for v in x["Open"]], "high": [rnd(v) for v in x["High"]],
                 "low": [rnd(v) for v in x["Low"]], "close": [rnd(v) for v in x["Close"]], "volume": [int(v) if v == v else 0 for v in x["Volume"]]}
            with open(os.path.join(tdir, t + ".json"), "w", encoding="utf-8") as f:
                json.dump(j, f)
            got += 1
    return got


def scan(country="IN", log=print):
    """{"rows": [...], "index": [[date, level, count], ...]}; empty outside India or when TradingView is unreachable."""
    if country != "IN":
        return {"rows": [], "index": []}
    try:
        ipos = ipo_list(country, log)
    except Exception as error:
        log(f"  ipo scan: TradingView unreachable ({error})")
        return {"rows": [], "index": []}
    tdir = config.ticker_dir(country)
    got = _fetch_missing(tdir, list(ipos), log)
    rows, series = [], {}
    for t, m in ipos.items():
        try:
            with open(os.path.join(tdir, t + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        while d.get("close") and d["close"][-1] is None:          # today's bar still forming
            for k in ("dates", "open", "high", "low", "close", "volume"):
                if d.get(k):
                    d[k].pop()
        if len(d.get("close") or []) < 2:
            continue
        r = classify(d, m)
        if not r:
            continue
        c, h, l, v = d["close"], d["high"], d["low"], d.get("volume") or []
        N = len(c) - 1
        w = range(max(0, N - 19), N + 1)
        adr = sum((h[i] / l[i] - 1) for i in w if h[i] and l[i]) / len(w) * 100
        turn = sum((c[i] or 0) * (v[i] or 0) for i in w if i < len(v)) / len(w)
        brk = r.get("seg") == "breakout"
        rows.append(dict(r, sym=t, name=m["name"], sector=m["sector"], industry=m["industry"], logoid=m["logoid"], marketCap=m["cap"],
                         ipoDate=m["ipoDate"], adr=round(adr, 2), turnover=round(turn), price=c[N],
                         chg=round((c[N] / c[N - 1] - 1) * 100, 2) if N and c[N - 1] else None,
                         daysAgo=0 if brk else None, firedDate=d["dates"][N] if brk else None))
        k0 = next((i for i, x in enumerate(d["dates"]) if x >= m["ipoDate"]), None)
        for i in range((k0 or 0) + 1, N + 1):
            if c[i] and c[i - 1]:
                series.setdefault(d["dates"][i], []).append(min(0.25, max(-0.25, c[i] / c[i - 1] - 1)))
    index, lvl = [], 100.0
    full = max((len(x) for x in series.values()), default=0)
    days = sorted(series)
    for day in days:
        # leave out the last few days when only part of the files have them yet
        if len(series[day]) < 10 or (day >= days[-1][:8] and len(series[day]) < full * 0.5):
            continue
        lvl *= 1 + sum(series[day]) / len(series[day])
        index.append([day, round(lvl, 2), len(series[day])])
    log(f"  ipo scan: {len(rows)} IPOs classified ({got} price files fetched from Yahoo)")
    return {"rows": rows, "index": index[-520:]}
