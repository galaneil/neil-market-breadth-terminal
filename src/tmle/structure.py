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

# Shortest run above the 30-week average that counts as an advance. Below this
# it is a poke over the line, not a move.
MIN_EPISODE_DAYS = 5

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


def last_completed_episode(arrays, i):
    """(start, end) of the most recent FINISHED advance at or before index i.

    Used when price is below the 30-week average, i.e. the advance is over.
    `end` is the last session it was still above; the advance is measured to
    there, while stage and drawdown are still read as of today.
    """
    close, ma30w = arrays["close"], arrays["ma30w"]
    valid = np.where(~np.isnan(ma30w))[0]
    if not len(valid) or i < valid[0]:
        return None
    first = valid[0]
    mask = close[first:i + 1] >= ma30w[first:i + 1]
    if not mask.any():
        return None                      # never advanced in the data we hold

    # Walk back through CONTIGUOUS runs above the average and take the most
    # recent one long enough to be an advance. Using simply the last session
    # above it does not work: a single day poking over the line is a run of
    # one, fails the length test, and the name gets dropped — which is how
    # NVDA and SNDK disappeared again once they had more history to poke with.
    idx = np.where(mask)[0]
    breaks = np.where(np.diff(idx) > 1)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [len(idx) - 1]))
    for s, e in zip(reversed(starts), reversed(ends)):
        run_start, run_end = first + idx[s], first + idx[e]
        if run_end - run_start >= MIN_EPISODE_DAYS:
            return run_start, run_end
    return None


def episode_stats(arrays, i, bench_close=None):
    """Describe the advance at index i — the one in progress, or the last one.

    WHY A BROKEN NAME IS STILL SCORED
    -----------------------------------------------------------------------
    This used to return None whenever price sat below the 30-week average, and
    the engine dropped the name entirely. That silently deleted every broken
    ex-leader from the output: on 2026-07-29 the universe came back 852 Stage 2
    and 148 Stage 3, with zero Stage 1 and zero Stage 4, on a day with 991
    decliners. NVDA, PLTR, SNDK and AAOI were not scored at all, and the Stage 4
    flag Neil asked for could never appear, because the only names that reached
    the stage check were the ones already above their 30-week.

    So when the advance is over, the FACTORS are still measured across it — the
    leadership it genuinely earned does not un-happen — while `stage` and
    `drawdown` are read as of today, which is what says "do not touch this".
    Score ranks, stage permits.
    """
    # Whether the advance is over is decided by where price is TODAY, not by
    # how long the current run has lasted. Routing on run length instead put
    # NVDA and SNDK — both of which had just reclaimed their 30-week within a
    # few days — down the "finished" branch, so they came back reading
    # "Advancing" and "ended" simultaneously, measured against a move that had
    # already been superseded.
    ma30w = arrays["ma30w"]
    above_today = (not np.isnan(ma30w[i])) and arrays["close"][i] >= ma30w[i]

    start = episode_start(arrays, i)
    ended = False
    measure_to = i

    if not above_today or start is None:
        closed = last_completed_episode(arrays, i)
        if closed is None:
            return None
        start, measure_to = closed
        ended = True
    elif i - start < MIN_EPISODE_DAYS:
        # Above the average, but only just — a genuine advance this young has
        # nothing to measure yet. Reported as running, with the flag set so
        # the factors and the UI can say "too early" rather than invent a
        # reading from three sessions.
        pass

    close = arrays["close"]
    # The advance itself is measured start..measure_to (which is today while it
    # is still running, and the day it broke once it is over).
    window = close[start:measure_to + 1]
    low = float(np.min(window))
    high = float(np.max(window))
    peak = float(np.max(close[start:i + 1]))   # highest close of the advance
    last = float(close[i])
    at_end = float(close[measure_to])

    stats = {
        "start_index": start,
        "end_index": int(measure_to),
        "ended": ended,
        # Above the 30-week, but for fewer sessions than MIN_EPISODE_DAYS —
        # a reclaim too fresh to read anything into.
        "young": bool(not ended and (measure_to - start) < MIN_EPISODE_DAYS),
        "length": int(measure_to - start + 1),
        "gain_from_low": (at_end / low - 1) * 100 if low > 0 else None,
        # Drawdown is always to TODAY from the advance's peak, running or not —
        # it is the "how damaged is this" reading, and on a finished advance
        # that is the whole question.
        "drawdown": (last / peak - 1) * 100 if peak > 0 else None,
        "stage": int(arrays["stage"][i]),
        "slope30w": float(arrays["slope30w"][i]) if not np.isnan(arrays["slope30w"][i]) else None,
        # Neil's own test for a clean leader: how often has it given up the
        # 20-day, and has it ever lost the 10-week while the move was on.
        "breaks_20": int(np.sum(window < arrays["ema20"][start:measure_to + 1])),
        "breaks_10w": int(np.sum(window < arrays["ma10w"][start:measure_to + 1])),
        "days_above_10w": int(np.sum(window >= arrays["ma10w"][start:measure_to + 1])),
    }
    stats["pct_below_20"] = round(100.0 * stats["breaks_20"] / stats["length"], 1)
    stats["pct_below_10w"] = round(100.0 * stats["breaks_10w"] / stats["length"], 1)
    # How long ago the advance ended, in sessions. Zero while it is running.
    stats["days_since_end"] = int(i - measure_to)

    if bench_close is not None:
        b = np.asarray(bench_close, dtype=float)
        if len(b) > measure_to and not np.isnan(b[start]) and b[start] > 0:
            bench_gain = (b[measure_to] / b[start] - 1) * 100
            stock_gain = (at_end / close[start] - 1) * 100 if close[start] > 0 else None
            if stock_gain is not None:
                stats["episode_rs"] = stock_gain - bench_gain
                stats["stock_gain"] = stock_gain
                stats["bench_gain"] = bench_gain
    return stats
