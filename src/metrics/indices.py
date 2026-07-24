"""
metrics/indices.py — NASDAQ / S&P 500 / Russell 2000: EMA10/20/50 and their
position/slope relative to price. Raw, directly observable data only — no
composite/derived signal (position above/below and slope direction are just
plain comparisons, not a blended score).

Computes the EMA series once (vectorized over the full cached price history)
and extracts records for a trailing window of dates, rather than one point at
a time — this is what makes real multi-month backfill cheap: the EMA math for
day N always depends on the whole history up to day N anyway, so computing
the whole series in one pass is both simpler and much faster than replaying
it separately per day.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import cache as cache_mod


def compute_index_series(price_cache, ticker):
    """Returns a DataFrame (indexed by date) with columns close/ema10/ema20/ema50,
    or None if there's no cached history for this ticker yet."""
    series = cache_mod.to_series(price_cache, ticker)
    if series.empty:
        return None
    df = pd.DataFrame({"close": series})
    df["ema10"] = series.ewm(span=10, adjust=False).mean()
    df["ema20"] = series.ewm(span=20, adjust=False).mean()
    df["ema50"] = series.ewm(span=50, adjust=False).mean()
    return df


def _record_from_row(df, i):
    row = df.iloc[i]

    def rising(col):
        return bool(i >= 1 and row[col] > df.iloc[i - 1][col])

    return {
        "date": df.index[i].strftime("%Y-%m-%d"),
        "close": float(row["close"]),
        "ema10": float(row["ema10"]),
        "ema20": float(row["ema20"]),
        "ema50": float(row["ema50"]),
        "above_ema10": bool(row["close"] > row["ema10"]),
        "above_ema20": bool(row["close"] > row["ema20"]),
        "above_ema50": bool(row["close"] > row["ema50"]),
        "ema10_rising": rising("ema10"),
        "ema20_rising": rising("ema20"),
        "ema50_rising": rising("ema50"),
    }


def backfill_index_history(price_cache, ticker, n_days):
    """Records for the trailing n_days of available cached history (fewer if
    the cache doesn't hold that much yet)."""
    df = compute_index_series(price_cache, ticker)
    if df is None or df.empty:
        return []
    start = max(0, len(df) - n_days)
    return [_record_from_row(df, i) for i in range(start, len(df))]


if __name__ == "__main__":
    import json
    from config import PRICE_CACHE_PATH, COUNTRIES, DEFAULT_COUNTRY, CHART_BACKFILL_DAYS

    price_cache = cache_mod.load(PRICE_CACHE_PATH)
    if not price_cache:
        print("Price cache is empty — run main.py's backfill step first.")
    else:
        ticker = COUNTRIES[DEFAULT_COUNTRY]["index_tickers"]["sp500"]
        records = backfill_index_history(price_cache, ticker, CHART_BACKFILL_DAYS)
        print(f"{len(records)} records for {ticker}")
        print(json.dumps(records[-1], indent=2))
