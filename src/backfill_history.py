"""
backfill_history.py — recompute breadth, index, sector, industry and
environment history back to 2020, then write one replay file per session.

WHY A SEPARATE SCRIPT
---------------------------------------------------------------------------
The nightly pipeline keeps a rolling ~524-day price cache and recomputes the
trailing year of every series from it. That is right for a daily refresh and
wrong for a one-off deepening: six years of prices is ~97MB, which is over
GitHub's 100MB per-file limit, and the history files it produces (30MB of
industry ranks alone) would be rewritten and committed every single night.

So this runs OUT OF BAND. It fetches deep prices into a scratch cache, computes
the full history, writes it, and never touches the committed rolling cache. The
nightly job carries on exactly as before.

WHY PER-DATE REPLAY FILES
---------------------------------------------------------------------------
The replay panel embeds its series inline in the page. That is fine for a year
and impossible for six — the industry ranks alone would be a 30MB payload
parsed before first paint. Written per session instead, the page fetches only
the date being looked at, the same way stock lookup fetches one ticker.

Usage:
    python src/backfill_history.py US
    python src/backfill_history.py IN
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import cache as cache_mod
import store
import tv_industry
from metrics import indices, sectors, industries, breadth, environment, hilo
from metrics import groups as groups_mod

START_DATE = "2020-01-01"
CHUNK_SIZE = 100

# Matches each market's nightly source, so the deep history and the daily
# updates are adjusted the same way. See backfill_tickers.py for the 2.7%
# RELIANCE discrepancy this avoids.
AUTO_ADJUST = {"US": False, "IN": True}

# Yahoo's symbols for the index levels each market charts.
INDEX_SYMBOLS = {
    "US": {"nasdaq": "^IXIC", "sp500": "^GSPC", "russell2000": "^RUT"},
    "IN": {"sensex": "^BSESN", "nifty500": "^CRSLDX",
           "niftymidcap150": "NIFTYMIDCAP150.NS",
           "niftysmallcap250": "NIFTYSMLCAP250.NS"},
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def deep_cache_path(country):
    """Scratch cache, deliberately outside the committed data directory."""
    return os.path.join(config.cache_dir(country), "price_window_deep.json")


def index_ohlc(country):
    """{key: [OHLC rows]} for the market's indices.

    The index panel draws HLC bars, so it needs full OHLC — the deep price
    cache carries closes only, which is all the breadth and rank maths needs.
    Cached to disk so a rerun does not refetch.
    """
    import yfinance as yf

    path = os.path.join(config.cache_dir(country), "index_ohlc_deep.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    out = {}
    for key, symbol in INDEX_SYMBOLS[country].items():
        frame = yf.download(symbol, start=START_DATE, progress=False,
                            auto_adjust=True, group_by="ticker", threads=False)
        sub = frame[symbol] if symbol in frame else frame
        sub = sub.dropna(how="all")
        rows = []
        for stamp, row in sub.iterrows():
            close = row.get("Close")
            if close != close or not close:
                continue
            rows.append({
                "date": stamp.strftime("%Y-%m-%d"),
                "open": float(row.get("Open") or close),
                "high": float(row.get("High") or close),
                "low": float(row.get("Low") or close),
                "close": float(close),
                "volume": int(row.get("Volume") or 0),
            })
        out[key] = rows
        log(f"  index {key}: {len(rows)} OHLC bars")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    return out


def yahoo_symbol(country, ticker):
    if country == "IN":
        import yf_client
        return yf_client.to_yahoo(ticker)
    return ticker.replace(".", "-")


def fetch_prices(country, tickers):
    """Deep closes for every ticker plus the market's index levels."""
    import yfinance as yf

    cache = {}
    symbol_of = {t: yahoo_symbol(country, t) for t in tickers}
    started = time.time()

    for i in range(0, len(tickers), CHUNK_SIZE):
        batch = tickers[i:i + CHUNK_SIZE]
        try:
            frame = yf.download(" ".join(symbol_of[t] for t in batch),
                                start=START_DATE, progress=False,
                                auto_adjust=AUTO_ADJUST.get(country, False),
                                group_by="ticker", threads=True)
        except Exception as exc:
            log(f"  chunk {i // CHUNK_SIZE + 1} failed ({exc}); continuing")
            continue
        for ticker in batch:
            try:
                sub = frame[symbol_of[ticker]].dropna(how="all")
            except Exception:
                continue
            for stamp, row in sub.iterrows():
                close = row.get("Close")
                if close == close and close:
                    cache_mod.set_value(cache, ticker, stamp.strftime("%Y-%m-%d"),
                                        float(close))
        done = min(i + CHUNK_SIZE, len(tickers))
        if done % 500 == 0 or done == len(tickers):
            log(f"  prices {done}/{len(tickers)} ({time.time() - started:.0f}s)")

    for key, symbol in INDEX_SYMBOLS[country].items():
        try:
            frame = yf.download(symbol, start=START_DATE, progress=False,
                                auto_adjust=True, group_by="ticker", threads=False)
            sub = frame[symbol].dropna(how="all") if symbol in frame else frame.dropna(how="all")
            for stamp, row in sub.iterrows():
                close = row.get("Close")
                if close == close and close:
                    cache_mod.set_value(cache, config.COUNTRIES[country]["index_tickers"][key],
                                        stamp.strftime("%Y-%m-%d"), float(close))
            log(f"  index {key}: {len(sub)} bars")
        except Exception as exc:
            log(f"  index {key} FAILED: {exc}")

    return cache


