"""
metrics/indices.py — NASDAQ / S&P 500 / Russell 2000: daily OHLC plus EMA10/20/50
and their position/slope relative to price. Raw, directly observable data only —
no composite/derived signal (position above/below and slope direction are just
plain comparisons, not a blended score).

Takes full OHLC rows straight from FMP rather than reading the shared price
cache: the cache only keeps closing prices (all the breadth maths needs), but
the dashboard draws these three indices as HLC bars, which needs high and low
too. It's only three symbols, so fetching them separately is cheap.

Computes the EMA series once (vectorized over the full history) and extracts
records for a trailing window of dates, rather than one point at a time — the
EMA for day N depends on the whole history up to day N anyway, so one pass is
both simpler and much faster than replaying it per day.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def build_ohlc_frame(eod_rows, volume_rows=None):
    """eod_rows: list of dicts from FMPClient.historical_eod (newest first).
    Returns a DataFrame indexed by date (ascending) with open/high/low/close
    plus ema10/ema20/ema50, or None if there's nothing usable.

    volume_rows, if given, is a second list of {date, volume} dicts merged in
    by date to add volume + a trailing 30-session average. It is a SEPARATE
    argument rather than a column read off eod_rows because the volume worth
    showing does not always come from the index ticker itself: an index has
    no real traded volume of its own, so some of these are actually a proxy
    ETF's volume (e.g. IWM standing in for Russell 2000) rather than the
    index's own (frequently zero or meaningless) figure. Omitted entirely —
    not just left null — for an index where no source gives a real number,
    so the frontend can tell "not tracked" apart from "zero volume today"."""
    rows = [r for r in eod_rows if r.get("close") is not None]
    if not rows:
        return None

    df = pd.DataFrame(rows)[["date", "open", "high", "low", "close"]]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df.astype(float)

    close = df["close"]
    df["ema10"] = close.ewm(span=10, adjust=False).mean()
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    if volume_rows is not None:
        vol_rows = [r for r in volume_rows
                    if r.get("date") is not None and r.get("volume") is not None]
        if vol_rows:
            vol_df = pd.DataFrame(vol_rows)[["date", "volume"]]
            vol_df["date"] = pd.to_datetime(vol_df["date"])
            vol_df = vol_df.set_index("date").sort_index()
            vol_df = vol_df[~vol_df.index.duplicated(keep="last")]
            df["volume"] = vol_df["volume"].astype(float).reindex(df.index)
            df["avg_vol30"] = df["volume"].rolling(30, min_periods=1).mean()
    return df


def _record_from_row(df, i, has_volume):
    row = df.iloc[i]

    def rising(col):
        return bool(i >= 1 and row[col] > df.iloc[i - 1][col])

    record = {
        "date": df.index[i].strftime("%Y-%m-%d"),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "ema10": float(row["ema10"]),
        "ema20": float(row["ema20"]),
        "ema50": float(row["ema50"]),
        "above_ema10": bool(row["close"] > row["ema10"]),
        "above_ema20": bool(row["close"] > row["ema20"]),
        "above_ema50": bool(row["close"] > row["ema50"]),
        "ema10_rising": rising("ema10"),
        "ema20_rising": rising("ema20"),
        "ema50_rising": rising("ema50"),
    }
    if has_volume:
        vol, avg_vol = row["volume"], row["avg_vol30"]
        record["volume"] = None if pd.isna(vol) else float(vol)
        record["avg_vol30"] = None if pd.isna(avg_vol) else float(avg_vol)
    return record


def backfill_index_history(eod_rows, n_days, volume_rows=None):
    """Records for the trailing n_days of available history (fewer if FMP
    returned less than that)."""
    df = build_ohlc_frame(eod_rows, volume_rows=volume_rows)
    if df is None or df.empty:
        return []
    has_volume = "volume" in df.columns
    start = max(0, len(df) - n_days)
    return [_record_from_row(df, i, has_volume) for i in range(start, len(df))]


if __name__ == "__main__":
    import json
    from fmp_client import FMPClient
    from config import COUNTRIES, DEFAULT_COUNTRY, CHART_BACKFILL_DAYS

    key = os.environ.get("FMP_API_KEY")
    if not key:
        print("Set FMP_API_KEY to smoke-test this module.")
    else:
        ticker = COUNTRIES[DEFAULT_COUNTRY]["index_tickers"]["sp500"]
        rows = FMPClient(key).historical_eod(ticker)
        records = backfill_index_history(rows, CHART_BACKFILL_DAYS)
        print(f"{len(records)} records for {ticker}")
        print(json.dumps(records[-1], indent=2))
