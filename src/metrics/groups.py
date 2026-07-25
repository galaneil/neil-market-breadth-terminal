"""
metrics/groups.py — unified sector/industry performance & rank, computed from
our own cached price history rather than a third-party "today only" performance
field. This is what makes real historical backfill possible: TradingView's live
scanner and FMP's sector-performance endpoint can only tell us about *today*,
but we already hold up to ~1 year of daily closes per stock (cache.py) for the
breadth calcs, so the same data can drive sector/industry trend charts too.

A "group" here is just a classification tag — GICS sector (from universe.py's
Wikipedia scrape) for the sector panel, TradingView industry (from tv_industry.py,
classification only now) for the industry panel. This module doesn't care which;
it only needs {ticker: group_name} and {ticker: market_cap}.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import cache as cache_mod
from config import GROUP_CHG_WINDOWS, MIN_GROUP_MEMBERS


def _pct_change_asof(series, as_of_date, window_days):
    """% change ending at the last available date <= as_of_date, over the
    trailing `window_days` trading days (by position, not calendar days).
    Returns None if there isn't enough prior history."""
    if series.empty:
        return None
    pos = series.index.searchsorted(pd.Timestamp(as_of_date), side="right") - 1
    if pos < window_days or pos >= len(series):
        return None
    end = float(series.iloc[pos])
    start = float(series.iloc[pos - window_days])
    if not start:
        return None
    return (end / start - 1) * 100.0


def _build_ticker_series(price_cache, ticker_groups):
    """Converts each relevant ticker's cached price dict to a pandas Series ONCE
    (used across every backfill date, instead of re-converting per date)."""
    series = {}
    for ticker in ticker_groups:
        s = cache_mod.to_series(price_cache, ticker)
        if not s.empty:
            series[ticker] = s
    return series


def compute_group_performance(price_cache, ticker_groups, market_caps, as_of_date, _series=None):
    """ticker_groups: {ticker: group_name}. market_caps: {ticker: float}, used to
    cap-weight the group aggregate (falls back to equal weight if missing).
    Returns a DataFrame[group, chg_1d, chg_5d, chg_20d, rank, n_members] for one date.
    `_series` lets backfill_group_history pass in pre-built series to avoid
    re-converting the same ticker's cached prices for every backfill date."""
    series_dict = _series if _series is not None else _build_ticker_series(price_cache, ticker_groups)

    members_by_group = {}
    for ticker, group in ticker_groups.items():
        if not group or ticker not in series_dict:
            continue
        changes = {key: _pct_change_asof(series_dict[ticker], as_of_date, w) for key, w in GROUP_CHG_WINDOWS.items()}
        if changes.get("chg_1d") is None:
            continue
        row = {"weight": market_caps.get(ticker) or 1.0}
        row.update(changes)
        members_by_group.setdefault(group, []).append(row)

    records = []
    for group, members in members_by_group.items():
        if len(members) < MIN_GROUP_MEMBERS:
            continue
        df = pd.DataFrame(members)
        record = {"group": group, "n_members": len(df)}
        for key in GROUP_CHG_WINDOWS:
            vals, weights = df[key], df["weight"]
            mask = vals.notna()
            record[key] = float((vals[mask] * weights[mask]).sum() / weights[mask].sum()) if mask.any() else None
        records.append(record)

    result = pd.DataFrame(records)
    if result.empty:
        return result
    # Rank by the 20-trading-day (~1 month) return, not the 1-day move: daily
    # returns are dominated by noise, so ranking on them produces a rank series
    # that whipsaws day to day with no visible trend. The 20d window is smooth
    # enough to show real rotation (a group actually gaining/losing relative
    # strength over weeks) while still updating daily.
    result = result.sort_values("chg_20d", ascending=False).reset_index(drop=True)
    result["rank"] = result.index + 1
    return result


def backfill_group_history(price_cache, ticker_groups, market_caps, dates):
    """Returns {date_str: [records]} for each date, reusing one set of
    pre-built per-ticker price Series across every date instead of rebuilding
    them per date (dominant cost when backfilling ~250 days x ~1500 tickers)."""
    series_dict = _build_ticker_series(price_cache, ticker_groups)
    out = {}
    for d in dates:
        result = compute_group_performance(price_cache, ticker_groups, market_caps, d, _series=series_dict)
        out[d] = [] if result.empty else result.to_dict(orient="records")
    return out


def trading_calendar(price_cache, reference_ticker, n_days):
    """Last n_days trading-day date strings (ascending) from one ticker's cached
    series, used as the canonical set of dates to backfill every panel against."""
    series = cache_mod.to_series(price_cache, reference_ticker)
    dates = series.index[-n_days:]
    return [d.strftime("%Y-%m-%d") for d in dates]
