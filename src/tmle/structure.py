"""
TMLE — structure.py

Stage analysis and episode detection. This is the piece that was missing, and
the reason a trailing window could not work.

THE PROBLEM IT SOLVES
---------------------------------------------------------------------------
A fixed lookback is dominated by the past, so it keeps calling a stock a leader
long after the move has ended. Measured on 2026-07-29, on real stored data:

    SNDK  trailing 1y +3451%   but 45% off its high (high 5 weeks earlier)
    AAOI  trailing 1y +1028%   but 56% off its high
    LITE  trailing 1y +1396%   but 32% off its high

All three would score maximum price leadership while broken. And the mirror
failure: NVDA's real advance ran Oct-2022 to Jan-2025, which no calendar year
and no trailing year contains — measured over the last 252 days it reads +13%
and scores badly.

THE FIX
---------------------------------------------------------------------------
Stop measuring over a fixed window. Measure over the MOVE, and report its
health separately:

  strength  how big and how clean the advance has been, measured from where
            the advance actually began
  stage     whether that advance is still intact

The advance's start is the Weinstein definition: the last time price closed
below its 30-week average. Everything before that belongs to a previous cycle.

  Stage 1  basing      below/around a flat 30-week
  Stage 2  advancing   above a rising 30-week          <- the only buyable one
  Stage 3  topping     above a 30-week that has stalled
  Stage 4  declining   below a falling 30-week         <- the no-go flag

The 10-week average carries the shorter-term trend, and the count of closes
below the 20-day and the 10-week is what separates a clean leader from a messy
one — a real leader rarely gives up the 20 and almost never loses the 10-week
while the move is on.
"""

import numpy as np

# Weinstein's weeks, expressed in trading days.
MA_10W = 50
MA_30W = 150
SHORT_EMA = 20          # the average a clean leader is not supposed to lose
SLOPE_DAYS = 20         # ~4 weeks, for judging whether the 30-week is rising
SLOPE_FLAT_PCT = 0.5    # |change| below this over SLOPE_DAYS counts as flat

STAGE_LABELS = {
    1: "Basing",
    2: "Advancing",
    3: "Topping",
    4: "Declining",
}


def _sma(values, window):
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    cumulative = np.cumsum(np.insert(values, 0, 0.0))
    out[window - 1:] = (cumulative[window:] - cumulative[:-window]) / window
    return out


def _ema(values, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(values))
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1 - alpha)
    return out


def compute(close):
    """Moving averages, 30-week slope, and a stage per session."""
    close = np.asarray(close, dtype=float)
    ma10w = _sma(close, MA_10W)
    ma30w = _sma(close, MA_30W)
    ema20 = _ema(close, SHORT_EMA)

    slope = np.full(len(close), np.nan)
    if len(close) > SLOPE_DAYS:
        prior = ma30w[:-SLOPE_DAYS]
        with np.errstate(invalid="ignore", divide="ignore"):
            slope[SLOPE_DAYS:] = (ma30w[SLOPE_DAYS:] / prior - 1) * 100

    above30 = close > ma30w
    rising = slope > SLOPE_FLAT_PCT
    falling = slope < -SLOPE_FLAT_PCT

    stage = np.zeros(len(close), dtype=int)
    stage[above30 & rising] = 2
    stage[above30 & ~rising] = 3
    stage[~above30 & falling] = 4
    stage[~above30 & ~falling] = 1
    # Anything without a 30-week average yet has no stage.
    stage[np.isnan(ma30w)] = 0

    return {"close": close, "ma10w": ma10w, "ma30w": ma30w,
            "ema20": ema20, "slope30w": slope, "stage": stage}


def episode_start(arrays, i):
    """Index where the current advance began: the session after the last close
    below the 30-week average. If price has never been below it in the data we
    hold, the episode starts at the first session with a valid 30-week value."""
    close, ma30w = arrays["close"], arrays["ma30w"]
    valid = np.where(~np.isnan(ma30w))[0]
    if not len(valid) or i < valid[0]:
        return None
    first = valid[0]
    below = np.where(close[first:i + 1] < ma30w[first:i + 1])[0]
    if not len(below):
        return first
    start = first + below[-1] + 1
    return start if start <= i else None


def episode_stats(arrays, i, bench_close=None):
    """Describe the advance in progress at index i.

    Returns None when there is not enough history, or when the name is not in
    an advance at all (price below its 30-week average — the episode has ended).
    """
    start = episode_start(arrays, i)
    if start is None or i - start < 5:
        return None

    close = arrays["close"]
    window = close[start:i + 1]
    low = float(np.min(window))
    high = float(np.max(window))
    last = float(close[i])

    stats = {
        "start_index": start,
        "length": int(i - start + 1),
        "gain_from_low": (last / low - 1) * 100 if low > 0 else None,
        "drawdown": (last / high - 1) * 100 if high > 0 else None,
        "stage": int(arrays["stage"][i]),
        "slope30w": float(arrays["slope30w"][i]) if not np.isnan(arrays["slope30w"][i]) else None,
        # Neil's own test for a clean leader: how often has it given up the
        # 20-day, and has it ever lost the 10-week while the move was on.
        "breaks_20": int(np.sum(window < arrays["ema20"][start:i + 1])),
        "breaks_10w": int(np.sum(window < arrays["ma10w"][start:i + 1])),
        "days_above_10w": int(np.sum(window >= arrays["ma10w"][start:i + 1])),
    }
    stats["pct_below_20"] = round(100.0 * stats["breaks_20"] / stats["length"], 1)
    stats["pct_below_10w"] = round(100.0 * stats["breaks_10w"] / stats["length"], 1)

    if bench_close is not None:
        b = np.asarray(bench_close, dtype=float)
        if len(b) > i and not np.isnan(b[start]) and b[start] > 0:
            bench_gain = (b[i] / b[start] - 1) * 100
            stock_gain = (last / close[start] - 1) * 100 if close[start] > 0 else None
            if stock_gain is not None:
                stats["episode_rs"] = stock_gain - bench_gain
                stats["stock_gain"] = stock_gain
                stats["bench_gain"] = bench_gain
    return stats
