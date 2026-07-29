"""
yf_client.py — Yahoo Finance price source, used for India.

Exists because FMP's Starter plan has zero Indian coverage: every NSE/BSE
symbol returns HTTP 402. Yahoo carries both the indices and the individual
names, needs no API key, and — measured, not assumed — returns a full year of
daily bars for 100 NSE tickers in about 8 seconds when downloaded in bulk,
which is what makes a 1,000-name universe practical inside a scheduled run.

Rows come back in exactly the shape FMPClient.historical_eod produces
({date, open, high, low, close}, newest first), so every consumer downstream —
cache.py, metrics/*, store.py — is identical for both countries.

Symbol translation (TradingView -> Yahoo), all verified against live data:

    M&M         -> M&M.NS          ampersands pass through untouched
    BAJAJ_AUTO  -> BAJAJ-AUTO.NS   underscore becomes a hyphen
    EMBASSY.RR  -> EMBASSY.NS      TradingView's REIT/InvIT suffix is dropped

Of the top 1000 NSE names, only KRT.RR and BAGMANE.RR have no Yahoo listing at
all — both recent REIT listings. They are skipped rather than failing the run.
"""

import time

import pandas as pd
import yfinance as yf

# Downloaded in chunks rather than one 1,000-symbol request: a single huge
# request is one all-or-nothing failure, and Yahoo starts dropping columns from
# very wide requests. 100 was measured end-to-end at ~8s per chunk.
CHUNK_SIZE = 100
SECONDS_BETWEEN_CHUNKS = 1.0


def to_yahoo(tv_symbol):
    """TradingView's NSE symbol -> Yahoo's. See module docstring for the rules."""
    s = tv_symbol.strip().upper()
    if s.endswith(".RR"):        # REIT / InvIT marker, not part of the symbol
        s = s[:-3]
    s = s.replace("_", "-")
    return s + ".NS"


def to_yahoo_index(symbol):
    """Index tickers are already stored in Yahoo form in config, so they pass
    through untouched — they must NOT get an .NS suffix."""
    return symbol


def _frame_to_rows(df):
    """One symbol's OHLC DataFrame -> newest-first list of dict rows."""
    if df is None or len(df) == 0:
        return []
    out = []
    for ts, row in df.iterrows():
        close = row.get("Close")
        if close is None or pd.isna(close):
            continue
        def val(key):
            v = row.get(key)
            return None if v is None or pd.isna(v) else round(float(v), 4)
        out.append({
            "date": ts.date().isoformat(),
            "open": val("Open"),
            "high": val("High"),
            "low": val("Low"),
            "close": round(float(close), 4),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def download_many(symbols, period="2y", on_progress=None):
    """Bulk-download OHLC for many symbols.

    Returns {original_symbol: [rows]} keyed by the symbol as PASSED IN (the
    TradingView form), so callers never have to think about the Yahoo spelling.
    Symbols Yahoo has nothing for are simply absent from the result.
    """
    symbols = list(dict.fromkeys(symbols))
    yahoo_of = {s: to_yahoo(s) for s in symbols}
    original_of = {}
    for original, y in yahoo_of.items():
        # If two TradingView symbols collapse onto one Yahoo symbol, first wins;
        # keeping both would double-count the name in breadth.
        original_of.setdefault(y, original)

    result = {}
    unique_yahoo = list(original_of)
    for start in range(0, len(unique_yahoo), CHUNK_SIZE):
        chunk = unique_yahoo[start:start + CHUNK_SIZE]
        try:
            data = yf.download(
                chunk, period=period, auto_adjust=True, threads=True,
                progress=False, group_by="ticker",
            )
        except Exception:
            data = None
        if data is not None and len(data):
            for y in chunk:
                try:
                    sub = data[y] if isinstance(data.columns, pd.MultiIndex) else data
                except KeyError:
                    continue
                rows = _frame_to_rows(sub.dropna(how="all"))
                if rows:
                    result[original_of[y]] = rows
        if on_progress:
            on_progress(min(start + CHUNK_SIZE, len(unique_yahoo)), len(unique_yahoo))
        if start + CHUNK_SIZE < len(unique_yahoo):
            time.sleep(SECONDS_BETWEEN_CHUNKS)
    return result


def historical_index(symbol, period="2y"):
    """Single index series. Kept separate from download_many because index
    tickers must not go through the .NS symbol translation."""
    try:
        df = yf.Ticker(to_yahoo_index(symbol)).history(period=period, auto_adjust=True)
    except Exception:
        return []
    return _frame_to_rows(df)


if __name__ == "__main__":
    for tv, expected in [("M&M", "M&M.NS"), ("BAJAJ_AUTO", "BAJAJ-AUTO.NS"),
                         ("EMBASSY.RR", "EMBASSY.NS"), ("RELIANCE", "RELIANCE.NS")]:
        got = to_yahoo(tv)
        assert got == expected, f"{tv} -> {got}, expected {expected}"
    print("symbol translation self-test passed")

    rows = historical_index("^NSEI")
    print(f"^NSEI: {len(rows)} rows, {rows[-1]['date']} -> {rows[0]['date']}")
    got = download_many(["RELIANCE", "M&M", "BAJAJ_AUTO", "EMBASSY.RR"])
    for k, v in got.items():
        print(f"  {k:12} {len(v):>4} rows  latest {v[0]['date']} close {v[0]['close']}")
