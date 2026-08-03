"""
repair_tickers.py — replace suspect Yahoo history with FMP's.

WHY THIS IS NEEDED
---------------------------------------------------------------------------
The 2020 backfill pulls from Yahoo because it is ~40x faster than FMP for deep
history. Yahoo is right about splits — NVDA's 10:1 and AAPL's 4:1 both come
back adjusted — but it is wrong about TICKER REUSE. When a symbol is reassigned
to a different company, Yahoo returns the old company's prices spliced onto the
new one's, which shows up as an impossible overnight gap:

    DEC   2023-12-05   Yahoo +1907%   FMP  +0.4%
    TECX  2024-06-24   Yahoo +1050%   FMP  -5.3%
    BMNR  2025-06-30   Yahoo  +695%   FMP  +696%   <- a real move, kept

A fabricated history is worse than a short one in a tool built for looking up
past setups, so any name with an unexplained gap is refetched from FMP and its
file rewritten from that source alone. Names whose gap FMP confirms are left
untouched: a genuine 700% day is data, not an error.

Usage:
    python src/repair_tickers.py US
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import store
from fmp_client import FMPClient

JUMP_THRESHOLD_PCT = 60.0
# How far apart the two sources may be on the same day before Yahoo is judged
# wrong. Well clear of ordinary adjustment noise, well under a splice.
DISAGREEMENT_PCT = 25.0

# A move this large is not a session in any real instrument. It survives a
# source swap because the underlying prints are junk — OGG closes at $0.018 and
# then at $17, which is a ratio, not a trade.
ABSURD_MOVE_PCT = 300.0
# Below this, one tick is a double-digit percentage and every derived figure
# built on the series is noise.
MIN_SANE_CLOSE = 0.05


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def suspects(country):
    """[(ticker, date, move)] for every unexplained overnight gap on disk."""
    out_dir = config.ticker_dir(country)
    found = {}
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            data = json.load(f)
        closes, dates = data.get("close", []), data.get("dates", [])
        for j in range(1, len(closes)):
            if closes[j - 1] and closes[j]:
                move = (closes[j] / closes[j - 1] - 1) * 100
                if abs(move) > JUMP_THRESHOLD_PCT:
                    found.setdefault(name[:-5], []).append((dates[j], round(move, 1)))
    return found


def trim_to_sane(rows):
    """Drop everything up to and including the last unusable print.

    Some series are junk in every source — sub-penny closes, or gaps no
    instrument can make. Rather than publish a chart that lies, the history is
    cut to the segment after the last bad bar. A short honest chart beats a
    long invented one in a tool built for studying real setups.
    """
    ordered = sorted((r for r in rows if r.get("close")), key=lambda r: r["date"])
    cut = -1
    for i, row in enumerate(ordered):
        if row["close"] < MIN_SANE_CLOSE:
            cut = i
        elif i and ordered[i - 1]["close"]:
            move = abs(row["close"] / ordered[i - 1]["close"] - 1) * 100
            if move > ABSURD_MOVE_PCT:
                cut = i
    return ordered[cut + 1:]


def repair(country):
    import main as pipeline

    client = FMPClient(pipeline.api_key_from_env_file())
    flagged = suspects(country)
    log(f"{country}: {len(flagged)} names carry an unexplained gap")

    out_dir = config.ticker_dir(country)
    replaced, confirmed, unavailable = 0, 0, 0
    started = time.time()

    for n, (ticker, gaps) in enumerate(sorted(flagged.items()), 1):
        try:
            rows = client.historical_eod(ticker, start="2020-01-01")
        except Exception:
            unavailable += 1
            continue
        if not rows:
            unavailable += 1
            continue

        by_date = {r["date"]: r for r in rows}
        ordered = sorted(by_date)

        # EVERY gap has to be confirmed, not just one. TECX carries five, and
        # checking only until the first match let one real move vouch for four
        # fabricated ones — the name was left untouched with a +1050% day still
        # in it.
        all_confirmed = True
        for date, move in gaps:
            if date not in by_date:
                all_confirmed = False
                break
            k = ordered.index(date)
            if k == 0:
                continue
            prev, cur = by_date[ordered[k - 1]]["close"], by_date[date]["close"]
            if not prev or abs((cur / prev - 1) * 100 - move) >= DISAGREEMENT_PCT:
                all_confirmed = False
                break
        if all_confirmed:
            confirmed += 1
            continue

        # Yahoo's history for this symbol is not this company's. Rewrite from
        # FMP alone — delete first, because write_ticker_ohlc merges, and
        # merging into the bad series would keep exactly what is being removed.
        path = os.path.join(out_dir, ticker.replace("/", "-") + ".json")
        preserved_rs = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            preserved_rs = {d: v for d, v in
                            zip(old.get("dates", []), old.get("rs") or [])
                            if v is not None}
            os.remove(path)
        rows = trim_to_sane(rows)
        if rows and store.write_ticker_ohlc(country, ticker, rows, preserved_rs or None):
            replaced += 1

        if n % 50 == 0:
            log(f"  {n}/{len(flagged)} checked ({replaced} replaced, {confirmed} confirmed real)")

    log(f"{country}: {replaced} rewritten from FMP, {confirmed} confirmed as real moves, "
        f"{unavailable} unavailable, {time.time() - started:.0f}s")


def sanitize_all(country):
    """Trim every stored file back to its last usable bar.

    Runs independently of the source comparison, because agreement between
    sources is not the same as correctness: FMP reports BSP's +2,699,900%
    exactly as Yahoo does. Both are faithfully recording prints at $0.0015,
    where a single tick is a multiple. Nothing built on that series means
    anything, so the chart starts after it.
    """
    out_dir = config.ticker_dir(country)
    trimmed, dropped = 0, 0
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        path = os.path.join(out_dir, name)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        dates = data.get("dates", [])
        rows = [{"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}
                for d, o, h, l, c, v in zip(
                    dates, data.get("open", []), data.get("high", []),
                    data.get("low", []), data.get("close", []),
                    data.get("volume") or [None] * len(dates))]
        kept = trim_to_sane(rows)
        if len(kept) == len(rows):
            continue
        preserved_rs = {d: v for d, v in zip(dates, data.get("rs") or [])
                        if v is not None}
        os.remove(path)
        if kept:
            store.write_ticker_ohlc(country, name[:-5], kept, preserved_rs or None)
            trimmed += 1
        else:
            dropped += 1        # nothing usable at all
    log(f"{country}: trimmed {trimmed} files, dropped {dropped} with no usable history")


if __name__ == "__main__":
    code = (sys.argv[1] if len(sys.argv) > 1 else "US").upper()
    if "--sanitize" in sys.argv:
        sanitize_all(code)
    else:
        repair(code)
        sanitize_all(code)
