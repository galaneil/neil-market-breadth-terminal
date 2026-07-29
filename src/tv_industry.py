"""
tv_industry.py — sector/industry CLASSIFICATION via TradingView's public
scanner API (through the `tradingview-screener` package).

Replicates the query pattern from Neil's prior TMLE project (TMLE_run.ipynb):
Query().select(...).where(...).get_scanner_data() against TradingView's
scanner — no browser, no auth.

This module only pulls today's classification (which industry a stock belongs
to) and market cap, NOT performance — TradingView's scanner has no historical
mode (it only ever answers "as of right now"), so performance/rank trend is
computed instead from our own cached price history in metrics/groups.py. Same
reasoning TMLE used: pull classification once, compute historical performance
yourself from OHLCV you already hold.

Works for both markets off the country config. The US is filtered by market cap
(everything above $300m, ~3,300 names); India is capped by count instead, since
below the top 1000 the NSE tail thins into names that barely trade.
"""

import pandas as pd
from tradingview_screener import Query, col


def fetch_industry_classification(country_cfg):
    """Pull (name, sector, industry, market_cap_basic) for one market's common
    stock universe. Drops preferred shares/units (e.g. "JPM/PD"), which aren't
    common equity and would pollute industry groups."""
    filters = [
        col("type") == "stock",
        col("exchange").isin(country_cfg["exchanges"]),
    ]
    min_cap = country_cfg.get("min_market_cap") or 0
    if min_cap:
        filters.append(col("market_cap_basic") > min_cap)
    else:
        # Still exclude unpriced shells — ordering by market cap is meaningless
        # if a chunk of the table has no market cap at all.
        filters.append(col("market_cap_basic") > 0)

    q = (
        Query()
        .set_markets(country_cfg["tv_market"])
        .select("name", "sector", "industry", "market_cap_basic")
        .where(*filters)
        .order_by("market_cap_basic", ascending=False)
        .limit(country_cfg["universe_limit"])
    )
    _, df = q.get_scanner_data()

    df = df[~df["name"].str.contains("/", regex=False)]
    df = df.dropna(subset=["industry", "sector"])
    return df.reset_index(drop=True)


if __name__ == "__main__":
    import config
    for code, cfg in config.COUNTRIES.items():
        df = fetch_industry_classification(cfg)
        print(f"\n{code}: {len(df)} stocks | {df['sector'].nunique()} sectors | "
              f"{df['industry'].nunique()} industries")
        print(f"   smallest market cap kept: {df['market_cap_basic'].min():,.0f}")
        print(df.head(5)[["name", "sector", "industry"]].to_string(index=False))
