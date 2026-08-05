"""
metrics/sectors.py — broad sector performance/rank, computed from our own
cached FMP price history via metrics/groups.py.

Uses TradingView's own sector taxonomy (from the same classification pull as
industries.py, `sector` column) rather than a second, unrelated taxonomy —
TradingView's sector/industry pair nests consistently as one hierarchy, and
this avoids a second live-classification call. FMP's dedicated
historical-sector-performance endpoint is no longer used: it only reports its
own snapshot, whereas computing from our own price cache lets us backfill as
much history as we retain.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metrics import groups


def extract_classification(industry_df):
    """industry_df: the same TradingView classification pull used by
    industries.py. Returns (ticker_to_sector: dict, market_caps: dict)."""
    ticker_to_sector = dict(zip(industry_df["name"], industry_df["sector"]))
    market_caps = dict(zip(industry_df["name"], industry_df["market_cap_basic"]))
    return ticker_to_sector, market_caps


def _rename(records):
    # See the note in industries._rename — {**r, ...} kept the old key too.
    return [{("sector" if k == "group" else k): v for k, v in r.items()}
            for r in records]


def compute_sector_ranks(price_cache, ticker_to_sector, market_caps, as_of_date):
    result = groups.compute_group_performance(price_cache, ticker_to_sector, market_caps, as_of_date)
    if result.empty:
        return []
    return _rename(result.to_dict(orient="records"))


def backfill_sector_history(price_cache, ticker_to_sector, market_caps, dates):
    """Returns {date_str: [records]} for each date."""
    by_date = groups.backfill_group_history(price_cache, ticker_to_sector, market_caps, dates)
    return {d: _rename(records) for d, records in by_date.items()}
