"""
recover_session.py — refetch a session the nightly run missed, and rebuild the
metrics that skipped it.

WHY THIS IS NEEDED
---------------------------------------------------------------------------
On 2026-08-03 the US price pull returned 41 of 3,388 tickers. FMP has the data
now — it simply was not available when that run happened — but the pipeline
only ever appends, so it will never revisit the day. Breadth, environment and
the sector/industry ranks skipped it permanently while the index files, which
are fetched separately, kept it. That mismatch is what showed up as a date
missing from replay.

WHAT IT DOES
---------------------------------------------------------------------------
  1. Refetches the named dates for every ticker already in the price cache and
     merges the closes in. Uses the SAME source as the nightly run (FMP for
     the US), because mixing sources for one day inside a series is how you
     get a phantom gap or spike on exactly that day.
  2. Recomputes breadth, sector ranks, industry ranks and the environment read
     across the trailing window, upserting rather than appending, so the
     recovered day slots in and every other day is rewritten identically.

Index files and per-ticker OHLC are left alone: they already have the day.

Usage:
    python src/recover_session.py US 2026-08-03
    python src/recover_session.py US 2026-08-03 2026-08-06
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import cache as cache_mod
import store
import main as pipeline
from fmp_client import FMPClient
from metrics import groups as groups_mod, sectors, industries, breadth, environment


def log(msg):
    print(msg, flush=True)


def coverage(price_cache, date):
    have = sum(1 for e in price_cache.values()
               if isinstance(e, dict) and date in e)
    total = sum(1 for e in price_cache.values() if isinstance(e, dict) and e)
    return have, total


def refetch(country, dates):
    """Pull the missing dates for every cached ticker and merge them in."""
    path = config.price_cache_path(country)
    price_cache = cache_mod.load(path)
    tickers = sorted(t for t, e in price_cache.items()
                     if isinstance(e, dict) and e)

    for date in dates:
        have, total = coverage(price_cache, date)
        log(f"  before  {date}: {have}/{total} tickers ({have/total:.1%})")

    key = os.environ.get("FMP_API_KEY") or pipeline.api_key_from_env_file()
    if not key:
        raise SystemExit("FMP_API_KEY is not set")
    client = FMPClient(key)

    start, end = min(dates), max(dates)
    wanted = set(dates)
    added, failed = 0, 0

    for i, ticker in enumerate(tickers, 1):
        try:
            rows = client.historical_eod(ticker, start=start, end=end) or []
        except Exception:
            failed += 1
            continue
        for row in rows:
            if row.get("date") in wanted and row.get("close") is not None:
                cache_mod.set_value(price_cache, ticker, row["date"],
                                    row["close"])
                added += 1
        if i % 250 == 0:
            log(f"    {i}/{len(tickers)} tickers · {added} closes recovered")

    cache_mod.save(price_cache, path)
    log(f"  fetched {len(tickers)} tickers, {added} closes added, "
        f"{failed} failed")

    for date in dates:
        have, total = coverage(price_cache, date)
        log(f"  after   {date}: {have}/{total} tickers ({have/total:.1%})")
    return price_cache


def rebuild(country, price_cache):
    """Recompute everything the missing session was excluded from.

    Classification and market caps come from the SAME live TradingView pull the
    nightly run uses. Falling back to equal weighting would have been the easy
    option and a bad one: it would silently rewrite every day in the window
    under a different weighting scheme, so the recovered session would be the
    only honest thing in a file of quietly changed numbers.
    """
    import tv_industry

    cfg = config.COUNTRIES[country]
    data_dir = config.data_dir(country)
    n_days = config.CHART_BACKFILL_DAYS

    dates = groups_mod.trading_calendar(
        price_cache, cfg["index_tickers"][cfg["calendar_index"]], n_days)
    log(f"  calendar now covers {len(dates)} sessions "
        f"({dates[0]} .. {dates[-1]})")

    log("  fetching classification and market caps...")
    industry_df = tv_industry.fetch_industry_classification(cfg)
    ticker_to_sector, sector_caps = sectors.extract_classification(industry_df)
    ticker_to_industry, industry_caps = industries.extract_classification(industry_df)
    log(f"    {len(ticker_to_sector)} classified names")

    # The breadth universe is the priced names that are also classified —
    # the same set main.py calls stock_tickers.
    universe = sorted(t for t, e in price_cache.items()
                      if isinstance(e, dict) and e and t in ticker_to_sector)
    log(f"  breadth over {len(universe)} names...")
    records = breadth.backfill_breadth_history(
        price_cache, universe, n_days, ticker_to_sector)
    for record in records:
        store.write_breadth(country, record)
    log(f"    {len(records)} days")

    log("  sector ranks...")
    for d, rows in sectors.backfill_sector_history(
            price_cache, ticker_to_sector, sector_caps, dates).items():
        if rows:
            store.write_sector_ranks(country, d, rows)

    log("  industry ranks...")
    for d, rows in industries.backfill_industry_history(
            price_cache, ticker_to_industry, industry_caps, dates).items():
        if rows:
            store.write_industry_ranks(country, d, rows)

    log("  environment...")
    env = environment.backfill_environment(
        {k: store.read_jsonl(os.path.join(data_dir, f"index_{k}.jsonl"))
         for k in cfg["index_tickers"]},
        store.read_jsonl(os.path.join(data_dir, "sector_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "industry_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_adv_decl.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_new_hilo.jsonl")),
        dates, cfg["largecap_keys"])
    for record in env:
        store.write_environment(country, record)
    log(f"    {len(env)} days")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    country = sys.argv[1].upper()
    dates = sorted(sys.argv[2:])

    log(f"recovering {country} {', '.join(dates)}\n")
    price_cache = refetch(country, dates)
    log("\nrebuilding metrics...")
    rebuild(country, price_cache)
    log("\ndone — re-render to publish the recovered days.")


if __name__ == "__main__":
    main()
