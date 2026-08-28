"""
main.py — daily pipeline orchestration, run once per country.

Every run recomputes the FULL trailing CHART_BACKFILL_DAYS window for every
panel (not just today) and upserts all of it via store.py, which is idempotent
per date — re-writing an unchanged historical day is harmless. This means the
pipeline is self-healing (a missed day, a widened backfill window, or a
one-off local re-run all just work) without needing separate "first run only"
backfill logic.

Order, per country: build universe -> backfill/update price cache -> pull
TradingView classification (industry + sector tags) -> backfill index/breadth/
sector/industry history from the cache -> upsert into data/<country>/*.jsonl ->
render that country's pages.

Price sources differ by market and are the ONLY country-specific step:
  US    FMP  (paid, has US coverage, no India coverage at all on Starter)
  India Yahoo via yfinance (no key; ~8s per 100 symbols in bulk)
Both hand back the same row shape, so everything downstream is shared.

A failure in one country does not abort the other — India breaking on a Yahoo
outage should never take the (paid, reliable) US terminal down with it.
"""

import os
import sys
import time
import traceback
from datetime import datetime, timezone

import budget
import config
import cache as cache_mod
import fundamentals as fundamentals_mod
import universe
import store
import render
import tv_industry
import yf_client
from fmp_client import FMPClient
from metrics import (indices, sectors, industries, breadth, environment, hilo,
                     groups as groups_mod, relative_strength as rs_mod)
from tmle import config as tmle_config, run as tmle_run


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def todays_bar_is_incomplete(country_cfg, now=None):
    """True while today's session is still forming for this market.

    Both FMP's "historical" endpoint and Yahoo hand back an in-progress bar
    mid-session that looks exactly like a finished one. Writing that into the
    history would show a partial day as a real close (and skew its EMAs, its
    breadth counts and the day-change figure), so today's row is dropped
    entirely until the session is genuinely over. Cutoffs live in config:
    21:30 UTC for the US (clears both EDT and EST closes), 10:30 UTC for India
    (15:30 IST, no daylight saving to worry about).
    """
    now = now or datetime.now(timezone.utc)
    return (now.hour, now.minute) < country_cfg["session_final_after_utc"]


# ---------------------------------------------------------------- price sources

def fetch_prices_fmp(client, tickers, index_tickers, drop_partial, price_cache,
                     country_code="US"):
    """US: FMP, one symbol per call (batch endpoints are plan-gated), so only
    symbols missing from the rolling cache are backfilled; the rest get today's
    close from the quote loop."""
    missing = [t for t in tickers if t not in price_cache]
    if missing:
        log(f"  backfilling {len(missing)} new tickers (up to {config.PRICE_WINDOW_DAYS} days each)...")
        failed = []
        for i, ticker in enumerate(missing):
            # One unfetchable symbol must never take the run down. Share-class
            # tickers like AGM.A return 402 on this plan, and a single one of
            # them was enough to abort the entire daily refresh before it wrote
            # anything at all.
            try:
                hist = drop_partial(client.historical_eod(ticker))[:config.PRICE_WINDOW_DAYS]
            except Exception:
                failed.append(ticker)
                continue
            for row in hist:
                cache_mod.set_value(price_cache, ticker, row["date"], row["close"])
            if (i + 1) % 100 == 0:
                log(f"    {i + 1}/{len(missing)}")
                cache_mod.save(price_cache, config.price_cache_path(country_code))
        if failed:
            preview = ", ".join(failed[:8]) + (" ..." if len(failed) > 8 else "")
            log(f"  {len(failed)} symbols had no fetchable history and were skipped: {preview}")
    return price_cache


def index_ohlc_rows(country_cfg, client, symbol, drop_partial):
    if country_cfg["price_source"] == "fmp":
        return drop_partial(client.historical_eod(symbol))
    return drop_partial(yf_client.historical_index(symbol, period="2y"))


def ticker_ohlc_rows(country_cfg, client, ticker, drop_partial, bulk):
    if country_cfg["price_source"] == "fmp":
        return drop_partial(client.historical_eod(ticker))
    return drop_partial(bulk.get(ticker, []))


