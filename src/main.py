"""
main.py — daily pipeline orchestration.

Order: build universe -> backfill/update price cache -> compute index + breadth
metrics -> backfill/update sector cache -> compute sector ranks -> compute
industry ranks (TradingView) -> append everything to data/*.jsonl -> render
docs/index.html.

Idempotent: safe to re-run on the same day (store.py upserts by date instead
of duplicating rows; cache.py backfill only pulls tickers not already cached).
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
from fmp_client import FMPClient
from metrics import indices, sectors, breadth, industries


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
        log(f"Backfilling price history for {len(missing)} new tickers (one-time cost)...")
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

    # ---------- Indices ----------
    log("Computing index metrics...")
    index_records = indices.compute_all(price_cache, country_cfg["index_tickers"], today)
    for key, record in index_records.items():
        if record:
            store.write_index(key, record)
    log("Index metrics written.")

    # ---------- Breadth ----------
    log("Computing breadth internals...")
    breadth_record = breadth.compute_breadth(price_cache, stock_tickers, today)
    store.write_breadth(breadth_record)
    log(f"Breadth: adv={breadth_record['advancers']} decl={breadth_record['decliners']} "
        f"new_hi={breadth_record['new_highs']} new_lo={breadth_record['new_lows']}")

    # ---------- Sectors ----------
    log("Updating sector performance cache...")
    sector_cache = cache_mod.load(config.SECTOR_CACHE_PATH)
    sectors.backfill_sector_cache(
        sector_cache, client, country_cfg["sectors"], country_cfg["exchanges"],
        on_progress=lambda d, t: log(f"  sector backfill {d}/{t}") if d % 10 == 0 else None,
    )
    sectors.append_today_snapshot(sector_cache, client, country_cfg["exchanges"], today)
    cache_mod.save(sector_cache, config.SECTOR_CACHE_PATH)

    sector_ranks = sectors.compute_sector_ranks(sector_cache, country_cfg["sectors"], country_cfg["exchanges"])
    store.write_sector_ranks(today, sector_ranks.to_dict(orient="records"))
    log(f"Sector ranks written ({len(sector_ranks)} sectors).")

    # ---------- Industries (TradingView) ----------
    log("Fetching TradingView industry performance...")
    industry_ranks = industries.compute_industry_ranks()
    store.write_industry_ranks(today, industry_ranks)
    log(f"Industry ranks written ({len(industry_ranks)} industries).")

    # ---------- Render ----------
    log("Rendering dashboard...")
    out_path = render.render_dashboard()
    log(f"Done. Dashboard written to {out_path}")


if __name__ == "__main__":
    main()
