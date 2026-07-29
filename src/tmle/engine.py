"""
TMLE — engine.py

Wires the factors together, applies the composite weights, and produces scores
for the whole universe at a set of dates.

Two numbers come out per name, deliberately kept apart:

  composite   how strong the advance is — the weighted factor score
  stage       whether that advance is still intact (Weinstein 1-4)

They are not combined. Folding "this is broken" into the score would destroy
both facts at once; kept separate, a Stage 4 ex-leader keeps the strength it
genuinely earned and simply never appears in a buy list. `actionable` is the
flag the leaderboard filters on.

The composite renormalises over whichever factors returned a value, so a name
missing one is not silently dragged down — and so adding F2/F2B later raises
coverage without rescaling anything already computed.
"""

import numpy as np

from tmle import config, factors, structure


def composite(scores):
    """Weighted composite over available factors, renormalised.
    Returns (composite, coverage)."""
    total, used = 0.0, 0.0
    for key, value in scores.items():
        if value is None:
            continue
        weight = config.WEIGHTS.get(key, 0.0)
        total += value * weight
        used += weight
    if used <= 0:
        return None, 0.0
    return round(total / used, 1), round(used, 2)


def _index_asof(dates, date_str):
    """Position of the last session at or before date_str."""
    lo, hi = 0, len(dates) - 1
    if not dates or dates[0] > date_str:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if dates[mid] <= date_str:
            lo = mid
        else:
            hi = mid - 1
    return lo


class Engine:
    """Scores a universe at one or more dates.

    `price_rows`   {ticker: [ohlcv rows]}
    `bench_rows`   [ohlcv rows] for the F1 benchmark
    `rank_lookup`  callable(ticker, date) -> industry rank on that date, or None
    """

    def __init__(self, price_rows, bench_rows, rank_lookup, market_caps=None):
        self.rank_lookup = rank_lookup
        self.market_caps = market_caps or {}

        bench = factors.prepare(bench_rows)
        if bench is None:
            raise ValueError("benchmark has no usable history")
        self.bench_dates = bench["dates"]
        self.bench_close = bench["close"]

        # Moving averages, stage and the volume helpers, computed once per name.
        self.arrays = {}
        for ticker, rows in price_rows.items():
            try:
                prepared = factors.prepare(rows)
            except Exception:
                prepared = None
            if prepared is not None:
                self.arrays[ticker] = prepared

    def _bench_aligned(self, dates):
        """Benchmark closes on the stock's own session dates, carried forward."""
        out = np.full(len(dates), np.nan)
        j, last = 0, np.nan
        for i, d in enumerate(dates):
            while j < len(self.bench_dates) and self.bench_dates[j] <= d:
                last = self.bench_close[j]
                j += 1
            out[i] = last
        return out

    def score_one(self, ticker, date_str, momentum_date):
        arrays = self.arrays.get(ticker)
        if arrays is None:
            return None
        i = _index_asof(arrays["dates"], date_str)
        if i is None:
            return None

        if "bench_aligned" not in arrays:
            arrays["bench_aligned"] = self._bench_aligned(arrays["dates"])

        episode = structure.episode_stats(arrays, i, arrays["bench_aligned"])
        if episode is None:
            # Not in an advance at all — below the 30-week average. There is no
            # move to score, which is itself the answer.
            return None

        scores = {
            "F1": factors.f1_score(episode),
            "F4": factors.f4_score(arrays, i, episode),
            "F4B": factors.f4b_score(arrays, i, episode),
            "F5": factors.f5_score(self.rank_lookup(ticker, date_str),
                                   self.rank_lookup(ticker, momentum_date)),
        }
        comp, coverage = composite(scores)
        if comp is None or coverage < config.MIN_COVERAGE:
            return None

        stage = episode["stage"]
        drawdown = episode["drawdown"]
        actionable = (stage in config.ACTIONABLE_STAGES and
                      drawdown is not None and
                      drawdown >= config.MAX_ACTIONABLE_DRAWDOWN)

        row = {
            "ticker": ticker,
            "composite": comp,
            "coverage": coverage,
            "stage": stage,
            "stage_label": structure.STAGE_LABELS.get(stage, "—"),
            "actionable": bool(actionable),
            "drawdown": round(drawdown, 1) if drawdown is not None else None,
            "gain": round(episode["stock_gain"], 1) if episode.get("stock_gain") is not None else None,
            "episode_days": episode["length"],
            "episode_start": arrays["dates"][episode["start_index"]],
            "pct_below_20": episode["pct_below_20"],
            "pct_below_10w": episode["pct_below_10w"],
        }
        for key, value in scores.items():
            row[key] = round(value, 1) if value is not None else None
        return row

    def score_universe(self, date_str, momentum_date, tickers=None):
        """All scores for one date, ranked best-first.

        Rank is assigned over ACTIONABLE names only. A Stage 3 or 4 name still
        carries its score and appears in the table, but it does not take a
        leadership rank away from a stock that is actually advancing.
        """
        tickers = tickers if tickers is not None else list(self.arrays)
        rows = []
        for ticker in tickers:
            if self.market_caps and self.market_caps.get(ticker, 0) < config.MIN_MARKET_CAP:
                continue
            row = self.score_one(ticker, date_str, momentum_date)
            if row:
                rows.append(row)

        rows.sort(key=lambda r: (r["actionable"], r["composite"]), reverse=True)
        rank = 0
        for row in rows:
            if row["actionable"]:
                rank += 1
                row["rank"] = rank
            else:
                row["rank"] = None
        return rows
