"""
fmp_client.py — thin wrapper around Financial Modeling Prep's /stable/ API.

Only the endpoints this project actually needs:
  - quote (current price/change, ONE symbol per call)
  - historical daily EOD price (for EMA calc + index/breadth/sector/industry history)

NOTE on batching: FMP's /stable/batch-quote, /stable/batch-quote-short, and even
/stable/quote?symbol=a,b,c (comma-joined) all return HTTP 402 "Restricted Endpoint" /
"Premium Query Parameter" under the Starter plan (confirmed by live testing) — batch
quoting is a higher-tier feature. Single-symbol /stable/quote?symbol=X works fine, so
quote_many() below just loops one call per symbol, paced under the 300 req/min Starter
rate limit. For ~1500 S&P1500 tickers this takes roughly 5 minutes, which is fine for a
once-daily scheduled run.
"""

import time
import requests

BASE_URL = "https://financialmodelingprep.com/stable"
MIN_SECONDS_PER_REQUEST = 0.21  # keeps us under 300 req/min with margin


class FMPClient:
    def __init__(self, api_key, session=None, retries=3, backoff=1.5):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.retries = retries
        self.backoff = backoff
        self._last_request_time = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < MIN_SECONDS_PER_REQUEST:
            time.sleep(MIN_SECONDS_PER_REQUEST - elapsed)
        self._last_request_time = time.monotonic()

    def _get(self, endpoint, params=None):
        params = dict(params or {})
        params["apikey"] = self.api_key
        url = f"{BASE_URL}/{endpoint}"

        last_exc = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self.backoff * (attempt + 1))
        raise RuntimeError(f"FMP request failed for {endpoint}: {last_exc}") from last_exc

    def quote_one(self, symbol):
        """Current quote (price, change, changePercentage, ...) for a single symbol.
        Returns None if FMP has no quote for it (delisted/invalid)."""
        data = self._get("quote", {"symbol": symbol})
        return data[0] if data else None

    def quote_many(self, symbols, on_progress=None):
        """Current quotes for a list of symbols, one request per symbol (see module
        docstring — batch endpoints are plan-gated). Skips symbols with no quote."""
        out, failed = [], 0
        for i, sym in enumerate(symbols):
            # Third place the same class of symbol broke a run: share-class
            # tickers (AGM.A, BRK.B, BF.A ...) 402 on this plan. Any per-symbol
            # loop against FMP has to treat one failure as a skip, never as an
            # abort — the backfill and index fetch already learned this.
            try:
                q = self.quote_one(sym)
            except Exception:
                failed += 1
                continue
            if q:
                out.append(q)
            if on_progress and (i + 1) % 100 == 0:
                on_progress(i + 1, len(symbols))
        if failed and on_progress:
            on_progress(len(symbols), len(symbols))
        self.last_quote_failures = failed
        return out

    def income_statement_quarterly(self, symbol, limit=8):
        """Quarterly income statements, newest first. Feeds TMLE's F2B, which
        needs a run of quarters to judge triple-digit growth and acceleration —
        TradingView only exposes TTM and the latest single quarter."""
        data = self._get("income-statement",
                         {"symbol": symbol, "period": "quarter", "limit": limit})
        return data or []

    def analyst_estimates(self, symbol, limit=6):
        """Annual consensus revenue/EPS estimates, for TMLE's forward-growth
        factor. This is the only LEADING input in the engine — every other
        fundamental is reported history, which by construction cannot tell you
        what a theme looked like before it moved."""
        data = self._get("analyst-estimates",
                         {"symbol": symbol, "period": "annual", "limit": limit})
        return data or []

    def historical_eod(self, symbol, start=None, end=None):
        """Full daily OHLCV history for one symbol. Returns list of dicts, newest first."""
        params = {"symbol": symbol}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        data = self._get("historical-price-eod/full", params)
        return data or []


if __name__ == "__main__":
    import os
    import sys

    key = os.environ.get("FMP_API_KEY")
    if not key:
        print("Set FMP_API_KEY to smoke-test this module.")
        sys.exit(1)

    client = FMPClient(key)

    print("-- quote_many(['AAPL', 'MSFT', '^GSPC']) --")
    print(client.quote_many(["AAPL", "MSFT", "^GSPC"]))

    print("\n-- historical_eod('^GSPC') last 3 rows --")
    hist = client.historical_eod("^GSPC")
    print(hist[:3])
