"""
setup_context.py — the objective market context for a stock on a given date.

The Setups Database logs discretionary chart examples. Everything measurable
about the tape on the buy date — where the name's sector and industry ranked,
what the market-environment trend score was, how far the stock sat below its
own 52-week high — is not something to type from memory. It is already in the
pipeline's own accumulated history, so it is read from there and stamped onto
the entry, and re-stamped on demand, so a logged example always carries the
same numbers the terminal itself would show for that date.

Nothing here writes. It only reads data/<country>/*.jsonl, the per-country
classification map, and the published per-ticker price files.
"""

import functools
import json
import os

import config
import store

# ~5 trading sessions is "a week ago" for the rank-move read; ~252 is a
# 52-week window for the high. Both are row counts, since every history file
# here is exactly one row per trading day.
WEEK_SESSIONS = 5
YEAR_SESSIONS = 252


@functools.lru_cache(maxsize=4)
def _classification(country):
    path = os.path.join(config.data_dir(country), "classification.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def classify(country, ticker):
    """(sector, industry, logoid, market_cap) for a ticker, any missing as None."""
    row = _classification(country).get((ticker or "").upper())
    if not row:
        return (None, None, None, None)
    row = list(row) + [None, None, None, None]
    return (row[0], row[1], row[2], row[3])


def _asof_index(rows, on_date):
    """Index of the last row dated on or before `on_date` (rows sorted by date).
    `on_date` None means the most recent row."""
    if not rows:
        return None
    if on_date is None:
        return len(rows) - 1
    hit = None
    for i, r in enumerate(rows):
        if r.get("date", "") <= on_date:
            hit = i
        else:
            break
    return hit


@functools.lru_cache(maxsize=8)
def _series(country, filename):
    rows = store.read_jsonl(os.path.join(config.data_dir(country), filename))
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def market_env(country, on_date=None):
    """Trend-factor score and regime label for a date (or today if None).

    The score is `factors_favourable / factors_total` from environment.jsonl —
    3 US indices x 3 EMAs = 9 factors, 4 Indian indices x 3 = 12 — which is the
    "8 out of 9" reading the market-environment panel shows.
    """
    rows = _series(country, "environment.jsonl")
    i = _asof_index(rows, on_date)
    if i is None:
        return None
    row = rows[i]
    trend = row.get("trend") or {}
    fav = trend.get("factors_favourable")
    total = trend.get("factors_total")
    return {
        "date": row.get("date"),
        "favourable": fav,
        "total": total,
        "score": None if fav is None or total is None else f"{fav} / {total}",
        "label": row.get("overall") or trend.get("label"),
    }


def _group_rank(country, filename, list_key, name_key, name, on_date):
    """Rank of one sector/industry on a date, plus how many places it has moved
    over the trailing week. Delta is positive when the rank number fell — i.e.
    the group climbed toward #1."""
    if not name:
        return {"rank": None, "delta_1w": None}
    rows = _series(country, filename)
    i = _asof_index(rows, on_date)
    if i is None:
        return {"rank": None, "delta_1w": None}

    def rank_in(row):
        for entry in row.get(list_key, []):
            if entry.get(name_key) == name:
                return entry.get("rank")
        return None

    now = rank_in(rows[i])
    prior = rank_in(rows[i - WEEK_SESSIONS]) if i - WEEK_SESSIONS >= 0 else None
    delta = None if now is None or prior is None else prior - now
    return {"rank": now, "delta_1w": delta}


def sector_rank(country, name, on_date=None):
    return _group_rank(country, "sector_ranks.jsonl", "sectors", "sector", name, on_date)


def industry_rank(country, name, on_date=None):
    return _group_rank(country, "industry_ranks.jsonl", "industries", "industry", name, on_date)


def tmle_score(country, ticker, on_date=None):
    """TMLE composite for a ticker on a date, if it was a ranked leader then.
    US only — the India pipeline does not run TMLE (see config.run_tmle)."""
    if not config.COUNTRIES.get(country, {}).get("run_tmle"):
        return None
    rows = _series(country, "tmle_leaders.jsonl")
    i = _asof_index(rows, on_date)
    if i is None:
        return None
    for leader in rows[i].get("leaders", []):
        if (leader.get("ticker") or "").upper() == (ticker or "").upper():
            return leader.get("composite")
    return None


def _ticker_series(country, ticker):
    path = os.path.join(config.ticker_dir(country), f"{(ticker or '').upper()}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def ticker_stats(country, ticker, on_date=None):
    """Sessions of price history before the buy date, and how far below the
    trailing 52-week high the close sat on that date."""
    data = _ticker_series(country, ticker)
    if not data or not data.get("dates"):
        return {"days_since_ipo": None, "pct_from_52w_high": None}

    dates = data["dates"]
    highs = data.get("high") or []
    closes = data.get("close") or []

    idx = len(dates) - 1
    if on_date is not None:
        idx = None
        for j, d in enumerate(dates):
            if d <= on_date:
                idx = j
            else:
                break
        if idx is None:
            return {"days_since_ipo": 0, "pct_from_52w_high": None}

    window_hi = None
    lo = max(0, idx - YEAR_SESSIONS + 1)
    for h in highs[lo:idx + 1]:
        if h is not None and (window_hi is None or h > window_hi):
            window_hi = h
    close = closes[idx] if idx < len(closes) else None
    pct = None
    if window_hi and close is not None:
        pct = round((window_hi - close) / window_hi * 100, 1)

    return {"days_since_ipo": idx + 1, "pct_from_52w_high": pct}


def context_at(country, ticker, buy_date):
    """Every auto-filled field for one logged entry, as of its buy date."""
    sector, industry, logoid, mktcap = classify(country, ticker)
    env = market_env(country, buy_date) or {}
    sr = sector_rank(country, sector, buy_date)
    ir = industry_rank(country, industry, buy_date)
    ts = ticker_stats(country, ticker, buy_date)
    return {
        "sector": sector,
        "industry": industry,
        "logoid": logoid,
        "marketCap": mktcap,
        "tmleScore": tmle_score(country, ticker, buy_date),
        "marketEnvScore": env.get("score"),
        "marketEnvFavourable": env.get("favourable"),
        "marketEnvTotal": env.get("total"),
        "marketEnvLabel": env.get("label"),
        "sectorRank": sr["rank"],
        "industryRank": ir["rank"],
        "sectorRankDelta1w": sr["delta_1w"],
        "industryRankDelta1w": ir["delta_1w"],
        "daysSinceIpo": ts["days_since_ipo"],
        "pctFrom52wHigh": ts["pct_from_52w_high"],
    }
