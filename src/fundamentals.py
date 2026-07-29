"""
fundamentals.py — quarterly income statements for TMLE's F2B factor.

F2B asks two things the TTM figures cannot answer: how many of the last four
quarters grew triple digits, and whether growth is ACCELERATING quarter over
quarter. Both need a run of quarters, and TradingView's scanner exposes only TTM
and the single latest quarter — so this is the one input that genuinely requires
FMP.

WHY THIS IS CACHED, AND WHY WEEKLY
---------------------------------------------------------------------------
Income statements change four times a year. Re-fetching 3,300 of them nightly
would double the run's API load to learn nothing — and the nightly run already
brushes FMP's rate ceiling, which is what slowed a recent run tenfold. So each
ticker is refreshed at most once a week, and the staleness check is per ticker
rather than global. That spreads the work: on a typical night roughly a seventh
of the universe is due, a few hundred calls instead of a few thousand.

Eight quarters are fetched to produce four year-on-year comparisons — Q1 versus
the Q1 before it, and so on. Comparing consecutive quarters instead would read
ordinary seasonality as growth.
"""

import json
import os
import time
from datetime import date, datetime, timedelta

import config

# A ticker's statements are refetched only once this stale. Quarterly data does
# not move faster than this, and staggering keeps the nightly call count low.
MAX_AGE_DAYS = 7

# Forward estimates from fewer than this many analysts are noise, not consensus.
MIN_ANALYSTS = 3

# Eight quarters in, four year-on-year growth figures out.
QUARTERS_FETCHED = 8
YOY_QUARTERS = 4


def cache_path(country):
    return os.path.join(config.cache_dir(country), "fundamentals.json")


def load(country):
    path = cache_path(country)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        # A truncated cache is not worth failing a run over; it will refill.
        return {}


def save(country, store):
    path = cache_path(country)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(store, f, separators=(",", ":"))


def _is_stale(entry, today=None):
    if not entry or not entry.get("fetched"):
        return True
    today = today or date.today()
    try:
        fetched = datetime.strptime(entry["fetched"], "%Y-%m-%d").date()
    except Exception:
        return True
    return (today - fetched) >= timedelta(days=MAX_AGE_DAYS)


def growths_from_quarters(rows):
    """Year-on-year growth for each of the last four quarters, oldest first.

    Each quarter is compared with the same quarter a year earlier (four rows
    back), so seasonality is not mistaken for growth. The stronger of revenue
    and EPS growth is used per quarter, as in the notebook — a company can lead
    on either.
    """
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=lambda r: r["date"], reverse=True)
    if len(rows) < YOY_QUARTERS + 4:
        return []

    def eps_of(row):
        return row.get("epsDiluted") or row.get("epsdiluted") or row.get("eps")

    growths = []
    for i in range(YOY_QUARTERS):
        now, prior = rows[i], rows[i + 4]
        candidates = []
        for current, before in ((now.get("revenue"), prior.get("revenue")),
                                (eps_of(now), eps_of(prior))):
            # A negative or zero base makes a growth rate meaningless — going
            # from -$1m to +$1m is not "200% growth".
            if current is None or before is None or before <= 0:
                continue
            candidates.append((current / before - 1) * 100)
        if candidates:
            growths.append(max(candidates))
    growths.reverse()   # oldest first, so acceleration reads left to right
    return growths


def forward_from_estimates(rows, today=None):
    """Next fiscal year's consensus growth over the current year's.

    Returns {revenue_growth, eps_growth, analysts, year} or None.

    Only the NEXT year is used. The far years carry one or two analysts and swing
    wildly — SNDK's 2030 line is a single estimate showing -78% revenue, which
    says nothing about the company and everything about the sample size.
    """
    today = today or date.today().isoformat()
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=lambda r: r["date"])
    if len(rows) < 2:
        return None

    nxt = None
    for i, r in enumerate(rows):
        if r["date"] > today and i > 0:
            nxt = i
            break
    if nxt is None:
        return None

    current, forward = rows[nxt - 1], rows[nxt]
    analysts = forward.get("numAnalystsRevenue") or forward.get("numAnalystsEps") or 0
    if analysts < MIN_ANALYSTS:
        return None

    def growth(key):
        a, b = current.get(key), forward.get(key)
        if a is None or b is None or a <= 0:
            return None
        return (b / a - 1) * 100

    return {
        "revenue_growth": growth("revenueAvg"),
        "eps_growth": growth("epsAvg"),
        "analysts": int(analysts),
        "year": forward["date"][:4],
    }


def refresh(country, client, tickers, log=print):
    """Fetch what is stale, return {ticker: [growths]} for everything known."""
    store = load(country)
    due = [t for t in tickers if _is_stale(store.get(t))]
    if due:
        log(f"  {len(due)} of {len(tickers)} tickers have stale fundamentals "
            f"(refreshed at most every {MAX_AGE_DAYS} days)")
        today = date.today().isoformat()
        failed = 0
        for i, ticker in enumerate(due):
            entry = {"fetched": today, "growths": [], "forward": None}
            try:
                rows = client.income_statement_quarterly(ticker, limit=QUARTERS_FETCHED)
                entry["growths"] = growths_from_quarters(rows or [])
            except Exception:
                # Record the attempt so a permanently unavailable name is not
                # retried every single night.
                failed += 1
            try:
                est = client.analyst_estimates(ticker, limit=6)
                entry["forward"] = forward_from_estimates(est or [])
            except Exception:
                pass
            store[ticker] = entry
            if (i + 1) % 250 == 0:
                log(f"    {i + 1}/{len(due)}")
                save(country, store)
        if failed:
            log(f"  {failed} had no fetchable statements")
        save(country, store)
    else:
        log("  all fundamentals are fresh; no calls needed")

    return (
        {t: entry.get("growths", []) for t, entry in store.items()},
        {t: entry.get("forward") for t, entry in store.items()},
    )


if __name__ == "__main__":
    import sys
    key = None
    for line in open(os.path.join(config.ROOT_DIR, ".env")):
        if line.startswith("FMP_API_KEY"):
            key = line.split("=", 1)[1].strip()
    from fmp_client import FMPClient
    client = FMPClient(key)
    for t in ["NVDA", "MU", "SEZL", "SYRE"]:
        rows = client.income_statement_quarterly(t, limit=8)
        g = growths_from_quarters(rows or [])
        print(f"{t:6} growths oldest->newest: {[round(x) for x in g]}")
