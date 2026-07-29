"""
relative_strength.py — one comparable number per stock per day.

WHY A RATING AND NOT A RATIO
---------------------------------------------------------------------------
The stock page already shows outperformance over four windows against two
indices — eight numbers, none of them comparable between stocks. SNDK reading
"517%" and AAOI reading "600%" cannot be set side by side, because each is
indexed from wherever its own series happened to start. That makes it useless
as a field in a setups database: you cannot learn "VCP works best above X" from
a number whose scale changes per name.

A cross-sectional PERCENTILE fixes it. Rank every stock's blended trailing
return against every other stock that day, and 92 always means "stronger than
92% of the market", on any ticker, on any date.

ONE RATING, NOT TWO
---------------------------------------------------------------------------
Rating against the Nasdaq and rating against the S&P would be the same number.
On a given day the benchmark's return is one constant subtracted from every
stock, so it shifts the whole distribution and cannot change the ORDER — and a
percentile depends only on order. The raw per-index outperformance still
differs and is still worth showing, but there is only one rating.

THE BLEND, AND WHY IT IS NOT THE CONVENTIONAL ONE
---------------------------------------------------------------------------
A conventional RS rating blends 3/6/9/12-month returns and therefore keeps a
broken stock in the 90s for months after its move has ended. Two rounds of
looking at real names killed that:

  MU on 2026-07-28   +56% over three months, but -28% over one month and 32%
                     off its high. Its relative-strength line versus the Nasdaq
                     peaked on 4 June around +20% and has since fallen to zero.
                     A conventional rating scored it 99.
  BBIO same day      +15% over three months — modest — but its RS line is at a
                     six-month HIGH. It is going up faster than the market
                     while leaders break down. A conventional rating scored it 75.

Backwards. So the blend is:

  50%  rank of the 21-day return      what it has done lately
  30%  rank of the 63-day return      the quarter, for context
  20%  rank of RS-LINE POSITION       where stock/index sits inside its own
                                      6-month range; 100 = the stock has never
                                      been stronger against the market

The third term is the one that matters most conceptually: it asks "is this name
gaining on the market right now", which is a different question from "how much
has it gone up". MU reads 54 on it (halfway down from its June peak) while BBIO
reads 100.

Each term is ranked across the market BEFORE blending. Blending raw returns
instead lets the largest number dominate regardless of its weight — a -36%
quarter contributed -14.3 against a +266% year contributing +53.1, so the
nominally double-weighted recent window was arithmetically invisible.
"""

import numpy as np
import pandas as pd

# Return windows (trading days) and their weights, plus the weight given to the
# RS-line position term. These three must sum to 1.0.
RS_WINDOWS = {21: 0.50, 63: 0.30}
RS_LINE_WEIGHT = 0.20
# The window the RS line's high/low range is measured over.
RS_LINE_RANGE_DAYS = 126
assert abs(sum(RS_WINDOWS.values()) + RS_LINE_WEIGHT - 1.0) < 1e-9

# A stock needs at least this much history before a rating means anything —
# a name three weeks past IPO has no trailing return to rank.
MIN_HISTORY_DAYS = 63

# Whose trading days define the calendar. Ranking must happen across a single
# real session, not across a union of every symbol's dates.
CALENDAR_SYMBOL = "^IXIC"


def build_frame(price_cache, tickers=None, calendar_symbol=None):
    """{ticker: {date: close}} -> wide DataFrame, dates ascending.

    Restricted to the calendar symbol's sessions. Without this the frame is the
    UNION of every ticker's dates — 808 rows where the market only traded 524 —
    and the extra rows carry prices for a handful of names each. A percentile
    computed on such a row ranks a stock against almost nobody, which silently
    produces nonsense ratings on those dates.
    """
    tickers = tickers if tickers is not None else list(price_cache)
    series = {t: pd.Series(price_cache[t]) for t in tickers if price_cache.get(t)}
    if not series:
        return pd.DataFrame()
    frame = pd.DataFrame(series)
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()

    calendar_symbol = calendar_symbol or CALENDAR_SYMBOL
    if calendar_symbol in frame.columns:
        sessions = frame[calendar_symbol].dropna().index
        frame = frame.loc[sessions]
    return frame


def compute_ratings(price_cache, tickers=None, benchmark=None):
    """RS rating 1-99 for every ticker on every date.

    Returns {ticker: {date_str: rating}}.
    """
    benchmark = benchmark or CALENDAR_SYMBOL
    frame = build_frame(price_cache, tickers, benchmark)
    if frame.empty or benchmark not in frame.columns:
        return {}

    # Each window is ranked across the market FIRST, and the RANKS are what get
    # blended.
    #
    # Blending the raw returns instead is wrong, and badly so. Returns over
    # different windows are on wildly different scales, so the largest number
    # swamps the rest no matter what weight sits in front of it. AAOI on
    # 2026-07-28: -36% over three months contributed -14.3 to the blend while
    # +266% over twelve months contributed +53.1, giving a rating of 97 for a
    # stock that had been bleeding for five weeks. The 3-month leg was weighted
    # double and was still arithmetically invisible.
    #
    # Ranking first puts every window on the same 0-100 scale, so a weight of
    # 0.40 actually means 40% of the answer. That -36% quarter lands in the
    # bottom decile of the market and carries its full weight.
    enough = frame.shift(MIN_HISTORY_DAYS).notna()

    blended = None
    for window, weight in RS_WINDOWS.items():
        ret = (frame / frame.shift(window) - 1).where(enough)
        window_rank = ret.rank(axis=1, pct=True) * 100
        contribution = window_rank * weight
        blended = contribution if blended is None else blended.add(contribution, fill_value=np.nan)

    # RS-line position: the stock divided by the index, expressed as where it
    # sits inside its own recent range. This is what separates "went up a lot
    # once" from "is gaining on the market now".
    rs_line = frame.div(frame[benchmark], axis=0)
    low = rs_line.rolling(RS_LINE_RANGE_DAYS).min()
    high = rs_line.rolling(RS_LINE_RANGE_DAYS).max()
    span = (high - low).replace(0, np.nan)
    position = ((rs_line - low) / span * 100).where(enough)
    blended = blended.add(position.rank(axis=1, pct=True) * 100 * RS_LINE_WEIGHT,
                          fill_value=np.nan)

    # Re-rank the blend so the output is itself a clean percentile rather than
    # a weighted average of percentiles, which would bunch toward the middle.
    ranked = blended.rank(axis=1, pct=True) * 100
    ranked = ranked.round().clip(1, 99)

    out = {}
    for ticker in ranked.columns:
        col = ranked[ticker].dropna()
        if col.empty:
            continue
        out[ticker] = {d.strftime("%Y-%m-%d"): int(v) for d, v in col.items()}
    return out


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cache as cache_mod
    import config

    cache = cache_mod.load(config.price_cache_path("US"))
    ratings = compute_ratings(cache)
    print(f"rated {len(ratings)} tickers")
    for t in ["MU", "SNDK", "WDC", "AAOI", "AXTI", "MBX", "BBIO", "DDOG", "NVDA"]:
        r = ratings.get(t)
        if not r:
            print(f"  {t}: no rating")
            continue
        last = sorted(r)[-1]
        print(f"  {t}: {r[last]} on {last}  ({len(r)} dated values)")
