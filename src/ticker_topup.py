"""Keep price history for the names you actually trade.

The nightly refresh only updates tickers in the market-breadth universe (the classified names). A stock you traded
that is outside it (ARM, for one) kept its old file, so the Trade Journal had no bars for the trade and drew no chart.
This tops a missing or stale ticker file up from Yahoo, on demand, the first time something asks for it.
"""
import json
import os
import threading
import time
from datetime import date, datetime

import config
import store

_LOCK = threading.Lock()
_TRIED = {}                       # (country, symbol) -> time of the last attempt, so a bad symbol is not retried every click
RETRY_SECONDS = 3 * 3600
STALE_DAYS = 4                    # newer than this counts as fresh (weekends and holidays)


def _last_date(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["dates"][-1]
    except Exception:
        return None


def ensure_fresh(country, symbol, log=print):
    """Returns True when the ticker file is current (already, or after a top-up)."""
    symbol = "".join(ch for ch in (symbol or "").upper() if ch.isalnum() or ch in ".-")[:16]
    if not symbol:
        return False
    path = os.path.join(config.ticker_dir(country), symbol + ".json")
    last = _last_date(path)
    if last and (date.today() - datetime.strptime(last, "%Y-%m-%d").date()).days <= STALE_DAYS:
        return True
    key = (country, symbol)
    with _LOCK:
        if time.time() - _TRIED.get(key, 0) < RETRY_SECONDS:
            return False
        _TRIED[key] = time.time()
    try:
        import yfinance as yf
        import yf_client
        ysym = symbol + ".NS" if country == "IN" else symbol
        df = yf.download(ysym, period="2y", auto_adjust=True, progress=False)
        if hasattr(df.columns, "levels"):            # newer yfinance returns a (field, ticker) column index even for one name
            df.columns = [c[0] for c in df.columns]
        rows = yf_client._frame_to_rows(df.dropna(how="all"))
        if not rows:
            log(f"  ticker top-up: nothing from Yahoo for {ysym}")
            return False
        store.write_ticker_ohlc(country, symbol, rows)
        log(f"  ticker top-up: {symbol} refreshed through {rows[0]['date']} (was {last})")
        return True
    except Exception as error:
        log(f"  ticker top-up failed for {symbol}: {error}")
        return False
