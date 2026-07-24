"""
tv_industry.py — granular industry performance + rank via TradingView's public
scanner API (through the `tradingview-screener` package).

This replicates the query pattern from Neil's prior TMLE project
(TMLE_run.ipynb): Query().select(...).where(...).get_scanner_data() against
TradingView's scanner — no browser, no auth. TMLE only pulled `industry` as a
tag and computed performance itself from separately-held OHLCV; here we pull
TradingView's own per-stock performance fields directly (`change`, `Perf.W`,
`Perf.1M`) and aggregate them to the industry level, cap-weighted — this is
the same kind of aggregation TradingView's own "Industries" screener shows,
just computed from raw per-stock fields instead of scraping their pre-rendered
table.

`Perf.W` / `Perf.1M` are TradingView's built-in trailing week/month performance
fields — used here as the practical stand-in for "trailing 5 / 20 trading days"
since TradingView doesn't expose an arbitrary trailing-N-trading-day field.
"""

import pandas as pd
from tradingview_screener import Query, col

MIN_MARKET_CAP = 300e6
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
SCAN_LIMIT = 5000
MIN_INDUSTRY_MEMBERS = 5  # drop industries too small to be a meaningful bucket


def fetch_stock_performance():
    """Pull (ticker, industry, market_cap, change/Perf.W/Perf.1M) for the broad
    US common-stock universe from TradingView's scanner."""
    q = (
        Query()
        .select("name", "sector", "industry", "market_cap_basic", "change", "Perf.W", "Perf.1M")
        .where(
            col("market_cap_basic") > MIN_MARKET_CAP,
            col("type") == "stock",
            col("exchange").isin(EXCHANGES),
        )
        .order_by("market_cap_basic", ascending=False)
        .limit(SCAN_LIMIT)
    )
    _, df = q.get_scanner_data()

    # Drop preferred shares / units (e.g. "JPM/PD") which pollute industry
    # aggregates with instruments that aren't common equity.
    df = df[~df["name"].str.contains("/", regex=False)]
    df = df.dropna(subset=["industry"])
    return df.reset_index(drop=True)


def _cap_weighted_mean(group, value_col):
    weights = group["market_cap_basic"]
    values = group[value_col]
    mask = values.notna() & weights.notna()
    if not mask.any():
        return None
    return float((values[mask] * weights[mask]).sum() / weights[mask].sum())


def aggregate_industries(df):
    """Cap-weighted industry aggregates for today / 5d / 20d performance, ranked
    best (1) to worst. Returns a DataFrame[industry, chg_1d, chg_5d, chg_20d, rank, n_members]."""
    rows = []
    for industry, group in df.groupby("industry"):
        if len(group) < MIN_INDUSTRY_MEMBERS:
            continue
        rows.append({
            "industry": industry,
            "chg_1d": _cap_weighted_mean(group, "change"),
            "chg_5d": _cap_weighted_mean(group, "Perf.W"),
            "chg_20d": _cap_weighted_mean(group, "Perf.1M"),
            "n_members": len(group),
        })

    result = pd.DataFrame(rows)
    result = result.sort_values("chg_1d", ascending=False).reset_index(drop=True)
    result["rank"] = result.index + 1
    return result


if __name__ == "__main__":
    stocks = fetch_stock_performance()
    print(f"Fetched {len(stocks)} stocks across {stocks['industry'].nunique()} industries")

    industries = aggregate_industries(stocks)
    print(f"\n{len(industries)} industries with >= {MIN_INDUSTRY_MEMBERS} members\n")
    print(industries.head(15).to_string(index=False))
