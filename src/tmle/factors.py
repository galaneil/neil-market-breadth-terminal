"""
TMLE — factors.py

The scoring factors, ported from the notebook and re-anchored.

Every band, weight and threshold is Neil's, unchanged. What changed is WHAT
WINDOW they are measured over — see structure.py for why a fixed window cannot
work. In short: the notebook measured a calendar year, the first port measured
a trailing year, and both keep scoring a stock as a leader after its move has
ended. These measure the move itself, from where it began.

  F1   Price Leadership    RS vs the Nasdaq 100, over the episode
  F4   Price Structure     trend discipline, new highs, and whether it held
                           its 20-day and 10-week while advancing
  F4B  Volume Behavior     up/down volume and big-day confirmation, over the
                           episode
  F5   Theme Alignment     its industry's rank and whether that rank is climbing

Stage is deliberately NOT a factor. It does not adjust the score — it decides
whether the score is actionable. Folding "this is broken" into a single number
loses both facts; kept apart, a Stage 4 ex-leader still shows the strength it
genuinely had, and simply never appears in a buy list.
"""

import numpy as np

from tmle import config, structure


def _band_score(value, bands, default=0):
    """[(threshold, points), ...] high->low; first match wins."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    for threshold, points in bands:
        if value >= threshold:
            return points
    return default


# ── F1 — PRICE LEADERSHIP ──────────────────────────────────────────────────
def f1_score(episode):
    """Relative strength versus the benchmark across the advance.

    Same scale as the notebook (50 + RS/200 * 50, clipped), but RS is now the
    stock's gain minus the benchmark's gain over the SAME episode window,
    instead of over a calendar or trailing year.
    """
    rs = episode.get("episode_rs")
    if rs is None:
        return None
    return float(np.clip(50 + (rs / config.F1_RS_SCALE) * 50, 0, 100))


# ── F4 — PRICE STRUCTURE ───────────────────────────────────────────────────
def f4_score(arrays, i, episode):
    """Trend discipline + new highs + moving-average discipline, over the
    episode rather than the year.

    The discipline component is Neil's own test for a clean leader, stated as
    "did not break the 20 more than twice, did not break the 50 at all". The
    10-week carries most of the weight because losing it is the more serious
    event; the 20-day is allowed to wobble.
    """
    start = episode["start_index"]
    close = arrays["close"][start:i + 1]
    ma10w = arrays["ma10w"][start:i + 1]
    length = len(close)
    if length < 5:
        return None

    # Trend discipline — share of the advance spent above the 10-week.
    pct_above = np.sum(close >= ma10w) / length * 100
    trend = (pct_above / 100) * config.F4_TREND_MAX

    # New highs made during the advance. Episodes differ in length, so a raw
    # count would just reward long ones — but the notebook's bands were
    # calibrated against a count over ~252 sessions, so the rate is scaled back
    # to a year-equivalent rather than converted to a percentage. That keeps
    # Neil's thresholds meaning what they meant.
    running_max = np.maximum.accumulate(close)
    new_highs = int(np.sum(close >= running_max * 0.999))
    per_year = new_highs / length * config.TRAILING_DAYS
    highs = _band_score(per_year, config.F4_NEWHIGH_BANDS)

    # Discipline: how often it gave up each average while advancing.
    tenw_quality = np.clip(1 - episode["pct_below_10w"] / config.F4_BREAK_10W_TOLERANCE, 0, 1)
    twenty_quality = np.clip(1 - episode["pct_below_20"] / config.F4_BREAK_20_TOLERANCE, 0, 1)
    ma_disc = config.F4_MA_DISCIPLINE_MAX * (
        config.F4_10W_SHARE * tenw_quality + config.F4_20_SHARE * twenty_quality)

    return float(np.clip(trend + highs + ma_disc, 0, 100))


# ── F4B — VOLUME BEHAVIOR ──────────────────────────────────────────────────
def f4b_score(arrays, i, episode):
    """Up/down volume, and whether big moves were confirmed by volume —
    measured across the advance."""
    volume = arrays.get("volume")
    if volume is None or np.all(np.isnan(volume)):
        return None

    start = episode["start_index"]
    if i - start < 10:
        return None

    close = arrays["close"]
    prior = np.empty_like(close)
    prior[0] = np.nan
    prior[1:] = close[:-1]

    sl = slice(start, i + 1)
    c, p, v = close[sl], prior[sl], volume[sl]
    adr, vol_avg = arrays["adr"][sl], arrays["vol_avg"][sl]

    up_vol = np.nansum(np.where(c > p, v, 0))
    down_vol = np.nansum(np.where(c < p, v, 0))
    ud_ratio = (up_vol / down_vol) if down_vol > 0 else 3.0
    ud_score = _band_score(ud_ratio, config.F4B_UD_BANDS, default=10)

    with np.errstate(invalid="ignore"):
        chg = (c - p) / p * 100
        big = np.abs(chg) >= config.F4B_BIG_DAY_ADR_MULT * adr
    big_up = big & (chg > 0)
    big_down = big & (chg < 0)

    # Neutral when there were no big days to judge, as in the notebook.
    if np.sum(big_up):
        bigup = np.sum(big_up & (v > vol_avg)) / np.sum(big_up) * 100
    else:
        bigup = 50.0
    if np.sum(big_down):
        bigdown = np.sum(big_down & (v < vol_avg)) / np.sum(big_down) * 100
    else:
        bigdown = 50.0

    return float(np.clip(ud_score * config.F4B_UD_WEIGHT +
                         bigup * config.F4B_BIGUP_WEIGHT +
                         bigdown * config.F4B_BIGDOWN_WEIGHT, 0, 100))


# ── F2 — FUNDAMENTAL QUALITY ───────────────────────────────────────────────
def f2_score(fund):
    """Revenue growth 40 / EPS growth 30 / operating margin 30 — Neil's bands,
    unchanged, fed from TradingView's TTM figures.

    THE IMPORTANT PART is what happens when there are no fundamentals.

    Returning None would be wrong. The composite renormalises over available
    factors, so a None here means the name is scored on price and theme alone
    and is not penalised at all — which is precisely how a pre-revenue biotech
    ended up ranked as a market leader. Companies with no revenue are not
    missing data; the absence IS the datum, and it scores zero.

    A genuine data gap (no financials published at all, e.g. a recent listing or
    a fund) also scores zero rather than being excused, because a leader we
    cannot verify has a business is not a leader we should be buying.
    """
    if not fund:
        return 0.0
    revenue = fund.get("revenue")
    if revenue is None or revenue <= 0:
        return 0.0

    rev = _band_score(fund.get("revenue_growth"), config.F2_REVENUE_BANDS)
    eps = _band_score(fund.get("eps_growth"), config.F2_EPS_BANDS)
    margin = _band_score(fund.get("operating_margin"), config.F2_MARGIN_BANDS)
    return float(rev + eps + margin)


# ── F5 — THEME & SECTOR ALIGNMENT ──────────────────────────────────────────
def _current_rank_score(rank):
    """The notebook's tiering: top 5 near 100, easing to a 30 floor past 20."""
    if rank is None or (isinstance(rank, float) and np.isnan(rank)):
        return config.F5_NO_DATA_SCORE
    if rank <= 5:
        return 100 - (rank - 1) * 3
    if rank <= 10:
        return 70 - (rank - 6) * 3
    if rank <= 20:
        return 50 - (rank - 11) * 2
    return config.F5_NO_DATA_SCORE


