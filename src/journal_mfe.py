"""MFE / MAE / capture for closed journal trades.

MFE (max favourable excursion) is the best open profit a trade ever showed
between its entry and exit, from the daily highs; MAE is the worst drawdown
from the daily lows. Capture = realised P&L% / MFE, i.e. how much of the peak
was kept. Prices come from the per-ticker history the terminal already keeps
(daily bars), so a trade whose ticker has no history simply gets no row.
"""
import json
import os

import config

_CACHE = {}


def _history(country, symbol):
    symbol = "".join(ch for ch in (symbol or "").upper() if ch.isalnum() or ch in ".-")[:16]
    if not symbol:
        return None
    sub = config.COUNTRIES[country].get("docs_subdir", "")
    candidates = [symbol]
    if country == "IN":
        candidates += [symbol.replace(".NS", "").replace(".BO", "")]
    for name in candidates:
        path = os.path.join(config.DOCS_DIR, sub, config.TICKER_DIR_NAME, name + ".json")
        if not os.path.exists(path):
            continue
        mtime = os.path.getmtime(path)
        hit = _CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _CACHE[path] = (mtime, data)
        return data
    return None


def _window(hist, start, end):
    """Indices of daily bars with start <= date <= end (ISO strings compare)."""
    dates = hist["dates"]
    return [i for i, d in enumerate(dates) if start <= d <= end]


def trade_row(t):
    if not (t.get("dateClosed") and t.get("dateOpened") and t.get("entryPrice")):
        return None
    hist = _history(t.get("country") or "US", t.get("ticker"))
    if not hist:
        return None
    idx = _window(hist, t["dateOpened"], t["dateClosed"])
    if not idx:
        return None
    entry = t["entryPrice"]
    highs = [hist["high"][i] for i in idx if hist["high"][i] is not None]
    lows = [hist["low"][i] for i in idx if hist["low"][i] is not None]
    if not highs or not lows:
        return None
    peak = max(highs)
    peak_date = hist["dates"][next(i for i in idx if hist["high"][i] == peak)]
    # A fill can sit outside the daily bar (weekend-dated exit, late logging),
    # and you cannot have kept more than the best price you actually sold at.
    if (t.get("exitPrice") or 0) > peak:
        peak, peak_date = t["exitPrice"], t["dateClosed"]
    mfe = peak / entry - 1
    mae = min(lows) / entry - 1
    pnl = t.get("pnlPct")
    row = {"pageId": t["pageId"], "mfe": round(mfe, 4), "mae": round(mae, 4),
           "peakDate": peak_date, "peak": round(peak, 2)}
    if pnl is not None:
        row["gaveBack"] = round(mfe - pnl, 4)
        # Capture is only meaningful for a winner that had a peak profit to keep.
        row["capture"] = round(pnl / mfe, 3) if mfe > 0.005 and pnl > 0 else None
    return row


def rows(trades):
    out = []
    for t in trades:
        r = trade_row(t)
        if r:
            out.append(r)
    return out


def path(country, symbol, start, end, pad=15):
    """Daily candles around a trade (with `pad` bars of context either side)."""
    hist = _history(country, symbol)
    if not hist:
        return {"error": "no price history for " + str(symbol)}
    idx = _window(hist, start, end or hist["dates"][-1])
    if not idx:
        return {"error": "no bars in the trade window"}
    lo, hi = max(0, idx[0] - pad), min(len(hist["dates"]), idx[-1] + pad + 1)
    return {"candles": [{"time": hist["dates"][i], "open": hist["open"][i], "high": hist["high"][i],
                         "low": hist["low"][i], "close": hist["close"][i]} for i in range(lo, hi)]}


# --------------------------------------------------------------------------
# Exit-rule backtest: replay each closed trade from its real entry with a
# different exit rule and compare with what actually happened.
# --------------------------------------------------------------------------

RULES = [
    {"key": "yourrule", "label": "Your rule: 2 closes < 20 EMA or close < 50 EMA",
     "desc": "Your stated rule: exit on two consecutive closes below the 20 EMA, or a close below the 50 EMA."},
    {"key": "ema20x2", "label": "2 closes below 20 EMA",
     "desc": "Exit on the 2nd consecutive close below the 20 EMA, the 2nd lower than the 1st."},
    {"key": "ema10", "label": "Close below 10 EMA", "desc": "Exit on the first close below the 10 EMA."},
    {"key": "ema50", "label": "Close below 50 EMA", "desc": "Exit on the first close below the 50 EMA."},
    {"key": "trail10", "label": "10% trailing stop",
     "desc": "Exit when price falls 10% from its highest high since entry (checked on daily lows)."},
]


