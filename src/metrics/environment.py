"""
metrics/environment.py — the daily "what is the market actually doing" read.

This is the one place in the project that deliberately synthesises rather than
reporting raw numbers. Everything else stays plain counts/percentages/ranks;
here we collapse them into bullish / choppy / bearish so the answer to "am I
sitting out, going long, or going short" is readable at a glance instead of
being reconstructed by eye from nine separate EMA comparisons every morning.

It describes what the tape IS doing, not what it will do — every input is a
completed, observable fact about the last closed session.

Three independent reads, kept separate rather than blended into one number:

  1. Trend      — the 9 index factors: NASDAQ / S&P 500 / Russell 2000, each
                  measured against its own 10, 20 and 50 EMA. Also reported
                  large-cap-only (6 factors, Russell excluded), since small
                  caps can drag the count while the large caps are fine.
  2. Participation — how broad the move is: the share of sectors, and of
                  industries, with a positive 20-day return.
  3. Internals  — direction of the breadth internals over the last 10
                  sessions (advancers vs decliners, new highs vs new lows).

Computed per date and stored, so any past day can be looked up later by the
replay view or by a trade log asking "what was the environment that day".
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    TREND_BULL_MIN, TREND_BEAR_MAX,
    PARTICIPATION_BULL_MIN, PARTICIPATION_BEAR_MAX,
    INTERNALS_LOOKBACK_DAYS,
)

EMA_KEYS = ["above_ema10", "above_ema20", "above_ema50"]
LARGE_CAP_KEYS = ["nasdaq", "sp500"]


def _label(value, bull_min, bear_max):
    if value >= bull_min:
        return "bullish"
    if value <= bear_max:
        return "bearish"
    return "choppy"


def _row_for_date(rows, date_str):
    """Last row on or before date_str (rows are date-ascending)."""
    match = None
    for r in rows:
        if r["date"] <= date_str:
            match = r
        else:
            break
    return match


def compute_trend(index_series, date_str):
    """index_series: {"nasdaq": [rows], "sp500": [rows], "russell2000": [rows]}.
    Counts how many of the 9 close-vs-EMA comparisons are favourable."""
    per_index = {}
    favourable = 0
    total = 0
    large_favourable = 0
    large_total = 0

    for key, rows in index_series.items():
        row = _row_for_date(rows, date_str) if rows else None
        if not row:
            continue
        hits = sum(1 for k in EMA_KEYS if row.get(k))
        per_index[key] = {
            "above_ema10": bool(row.get("above_ema10")),
            "above_ema20": bool(row.get("above_ema20")),
            "above_ema50": bool(row.get("above_ema50")),
            "score": hits,
        }
        favourable += hits
        total += len(EMA_KEYS)
        if key in LARGE_CAP_KEYS:
            large_favourable += hits
            large_total += len(EMA_KEYS)

    if not total:
        return None

    return {
        "factors_favourable": favourable,
        "factors_total": total,
        "label": _label(favourable, TREND_BULL_MIN, TREND_BEAR_MAX),
        "large_cap_favourable": large_favourable,
        "large_cap_total": large_total,
        "per_index": per_index,
    }


def _positive_share(items, field="chg_20d"):
    values = [i.get(field) for i in items if i.get(field) is not None]
    if not values:
        return None
    return round(100.0 * sum(1 for v in values if v > 0) / len(values), 1)


def compute_participation(sector_rows, industry_rows, date_str):
    """Share of sectors / industries with a positive 20-day return — how
    broad the move is, as opposed to how the indices themselves look."""
    sector_row = _row_for_date(sector_rows, date_str) if sector_rows else None
    industry_row = _row_for_date(industry_rows, date_str) if industry_rows else None

    sector_pct = _positive_share(sector_row["sectors"]) if sector_row else None
    industry_pct = _positive_share(industry_row["industries"]) if industry_row else None

    parts = [p for p in (sector_pct, industry_pct) if p is not None]
    if not parts:
        return None
    combined = sum(parts) / len(parts)

    return {
        "sectors_positive_pct": sector_pct,
        "industries_positive_pct": industry_pct,
        "label": _label(combined, PARTICIPATION_BULL_MIN, PARTICIPATION_BEAR_MAX),
    }


def _trailing_mean(rows, date_str, field, n):
    window = [r for r in rows if r["date"] <= date_str][-n:]
    values = [r.get(field) for r in window if r.get(field) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def compute_internals(adv_decl_rows, new_hilo_rows, date_str):
    """Direction of the breadth internals over the trailing window. Averaged
    rather than read off the latest day, because a single day's net figure
    whipsaws too much to describe an environment."""
    n = INTERNALS_LOOKBACK_DAYS
    adv_avg = _trailing_mean(adv_decl_rows, date_str, "net", n) if adv_decl_rows else None
    hilo_avg = _trailing_mean(new_hilo_rows, date_str, "net", n) if new_hilo_rows else None

    signals = [v for v in (adv_avg, hilo_avg) if v is not None]
    if not signals:
        return None
    positive = sum(1 for v in signals if v > 0)

    if positive == len(signals):
        label = "bullish"
    elif positive == 0:
        label = "bearish"
    else:
        label = "choppy"

    return {
        "adv_decl_avg": round(adv_avg, 1) if adv_avg is not None else None,
        "new_hilo_avg": round(hilo_avg, 1) if hilo_avg is not None else None,
        "lookback_days": n,
        "label": label,
    }


def _overall(labels):
    """Overall read across the three components. Unanimity is required for a
    directional call — if the components disagree, the honest description of
    the tape is that it is choppy."""
    present = [l for l in labels if l]
    if not present:
        return "unknown"
    if all(l == "bullish" for l in present):
        return "bullish"
    if all(l == "bearish" for l in present):
        return "bearish"
    return "choppy"


def compute_environment(index_series, sector_rows, industry_rows,
                        adv_decl_rows, new_hilo_rows, date_str):
    trend = compute_trend(index_series, date_str)
    participation = compute_participation(sector_rows, industry_rows, date_str)
    internals = compute_internals(adv_decl_rows, new_hilo_rows, date_str)

    labels = [
        trend["label"] if trend else None,
        participation["label"] if participation else None,
        internals["label"] if internals else None,
    ]

    return {
        "date": date_str,
        "overall": _overall(labels),
        "trend": trend,
        "participation": participation,
        "internals": internals,
    }


def backfill_environment(index_series, sector_rows, industry_rows,
                         adv_decl_rows, new_hilo_rows, dates):
    return [
        compute_environment(index_series, sector_rows, industry_rows,
                            adv_decl_rows, new_hilo_rows, d)
        for d in dates
    ]