def f5_score(rank_now, rank_then):
    """Industry standing now, plus how far that rank has climbed.

    The notebook built its own monthly industry ranks. The breadth terminal
    already stores this daily and backfilled a year, so this is the one factor
    that got better in the port rather than merely equivalent.
    """
    current = _current_rank_score(rank_now)
    if rank_now is None or rank_then is None:
        momentum = _band_score(None, config.F5_MOMENTUM_BANDS, default=5)
    else:
        climb = rank_then - rank_now      # positive = improved (rank number fell)
        momentum = _band_score(climb, config.F5_MOMENTUM_BANDS, default=5)
    return float(current * config.F5_CURRENT_WEIGHT +
                 momentum * config.F5_MOMENTUM_WEIGHT)


def prepare(rows):
    """Per-ticker arrays needed by every factor, computed once."""
    if not rows:
        return None
    rows = sorted((r for r in rows if r.get("close") is not None), key=lambda r: r["date"])
    if len(rows) < structure.MA_30W + structure.SLOPE_DAYS:
        return None

    dates = [r["date"] for r in rows]
    close = np.array([float(r["close"]) for r in rows])
    high = np.array([float(r["high"]) if r.get("high") else np.nan for r in rows])
    low = np.array([float(r["low"]) if r.get("low") else np.nan for r in rows])
    volume = np.array([float(r["volume"]) if r.get("volume") else np.nan for r in rows])

    arrays = structure.compute(close)
    arrays["dates"] = dates
    arrays["volume"] = volume

    with np.errstate(invalid="ignore", divide="ignore"):
        true_range = (high - low) / close * 100
    arrays["adr"] = _rolling_mean(true_range, config.F4B_ADR_WINDOW)
    arrays["vol_avg"] = _rolling_mean(volume, config.F4B_VOL_AVG_WINDOW)
    return arrays


def _rolling_mean(values, window):
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    series = np.nan_to_num(values, nan=0.0)
    counts = (~np.isnan(values)).astype(float)
    csum = np.cumsum(np.insert(series, 0, 0.0))
    ccnt = np.cumsum(np.insert(counts, 0, 0.0))
    total = csum[window:] - csum[:-window]
    n = ccnt[window:] - ccnt[:-window]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[window - 1:] = np.where(n > 0, total / n, np.nan)
    return out
