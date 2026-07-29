"""
TMLE — run.py

Orchestration: build the engine from data the daily pipeline already has,
score the universe at a series of checkpoints, and persist the results.

What gets stored, and why in two shapes:

  data/<country>/tmle_scores.jsonl    every scored name, composite only, per
                                      checkpoint. Compact (ticker + integer),
                                      and it is what makes "who is CLIMBING"
                                      answerable — a name going from rank 900
                                      to 300 never appears in a top-250 table,
                                      so the leaderboard alone cannot tell you.

  data/<country>/tmle_leaders.jsonl   the top slice with full factor detail.
                                      This is the accumulating record worth
                                      keeping forever.

  docs/<country>/tmle/<TICKER>.json   one small per-name trajectory, fetched on
                                      demand by the stock card. Same pattern as
                                      the price files: regenerated each run,
                                      published to gh-pages, never committed.
"""

import json
import os

import config as app_config
import store
from tmle import config, engine


def build_rank_lookup(country):
    """(ticker, date) -> its industry's rank that day.

    Reads the industry rank history the breadth terminal already accumulates,
    which is daily and already backfilled a year — finer than the monthly table
    the notebook had to assemble for itself.
    """
    data_dir = app_config.data_dir(country)

    classification = {}
    path = os.path.join(data_dir, "classification.json")
    if os.path.exists(path):
        with open(path) as f:
            classification = json.load(f)
    industry_of = {t: v[1] for t, v in classification.items() if len(v) > 1}

    ranks_by_date = {}
    for row in store.read_jsonl(os.path.join(data_dir, "industry_ranks.jsonl")):
        ranks_by_date[row["date"]] = {
            item["industry"]: item.get("rank")
            for item in row.get("industries", [])
        }
    dates = sorted(ranks_by_date)

    def lookup(ticker, date_str):
        industry = industry_of.get(ticker)
        if not industry:
            return None
        # Last rank table at or before the date, so a checkpoint landing on a
        # non-trading day still resolves.
        chosen = None
        for d in dates:
            if d <= date_str:
                chosen = d
            else:
                break
        if chosen is None:
            return None
        return ranks_by_date[chosen].get(industry)

    return lookup


def checkpoint_dates(session_dates):
    """Weekly checkpoints across the backfill window, always including the most
    recent session. Leadership does not turn over daily, so a weekly point is
    enough to read a trajectory — and it keeps the stored history small."""
    window = session_dates[-config.BACKFILL_DAYS:]
    if not window:
        return []
    picked = window[::-1][::config.BACKFILL_EVERY][::-1]
    if picked[-1] != window[-1]:
        picked.append(window[-1])
    return picked


def momentum_date_for(session_dates, date_str):
    """The session ~3 months before date_str, for F5's rank-change read."""
    idx = None
    for i, d in enumerate(session_dates):
        if d <= date_str:
            idx = i
        else:
            break
    if idx is None:
        return date_str
    return session_dates[max(0, idx - config.F5_MOMENTUM_DAYS)]


def write_scores(country, date_str, rows):
    """Compact full-universe record: aligned ticker and composite arrays."""
    store.upsert_jsonl(
        store.series_path(country, "tmle_scores.jsonl"),
        {
            "date": date_str,
            "t": [r["ticker"] for r in rows],
            "c": [int(round(r["composite"])) for r in rows],
            # Stage travels with the score. Without it the compact record could
            # not answer "was this name actionable then", which is the whole
            # point of storing history rather than only today.
            "s": [r["stage"] for r in rows],
        },
    )


def write_theme_leaders(country, date_str, rows):
    """Ranked on fundamentals and theme alone, ignoring price entirely.

    Not gated on stage or drawdown: the point of this list is precisely the names
    whose business is strong while their chart is not.
    """
    ranked = sorted((r for r in rows if r.get("theme_composite") is not None),
                    key=lambda r: r["theme_composite"], reverse=True)
    top = ranked[:config.LEADERBOARD_SIZE]
    store.upsert_jsonl(
        store.series_path(country, "tmle_theme.jsonl"),
        {
            "date": date_str,
            "leaders": [
                dict({k: r.get(k) for k in
                      ("ticker", "theme_composite", "stage", "actionable", "drawdown",
                       "gain", "episode_days", "F1", "F2", "F2B", "F6", "F4", "F4B", "F5")},
                     theme_rank=i + 1)
                for i, r in enumerate(top)
            ],
        },
    )


