"""
store.py — accumulating JSON Lines history: one row per trading day, forever.
This IS the chart history (as opposed to cache.py's internal rolling buffers).

upsert_jsonl is idempotent on `date`: re-running the pipeline twice on the same
day (e.g. a manual workflow re-trigger) replaces that day's row instead of
duplicating it, while every prior day's row is left untouched.

Every writer takes a country code and resolves its own paths through config, so
the two markets accumulate side by side (data/us/, data/in/) with no shared
filenames and no chance of one overwriting the other.
"""

import json
import os

import config


def upsert_jsonl(path, record):
    """Appends `record` (must have a 'date' key) to the JSONL file at `path`,
    replacing any existing row for the same date."""
    rows = []
    if os.path.exists(path):
        with open(path, "r") as f:
            rows = [json.loads(line) for line in f if line.strip()]

    rows = [r for r in rows if r.get("date") != record["date"]]
    rows.append(record)
    rows.sort(key=lambda r: r["date"])

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def series_path(country, filename):
    return os.path.join(config.data_dir(country), filename)


def write_index(country, key, record):
    upsert_jsonl(series_path(country, f"index_{key}.jsonl"), record)


def write_sector_ranks(country, date_str, sector_records):
    upsert_jsonl(series_path(country, "sector_ranks.jsonl"),
                 {"date": date_str, "sectors": sector_records})


def write_industry_ranks(country, date_str, industry_records):
    upsert_jsonl(series_path(country, "industry_ranks.jsonl"),
                 {"date": date_str, "industries": industry_records})


def write_ticker_ohlc(country, ticker, eod_rows, rs_by_date=None):
    """One small JSON per name under docs/, fetched on demand by the stock page.
    Carries full OHLC rather than the close-only series the shared price cache
    holds, because the stock chart draws HLC bars."""
    out_dir = config.ticker_dir(country)
    os.makedirs(out_dir, exist_ok=True)

    rows = [r for r in eod_rows if r.get("close") is not None]
    rows.sort(key=lambda r: r["date"])
    if not rows:
        return None

    payload = {
        "dates": [r["date"] for r in rows],
        "open": [round(r.get("open") or r["close"], 4) for r in rows],
        "high": [round(r.get("high") or r["close"], 4) for r in rows],
        "low": [round(r.get("low") or r["close"], 4) for r in rows],
        "close": [round(r["close"], 4) for r in rows],
        # Volume was being dropped even though every price source returns it.
        # TMLE's volume-behaviour factor needs it, and it costs nothing to keep.
        "volume": [int(r["volume"]) if r.get("volume") else None for r in rows],
    }
    # RS rating per session, so the stock page can show the number for whatever
    # PAST date is selected rather than only for today — the whole point is
    # logging what the setup looked like on the day it triggered.
    if rs_by_date:
        payload["rs"] = [rs_by_date.get(r["date"]) for r in rows]
    # "/" would create a subdirectory; "&" is legal on disk and in a URL path
    # but several Indian symbols carry it (M&M, J&KBANK), so it is percent-safe
    # only because the page encodes the ticker before fetching.
    name = ticker.replace("/", "-")
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    return name


def write_benchmarks(country, price_cache, index_tickers):
    out_dir = config.ticker_dir(country)
    os.makedirs(out_dir, exist_ok=True)
    written = []

    benchmarks = {}
    for key, symbol in index_tickers.items():
        series = price_cache.get(symbol)
        if series:
            benchmarks[key] = series
    if benchmarks:
        dates = sorted(set().union(*[set(s.keys()) for s in benchmarks.values()]))
        payload = {"dates": dates}
        for key, series in benchmarks.items():
            payload[key] = [series.get(d) for d in dates]
        with open(os.path.join(out_dir, "_benchmarks.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written.append("_benchmarks")

    return written


def write_classification(country, ticker_map):
    """{ticker: [sector, industry]} — lets the replay view resolve a symbol to
    the groups it belongs to. Only today's classification is kept: TradingView
    has no historical mode, so a stock that changed industry mid-year will show
    under its current one for older dates. Same approximation the prior TMLE
    project made."""
    path = series_path(country, "classification.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(ticker_map, f, separators=(",", ":"), sort_keys=True)


def write_environment(country, record):
    upsert_jsonl(series_path(country, "environment.jsonl"), record)


def write_breadth(country, breadth_record):
    date_str = breadth_record["date"]

    upsert_jsonl(series_path(country, "breadth_adv_decl.jsonl"), {
        "date": date_str,
        "advancers": breadth_record["advancers"],
        "decliners": breadth_record["decliners"],
        "net": breadth_record["net_adv_decl"],
    })
    upsert_jsonl(series_path(country, "breadth_new_hilo.jsonl"), {
        "date": date_str,
        "new_highs": breadth_record["new_highs"],
        "new_lows": breadth_record["new_lows"],
        "net": breadth_record["net_new_hilo"],
        # Which sectors those highs and lows came from. Only non-zero sectors
        # are present, so a quiet day carries a near-empty map.
        "hi_by_sector": breadth_record.get("hi_by_sector") or {},
        "lo_by_sector": breadth_record.get("lo_by_sector") or {},
    })
    for metric_key, filename in [
        ("pct_up20", "breadth_pct_up20.jsonl"),
        ("pct_up30", "breadth_pct_up30.jsonl"),
        ("pct_down20", "breadth_pct_down20.jsonl"),
        ("pct_down30", "breadth_pct_down30.jsonl"),
    ]:
        upsert_jsonl(series_path(country, filename), {
            "date": date_str,
            "value": breadth_record[metric_key],
        })


def write_hilo(country, count_records, name_records, quotes):
    """Multi-window high/low data: counts accumulate, names and quotes are
    snapshots rewritten each run (they describe the present, not a series)."""
    for record in count_records:
        upsert_jsonl(series_path(country, "hilo_counts.jsonl"), record)

    for filename, payload in (("hilo_names.json", name_records),
                              ("hilo_quotes.json", quotes)):
        path = series_path(country, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))


def write_index_membership(country, membership):
    """{ticker: [index codes]} — a snapshot, rewritten each run."""
    path = series_path(country, "index_membership.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(membership, f, separators=(",", ":"), sort_keys=True)


def write_breadth_members(country, counts):
    """{sector: member count} for the breadth universe, rewritten each run."""
    path = series_path(country, "breadth_sector_members.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(counts, f, indent=1, sort_keys=True)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    test_path = os.path.join(config.DATA_DIR, "_store_selftest.jsonl")
    upsert_jsonl(test_path, {"date": "2026-01-01", "value": 1})
    upsert_jsonl(test_path, {"date": "2026-01-02", "value": 2})
    upsert_jsonl(test_path, {"date": "2026-01-01", "value": 99})  # should replace, not duplicate
    rows = read_jsonl(test_path)
    assert rows == [{"date": "2026-01-01", "value": 99}, {"date": "2026-01-02", "value": 2}], rows
    os.remove(test_path)
    print("store.py self-test passed")