def run(country):
    started = time.time()
    cfg = config.COUNTRIES[country]

    log(f"{country}: classification...")
    industry_df = tv_industry.fetch_industry_classification(cfg)
    ticker_to_sector, sector_caps = sectors.extract_classification(industry_df)
    ticker_to_industry, industry_caps = industries.extract_classification(industry_df)

    if country == "US":
        import universe
        breadth_tickers = universe.build_sp1500()["ticker"].tolist()
    else:
        breadth_tickers = industry_df["name"].tolist()
    all_tickers = sorted(set(breadth_tickers) | set(industry_df["name"]))
    log(f"{country}: {len(all_tickers)} names, {len(breadth_tickers)} in the breadth universe")

    path = deep_cache_path(country)
    if os.path.exists(path):
        log(f"{country}: reusing {path}")
        price_cache = cache_mod.load(path)
    else:
        log(f"{country}: fetching deep prices from {START_DATE}...")
        price_cache = fetch_prices(country, all_tickers)
        cache_mod.save(price_cache, path)
    log(f"  cache holds {len(price_cache)} series")

    sessions = groups_mod.trading_calendar(
        price_cache, cfg["index_tickers"][cfg["calendar_index"]], 99999)
    log(f"{country}: {len(sessions)} sessions ({sessions[0]} -> {sessions[-1]})")
    n_days = len(sessions)

    log(f"{country}: index EMAs...")
    ohlc = index_ohlc(country)
    for key in cfg["index_tickers"]:
        records = indices.backfill_index_history(ohlc.get(key, []), n_days)
        store.upsert_many(store.series_path(country, f"index_{key}.jsonl"), records)
        log(f"  {key}: {len(records)} days")

    log(f"{country}: breadth internals...")
    records = breadth.backfill_breadth_history(price_cache, breadth_tickers,
                                               n_days, ticker_to_sector)
    store.write_breadth_bulk(country, records)
    log(f"  {len(records)} days")

    log(f"{country}: high/low counts and names...")
    counts, names = hilo.compute(price_cache, all_tickers, n_days, ticker_to_sector)
    store.upsert_many(store.series_path(country, "hilo_counts.jsonl"), counts)
    for filename, payload in (("hilo_names.json", names),
                              ("hilo_quotes.json", hilo.last_quotes(price_cache, all_tickers))):
        with open(store.series_path(country, filename), "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
    log(f"  {len(counts)} days")

    log(f"{country}: sector ranks over {len(sessions)} sessions...")
    history = sectors.backfill_sector_history(price_cache, ticker_to_sector,
                                              sector_caps, sessions)
    store.upsert_many(store.series_path(country, "sector_ranks.jsonl"),
                      [{"date": d, "sectors": r} for d, r in history.items() if r])
    log(f"  {sum(1 for v in history.values() if v)} days")

    log(f"{country}: industry ranks...")
    history = industries.backfill_industry_history(price_cache, ticker_to_industry,
                                                   industry_caps, sessions)
    store.upsert_many(store.series_path(country, "industry_ranks.jsonl"),
                      [{"date": d, "industries": r} for d, r in history.items() if r])
    log(f"  {sum(1 for v in history.values() if v)} days")

    log(f"{country}: environment read...")
    data_dir = config.data_dir(country)
    env_records = environment.backfill_environment(
        {key: store.read_jsonl(os.path.join(data_dir, f"index_{key}.jsonl"))
         for key in cfg["index_tickers"]},
        store.read_jsonl(os.path.join(data_dir, "sector_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "industry_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_adv_decl.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_new_hilo.jsonl")),
        sessions,
        cfg["largecap_keys"],
    )
    store.upsert_many(store.series_path(country, "environment.jsonl"), env_records)
    log(f"  {len(env_records)} days")

    store.write_classification(country, {
        row["name"]: [row["sector"], row["industry"]]
        for _, row in industry_df.iterrows()
        if row.get("sector") and row.get("industry")
    })
    store.write_index_membership(country, tv_industry.index_membership(industry_df, country))

    log(f"{country}: done in {time.time() - started:.0f}s")


if __name__ == "__main__":
    run((sys.argv[1] if len(sys.argv) > 1 else "US").upper())
