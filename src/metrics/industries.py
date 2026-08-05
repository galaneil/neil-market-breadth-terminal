"""
metrics/industries.py — TradingView-industry-tagged performance/rank, computed
from our own cached FMP price history via metrics/groups.py (see that module's
docstring for why: TradingView's live scanner has no historical mode).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metrics import groups


def extract_classification(industry_df):
    """industry_df: TradingView classification pull from tv_industry.py.
    Returns (ticker_to_industry: dict, market_caps: dict)."""
    ticker_to_industry = dict(zip(industry_df["name"], industry_df["industry"]))
    market_caps = dict(zip(industry_df["name"], industry_df["market_cap_basic"]))
    return ticker_to_industry, market_caps


def _rename(records):
    # Not {**r, "industry": r.pop("group")}: the unpacking is evaluated before
    # the pop, so every record kept BOTH keys with the same value. Over six
    # years that duplicate was a fifth of the published payload.
    return [{("industry" if k == "group" else k): v for k, v in r.items()}
            for r in records]


def compute_industry_ranks(price_cache, ticker_to_industry, market_caps, as_of_date):
    result = groups.compute_group_performance(price_cache, ticker_to_industry, market_caps, as_of_date)
    if result.empty:
        return []
    return _rename(result.to_dict(orient="records"))


def backfill_industry_history(price_cache, ticker_to_industry, market_caps, dates):
    """Returns {date_str: [records]} for each date."""
    by_date = groups.backfill_group_history(price_cache, ticker_to_industry, market_caps, dates)
    return {d: _rename(records) for d, records in by_date.items()}