def write_leaders(country, date_str, rows):
    top = rows[:config.LEADERBOARD_SIZE]
    store.upsert_jsonl(
        store.series_path(country, "tmle_leaders.jsonl"),
        {
            "date": date_str,
            "leaders": [
                {k: r.get(k) for k in
                 ("ticker", "rank", "composite", "coverage", "stage", "actionable",
                  "drawdown", "gain", "episode_days", "episode_start",
                  "pct_below_20", "pct_below_10w", "F1", "F2", "F2B", "F6", "F4", "F4B", "F5")}
                for r in top
            ],
        },
    )


def write_trajectories(country, history, as_of=None):
    """history: {ticker: [{date, composite, rank, F1, F4, F4B, F5}, ...]}"""
    out_dir = os.path.join(app_config.docs_dir(country), "tmle")
    os.makedirs(out_dir, exist_ok=True)
    written = 0
    for ticker, rows in history.items():
        rows = sorted(rows, key=lambda r: r["date"])
        payload = {
            "ticker": ticker,
            # The last checkpoint the ENGINE ran, so the page can tell whether
            # this name's own last reading is current or stale.
            "as_of": as_of,
            "dates": [r["date"] for r in rows],
            "composite": [r["composite"] for r in rows],
            "rank": [r["rank"] for r in rows],
            "stage": [r["stage"] for r in rows],
            "drawdown": [r["drawdown"] for r in rows],
            # Episode facts travel with the trajectory so the score card can
            # state its read in words without a second fetch.
            "gain": [r.get("gain") for r in rows],
            "episode_days": [r.get("episode_days") for r in rows],
            "pct_below_10w": [r.get("pct_below_10w") for r in rows],
            "pct_below_20": [r.get("pct_below_20") for r in rows],
            "factors": {
                key: [r.get(key) for r in rows]
                for key in ("F1", "F2", "F2B", "F6", "F4", "F4B", "F5")
            },
        }
        name = ticker.replace("/", "-")
        with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written += 1
    return written


def run(country, price_rows, bench_rows, session_dates, market_caps,
        fundamentals=None, quarterly=None, rs_ratings=None, forward=None,
        log=print):
    """Score the universe at weekly checkpoints and persist everything.

    Returns the latest checkpoint's ranked rows.
    """
    lookup = build_rank_lookup(country)
    eng = engine.Engine(price_rows, bench_rows, lookup, market_caps,
                        fundamentals, quarterly, rs_ratings, forward)
    log(f"  price structure built for {len(eng.arrays)} names")

    dates = checkpoint_dates(session_dates)
    if not dates:
        log("  no session dates — nothing to score")
        return []
    log(f"  scoring {len(dates)} checkpoints ({dates[0]} -> {dates[-1]})")

    history, latest = {}, []
    for i, date_str in enumerate(dates):
        rows = eng.score_universe(date_str, momentum_date_for(session_dates, date_str))
        if not rows:
            continue
        write_scores(country, date_str, rows)
        write_leaders(country, date_str, rows)
        write_theme_leaders(country, date_str, rows)
        latest = rows
        # EVERY scored name gets a trajectory, not just the current top slice.
        # Restricting it to the leaderboard meant a name that dropped out simply
        # stopped having history: AAOI's file ended 2026-06-22 while the engine
        # had scored through 07-28, and the card displayed that five-week-old
        # state as if it were current.
        for row in rows:
            history.setdefault(row["ticker"], []).append({
                "date": date_str, "composite": row["composite"], "rank": row["rank"],
                "stage": row["stage"], "drawdown": row.get("drawdown"),
                "gain": row.get("gain"), "episode_days": row.get("episode_days"),
                "pct_below_10w": row.get("pct_below_10w"),
                "pct_below_20": row.get("pct_below_20"),
                "F1": row.get("F1"), "F2": row.get("F2"), "F2B": row.get("F2B"), "F6": row.get("F6"),
                "F4": row.get("F4"),
                "F4B": row.get("F4B"), "F5": row.get("F5"),
            })
        if (i + 1) % 10 == 0:
            log(f"    {i + 1}/{len(dates)} checkpoints")

    written = write_trajectories(country, history, as_of=dates[-1])
    log(f"  {len(latest)} names scored at the latest checkpoint; "
        f"{written} trajectory files written")
    return latest
