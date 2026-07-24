"""
metrics/breadth.py — breadth internals over the S&P1500 universe, computed from
the rolling price cache (cache.py). All plain counts/percentages — no composite
indicator (explicitly excludes TRIN, Zweig Breadth Thrust, Deemer, etc.):

  - Net Advancers - Decliners
  - Net New 52-week Highs - New Lows
  - % of stocks up 20%+ / 30%+ in the last 5 trading days
  - % of stocks down 20%+ / 30%+ in the last 5 trading days
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache as cache_mod
from config import NEW_HIGH_LOW_WINDOW, PCT_MOVE_LOOKBACK_DAYS, PCT_MOVE_THRESHOLDS


def compute_breadth(price_cache, tickers, date_str):
    advancers = decliners = 0
    new_highs = new_lows = 0
    up_counts = {t: 0 for t in PCT_MOVE_THRESHOLDS}
    down_counts = {t: 0 for t in PCT_MOVE_THRESHOLDS}
    n_change = n_hilo = n_5d = 0

    for ticker in tickers:
        s = cache_mod.to_series(price_cache, ticker)
        if len(s) < 2:
            continue

        today_close = float(s.iloc[-1])
        prev_close = float(s.iloc[-2])
        if today_close > prev_close:
            advancers += 1
        elif today_close < prev_close:
            decliners += 1
        n_change += 1

        window = s.iloc[-NEW_HIGH_LOW_WINDOW:]
        if len(window) >= 2:
            if today_close >= float(window.max()):
                new_highs += 1
            if today_close <= float(window.min()):
                new_lows += 1
            n_hilo += 1

        if len(s) >= PCT_MOVE_LOOKBACK_DAYS + 1:
            close_prior = float(s.iloc[-(PCT_MOVE_LOOKBACK_DAYS + 1)])
            if close_prior:
                pct = (today_close / close_prior - 1) * 100
                for threshold in PCT_MOVE_THRESHOLDS:
                    if pct >= threshold:
                        up_counts[threshold] += 1
                    if pct <= -threshold:
                        down_counts[threshold] += 1
                n_5d += 1

    def pct_of(count, total):
        return round(100.0 * count / total, 2) if total else None

    return {
        "date": date_str,
        "advancers": advancers,
        "decliners": decliners,
        "net_adv_decl": advancers - decliners,
        "n_adv_decl": n_change,
        "new_highs": new_highs,
        "new_lows": new_lows,
        "net_new_hilo": new_highs - new_lows,
        "n_hilo": n_hilo,
        "pct_up20": pct_of(up_counts[20], n_5d),
        "pct_up30": pct_of(up_counts[30], n_5d),
        "pct_down20": pct_of(down_counts[20], n_5d),
        "pct_down30": pct_of(down_counts[30], n_5d),
        "n_5d": n_5d,
    }


if __name__ == "__main__":
    import json
    from datetime import date
    from config import PRICE_CACHE_PATH
    import universe

    price_cache = cache_mod.load(PRICE_CACHE_PATH)
    if not price_cache:
        print("Price cache is empty — run main.py's backfill step first.")
    else:
        uni = universe.build_sp1500()
        record = compute_breadth(price_cache, uni["ticker"].tolist(), date.today().isoformat())
        print(json.dumps(record, indent=2))
