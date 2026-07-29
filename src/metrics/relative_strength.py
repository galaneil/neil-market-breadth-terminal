"""
relative_strength.py — one comparable number per stock per day.

WHY A RATING AND NOT A RATIO
---------------------------------------------------------------------------
The stock page already shows outperformance over four windows against two
indices — eight numbers, none of them comparable between stocks. SNDK reading
"517%" and AAOI reading "600%" cannot be set side by side, because each is
indexed from wherever its own series happened to start. That makes it useless
as a field in a setups database: you cannot learn "VCP works best above X" from
a number whose scale changes per name.

A cross-sectional PERCENTILE fixes it. Rank every stock's blended trailing
return against every other stock that day, and 92 always means "stronger than
92% of the market", on any ticker, on any date.

ONE RATING, NOT TWO
---------------------------------------------------------------------------
Rating against the Nasdaq and rating against the S&P would be the same number.
On a given day the benchmark's return is one constant subtracted from every
stock, so it shifts the whole distribution and cannot change the ORDER — and a
percentile depends only on order. The raw per-index outperformance still
differs and is still worth showing, but there is only one rating.

THE BLEND
---------------------------------------------------------------------------
Weighted toward recent performance, in the long-standing style: the last
quarter counts double the others, so a stock turning up now outranks one
coasting on a move it made nine months ago.
"""

import numpy as np
import pandas as pd

# Trailing windows (trading days) and their weights. The nearest quarter is
# double-weighted; the rest split the remainder evenly.
RS_WINDOWS = {63: 0.40, 126: 0.20, 189: 0.20, 252: 0.20}

# A stock needs at least this much history before a rating means anything —
# a name three weeks past IPO has no trailing return to rank.
MIN_HISTORY_DAYS = 63


def build_frame(price_cache, tickers=None):
    """{ticker: {date: close}} -> wide DataFrame, dates ascending."""
    tickers = tickers if tickers is not None else list(price_cache)
    series = {t: pd.Series(price_cache[t]) for t in tickers if price_cache.get(t)}
    if not series:
        return pd.DataFrame()
    frame = pd.DataFrame(series)
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def compute_ratings(price_cache, tickers=None):
    """RS rating 1-99 for every ticker on every date.

    Returns {ticker: {date_str: rating}}.
    """
    frame = build_frame(price_cache, tickers)
    if frame.empty:
        return {}

    # Blended trailing return, weighted toward the recent quarter.
    blended = None
    for window, weight in RS_WINDOWS.items():
        ret = frame / frame.shift(window) - 1
        contribution = ret * weight
        blended = contribution if blended is None else blended.add(contribution, fill_value=np.nan)

    # A row is only comparable where the shortest window exists, so drop names
    # that have not traded long enough rather than ranking them against a
    # partial return.
    enough = frame.shift(MIN_HISTORY_DAYS).notna()
    blended = blended.where(enough)

    # Cross-sectional percentile per DAY: rank across columns, not down them.
    # This is what makes the number mean the same thing on every date.
    ranked = blended.rank(axis=1, pct=True) * 100
    ranked = ranked.round().clip(1, 99)

    out = {}
    for ticker in ranked.columns:
        col = ranked[ticker].dropna()
        if col.empty:
            continue
        out[ticker] = {d.strftime("%Y-%m-%d"): int(v) for d, v in col.items()}
    return out


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cache as cache_mod
    import config

    cache = cache_mod.load(config.price_cache_path("US"))
    ratings = compute_ratings(cache)
    print(f"rated {len(ratings)} tickers")
    for t in ["NVDA", "SNDK", "AAOI", "PANW", "SYRE"]:
        r = ratings.get(t)
        if not r:
            print(f"  {t}: no rating")
            continue
        last = sorted(r)[-1]
        print(f"  {t}: {r[last]} on {last}  ({len(r)} dated values)")
