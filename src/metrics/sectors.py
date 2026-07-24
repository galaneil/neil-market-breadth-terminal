"""
metrics/sectors.py — broad FMP sector performance: today / 5d / 20d % move + rank.

FMP's sector-performance-snapshot / historical-sector-performance endpoints report
one averageChange per (sector, exchange, date) — under the Starter plan, "exchange"
must be one of NASDAQ/NYSE/AMEX individually (the combined "ALL" value is plan-gated).
So each sector's daily change here is the mean of its NASDAQ/NYSE/AMEX averageChange
values, and 5d/20d are the compounded (geometric) product of that combined daily
change over the trailing window — not a simple sum, since daily percentages compound.

Backfill/rolling-cache split mirrors cache.py: a small internal buffer
(data/_cache/sector_window.json, ~25 trading days) is backfilled once via
historical-sector-performance, then extended daily via sector-performance-snapshot.
This buffer is not the chart history — the chart history is the accumulating
data/sector_ranks.jsonl written once per day by store.py.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import cache as cache_mod
from config import SECTOR_WINDOW_DAYS


def backfill_sector_cache(sector_cache, client, sectors, exchanges, on_progress=None):
    """One-time pull of recent daily averageChange per (sector, exchange)."""
    total = len(sectors) * len(exchanges)
    done = 0
    for sector in sectors:
        for exchange in exchanges:
            series_id = f"{sector}|{exchange}"
            if series_id in sector_cache:
                done += 1
                continue
            rows = client.historical_sector_performance(sector=sector)
            rows = [r for r in rows if r.get("exchange") == exchange][:SECTOR_WINDOW_DAYS]
            for row in rows:
                cache_mod.set_value(sector_cache, series_id, row["date"], row["averageChange"])
            done += 1
            if on_progress:
                on_progress(done, total)
    return sector_cache


def append_today_snapshot(sector_cache, client, exchanges, date_str):
    """Appends today's per-(sector, exchange) averageChange from the fast
    all-sectors-at-once snapshot endpoint (one call per exchange, not per sector)."""
    for exchange in exchanges:
        rows = client.sector_performance_snapshot(date=date_str, exchange=exchange)
        for row in rows:
            series_id = f"{row['sector']}|{exchange}"
            cache_mod.set_value(sector_cache, series_id, date_str, row["averageChange"])
    return sector_cache


def _combined_daily_series(sector_cache, sector, exchanges):
    """Mean across exchanges for each date -> one pandas Series per sector."""
    per_exchange = [cache_mod.to_series(sector_cache, f"{sector}|{ex}") for ex in exchanges]
    per_exchange = [s for s in per_exchange if len(s)]
    if not per_exchange:
        return pd.Series(dtype=float)
    return pd.concat(per_exchange, axis=1).mean(axis=1).sort_index()


def _compound_pct(daily_pct_series, n_days):
    """Compounded % return over the trailing n_days of daily % changes, or
    None if there isn't enough history yet."""
    if len(daily_pct_series) < n_days:
        return None
    window = daily_pct_series.iloc[-n_days:]
    growth = (1 + window / 100.0).prod()
    return float((growth - 1) * 100.0)


def compute_sector_ranks(sector_cache, sectors, exchanges):
    """Returns a DataFrame[sector, chg_1d, chg_5d, chg_20d, rank]."""
    rows = []
    for sector in sectors:
        daily = _combined_daily_series(sector_cache, sector, exchanges)
        if daily.empty:
            continue
        rows.append({
            "sector": sector,
            "chg_1d": float(daily.iloc[-1]),
            "chg_5d": _compound_pct(daily, 5),
            "chg_20d": _compound_pct(daily, 20),
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = result.sort_values("chg_1d", ascending=False).reset_index(drop=True)
    result["rank"] = result.index + 1
    return result


if __name__ == "__main__":
    from datetime import date
    from config import SECTOR_CACHE_PATH, COUNTRIES, DEFAULT_COUNTRY

    sector_cache = cache_mod.load(SECTOR_CACHE_PATH)
    if not sector_cache:
        print("Sector cache is empty — run main.py's backfill step first.")
    else:
        cfg = COUNTRIES[DEFAULT_COUNTRY]
        ranks = compute_sector_ranks(sector_cache, cfg["sectors"], cfg["exchanges"])
        print(ranks.to_string(index=False))
