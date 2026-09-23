"""
home_data.py -- everything the hub's Market Environment landing page shows,
computed server-side from the terminal's own data files.

The page mixes public market data with the user's own book (positions, stops,
journal backlog), so it is built and served ONLY by the local hub -- never by
render.py, whose output is published to GitHub Pages.

Each function returns plain JSON-able data and never raises for a missing
input: a card whose source is unavailable comes back as {"error": "..."} so
one broken feed cannot blank the whole page.
"""

import json
import os
import threading
import time
from datetime import date

import config

_CACHE = {}
_TRADES_LOCK = threading.Lock()


def _cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (time.time(), value)
    return value


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _fmp_key():
    path = os.path.join(config.ROOT_DIR, ".env")
    if os.environ.get("FMP_API_KEY"):
        return os.environ["FMP_API_KEY"]
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("FMP_API_KEY"):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


# --------------------------------------------------------------------------
# Trend tracker
#
# The rule, exactly as the user defined it:
#   a new UPTREND starts on the 2nd consecutive close above the 20 EMA;
#   it BREAKS on 2 consecutive closes below the 20 EMA where the second close
#   is lower than the first (a real breakdown, not a one-day shakeout).
# A segment is an EMA-relationship state, not a price-direction claim: price
# can drift against the label inside it.
# --------------------------------------------------------------------------

def trend_segments(rows, lookback_days=200, keep=10):
    """Uptrend / downtrend states for one index.

    Two independent rules. (1) The 20 EMA rule: an uptrend starts on the 2nd
    consecutive close above the 20 EMA and breaks on 2 consecutive closes below
    it with the 2nd lower than the 1st. (2) The 200 EMA gate: a close below the
    200 EMA can never be an uptrend, so it forces a downtrend. Rising back above
    the 200 only makes the index *eligible*; it becomes an uptrend only when the
    20 EMA rule is also met."""
    if len(rows) < 30:
        return []
    last = date.fromisoformat(rows[-1]["date"])
    start = next((i for i, r in enumerate(rows)
                  if (last - date.fromisoformat(r["date"])).days <= lookback_days), 0)
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    ema20 = [r["ema20"] for r in rows]
    ema200 = _ema(closes, 200)      # from the whole history, so the lookback window is warmed up
    segments, state, seg_start = [], None, start

    def close_seg(kind, a, b):
        segments.append({"type": kind, "start": dates[a], "end": dates[b],
                         "c0": closes[a], "c1": closes[b]})

    for i in range(start + 1, len(rows)):
        gate_ok = closes[i] >= ema200[i]
        above = closes[i] > ema20[i] and closes[i - 1] > ema20[i - 1]
        below = closes[i] < ema20[i] and closes[i - 1] < ema20[i - 1]
        if state == "up" and not gate_ok:
            close_seg("up", seg_start, i)
            seg_start, state = i, "down"
        elif state != "up" and above and gate_ok:
            if state:
                close_seg(state, seg_start, i - 1)
            seg_start, state = i, "up"
        elif state == "up" and below and closes[i] < closes[i - 1]:
            close_seg("up", seg_start, i)
            seg_start, state = i, "down"
        elif state is None and not gate_ok:
            seg_start, state = i, "down"
    if state:
        close_seg(state, seg_start, len(rows) - 1)
    return segments[-keep:]


def trend_tracker(country="US"):
    cfg = config.COUNTRIES[country]
    out = {}
    for key, label in cfg["index_labels"].items():
        rows = _read_jsonl(os.path.join(config.data_dir(country), f"index_{key}.jsonl"))
        out[key] = {"label": label, "segments": trend_segments(rows),
                    "asOf": rows[-1]["date"] if rows else None}
    return out


# --------------------------------------------------------------------------
# QQQ / SPY leadership ratio (US only): per-bar OHLC ratio + EMA 10/20/50
# --------------------------------------------------------------------------

def _ema(values, period):
    k, prev, out = 2 / (period + 1), None, []
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _build_ratio():
    import fmp_client
    key = _fmp_key()
    if not key:
        return {"error": "no FMP key"}
    client = fmp_client.FMPClient(key)
    qqq = {r["date"]: r for r in client.historical_eod("QQQ")}
    spy = {r["date"]: r for r in client.historical_eod("SPY")}
    days = sorted(set(qqq) & set(spy))[-160:]
    ohlc = {k: [] for k in ("open", "high", "low", "close")}
    for d in days:
        for k in ohlc:
            ohlc[k].append(round(qqq[d][k] / spy[d][k], 5))
    out = {"dates": days, **ohlc}
    for p in (10, 20, 50):
        out[f"ema{p}"] = [round(v, 5) for v in _ema(ohlc["close"], p)]
    return out


