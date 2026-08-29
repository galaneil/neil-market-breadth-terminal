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
        .select("name", "sector", "industry", "market_cap_basic",
                # TradingView's own logo slug — free in this same query, and
                # the one source that actually covers India: FMP's logo images
                # only exist for US-listed tickers, so NSE positions had no
                # real logo at all before this, only a colour-initials
                # fallback. https://s3-symbol-logo.tradingview.com/{logoid}.svg
                # resolves for both markets off the same field.
                "logoid",
                # Fundamentals for TMLE's F2, free in the query already being
                # made. FMP would cost one call per ticker; this costs nothing.
                "total_revenue_ttm", "total_revenue_yoy_growth_ttm",
                "operating_margin_ttm", "earnings_per_share_diluted_yoy_growth_ttm",
                # Index membership, so the screener can be narrowed to real
                # index constituents instead of every listed shell. Comes free
                # in this query. The alternatives were all dead ends: iShares'
                # holdings CSVs need a cookie a plain request cannot set, FMP's
                # constituent endpoints are 402 on this plan, and Wikipedia has
                # no Russell 2000 table at all.
                "indexes")
        .where(*filters)
        .order_by("market_cap_basic", ascending=False)
        .limit(country_cfg["universe_limit"])
    )
    _, df = q.get_scanner_data()

    df = df[~df["name"].str.contains("/", regex=False)]
    df = df.dropna(subset=["industry", "sector"])
    return df.reset_index(drop=True)


def fundamentals_map(df):
    """{ticker: {revenue, revenue_growth, eps_growth, operating_margin}}.

    NaN is preserved as None rather than coerced to zero: "margin unknown" and
    "margin is zero" are different, and only the band lookup should decide what
    an unknown is worth.
    """
    def clean(v):
        return None if v is None or pd.isna(v) else float(v)

    out = {}
    for _, row in df.iterrows():
        out[row["name"]] = {
            "revenue": clean(row.get("total_revenue_ttm")),
            "revenue_growth": clean(row.get("total_revenue_yoy_growth_ttm")),
            "eps_growth": clean(row.get("earnings_per_share_diluted_yoy_growth_ttm")),
            "operating_margin": clean(row.get("operating_margin_ttm")),
        }
    return out


def index_membership(df, country_code):
    """{ticker: [codes]} for the indices that country's screener filters by.

    TradingView returns every index a name belongs to — often 25 of them,
    including STOXX and ESG variants nobody screens on. Only the tracked ones
    are kept, so the payload carries a few short codes per name instead of a
    paragraph.
    """
    if "indexes" not in df.columns:
        return {}
    import config
    by_name = {tv: code for code, _label, tv in config.SCREENER_INDEXES.get(country_code, [])}
    out = {}
    for _, row in df.iterrows():
        memberships = row.get("indexes")
        if not isinstance(memberships, list):
            continue
        codes = [by_name[m.get("name")] for m in memberships
                 if isinstance(m, dict) and m.get("name") in by_name]
        if codes:
            out[row["name"]] = sorted(codes)
    return out


if __name__ == "__main__":
    import config
    for code, cfg in config.COUNTRIES.items():
        df = fetch_industry_classification(cfg)
        print(f"\n{code}: {len(df)} stocks | {df['sector'].nunique()} sectors | "
              f"{df['industry'].nunique()} industries")
        print(f"   smallest market cap kept: {df['market_cap_basic'].min():,.0f}")
        print(df.head(5)[["name", "sector", "industry"]].to_string(index=False))
