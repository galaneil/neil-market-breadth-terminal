"""Numbers the Setups page measures from a stock's own prices, so the page and Notion always read the same values.

Every rule is plain arithmetic, and the logging form spells each one out:
  base         from the highest high of the 120 sessions before the entry to the entry day (the Setup Finder's rule);
               depth is the lowest low since that high
  52-week high the entry close against the highest high of the last 252 sessions, entry day included (as setup_context)
  volume       entry-day volume / average of the 50 sessions before; dry-up = last 10 sessions / those 50
  tightness    highest high minus lowest low of the 10 sessions before the entry, as % of the entry close
  prior run    from the lowest close of the 126 sessions before the base to the base high
  after entry  best close within 20 / 60 / 120 sessions, lowest low in the first 20, sessions to the first +20% close,
               and whether an 8% stop was hit before that first +20% close
  exit rules   the same six replays the old backtest ran; stops fill at the stop, or at the open after a gap down
"""
import threading

import journal_mfe

EXIT_RULES = [
    ("ema20x2", "2 closes below 20 EMA", "#2563eb"),
    ("combo", "2 closes < 20 or close < 50", "#8b5cf6"),
    ("e10", "Close below 10 EMA", "#f59e0b"),
    ("e50", "Close below 50 EMA", "#0891b2"),
    ("trail", "10% trailing stop", "#db2777"),
    ("hold", "Hold to latest close", "#64748b"),
]
DEFAULT_STOP_PCT = 8.0

_LOCK = threading.Lock()
_SERIES = {}
_DERIVED = {}


def _ema(vals, n):
    k, out, prev = 2.0 / (n + 1), [], None
    for v in vals:
        if v is not None:
            prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _sma(vals, n):
    out, q, s = [], [], 0.0
    for v in vals:
        if v is not None:
            q.append(v)
            s += v
            if len(q) > n:
                s -= q.pop(0)
        out.append(s / n if len(q) == n else None)
    return out


def _r(x, d=2):
    return None if x is None else round(float(x), d)


def index_on(dates, d):
    """The entry session: the first session on or after `d` (a weekend or holiday date moves to the next session)."""
    for i, x in enumerate(dates):
        if x >= d:
            return i
    return len(dates) - 1


def series(country, ticker):
    hist = journal_mfe._history(country, ticker)
    if not hist or not hist.get("dates"):
        return None
    dates = hist["dates"]
    key = (country, (ticker or "").upper(), len(dates), dates[-1])
    with _LOCK:
        hit = _SERIES.get(key)
    if hit:
        return hit
    c = hist.get("close") or []
    s = {"d": dates, "o": hist.get("open") or [], "h": hist.get("high") or [], "l": hist.get("low") or [], "c": c,
         "v": hist.get("volume") or [0] * len(dates)}
    s["e10"], s["e20"], s["e50"], s["s50"] = _ema(c, 10), _ema(c, 20), _ema(c, 50), _sma(c, 50)
    with _LOCK:
        if len(_SERIES) > 400:
            _SERIES.clear()
        _SERIES[key] = s
    return s


def measure(S, k):
    h, l, c, v, d = S["h"], S["l"], S["c"], S["v"], S["d"]
    m = {}
    lo = max(0, k - 120)
    if k - lo >= 5:
        hi, hi_i = None, None
        for i in range(lo, k):
            if h[i] is not None and (hi is None or h[i] > hi):
                hi, hi_i = h[i], i
        if hi_i is not None:
            tr = min((x for x in l[hi_i:k + 1] if x is not None), default=None)
            if tr is not None and hi > 0:
                m.update(baseStart=d[hi_i], baseHigh=_r(hi), baseLow=_r(tr), baseDays=k - hi_i, depth=_r((tr / hi - 1) * 100, 1))
    a = max(0, k - 251)
    h52 = max((x for x in h[a:k + 1] if x is not None), default=None)
    m["h52"] = _r(h52)
    m["off52"] = _r((h52 - c[k]) / h52 * 100, 1) if h52 and c[k] is not None else None
    m["short52"] = k - 251 < 0

    def avg_v(x, y):
        vals = [z for z in v[max(0, x):y] if z]
        return sum(vals) / len(vals) if vals else None
    v50, v10 = avg_v(k - 50, k), avg_v(k - 10, k)
    m["volX"] = _r(v[k] / v50, 2) if v50 and k < len(v) and v[k] else None
    m["dry"] = _r(v10 / v50, 2) if v50 and v10 else None
    hs = [x for x in h[max(0, k - 10):k] if x is not None]
    ls = [x for x in l[max(0, k - 10):k] if x is not None]
    m["tight"] = _r((max(hs) - min(ls)) / c[k] * 100, 1) if hs and ls and c[k] else None
    if m.get("baseStart"):
        b0 = k - m["baseDays"]
        low_c = min((x for x in c[max(0, b0 - 126):b0 + 1] if x is not None), default=None)
        m["run"] = _r((m["baseHigh"] / low_c - 1) * 100, 0) if low_c else None
    return m