def qqq_spy(country="US"):
    if country != "US":
        return {"error": "US only"}
    try:
        return _cached("qqq_spy", 6 * 3600, _build_ratio)
    except Exception as error:
        return {"error": str(error)}


# --------------------------------------------------------------------------
# Money Flows, restricted to the S&P 500 -- the same 13/26/52-week view the
# Money Flows page shows, as one line per window: how many names at new
# highs vs new lows and which sector leads each side.
# --------------------------------------------------------------------------

MEMBERSHIP_CODE = {"sp500": "SPX", "nasdaq": "NDX", "russell2000": "RUT"}


def money_flows(country="US", index_key="sp500", sessions=5):
    data_dir = config.data_dir(country)
    names_path = os.path.join(data_dir, "hilo_names.json")
    try:
        with open(names_path, encoding="utf-8") as f:
            names = json.load(f)
        with open(os.path.join(data_dir, "classification.json"), encoding="utf-8") as f:
            classification = json.load(f)
        with open(os.path.join(data_dir, "index_membership.json"), encoding="utf-8") as f:
            membership = json.load(f)
    except (OSError, ValueError) as error:
        return {"error": str(error)}
    latest = names[-1]
    recent = names[-sessions:]
    code = MEMBERSHIP_CODE.get(index_key, index_key)
    members = {t for t, idx in membership.items() if code in idx}
    windows = {}
    for w in ("w13", "w26", "w52"):
        window = {}
        for side in ("hi", "lo"):
            # distinct companies across the lookback, like the Money Flows page
            tickers = sorted({t for day in recent for t in day[w][side] if t in members})
            by_sector = {}
            for t in tickers:
                info = classification.get(t)
                if info:
                    by_sector[info[0]] = by_sector.get(info[0], 0) + 1
            lead = max(by_sector.items(), key=lambda kv: kv[1]) if by_sector else (None, 0)
            window[side] = {"count": len(tickers), "leadSector": lead[0], "leadCount": lead[1]}
        windows[w] = window
    return {"index": index_key, "asOf": latest["date"], "sessions": sessions, "windows": windows}


# --------------------------------------------------------------------------
# Market environment headline + leaders, straight from environment.jsonl
# --------------------------------------------------------------------------

def environment(country="US", series_days=260):
    rows = _read_jsonl(os.path.join(config.data_dir(country), "environment.jsonl"))
    if not rows:
        return {"error": "environment.jsonl missing"}
    latest = rows[-1]
    trend = latest.get("trend") or {}
    return {
        "asOf": latest["date"],
        "overall": latest.get("overall"),
        "trend": {"favourable": trend.get("factors_favourable"), "total": trend.get("factors_total"),
                  "largeCapFavourable": trend.get("large_cap_favourable"),
                  "largeCapTotal": trend.get("large_cap_total"),
                  "perIndex": trend.get("per_index")},
        "participation": latest.get("participation"),
        "internals": latest.get("internals"),
        "leaders": latest.get("leaders"),
        "series": [{"d": r["date"], "v": (r.get("trend") or {}).get("factors_favourable")}
                   for r in rows[-series_days:]],
    }


# --------------------------------------------------------------------------
# The user's own book: journal backlog per account, and value at risk
# --------------------------------------------------------------------------

def _trades():
    """One Notion read shared by every personal card. Serialised because the
    risk and journal cards fire together and two concurrent full reads trip
    Notion's rate limit; an empty result is treated as a failure, never cached."""
    import notion_sync

    def load():
        rows = notion_sync.fetch_trades(log=lambda *a: None)
        if not rows:
            raise RuntimeError("Notion returned no trades")
        return rows
    with _TRADES_LOCK:
        return _cached("trades", 90, load)


def _journal_summary():
    import notion_sync
    trades = _trades()
    block_ids = [t["pageId"] for t in trades if t.get("chartMode") != "property"]
    presence = notion_sync.bulk_chart_presence(block_ids)
    accounts = {}
    total_pending = 0
    for t in trades:
        a = accounts.setdefault(t["account"], {"open": 0, "pending": 0, "noSetup": 0})
        has_chart = (bool(t.get("charts")) if t.get("chartMode") == "property"
                     else presence.get(t["pageId"]))
        # Same rule as /api/journal/pending, so the two counts always agree.
        if not t.get("entryThesis") or has_chart is False:
            a["pending"] += 1
            total_pending += 1
        if t.get("parentId") is None and not t.get("dateClosed"):
            a["open"] += 1
            if not t.get("entrySetup"):
                a["noSetup"] += 1
    return {"pending": total_pending, "accounts": accounts}


def journal_summary():
    try:
        return _cached("journal", 120, _journal_summary)
    except Exception as error:
        return {"error": str(error)}


