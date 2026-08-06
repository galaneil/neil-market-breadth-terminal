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
    trend_thresholds,
    PARTICIPATION_BULL_MIN, PARTICIPATION_BEAR_MAX,
    INTERNALS_LOOKBACK_DAYS,
    TOP_MOVERS_COUNT, MOVER_WINDOWS,
)

EMA_KEYS = ["above_ema10", "above_ema20", "above_ema50"]


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


def compute_trend(index_series, date_str, large_cap_keys=()):
    """index_series: {index_key: [rows]} for whichever indices this country
    tracks. Counts how many of the close-vs-EMA comparisons are favourable —
    9 for a 3-index market like the US, 12 for India's 4.

    `large_cap_keys` names the broad/large-cap indices, so the summary can also
    report the read with the small-cap index excluded."""
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
        if key in large_cap_keys:
            large_favourable += hits
            large_total += len(EMA_KEYS)

    if not total:
        return None

    return {
        "factors_favourable": favourable,
        "factors_total": total,
        "label": _label(favourable, *trend_thresholds(total)),
        "large_cap_favourable": large_favourable,
        "large_cap_total": large_total,
        "per_index": per_index,
    }


def _positive_counts(items, field="chg_20d"):
    """(positive_count, total, pct) — the raw counts matter as much as the
    share: "12 of 19" is a different confidence than "63%" alone implies."""
    values = [i.get(field) for i in items if i.get(field) is not None]
    if not values:
        return None, None, None
    positive = sum(1 for v in values if v > 0)
    return positive, len(values), round(100.0 * positive / len(values), 1)


def compute_participation(sector_rows, industry_rows, date_str):
    """Share of sectors / industries with a positive 20-day return — how
    broad the move is, as opposed to how the indices themselves look."""
    sector_row = _row_for_date(sector_rows, date_str) if sector_rows else None
    industry_row = _row_for_date(industry_rows, date_str) if industry_rows else None

    s_pos, s_total, s_pct = _positive_counts(sector_row["sectors"]) if sector_row else (None, None, None)
    i_pos, i_total, i_pct = _positive_counts(industry_row["industries"]) if industry_row else (None, None, None)

    parts = [p for p in (s_pct, i_pct) if p is not None]
    if not parts:
        return None
    combined = sum(parts) / len(parts)

    return {
        "sectors_positive": s_pos,
        "sectors_total": s_total,
        "sectors_positive_pct": s_pct,
        "industries_positive": i_pos,
        "industries_total": i_total,
        "industries_positive_pct": i_pct,
        "label": _label(combined, PARTICIPATION_BULL_MIN, PARTICIPATION_BEAR_MAX),
    }


def _movers(items, name_field, value_field, n):
    """Best and worst n groups by return over one window."""
    usable = [i for i in items if i.get(value_field) is not None]
    if not usable:
        return {"top": [], "bottom": []}
    ordered = sorted(usable, key=lambda i: i[value_field], reverse=True)

    def shape(entries):
        return [{"name": e[name_field], "chg": round(e[value_field], 2)} for e in entries]

    return {"top": shape(ordered[:n]), "bottom": shape(ordered[-n:][::-1])}


def compute_leaders(sector_rows, industry_rows, date_str):
    """Which sectors and industries are actually gaining and losing traction,
    over a week and a month. Deliberately not a 1-day view: a single session
    reshuffles the order without telling you anything about rotation."""
    sector_row = _row_for_date(sector_rows, date_str) if sector_rows else None
    industry_row = _row_for_date(industry_rows, date_str) if industry_rows else None
    if not sector_row and not industry_row:
        return None

    out = {}
    for window, field in MOVER_WINDOWS.items():
        out[window] = {
            "sectors": _movers(sector_row["sectors"], "sector", field, TOP_MOVERS_COUNT) if sector_row else {"top": [], "bottom": []},
            "industries": _movers(industry_row["industries"], "industry", field, TOP_MOVERS_COUNT) if industry_row else {"top": [], "bottom": []},
        }
    return out


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


def _overall(trend):
    """The verdict follows the INDEX ACTION, and nothing else.

    It used to require unanimity across trend, participation and internals,
    which meant any one of them could veto the other two. That went wrong in
    practice: internals is judged on the SIGN of two 10-day averages, so an
    advance-decline spread of -25 on a 981-name market — about 9% of a typical
    day's spread, statistically flat — outvoted a 12/12 trend and 69%
    participation. The result was "choppy" on two days in three in both
    markets, which is not a description of anything.

    Participation and internals are still computed and still shown. They are
    context for HOW the move is happening — broad or narrow, confirmed or
    diverging — which is a different question from what the tape is doing.
    """
    return trend["label"] if trend else "unknown"


def compute_environment(index_series, sector_rows, industry_rows,
                        adv_decl_rows, new_hilo_rows, date_str, large_cap_keys=()):
    trend = compute_trend(index_series, date_str, large_cap_keys)
    participation = compute_participation(sector_rows, industry_rows, date_str)
    internals = compute_internals(adv_decl_rows, new_hilo_rows, date_str)
    leaders = compute_leaders(sector_rows, industry_rows, date_str)

    return {
        "date": date_str,
        "overall": _overall(trend),
        "trend": trend,
        "participation": participation,
        "internals": internals,
        "leaders": leaders,
    }


def backfill_environment(index_series, sector_rows, industry_rows,
                         adv_decl_rows, new_hilo_rows, dates, large_cap_keys=()):
    return [
        compute_environment(index_series, sector_rows, industry_rows,
                            adv_decl_rows, new_hilo_rows, d, large_cap_keys)
        for d in dates
    ]