def outcome(S, k, entry):
    h, l, c = S["h"], S["l"], S["c"]
    n = len(c)
    o = {"after": n - 1 - k}

    def best(x):
        vals = [z for z in c[k + 1:min(n, k + x + 1)] if z is not None]
        return _r((max(vals) / entry - 1) * 100, 1) if vals else None
    o["g20"] = best(20) if o["after"] >= 20 else None
    o["g60"] = best(60) if o["after"] >= 60 else None
    o["g120"] = best(120) if o["after"] >= 120 else None
    lows = [z for z in l[k + 1:min(n, k + 21)] if z is not None]
    o["dip"] = _r(min(0.0, (min(lows) / entry - 1) * 100), 1) if lows else None
    o["dipFull"] = o["after"] >= 20
    o["to20"] = next((j - k for j in range(k + 1, n) if c[j] is not None and c[j] >= entry * 1.2), None)
    o["stop8"] = None                      # >0: an 8% stop was hit that many sessions in; 0: the first +20% close came first
    for q in range(k + 1, n):
        if l[q] is not None and l[q] <= entry * 0.92:
            o["stop8"] = q - k
            break
        if c[q] is not None and c[q] >= entry * 1.2:
            o["stop8"] = 0
            break
    return o


def simulate(S, key, i0, entry, stop):
    o, h, l, c, e10, e20, e50 = S["o"], S["h"], S["l"], S["c"], S["e10"], S["e20"], S["e50"]
    n = len(c)

    def fire(i):
        if key == "ema20x2":
            return i - 1 > i0 and c[i] < e20[i] and c[i - 1] < e20[i - 1] and c[i] < c[i - 1]
        if key == "combo":
            return c[i] < e50[i] or (i - 1 > i0 and c[i] < e20[i] and c[i - 1] < e20[i - 1])
        if key == "e10":
            return c[i] < e10[i]
        if key == "e50":
            return c[i] < e50[i]
        return False
    peak = top = entry
    for i in range(i0 + 1, n):
        if l[i] is None or c[i] is None:
            continue
        if stop and l[i] <= stop:
            return {"i": i, "px": min(stop, o[i] if o[i] is not None else stop), "kind": "stop", "peak": top}
        top = max(top, h[i] if h[i] is not None else top)
        if key == "trail":
            peak = max(peak, h[i] if h[i] is not None else peak)
            lv = peak * 0.9
            if l[i] <= lv:
                return {"i": i, "px": min(lv, o[i] if o[i] is not None else lv), "kind": "rule", "peak": top}
        elif fire(i):
            return {"i": i, "px": c[i], "kind": "rule", "peak": top}
    return {"i": n - 1, "px": c[n - 1], "kind": "hold", "peak": top}


def is_breakout(S, j):
    h, c, s50 = S["h"], S["c"], S["s50"]
    if j < 41 or c[j] is None or s50[j] is None:
        return False
    hi = max((x for x in h[j - 40:j] if x is not None), default=None)
    return hi is not None and c[j] > hi and c[j] > s50[j]


def breakout_near(S, k):
    """The Setup Finder's breakout rule (close above the prior 40-session high and the 50-day average), at or near k."""
    n = len(S["c"])
    if is_breakout(S, k):
        return k
    for dd in range(1, 21):
        if k + dd < n and is_breakout(S, k + dd):
            return k + dd
        if k - dd >= 0 and is_breakout(S, k - dd):
            return k - dd
    return None


def first_breakout_after(country, ticker, start_date, limit=252):
    """The Setup Finder's default entry for a move: the first breakout session after the move's start date."""
    S = series(country, ticker)
    if not S:
        return None
    k0 = index_on(S["d"], start_date)
    for j in range(k0 + 1, min(len(S["d"]), k0 + limit)):
        if is_breakout(S, j):
            return S["d"][j]
    return S["d"][k0]


def derive(country, ticker, buy_date=None, entry_px=None, stop=None):
    """Everything the page shows that comes from prices, for one entry (or one candidate)."""
    S = series(country, ticker)
    if not S:
        return {"error": "no price history for " + str(ticker)}
    d, c = S["d"], S["c"]
    n = len(d)
    ck = (country, (ticker or "").upper(), buy_date, entry_px, stop, n, d[-1])
    with _LOCK:
        hit = _DERIVED.get(ck)
    if hit:
        return hit
    k = index_on(d, buy_date) if buy_date else n - 1
    entry = float(entry_px) if entry_px else c[k]
    if not entry:
        return {"error": "no close on that day"}
    stop_used = float(stop) if stop else round(entry * (1 - DEFAULT_STOP_PCT / 100), 2)
    bo = breakout_near(S, k)
    exits = []
    for key, label, color in EXIT_RULES:
        s = simulate(S, key, k, entry, stop_used)
        exits.append({"k": key, "label": label, "color": color, "date": d[s["i"]], "px": _r(s["px"]), "kind": s["kind"],
                      "pct": _r((s["px"] / entry - 1) * 100, 1), "days": s["i"] - k})
    a, b = max(0, k - 70), min(n - 1, k + 60)
    m = measure(S, k)
    out = {
        "date": d[k], "requested": buy_date, "entryPx": _r(entry), "stopUsed": _r(stop_used), "stopAssumed": not stop,
        "m": m, "out": outcome(S, k, entry), "breakout": d[bo] if bo is not None else None, "exits": exits,
        "spark": {"c": [_r(x) for x in c[a:b + 1]], "k": k - a,
                  "bs": (index_on(d, m["baseStart"]) - a) if m.get("baseStart") else None},
        "path": [(_r(c[k + t] / entry, 4) if 0 <= k + t < n and c[k + t] is not None else None) for t in range(-20, 121)],
        "last": d[-1],
    }
    with _LOCK:
        if len(_DERIVED) > 3000:
            _DERIVED.clear()
        _DERIVED[ck] = out
    return out


def notion_fields(derived):
    """The measured numbers as the auto columns written to Notion with each entry."""
    m = (derived or {}).get("m") or {}
    return {"baseDays": m.get("baseDays"), "baseDepth": m.get("depth"), "breakoutVol": m.get("volX"),
            "dryUp": m.get("dry"), "tightness": m.get("tight"), "priorRun": m.get("run")}
