"""
backfill_tickers.py — one-off deep history for the per-ticker chart files.

WHY YAHOO AND NOT FMP
---------------------------------------------------------------------------
Measured on this universe: FMP returns a 2020-to-date history in 2.09s per
symbol, so 3,300 names is nearly two hours. Yahoo returns 100 symbols in 6
seconds — the same job in about three minutes, free, and with no rate ceiling
to trip over. FMP's default window also stops at exactly five years on this
plan; reaching 2020 needs an explicit `from`, which Yahoo does not require.

FMP stays the nightly source. This runs once (or whenever the window moves) to
give the stock chart real depth, and store.write_ticker_ohlc merges rather than
overwrites, so the nightly shallow refresh cannot erode it.

A NOTE ON ADJUSTMENT
---------------------------------------------------------------------------
Yahoo and FMP adjust splits and dividends differently, so a file half-written
by each can show a step at the seam. This fetches the WHOLE span from Yahoo in
one request per symbol rather than splicing, and --check reports names whose
history contains an unexplained overnight jump, which is what a bad adjustment
looks like.

Usage:
    python src/backfill_tickers.py US            # fetch and write
    python src/backfill_tickers.py US --check    # verify what is on disk
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import store

START_DATE = "2020-01-01"

# Must match how each country's NIGHTLY source adjusts, or the join between
# backfilled history and daily updates shows a step.
#
#   US    FMP is split-adjusted but not dividend-adjusted -> auto_adjust=False
#   India yf_client already fetches with auto_adjust=True  -> auto_adjust=True
#
# India was nearly backfilled with the US setting. Indian dividend yields are
# small, but six years of them compound into a visible level shift right at the
# seam — which would look exactly like a gap in the chart.
AUTO_ADJUST = {"US": False, "IN": True}
CHUNK_SIZE = 100
SECONDS_BETWEEN_CHUNKS = 0.5

# An overnight move beyond this, with no matching move in the days around it,
# is far more likely to be an unadjusted split than a real session.
JUMP_THRESHOLD_PCT = 60.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def universe(country):
    """Every name the terminal holds a classification for."""
    path = os.path.join(config.data_dir(country), "classification.json")
    with open(path, encoding="utf-8") as f:
        return sorted(json.load(f))


def to_yahoo(country, ticker):
    if country == "IN":
        import yf_client
        return yf_client.to_yahoo(ticker)
    # US symbols match, except that Yahoo writes share classes with a hyphen.
    return ticker.replace(".", "-")


def rows_from_frame(frame):
    out = []
    for stamp, row in frame.iterrows():
        close = row.get("Close")
        if close is None or close != close:      # NaN
            continue
        volume = row.get("Volume")
        out.append({
            "date": stamp.strftime("%Y-%m-%d"),
            "open": float(row.get("Open") or close),
            "high": float(row.get("High") or close),
            "low": float(row.get("Low") or close),
            "close": float(close),
            "volume": int(volume) if volume == volume and volume else None,
        })
    return out


def backfill(country):
    import yfinance as yf

    tickers = universe(country)
    log(f"{country}: {len(tickers)} names, fetching from {START_DATE}")
    symbol_of = {t: to_yahoo(country, t) for t in tickers}

    written, empty, deepest = 0, 0, 0
    started = time.time()
    for i in range(0, len(tickers), CHUNK_SIZE):
        batch = tickers[i:i + CHUNK_SIZE]
        symbols = [symbol_of[t] for t in batch]
        try:
            frame = yf.download(" ".join(symbols), start=START_DATE, progress=False,
                                auto_adjust=AUTO_ADJUST.get(country, False),
                                group_by="ticker", threads=True)
        except Exception as exc:
            log(f"  chunk {i // CHUNK_SIZE + 1} failed ({exc}); continuing")
            continue

        for ticker in batch:
            symbol = symbol_of[ticker]
            try:
                sub = frame[symbol].dropna(how="all")
            except Exception:
                empty += 1
                continue
            rows = rows_from_frame(sub)
            if not rows:
                empty += 1
                continue
            deepest = max(deepest, len(rows))
            if store.write_ticker_ohlc(country, ticker, rows):
                written += 1

        done = min(i + CHUNK_SIZE, len(tickers))
        if done % 500 == 0 or done == len(tickers):
            rate = done / max(time.time() - started, 1)
            log(f"  {done}/{len(tickers)}  ({rate:.0f}/s, {written} written)")
        time.sleep(SECONDS_BETWEEN_CHUNKS)

    log(f"{country}: wrote {written}, {empty} had no history, deepest {deepest} bars, "
        f"{time.time() - started:.0f}s")


def check(country):
    """Report depth, and any suspicious overnight jumps."""
    out_dir = config.ticker_dir(country)
    depths, suspicious = [], []
    for name in os.listdir(out_dir):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        with open(os.path.join(out_dir, name), encoding="utf-8") as f:
            data = json.load(f)
        closes, dates = data.get("close", []), data.get("dates", [])
        depths.append(len(closes))
        for j in range(1, len(closes)):
            if closes[j - 1] and closes[j]:
                move = (closes[j] / closes[j - 1] - 1) * 100
                if abs(move) > JUMP_THRESHOLD_PCT:
                    suspicious.append((name[:-5], dates[j], round(move, 1)))
    depths.sort()
    log(f"{country}: {len(depths)} files | median {depths[len(depths) // 2]} bars | "
        f"deepest {depths[-1]} | shallowest {depths[0]}")
    if suspicious:
        log(f"  {len(suspicious)} overnight moves beyond {JUMP_THRESHOLD_PCT}% "
            f"(check for unadjusted splits):")
        for ticker, date, move in sorted(suspicious, key=lambda x: -abs(x[2]))[:15]:
            log(f"    {ticker:6} {date}  {move:+.1f}%")
    else:
        log("  no suspicious overnight jumps")


if __name__ == "__main__":
    code = (sys.argv[1] if len(sys.argv) > 1 else "US").upper()
    if "--check" in sys.argv:
        check(code)
    else:
        backfill(code)
        check(code)
