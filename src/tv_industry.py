"""
tv_industry.py — granular industry CLASSIFICATION via TradingView's public
scanner API (through the `tradingview-screener` package).

Replicates the query pattern from Neil's prior TMLE project (TMLE_run.ipynb):
Query().select(...).where(...).get_scanner_data() against TradingView's
scanner — no browser, no auth.

This module only pulls today's classification (which industry a stock belongs
to) and market cap, NOT performance — TradingView's scanner has no historical
mode (it only ever answers "as of right now"), so performance/rank trend is
computed instead from our own cached FMP price history in metrics/groups.py.
Same reasoning TMLE used: pull classification once, compute historical
performance yourself from OHLCV you already hold.
"""

import pandas as pd
from tradingview_screener import Query, col

MIN_MARKET_CAP = 300e6
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
SCAN_LIMIT = 5000


def fetch_industry_classification():
    """Pull (ticker, industry, market_cap) for the broad US common-stock
    universe from TradingView's scanner. Drops preferred shares/units (e.g.
    "JPM/PD") which aren't common equity and would pollute industry groups."""
    q = (
        Query()
        .select("name", "sector", "industry", "market_cap_basic")
        .where(
            col("market_cap_basic") > MIN_MARKET_CAP,
            col("type") == "stock",
            col("exchange").isin(EXCHANGES),
        )
        .order_by("market_cap_basic", ascending=False)
        .limit(SCAN_LIMIT)
    )
    _, df = q.get_scanner_data()

    df = df[~df["name"].str.contains("/", regex=False)]
    df = df.dropna(subset=["industry"])
    return df.reset_index(drop=True)


if __name__ == "__main__":
    df = fetch_industry_classification()
    print(f"Fetched {len(df)} stocks across {df['industry'].nunique()} industries")
    print(df.head(10).to_string(index=False))