# ---------------------------------------------------------------- per country

def run_country(code, client=None):
    cfg = config.COUNTRIES[code]
    today = datetime.now(timezone.utc).date().isoformat()

    skip_today = todays_bar_is_incomplete(cfg)
    if skip_today:
        log(f"{code}: session for {today} has not closed yet — today's partial bar "
            f"will be excluded; the dashboard reports through the last completed session.")

    def drop_partial(rows):
        return [r for r in rows if r.get("date") != today] if skip_today else rows

    # ---------- Classification (also the universe for India) ----------
    log(f"{code}: fetching TradingView classification...")
    industry_df = tv_industry.fetch_industry_classification(cfg)
    ticker_to_sector, sector_caps = sectors.extract_classification(industry_df)
    ticker_to_industry, industry_caps = industries.extract_classification(industry_df)
    log(f"{code}: classified {len(industry_df)} stocks — "
        f"{industry_df['sector'].nunique()} sectors, {industry_df['industry'].nunique()} industries")

    # The US breadth universe is the S&P 1500 (a defined index membership).
    # India has no equivalent free constituent list, so the breadth universe is
    # the same top-1000-by-market-cap set the classification pull returns.
    if code == "US":
        uni = universe.build_sp1500()
        stock_tickers = uni["ticker"].tolist()
        extra = [t for t in config.WATCHLIST if t not in stock_tickers]
    else:
        stock_tickers = industry_df["name"].tolist()
        extra = []

    index_tickers = list(cfg["index_tickers"].values())
    all_price_tickers = sorted(set(stock_tickers) | set(extra) | set(industry_df["name"]))
    log(f"{code}: universe {len(stock_tickers)} breadth names, "
        f"{len(all_price_tickers)} priced names, {len(index_tickers)} indices")

    # ---------- Price cache ----------
    cache_path = config.price_cache_path(code)
    price_cache = cache_mod.load(cache_path)
    bulk = {}
    if cfg["price_source"] == "fmp":
        fetch_prices_fmp(client, all_price_tickers + index_tickers, index_tickers,
                         drop_partial, price_cache, code)
        if skip_today:
            log(f"{code}: skipping today's close — a live quote mid-session is not a close.")
        else:
            targets = all_price_tickers + index_tickers
            log(f"{code}: fetching today's close for {len(targets)} tickers...")
            # This used to be client.quote_many() — FMP's real-time /quote
            # endpoint, one call per symbol. On three separate days it went
            # dark for the whole run (each symbol "failed" individually, each
            # failure swallowed individually, exactly like every other
            # per-symbol loop against this API has to), and the run kept
            # reporting success while pricing 10-25 of 3,489 tickers instead
            # of the usual ~3,400. Every downstream metric that requires a
            # quorum of same-day prices correctly refused to count that as a
            # session and skipped it — while index history, fetched via THIS
            # endpoint rather than /quote, sailed through regardless. That
            # mismatch is what actually produced the "different panels show
            # different dates" symptom. Using the same endpoint here that
            # already backfills every other day reliably removes the failure
            # mode; the coverage check below is the backstop in case it
            # doesn't.
            got = 0
            for i, sym in enumerate(targets):
                try:
                    rows = client.historical_eod(sym, start=today, end=today)
                except Exception:
                    rows = []
                if rows and rows[0].get("close") is not None:
                    cache_mod.set_value(price_cache, sym, today, rows[0]["close"])
                    got += 1
                if (i + 1) % 250 == 0:
                    log(f"    {i + 1}/{len(targets)}")
            coverage = got / len(targets) if targets else 1.0
            log(f"{code}: priced {got}/{len(targets)} tickers for {today} ({coverage:.1%})")
            if coverage < config.MIN_DAILY_PRICE_COVERAGE:
                raise RuntimeError(
                    f"{code}: only {got}/{len(targets)} tickers priced for {today} "
                    f"({coverage:.1%}, below the {config.MIN_DAILY_PRICE_COVERAGE:.0%} "
                    "floor) — treating this as a fetch failure, not a thin session, "
                    "and aborting before anything for today is committed.")
    else:
        bulk = yf_client.download_many(
            all_price_tickers, period="2y",
            on_progress=lambda done, total: log(f"    {done}/{total}"),
        )
        for ticker, rows in bulk.items():
            for row in drop_partial(rows)[:config.PRICE_WINDOW_DAYS]:
                cache_mod.set_value(price_cache, ticker, row["date"], row["close"])
        log(f"{code}: {len(bulk)}/{len(all_price_tickers)} symbols returned usable history")
        for symbol in index_tickers:
            for row in drop_partial(yf_client.historical_index(symbol, period="2y"))[:config.PRICE_WINDOW_DAYS]:
                cache_mod.set_value(price_cache, symbol, row["date"], row["close"])

    cache_mod.trim(price_cache, config.PRICE_WINDOW_DAYS)
    cache_mod.save(price_cache, cache_path)
    log(f"{code}: price cache updated ({len(price_cache)} symbols)")

    n_days = config.CHART_BACKFILL_DAYS

    # ---------- Indices ----------
    # Fetched separately from the shared price cache: the cache holds closes
    # only (all the breadth maths needs), but the index panels draw HLC bars,
    # which needs high/low too.
    log(f"{code}: backfilling index history (OHLC + EMA10/20/50)...")
    for key, symbol in cfg["index_tickers"].items():
        try:
            eod_rows = index_ohlc_rows(cfg, client, symbol, drop_partial)
        except Exception:
            log(f"  {key}: fetch failed, leaving the stored history untouched")
            continue
        records = indices.backfill_index_history(eod_rows, n_days)
        for record in records:
            store.write_index(code, key, record)
        log(f"  {key}: {len(records)} days")

    # ---------- Breadth ----------
    log(f"{code}: backfilling breadth internals...")
    breadth_records = breadth.backfill_breadth_history(
        price_cache, stock_tickers, n_days, ticker_to_sector)
    # Sector weights for the breadth universe specifically, so the panel can
    # ask "more highs than its size implies?" rather than "most highs", which
    # the largest sector wins by default.
    store.write_breadth_members(code, {
        s: sum(1 for t in stock_tickers if ticker_to_sector.get(t) == s)
        for s in sorted(set(filter(None, (ticker_to_sector.get(t) for t in stock_tickers))))
    })
    for record in breadth_records:
        store.write_breadth(code, record)
    if breadth_records:
        last = breadth_records[-1]
        log(f"  {len(breadth_records)} days (latest: adv={last['advancers']} decl={last['decliners']} "
            f"new_hi={last['new_highs']} new_lo={last['new_lows']})")

    store.write_index_membership(code, tv_industry.index_membership(industry_df, code))

    store.write_classification(code, {
        row["name"]: [row["sector"], row["industry"]]
        for _, row in industry_df.iterrows()
        if row.get("sector") and row.get("industry")
    })

    dates = groups_mod.trading_calendar(
        price_cache, cfg["index_tickers"][cfg["calendar_index"]], n_days)

    # A near-total fetch failure is invisible downstream — it just looks like a
    # holiday. Report coverage every run so the next one is caught the same
    # night rather than a week later as a panel being mysteriously behind.
    for day, have, pool_size in groups_mod.session_coverage(price_cache):
        share = have / pool_size if pool_size else 0
        if share < groups_mod.SESSION_QUORUM:
            log(f"{code}: !! {day} has data for only {have} of {pool_size} "
                f"tickers ({share:.1%}) — treated as a non-session. If the "
                f"market was open, this fetch failed.")
        elif share < groups_mod.SESSION_HEALTHY:
            log(f"{code}: !  {day} built on {have} of {pool_size} tickers "
                f"({share:.1%}) — breadth counts for this day understate "
                f"the tape by roughly the missing share.")
    log(f"{code}: calendar covers {len(dates)} sessions, "
        f"latest {dates[-1] if dates else 'none'}")

    log(f"{code}: backfilling sector performance/rank...")
    sector_history = sectors.backfill_sector_history(price_cache, ticker_to_sector, sector_caps, dates)
    for d, records in sector_history.items():
        if records:
            store.write_sector_ranks(code, d, records)
    log(f"  {sum(1 for r in sector_history.values() if r)} days")

    log(f"{code}: backfilling industry performance/rank...")
    industry_history = industries.backfill_industry_history(price_cache, ticker_to_industry, industry_caps, dates)
    for d, records in industry_history.items():
        if records:
            store.write_industry_ranks(code, d, records)
    log(f"  {sum(1 for r in industry_history.values() if r)} days")

    # ---------- Environment read ----------
    # Computed here (not in the browser) so the label exists for every past
    # date too — the replay view and, later, the trade log both need to ask
    # "what was the environment on this date" without recomputing anything.
    log(f"{code}: computing environment read...")
    data_dir = config.data_dir(code)
    env_records = environment.backfill_environment(
        {key: store.read_jsonl(os.path.join(data_dir, f"index_{key}.jsonl"))
         for key in cfg["index_tickers"]},
        store.read_jsonl(os.path.join(data_dir, "sector_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "industry_ranks.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_adv_decl.jsonl")),
        store.read_jsonl(os.path.join(data_dir, "breadth_new_hilo.jsonl")),
        dates,
        cfg["largecap_keys"],
    )
    for record in env_records:
        store.write_environment(code, record)
    if env_records:
        latest = env_records[-1]
        log(f"  {len(env_records)} days (latest {latest['date']}: {latest['overall']}, "
            f"trend {latest['trend']['factors_favourable']}/{latest['trend']['factors_total']})")

    # ---------- Per-ticker OHLC for the stock page ----------
    # Every classified name, not a watchlist: the whole point of the stock page
    # is looking up something unfamiliar, which a curated list can never cover.
    # Cross-sectional RS ratings, computed once for the whole universe: a
    # percentile only means something relative to everyone else that day, so it
    # cannot be produced per ticker inside the loop below.
    log(f"{code}: computing relative strength ratings...")
    rs_ratings = rs_mod.compute_ratings(price_cache, benchmark=cfg["rs_benchmark"])
    log(f"{code}: rated {len(rs_ratings)} names")

    lookup_tickers = sorted(set(industry_df["name"]) | set(stock_tickers))
    log(f"{code}: writing OHLC for {len(lookup_tickers)} names...")
    written, skipped = 0, 0
    # TMLE needs a deeper window than the stock page draws (a trailing-year
    # factor measured across a year of checkpoints needs two years behind it),
    # so the untruncated rows are kept as they go past rather than re-fetched.
    tmle_prices = {}
    # ADR needs the daily high/low, which the close-only price cache does not
    # carry. Computed here rather than in a second pass because the OHLC is
    # already in hand at this point and would otherwise be re-fetched.
    adr_by_ticker = {}
    want_tmle = cfg.get("run_tmle")
    for i, ticker in enumerate(lookup_tickers):
        try:
            all_rows = ticker_ohlc_rows(cfg, client, ticker, drop_partial, bulk)
        except Exception:
            skipped += 1
            continue
        if want_tmle and all_rows:
            tmle_prices[ticker] = all_rows
        if all_rows:
            ordered = sorted(all_rows, key=lambda r: r["date"])
            adr = hilo.adr_percent([r.get("high") for r in ordered],
                                   [r.get("low") for r in ordered])
            if adr is not None:
                adr_by_ticker[ticker] = adr
        if store.write_ticker_ohlc(code, ticker, all_rows[:config.TICKER_HISTORY_DAYS],
                                   rs_ratings.get(ticker)):
            written += 1
        else:
            skipped += 1
        if (i + 1) % 250 == 0:
            log(f"    {i + 1}/{len(lookup_tickers)}")
    store.write_benchmarks(code, price_cache, cfg["index_tickers"])
    log(f"{code}: wrote {written} per-ticker files ({skipped} had no usable history)")

    # New highs/lows at 13, 26 and 52 weeks, with the ticker names — the
    # screener's data. Same price cache, no extra API calls.
    # Run over EVERY priced name, not the breadth universe. Breadth counts are
    # a property of the index and are correctly scoped to its 1,500 members,
    # but a screener scoped that way cannot show the names worth finding —
    # AAOI is not in the S&P 1500, and the turns Neil is hunting happen in
    # exactly the smaller names the index excludes.
    log(f"{code}: building high/low screener data (13/26/52 week)...")
    screener_tickers = [t for t in all_price_tickers if t in ticker_to_sector]
    hilo_counts, hilo_names = hilo.compute(price_cache, screener_tickers, n_days,
                                           ticker_to_sector)
    store.write_hilo(code, hilo_counts, hilo_names,
                     hilo.last_quotes(price_cache, screener_tickers, adr_by_ticker))
    if hilo_counts:
        last = hilo_counts[-1]
        log("  " + " · ".join(
            f"{hilo.WINDOW_LABELS[k]}: {last[k]['hi']}h/{last[k]['lo']}l"
            for k in hilo.WINDOWS))

    # ---------- TMLE ----------
    if want_tmle and tmle_prices:
        log(f"{code}: running TMLE over {len(tmle_prices)} names...")
        try:
            bench_rows = drop_partial(client.historical_eod(tmle_config.PRIMARY_BENCHMARK))
            caps = {row["name"]: row.get("market_cap_basic") or 0
                    for _, row in industry_df.iterrows()}
            funds = tv_industry.fundamentals_map(industry_df)
            quarterly, forward = {}, {}
            if cfg["price_source"] == "fmp":
                log(f"{code}: refreshing quarterly fundamentals for F2B...")
                quarterly, forward = fundamentals_mod.refresh(code, client, lookup_tickers, log=log)
            leaders = tmle_run.run(code, tmle_prices, bench_rows, dates, caps,
                                   fundamentals=funds, quarterly=quarterly,
                                   rs_ratings=rs_ratings, forward=forward, log=log)
            if leaders:
                top = ", ".join(f"{r['ticker']} {r['composite']}" for r in leaders[:5])
                log(f"{code}: TMLE top 5 — {top}")
        except Exception:
            # A scoring failure must not cost the breadth refresh, which is the
            # part that has to be right every day.
            log(f"{code}: TMLE FAILED (breadth data is unaffected)")
            traceback.print_exc()

    # ---------- Render ----------
    dashboard, panels = render.render_country(code)
    log(f"{code}: rendered {dashboard} + {len(panels)} panel pages")

    # Both times the embeds became unusable, the number that would have shown
    # it was on disk and nobody was looking. index.html reached 41MB by
    # growing ~18KB a weekday for six months. Report it every run so the next
    # regression is caught in week one rather than by Neil noticing.
    budget.check(code, log=lambda m: log(m.strip()))


def api_key_from_env_file():
    """Read FMP_API_KEY out of .env for local runs.

    In Actions the key arrives as a real environment variable from repo
    secrets; locally it lives only in .env, which is gitignored. Reading it
    here means a local run is plain `python src/main.py US` with no secret on
    the command line — nothing to leak into shell history or a log.
    """
    path = os.path.join(config.ROOT_DIR, ".env")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("FMP_API_KEY"):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


def main():
    only = sys.argv[1].upper() if len(sys.argv) > 1 else None
    codes = [only] if only else list(config.COUNTRIES)

    client = None
    if any(config.COUNTRIES[c]["price_source"] == "fmp" for c in codes):
        api_key = os.environ.get("FMP_API_KEY") or api_key_from_env_file()
        if not api_key:
            log("FMP_API_KEY is not set.")
            sys.exit(1)
        client = FMPClient(api_key)

    failures = []
    for code in codes:
        started = time.time()
        try:
            run_country(code, client)
            log(f"{code}: done in {time.time() - started:.0f}s")
        except Exception:
            # One market failing must not take the other down with it.
            failures.append(code)
            log(f"{code}: FAILED after {time.time() - started:.0f}s")
            traceback.print_exc()

    if failures:
        log(f"Completed with failures: {', '.join(failures)}")
        sys.exit(1)
    log("All countries complete.")


if __name__ == "__main__":
    main()
