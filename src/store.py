"""
store.py — accumulating JSON Lines history: one row per trading day, forever.
This IS the chart history (as opposed to cache.py's internal rolling buffers).

upsert_jsonl is idempotent on `date`: re-running the pipeline twice on the same
day (e.g. a manual workflow re-trigger) replaces that day's row instead of
duplicating it, while every prior day's row is left untouched.
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


def write_index(key, record):
    path = os.path.join(config.DATA_DIR, f"index_{key}.jsonl")
    upsert_jsonl(path, record)


def write_sector_ranks(date_str, sector_records):
    path = os.path.join(config.DATA_DIR, "sector_ranks.jsonl")
    upsert_jsonl(path, {"date": date_str, "sectors": sector_records})


def write_industry_ranks(date_str, industry_records):
    path = os.path.join(config.DATA_DIR, "industry_ranks.jsonl")
    upsert_jsonl(path, {"date": date_str, "industries": industry_records})


def write_ticker_ohlc(ticker, eod_rows):
    """One small JSON per watchlist name under docs/, fetched on demand by the
    stock page. Carries full OHLC rather than the close-only series the shared
    price cache holds, because the stock chart draws HLC bars — worth the extra
    call for the handful of names on the watchlist, not for all 1,500."""
    out_dir = os.path.join(config.DOCS_DIR, config.TICKER_DIR_NAME)
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
    }
    name = ticker.replace("/", "-")
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    return name


def write_benchmarks(price_cache, index_tickers):
    out_dir = os.path.join(config.DOCS_DIR, config.TICKER_DIR_NAME)
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


def write_classification(ticker_map):
    """{ticker: [sector, industry]} — lets the replay view resolve a symbol to
    the groups it belongs to. Only today's classification is kept: TradingView
    has no historical mode, so a stock that changed industry mid-year will show
    under its current one for older dates. Same approximation the prior TMLE
    project made."""
    path = os.path.join(config.DATA_DIR, "classification.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(ticker_map, f, separators=(",", ":"), sort_keys=True)


def write_environment(record):
    upsert_jsonl(os.path.join(config.DATA_DIR, "environment.jsonl"), record)


def write_breadth(breadth_record):
    date_str = breadth_record["date"]

    upsert_jsonl(os.path.join(config.DATA_DIR, "breadth_adv_decl.jsonl"), {
        "date": date_str,
        "advancers": breadth_record["advancers"],
        "decliners": breadth_record["decliners"],
        "net": breadth_record["net_adv_decl"],
    })
    upsert_jsonl(os.path.join(config.DATA_DIR, "breadth_new_hilo.jsonl"), {
        "date": date_str,
        "new_highs": breadth_record["new_highs"],
        "new_lows": breadth_record["new_lows"],
        "net": breadth_record["net_new_hilo"],
    })
    for metric_key, filename in [
        ("pct_up20", "breadth_pct_up20.jsonl"),
        ("pct_up30", "breadth_pct_up30.jsonl"),
        ("pct_down20", "breadth_pct_down20.jsonl"),
        ("pct_down30", "breadth_pct_down30.jsonl"),
    ]:
        upsert_jsonl(os.path.join(config.DATA_DIR, filename), {
            "date": date_str,
            "value": breadth_record[metric_key],
        })


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
