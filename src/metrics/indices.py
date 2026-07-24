"""
metrics/indices.py — NASDAQ / S&P 500 / Russell 2000: EMA10/20/50 and their
position/slope relative to price. Raw, directly observable data only — no
composite/derived signal (position above/below and slope direction are just
plain comparisons, not a blended score).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache as cache_mod
from config import PRICE_WINDOW_DAYS


def compute_index_record(price_cache, ticker, date_str):
    """Returns a record dict for one index on one date, or None if there isn't
    enough price history yet in the cache."""
    series = cache_mod.to_series(price_cache, ticker)
    if len(series) < 2:
        return None

    ema10 = series.ewm(span=10, adjust=False).mean()
    ema20 = series.ewm(span=20, adjust=False).mean()
    ema50 = series.ewm(span=50, adjust=False).mean()

    close = float(series.iloc[-1])
    e10, e20, e50 = float(ema10.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])

    def rising(ema_series):
        return len(ema_series) >= 2 and float(ema_series.iloc[-1]) > float(ema_series.iloc[-2])

    return {
        "date": date_str,
        "close": close,
        "ema10": e10,
        "ema20": e20,
        "ema50": e50,
        "above_ema10": close > e10,
        "above_ema20": close > e20,
        "above_ema50": close > e50,
        "ema10_rising": rising(ema10),
        "ema20_rising": rising(ema20),
        "ema50_rising": rising(ema50),
    }


def compute_all(price_cache, index_tickers, date_str):
    """index_tickers: dict like {"nasdaq": "^IXIC", "sp500": "^GSPC", ...}.
    Returns {key: record_or_None}."""
    return {
        key: compute_index_record(price_cache, ticker, date_str)
        for key, ticker in index_tickers.items()
    }


if __name__ == "__main__":
    import json
    from datetime import date
    from config import PRICE_CACHE_PATH, COUNTRIES, DEFAULT_COUNTRY

    price_cache = cache_mod.load(PRICE_CACHE_PATH)
    if not price_cache:
        print("Price cache is empty — run main.py's backfill step first.")
    else:
        today = date.today().isoformat()
        records = compute_all(price_cache, COUNTRIES[DEFAULT_COUNTRY]["index_tickers"], today)
        print(json.dumps(records, indent=2))