def _book_risk(account="NG-IBKR"):
    import fmp_client
    import notion_sync
    key = _fmp_key()
    trades = [t for t in _trades()
              if t["account"] == account and t.get("parentId") is None
              and not t.get("dateClosed") and t.get("entryPrice") and t.get("shares")]
    client = fmp_client.FMPClient(key)
    cost = value = risk = covered_value = 0.0
    covered = 0
    for t in trades:
        last = (client.quote_one(t["ticker"]) or {}).get("price")
        if not last:
            continue
        cost += t["entryPrice"] * t["shares"]
        value += last * t["shares"]
        pct = t.get("initialStopPct")
        # The journal stores a placeholder of 1 (100%) when no stop was ever
        # entered; anything that large is "no real stop", not a real risk figure.
        if pct is not None and 0 < pct < 0.5:
            stop = t["entryPrice"] * (1 - pct)
            risk += max(0.0, (last - stop) * t["shares"])
            covered_value += last * t["shares"]
            covered += 1
    newest = max(trades, key=lambda t: t.get("dateOpened") or "", default=None)
    return {"account": account, "lastTicker": newest["ticker"] if newest else None,
            "positions": len(trades), "positionsWithStop": covered,
            "cost": round(cost, 2), "value": round(value, 2),
            "unrealized": round(value - cost, 2),
            "unrealizedPct": round((value / cost - 1) * 100, 2) if cost else None,
            "riskToStops": round(risk, 2),
            "riskPctOfBook": round(risk / value * 100, 2) if value else None,
            "bookCoveredByStopPct": round(covered_value / value * 100, 1) if value else None}


def book_risk(account="NG-IBKR"):
    try:
        return _cached("book:" + account, 60, lambda: _book_risk(account))
    except Exception as error:
        return {"error": str(error)}


# --------------------------------------------------------------------------
# One stock for the Screener card: candles, EMA 10/20/50, an O'Neil-style RS
# line against the country's benchmark index, and 1W/1M/3M/6M relative
# performance. Read from the per-ticker history the terminal already keeps.
# --------------------------------------------------------------------------

BENCHMARK = {"US": ("sp500", "S&P 500"), "IN": ("nifty500", "Nifty 500")}
PERIODS = (("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126))


def ticker_view(symbol, country="US", bars=130):
    symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in ".-")[:12]
    cfg = config.COUNTRIES[country]
    sub = cfg.get("docs_subdir", "")
    path = os.path.join(config.DOCS_DIR, sub, config.TICKER_DIR_NAME, symbol + ".json")
    if not symbol or not os.path.exists(path):
        return {"error": f"no history for {symbol or 'that symbol'}"}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    bench_key, bench_label = BENCHMARK.get(country, next(iter(cfg["index_tickers"])) and (next(iter(cfg["index_tickers"])), "index"))
    idx = {r["date"]: r["close"] for r in _read_jsonl(
        os.path.join(config.data_dir(country), f"index_{bench_key}.jsonl"))}
    dates, closes = d["dates"], d["close"]
    ema = {p: _ema([c for c in closes], p) for p in (10, 20, 50)}
    n = min(bars, len(dates))
    sl = slice(len(dates) - n, len(dates))
    rs = []
    for dt, c in zip(dates[sl], closes[sl]):
        b = idx.get(dt)
        rs.append(c / b if b else None)
    base = next((x for x in rs if x), None)
    rs = [round(x / base * 100, 3) if x and base else None for x in rs]
    rel = []
    bench_closes = [idx.get(dt) for dt in dates]
    for label, k in PERIODS:
        if len(closes) > k and bench_closes[-1] and bench_closes[-1 - k]:
            m = closes[-1] / closes[-1 - k] - 1
            b = bench_closes[-1] / bench_closes[-1 - k] - 1
            rel.append({"window": label, "stock": round(m * 100, 1),
                        "bench": round(b * 100, 1), "rel": round((m - b) * 100, 1)})
    classification = {}
    cpath = os.path.join(config.data_dir(country), "classification.json")
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as f:
            classification = json.load(f).get(symbol) or []
    return {
        "symbol": symbol, "benchmark": bench_label,
        "sector": classification[0] if len(classification) > 0 else None,
        "industry": classification[1] if len(classification) > 1 else None,
        "logoid": classification[2] if len(classification) > 2 else None,
        "dates": dates[sl], "open": d["open"][sl], "high": d["high"][sl],
        "low": d["low"][sl], "close": closes[sl],
        "ema10": [round(v, 4) for v in ema[10][sl]], "ema20": [round(v, 4) for v in ema[20][sl]],
        "ema50": [round(v, 4) for v in ema[50][sl]],
        "rs": rs, "relative": rel,
    }


def build(country="US"):
    return {
        "country": country,
        "environment": environment(country),
        "trendTracker": trend_tracker(country),
        "qqqSpy": qqq_spy(country),
        "moneyFlows": money_flows(country),
    }
