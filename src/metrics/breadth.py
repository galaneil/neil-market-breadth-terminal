"""
metrics/breadth.py — breadth internals over the S&P1500 universe, computed from
the rolling price cache (cache.py). All plain counts/percentages — no composite
indicator (explicitly excludes TRIN, Zweig Breadth Thrust, Deemer, etc.):

  - Net Advancers - Decliners
  - Net New 52-week Highs - New Lows
  - % of stocks up 20%+ / 30%+ in the last 5 trading days
  - % of stocks down 20%+ / 30%+ in the last 5 trading days

Builds one wide DataFrame (dates x tickers) and vectorizes every comparison
across the whole grid at once, rather than looping ticker-by-ticker per day —
this is what makes backfilling a full year of daily breadth cheap: rolling
252-day max/min, shifted lookbacks etc. are computed once per column (pandas
does this natively), not replayed separately for every historical date.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import cache as cache_mod
from config import NEW_HIGH_LOW_WINDOW, PCT_MOVE_LOOKBACK_DAYS, PCT_MOVE_THRESHOLDS


def _counts_by_sector(mask, ticker_to_sector, dates):
    """{date: {sector: count}} for a boolean (dates x tickers) frame.

    WHY THIS EXISTS: the sector rank panel is cap-weighted, so a sector can be
    top-ranked because two megacaps carried it while most of its members went
    nowhere. A count of MEMBERS at new highs cannot be distorted that way — one
    company, one vote — so this is the unweighted read on which sectors are
    actually broadly strong or broadly breaking.

    Zero counts are dropped: over 20 sectors and a year of history most cells
    are zero, and storing them triples the file for no information.
    """
    if not ticker_to_sector:
        return {}
    cols = [c for c in mask.columns if ticker_to_sector.get(c)]
    if not cols:
        return {}
    # Transposed groupby rather than groupby(axis=1), which pandas 2 removed.
    grouped = mask[cols].T.groupby([ticker_to_sector[c] for c in cols]).sum().T
    out = {}
    for d in dates:
        row = grouped.loc[d]
        out[d] = {sector: int(n) for sector, n in row.items() if n}
    return out


def backfill_breadth_history(price_cache, tickers, n_days, ticker_to_sector=None):
    """Records for the trailing n_days of available cached history (fewer if
    the cache doesn't hold that much yet).

    `ticker_to_sector` is optional; when supplied, each record also carries the
    sector composition of that day's new highs and new lows.
    """
    series_dict = {t: cache_mod.to_series(price_cache, t) for t in tickers}
    series_dict = {t: s for t, s in series_dict.items() if not s.empty}
    if not series_dict:
        return []

    wide = pd.concat(series_dict, axis=1).sort_index()

    prev = wide.shift(1)
    roll_max = wide.rolling(window=NEW_HIGH_LOW_WINDOW, min_periods=2).max()
    roll_min = wide.rolling(window=NEW_HIGH_LOW_WINDOW, min_periods=2).min()
    close_lookback = wide.shift(PCT_MOVE_LOOKBACK_DAYS)

    advancers = (wide > prev).sum(axis=1)
    decliners = (wide < prev).sum(axis=1)
    n_change = prev.notna().sum(axis=1)

    new_highs = (wide >= roll_max).sum(axis=1)
    new_lows = (wide <= roll_min).sum(axis=1)
    n_hilo = (roll_max.notna() & roll_min.notna()).sum(axis=1)

    pct_move = (wide / close_lookback - 1) * 100
    n_5d = close_lookback.notna().sum(axis=1)

    up_counts = {t: (pct_move >= t).sum(axis=1) for t in PCT_MOVE_THRESHOLDS}
    down_counts = {t: (pct_move <= -t).sum(axis=1) for t in PCT_MOVE_THRESHOLDS}

    def pct_of(count, total):
        return round(float(100.0 * count / total), 2) if total else None

    dates = wide.index[-n_days:]

    hi_mask = wide >= roll_max
    lo_mask = wide <= roll_min
    hi_by_sector = _counts_by_sector(hi_mask, ticker_to_sector, dates)
    lo_by_sector = _counts_by_sector(lo_mask, ticker_to_sector, dates)

    records = []
    for d in dates:
        n5 = int(n_5d.loc[d])
        records.append({
            "date": d.strftime("%Y-%m-%d"),
            "hi_by_sector": hi_by_sector.get(d, {}),
            "lo_by_sector": lo_by_sector.get(d, {}),
            "advancers": int(advancers.loc[d]),
            "decliners": int(decliners.loc[d]),
            "net_adv_decl": int(advancers.loc[d] - decliners.loc[d]),
            "n_adv_decl": int(n_change.loc[d]),
            "new_highs": int(new_highs.loc[d]),
            "new_lows": int(new_lows.loc[d]),
            "net_new_hilo": int(new_highs.loc[d] - new_lows.loc[d]),
            "n_hilo": int(n_hilo.loc[d]),
            "pct_up20": pct_of(up_counts[20].loc[d], n5),
            "pct_up30": pct_of(up_counts[30].loc[d], n5),
            "pct_down20": pct_of(down_counts[20].loc[d], n5),
            "pct_down30": pct_of(down_counts[30].loc[d], n5),
            "n_5d": n5,
        })
    return records


if __name__ == "__main__":
    from config import price_cache_path, CHART_BACKFILL_DAYS, DEFAULT_COUNTRY
    import universe

    price_cache = cache_mod.load(price_cache_path(DEFAULT_COUNTRY))
    if not price_cache:
        print("Price cache is empty — run main.py's backfill step first.")
    else:
        uni = universe.build_sp1500()
        records = backfill_breadth_history(price_cache, uni["ticker"].tolist(), CHART_BACKFILL_DAYS)
        print(f"{len(records)} records")
        print(records[-1])
