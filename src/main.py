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
from datetime import date

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


def main():
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        log("FMP_API_KEY is not set.")
        sys.exit(1)

    client = FMPClient(api_key)
    country_cfg = config.COUNTRIES[config.DEFAULT_COUNTRY]
    today = date.today().isoformat()

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
            hist = client.historical_eod(ticker)[:config.PRICE_WINDOW_DAYS]
            for row in hist:
                cache_mod.set_value(price_cache, ticker, row["date"], row["close"])
            if (i + 1) % 100 == 0:
                log(f"  backfilled {i + 1}/{len(missing)}")
                cache_mod.save(price_cache, config.PRICE_CACHE_PATH)  # checkpoint

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
    log("Backfilling index history (EMA10/20/50)...")
    for key, ticker in country_cfg["index_tickers"].items():
        records = indices.backfill_index_history(price_cache, ticker, n_days)
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
    log("Rendering dashboard...")
    out_path = render.render_dashboard()
    log(f"Done. Dashboard written to {out_path}")


if __name__ == "__main__":
    main()
