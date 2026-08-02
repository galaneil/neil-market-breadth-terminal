"""
metrics/hilo.py — new highs and new lows at three lookbacks, with names.

WHY THIS EXISTS SEPARATELY FROM breadth.py
---------------------------------------------------------------------------
breadth.py answers "how many names printed a new 52-week high today" and stores
a single number per session. That number cannot answer the question actually
being asked of it — "how many stocks made a new high THIS WEEK" — because
summing five daily counts counts one stock five times. 215 distinct companies
made a 52-week high in the week to 2026-07-29; the daily figures for that week
sum to well over 400.

So this module stores the TICKERS, not just the totals. Distinct counts over any
period then fall out of a set union in the browser, and the same data drives the
screener: the names themselves, which is what a count can never give you.

THREE LOOKBACKS, NOT ONE
---------------------------------------------------------------------------
A 52-week high is a late signal for a stock coming off a deep base. Neil's DXDI
case is the point: down ~70% from its high, then +50% in two sessions — a stock
like that prints 13-week highs long before it can print a 52-week one, and the
13-week screen is where a turn shows up first. 26 weeks sits between the two.

  13 weeks =  65 sessions (a quarter)
  26 weeks = 130 sessions (half a year)
  52 weeks = 252 sessions (the full year)

STORAGE
---------------------------------------------------------------------------
Counts and sector composition are stored for the full history — they are small.
Ticker lists are capped at NAMES_HISTORY_DAYS, because they are not: the full
year across three windows runs to roughly a megabyte of JSON inlined into a
page, and a Notion embed has to parse all of it before the first paint. Six
months covers every timeframe the screener offers.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import cache as cache_mod
import config

# Trading sessions per lookback. Keyed by the label the UI shows.
WINDOWS = {"w13": 65, "w26": 130, "w52": 252}
WINDOW_LABELS = {"w13": "13-week", "w26": "26-week", "w52": "52-week"}

# How far back ticker lists are kept. Counts go back further.
NAMES_HISTORY_DAYS = 126


def _wide_frame(price_cache, tickers):
    series = {t: cache_mod.to_series(price_cache, t) for t in tickers}
    series = {t: s for t, s in series.items() if not s.empty}
    if not series:
        return None
    return pd.concat(series, axis=1, sort=True).sort_index()


def compute(price_cache, tickers, n_days, ticker_to_sector=None):
    """Returns (count_records, name_records).

    count_records: one per session, counts + sector composition per window.
    name_records:  one per session for the last NAMES_HISTORY_DAYS, tickers
                   per window per side.
    """
    wide = _wide_frame(price_cache, tickers)
    if wide is None:
        return [], []

    ticker_to_sector = ticker_to_sector or {}
    dates = wide.index[-n_days:]
    name_dates = set(wide.index[-NAMES_HISTORY_DAYS:])

    # Per window: the boolean grids, computed once for the whole frame.
    masks = {}
    for key, span in WINDOWS.items():
        roll_max = wide.rolling(window=span, min_periods=2).max()
        roll_min = wide.rolling(window=span, min_periods=2).min()
        masks[key] = {"hi": wide >= roll_max, "lo": wide <= roll_min}

    # Sector composition, per window per side, grouped once rather than per day.
    sector_cols = [c for c in wide.columns if ticker_to_sector.get(c)]
    sector_of = [ticker_to_sector[c] for c in sector_cols]
    grouped = {}
    for key in WINDOWS:
        grouped[key] = {}
        for side in ("hi", "lo"):
            if sector_cols:
                grouped[key][side] = masks[key][side][sector_cols].T.groupby(sector_of).sum().T
            else:
                grouped[key][side] = None

    counts, names = [], []
    for d in dates:
        date_str = d.strftime("%Y-%m-%d")
        row = {"date": date_str}
        name_row = {"date": date_str}
        for key in WINDOWS:
            hi_mask, lo_mask = masks[key]["hi"].loc[d], masks[key]["lo"].loc[d]
            entry = {"hi": int(hi_mask.sum()), "lo": int(lo_mask.sum())}
            for side in ("hi", "lo"):
                g = grouped[key][side]
                if g is not None:
                    s = g.loc[d]
                    entry[side + "_by_sector"] = {k: int(v) for k, v in s.items() if v}
            row[key] = entry

            if d in name_dates:
                name_row[key] = {
                    "hi": sorted(hi_mask.index[hi_mask].tolist()),
                    "lo": sorted(lo_mask.index[lo_mask].tolist()),
                }
        counts.append(row)
        if d in name_dates:
            names.append(name_row)

    return counts, names


def adr_percent(highs, lows, window=None):
    """Average Daily Range as a percent: mean of (high/low - 1) over `window`.

    The measure Neil screens by, and one a percentage change cannot give you.
    Two stocks can both be at a new high with the same 1-day move while one
    travels 1% intraday and the other 7% — only the second is tradeable on a
    stop that isn't inside the noise.

    Uses the true daily range rather than close-to-close, which is why it needs
    OHLC and not the close-only price cache.
    """
    window = window or config.ADR_WINDOW
    pairs = [(h, l) for h, l in zip(highs[-window:], lows[-window:])
             if h and l and l > 0]
    if len(pairs) < 5:
        return None
    return round(100 * sum(h / l - 1 for h, l in pairs) / len(pairs), 2)


def last_quotes(price_cache, tickers, adr_by_ticker=None):
    """{ticker: [last_close, pct_change_1d, adr_percent]} — the screener's row.

    Close and change come from the price cache rather than a quote call: it is
    already loaded, already current as of the run, and costs no API budget.
    ADR is passed in, since it needs the OHLC the cache does not hold.
    """
    adr_by_ticker = adr_by_ticker or {}
    out = {}
    for t in tickers:
        s = cache_mod.to_series(price_cache, t)
        if s.empty:
            continue
        last = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) > 1 else None
        chg = round((last / prev - 1) * 100, 2) if prev else None
        out[t] = [round(last, 2), chg, adr_by_ticker.get(t)]
    return out


if __name__ == "__main__":
    import config
    import universe

    pc = cache_mod.load(config.price_cache_path("US"))
    uni = universe.build_sp1500()["ticker"].tolist()
    counts, names = compute(pc, uni, config.CHART_BACKFILL_DAYS)
    latest = counts[-1]
    print(f"{latest['date']}:")
    for key, label in WINDOW_LABELS.items():
        print(f"  {label:9} {latest[key]['hi']:4} highs  {latest[key]['lo']:4} lows")
    print(f"{len(names)} sessions of ticker lists")
