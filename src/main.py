"""
main.py — daily pipeline orchestration.

Every run recomputes the FULL trailing CHART_BACKFILL_DAYS window for every
panel (not just today) and upserts all of it via store.py, which is idempotent
per date — re-writing an unchanged historical day is harmless. This means the
pipeline is self-healing (a missed day, a widened backfill window, or a
one-off local re-run all just work) without needing separate "first run only"
backfill logic.

Order: build universe -> backfill/update price cache -> pull TradingView
classification (industry + sector tags) -> backfill index/breadth/sector/
industry history from the cache -> upsert into data/*.jsonl -> render
docs/index.html.
"""

import os
import sys
import time
from datetime import date, datetime, timezone

import config
import cache as cache_mod
import universe
import store
import render
import tv_industry
from fmp_client import FMPClient
from metrics import indices, sectors, industries, breadth, groups as groups_mod


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# The US cash session closes at 20:00 UTC in EDT and 21:00 UTC in EST. Anything
# earlier than this cutoff means today's daily bar is still forming, and both
# FMP's "historical" endpoint and its live quote will hand back an in-progress
# value that looks exactly like a finished session. Writing that into the
# history would show a partial day as a real close (and skew its EMAs, its
# breadth counts and the day-change figure), so the pipeline drops today's row
# entirely until the session is genuinely over.
SESSION_FINAL_AFTER_UTC = (21, 30)


def todays_bar_is_incomplete(now=None):
    now = now or datetime.now(timezone.utc)
    return (now.hour, now.minute) < SESSION_FINAL_AFTER_UTC


def main():
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        log("FMP_API_KEY is not set.")
        sys.exit(1)

    client = FMPClient(api_key)
    country_cfg = config.COUNTRIES[config.DEFAULT_COUNTRY]
    today = datetime.now(timezone.utc).date().isoformat()

    skip_today = todays_bar_is_incomplete()
    if skip_today:
        log(f"US session for {today} has not closed yet — today's partial bar will be "
            f"excluded; the dashboard will report through the last completed session.")

    def drop_partial(rows):
        return [r for r in rows if r.get("date") != today] if skip_today else rows

    log("Building S&P 1500 universe...")
    uni = universe.build_sp1500()
    stock_tickers = uni["ticker"].tolist()
    index_tickers = list(country_cfg["index_tickers"].values())
    all_price_tickers = stock_tickers + index_tickers
    log(f"Universe: {len(stock_tickers)} stocks + {len(index_tickers)} indices")

    # ---------- Price cache: backfill missing, then refresh today for all ----------
    price_cache = cache_mod.load(config.PRICE_CACHE_PATH)
    missing = [t for t in all_price_tickers if t not in price_cache]
    if missing:
        log(f"Backfilling price history for {len(missing)} new tickers "
            f"(up to {config.PRICE_WINDOW_DAYS} trading days each, one-time cost)...")
        for i, ticker in enumerate(missing):
            hist = drop_partial(client.historical_eod(ticker))[:config.PRICE_WINDOW_DAYS]
            for row in hist:
                cache_mod.set_value(price_cache, ticker, row["date"], row["close"])
            if (i + 1) % 100 == 0:
                log(f"  backfilled {i + 1}/{len(missing)}")
                cache_mod.save(price_cache, config.PRICE_CACHE_PATH)  # checkpoint

    if skip_today:
        log("Skipping the quote snapshot — a live quote mid-session is not a close.")
    else:
        log(f"Fetching today's quotes for {len(all_price_tickers)} tickers...")
        quotes = client.quote_many(all_price_tickers, on_progress=lambda d, t: log(f"  quoted {d}/{t}"))
        for q in quotes:
            if q.get("symbol") and q.get("price") is not None:
                cache_mod.set_value(price_cache, q["symbol"], today, q["price"])

    cache_mod.trim(price_cache, config.PRICE_WINDOW_DAYS)
    cache_mod.save(price_cache, config.PRICE_CACHE_PATH)
    log("Price cache updated.")

    n_days = config.CHART_BACKFILL_DAYS

    # ---------- Indices ----------
    # Fetched separately from the shared price cache: the cache holds closes
    # only (all the breadth maths needs), but the index panels draw HLC bars,
    # which needs high/low too. Three extra API calls.
    log("Backfilling index history (OHLC + EMA10/20/50)...")
    for key, ticker in country_cfg["index_tickers"].items():
        eod_rows = drop_partial(client.historical_eod(ticker))
        records = indices.backfill_index_history(eod_rows, n_days)
        for record in records:
            store.write_index(key, record)
        log(f"  {key}: {len(records)} days written")

    # ---------- Breadth ----------
    log("Backfilling breadth internals...")
    breadth_records = breadth.backfill_breadth_history(price_cache, stock_tickers, n_days)
    for record in breadth_records:
        store.write_breadth(record)
    log(f"Breadth: {len(breadth_records)} days written "
        f"(today: adv={breadth_records[-1]['advancers']} decl={breadth_records[-1]['decliners']} "
        f"new_hi={breadth_records[-1]['new_highs']} new_lo={breadth_records[-1]['new_lows']})")

    # ---------- Sector / industry classification (one TradingView pull, shared) ----------
    log("Fetching TradingView classification (sector + industry tags)...")
    industry_df = tv_industry.fetch_industry_classification()
    ticker_to_sector, sector_caps = sectors.extract_classification(industry_df)
    ticker_to_industry, industry_caps = industries.extract_classification(industry_df)
    log(f"Classified {len(industry_df)} stocks: "
        f"{industry_df['sector'].nunique()} sectors, {industry_df['industry'].nunique()} industries")

    dates = groups_mod.trading_calendar(price_cache, country_cfg["index_tickers"]["sp500"], n_days)

    log("Backfilling sector performance/rank...")
    sector_history = sectors.backfill_sector_history(price_cache, ticker_to_sector, sector_caps, dates)
    for d, records in sector_history.items():
        if records:
            store.write_sector_ranks(d, records)
    log(f"Sector ranks: {sum(1 for r in sector_history.values() if r)} days written")

    log("Backfilling industry performance/rank...")
    industry_history = industries.backfill_industry_history(price_cache, ticker_to_industry, industry_caps, dates)
    for d, records in industry_history.items():
        if records:
            store.write_industry_ranks(d, records)
    log(f"Industry ranks: {sum(1 for r in industry_history.values() if r)} days written")

    # ---------- Render ----------
    log("Rendering combined dashboard...")
    out_path = render.render_dashboard()
    log("Rendering individual panel pages...")
    panel_paths = render.render_all_panels()
    log(f"Done. Dashboard written to {out_path}, plus {len(panel_paths)} individual panel pages")


if __name__ == "__main__":
    main()