def _ema(values, n):
    k = 2 / (n + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else (v * k + prev * (1 - k) if v is not None else prev)
        out.append(prev)
    return out


def _simulate(hist, ind, i0, entry, stop, rule):
    """Walk forward from the bar after entry; returns (exit_index, price, kind)."""
    close, low, opn, dates = hist["close"], hist["low"], hist["open"], hist["dates"]
    peak = entry
    n = len(dates)
    for i in range(i0 + 1, n):
        # A protective stop fills intraday (or at the open if price gapped through).
        if stop and low[i] is not None and low[i] <= stop:
            return i, min(stop, opn[i]) if opn[i] else stop, "stop"
        c = close[i]
        if c is None:
            continue
        if rule == "trail10":
            peak = max(peak, hist["high"][i] or peak)
            level = peak * 0.9
            if low[i] is not None and low[i] <= level:
                return i, min(level, opn[i]) if opn[i] else level, "rule"
        elif rule == "ema10":
            if c < ind["ema10"][i]:
                return i, c, "rule"
        elif rule == "ema50":
            if c < ind["ema50"][i]:
                return i, c, "rule"
        elif rule == "yourrule":
            if c < ind["ema50"][i] or (i - 1 > i0 and c < ind["ema20"][i] and close[i - 1] is not None
                                       and close[i - 1] < ind["ema20"][i - 1]):
                return i, c, "rule"
        elif rule == "ema20x2":
            if (i - 1 > i0 and c < ind["ema20"][i] and close[i - 1] is not None
                    and close[i - 1] < ind["ema20"][i - 1] and c < close[i - 1]):
                return i, c, "rule"
    last = n - 1
    return last, close[last], "holding"


def backtest(trades, use_stop=True):
    out = []
    for t in trades:
        if not (t.get("dateClosed") and t.get("dateOpened") and t.get("entryPrice")):
            continue
        hist = _history(t.get("country") or "US", t.get("ticker"))
        if not hist:
            continue
        dates = hist["dates"]
        i0 = next((i for i, d in enumerate(dates) if d >= t["dateOpened"]), None)
        if i0 is None or i0 >= len(dates) - 1:
            continue
        ind = {k: _ema(hist["close"], n) for k, n in (("ema10", 10), ("ema20", 20), ("ema50", 50))}
        entry = t["entryPrice"]
        stop = t.get("initialStop") if use_stop else None
        if stop and not (0 < stop < entry):
            stop = None
        res = {}
        for r in RULES:
            i, px, kind = _simulate(hist, ind, i0, entry, stop, r["key"])
            res[r["key"]] = {"pct": round(px / entry - 1, 4), "date": dates[i], "kind": kind}
        out.append({"pageId": t["pageId"], "results": res})
    return {"rules": RULES, "trades": out}


# --------------------------------------------------------------------------
# Group strength at entry: the ticker's industry rank on the day you bought.
# --------------------------------------------------------------------------

_RANKS = {}


def _load(path, reader):
    if not os.path.exists(path):
        return None
    m = os.path.getmtime(path)
    hit = _RANKS.get(path)
    if hit and hit[0] == m:
        return hit[1]
    val = reader(path)
    _RANKS[path] = (m, val)
    return val


def _read_ranks(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out.append((r["date"], {i["industry"]: i["rank"] for i in r.get("industries", [])}))
    return out


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def group_strength(trades):
    import bisect
    out = []
    cache = {}
    for t in trades:
        if not t.get("dateOpened") or not t.get("ticker"):
            continue
        country = t.get("country") or "US"
        if country not in cache:
            base = config.data_dir(country) if hasattr(config, "data_dir") else None
            base = base or os.path.join(config.ROOT_DIR, "data", country.lower())
            cache[country] = (_load(os.path.join(base, "industry_ranks.jsonl"), _read_ranks),
                              _load(os.path.join(base, "classification.json"), _read_json))
        ranks, cls = cache[country]
        if not ranks or not cls:
            continue
        info = cls.get(t["ticker"]) or cls.get(t["ticker"].replace(".NS", ""))
        if not info:
            continue
        dates = [d for d, _ in ranks]
        i = bisect.bisect_right(dates, t["dateOpened"]) - 1
        if i < 0:
            continue
        day = ranks[i][1]
        rank = day.get(info[1])
        if not rank:
            continue
        of = len(day)
        q = min(4, int((rank - 1) / of * 4) + 1)
        out.append({"pageId": t["pageId"], "industry": info[1], "sector": info[0], "rank": rank, "of": of, "quartile": q})
    return out
