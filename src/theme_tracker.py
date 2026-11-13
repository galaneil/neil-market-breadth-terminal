"""
theme_tracker.py — a curated, hand-picked basket of sector/thematic/country/
equal-weight ETFs, tracked the way Ariel Hernandez's "Market Pulse" actually
does it: there's no classification rule behind the list, it's picked by
hand (confirmed by reading the real open-source Pine Script -- "Theme
Tracker" by Amphibiantrading -- behind that tool). Trying to auto-derive a
"thematic" list from FMP's full ~7,200-ETF universe by keyword/exclusion
rules was tried and abandoned in favor of this: a small, static, editable
list, same as the tool being mirrored.

Performance is computed from FMP's own daily EOD history -- the same
source and math the rest of this pipeline already uses for indices. No
live intraday feed here (yet); see THEME_TRACKER_LIVE_TODO if that gets
built later.
"""

import io
import json
import os
import time
import urllib.request

import openpyxl

import config
from fmp_client import FMPClient

CACHE_PATH = os.path.join(config.ROOT_DIR, "data", "us", "_cache", "theme_tracker.json")
CACHE_TTL_SECONDS = 6 * 3600  # a few refreshes a day is plenty for EOD-based numbers

# (category, display name, ticker, TradingView logoid-or-None)
# Logoids verified against TradingView's own symbol-search endpoint, not
# guessed -- an ETF mostly gets a generic sector icon or its issuer's mark
# since TradingView doesn't keep a unique image per fund the way it does
# per company. None here just falls back to a colored initials chip.
THEMES = [
    ("theme", "AI", "BOTZ", None),
    ("theme", "Biotech", "XBI", "sector/biotech-etf"),
    ("theme", "Bitcoin Miners", "WGMI", "coinshares"),
    ("theme", "China Internet", "KWEB", "kraneshares"),
    ("theme", "Cybersecurity", "HACK", "amplify"),
    ("theme", "Cybersecurity (2)", "CIBR", "first-trust"),
    ("theme", "Aerospace & Defense", "ITA", "ishares"),
    ("theme", "Airlines", "JETS", "u-s-global-investors"),
    ("theme", "Genomics", "ARKG", "ark-etf-tr"),
    ("theme", "Gold Miners", "GDX", "vaneck"),
    ("theme", "Growth Stocks", "VUG", "vanguard"),
    ("theme", "Homebuilders", "XHB", "spdr-sandp500-etf-tr"),
    ("theme", "Medical Devices", "IHI", "ishares"),
    ("theme", "Quantum", "QTUM", "differential-series-solutions"),
    ("theme", "Retail", "XRT", "spdr-sandp500-etf-tr"),
    ("theme", "Robotics", "ROBO", "robo-global-artificial"),
    ("theme", "Semiconductors", "SMH", "vaneck"),
    ("theme", "Silver Miners", "SIL", "metal/silver"),
    ("theme", "Social Media", "SOCL", "global-x"),
    ("theme", "Software", "IGV", "ishares"),
    ("theme", "Steel", "SLX", "vaneck"),
    ("theme", "Telecom", "IYZ", "ishares"),
    ("theme", "Transports", "IYT", "ishares"),
    ("theme", "Long Term Treasuries", "TLT", "ishares"),
    ("theme", "Regional Banks", "KRE", "spdr-sandp500-etf-tr"),
    ("theme", "Uranium / Nuclear", "URA", "global-x"),
    ("theme", "Copper Miners", "COPX", "global-x"),
    ("theme", "Lithium / Battery", "LIT", "global-x"),
    ("theme", "Cloud Computing", "SKYY", "first-trust"),
    ("theme", "Space", "ARKX", "ark-etf-tr"),
    ("theme", "Rare Earth Metals", "REMX", "vaneck"),
    ("theme", "Lithography & EUV", "EUV", "corgi-funds"),
    ("theme", "Pure-Play Photonics", "FOTO", None),
    ("theme", "Tanker Shipping", "BWET", "amplify"),
    ("theme", "Memory / DRAM", "DRAM", "roundhmetave-aeoa"),
    ("sectors", "Technology", "XLK", "sector/technology"),
    ("sectors", "Financials", "XLF", "sector/financial"),
    ("sectors", "Communication Services", "XLC", "communication-services"),
    ("sectors", "Energy", "XLE", "sector/energy"),
    ("sectors", "Health Care", "XLV", "sector/health-care"),
    ("sectors", "Industrials", "XLI", "sector/industrial"),
    ("sectors", "Materials", "XLB", "sector/materials"),
    ("sectors", "Consumer Discretionary", "XLY", "sector/consumer-discretionary"),
    ("sectors", "Consumer Staples", "XLP", "sector/consumer-staples"),
    ("sectors", "Utilities", "XLU", "sector/utilities"),
    ("sectors", "Real Estate", "XLRE", "sector/real-estate"),
    ("country", "Emerging Markets", "EEM", "ishares"),
    ("country", "India", "INDA", "ishares"),
    ("country", "Taiwan", "EWT", "ishares"),
    ("country", "Japan", "EWJ", "ishares"),
    ("country", "South Korea", "EWY", "ishares"),
    ("country", "Brazil", "EWZ", "ishares"),
    ("country", "Germany", "EWG", "ishares"),
    ("equal", "Technology (EW)", "RSPT", "invesco"),
    ("equal", "Energy (EW)", "RSPG", "invesco"),
    ("equal", "Financials (EW)", "RSPF", "invesco"),
    ("equal", "Health Care (EW)", "RSPH", "invesco"),
    ("equal", "Industrials (EW)", "RSPN", "invesco"),
    ("equal", "Materials (EW)", "RSPM", "invesco"),
]


def _api_key():
    key = os.environ.get("FMP_API_KEY")
    if key:
        return key
    path = os.path.join(config.ROOT_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("FMP_API_KEY"):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return None


def _pct_between(closes, end, bars_back):
    start = end - bars_back
    if start < 0 or closes[start] in (None, 0):
        return None
    return (closes[end] / closes[start] - 1) * 100


def _ytd_bars(dates, end):
    year = dates[end][:4]
    for i, d in enumerate(dates):
        if d[:4] == year:
            return end - i
    return end


def _row_from_history(category, name, ticker, logoid, hist):
    hist = sorted(hist, key=lambda r: r.get("date") or "")
    closes = [r.get("close") for r in hist]
    dates = [r.get("date") for r in hist]
    if not closes or closes[-1] is None:
        return None
    end = len(closes) - 1
    return {
        "category": category, "name": name, "ticker": ticker, "logoid": logoid,
        "price": closes[end],
        "today": _pct_between(closes, end, 1),
        "w1": _pct_between(closes, end, 5),
        "m1": _pct_between(closes, end, 21),
        "m3": _pct_between(closes, end, 63),
        "ytd": _pct_between(closes, end, _ytd_bars(dates, end)),
    }


def fetch_all(log=print):
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("FMP_API_KEY is not set")
    client = FMPClient(api_key)
    rows = []
    for category, name, ticker, logoid in THEMES:
        try:
            hist = client.historical_eod(ticker)
        except Exception as error:
            log(f"  theme tracker: {ticker} failed: {error}")
            continue
        row = _row_from_history(category, name, ticker, logoid, hist) if hist else None
        if row:
            rows.append(row)
        else:
            log(f"  theme tracker: {ticker} has no history yet")
    return rows


def get_data(log=print, force=False):
    """Cached rows for every THEMES entry -- a full refetch is one FMP call
    per ticker (~53), so this is deliberately not run on every page load."""
    if not force and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("fetchedAt", 0) < CACHE_TTL_SECONDS:
                return cached["rows"]
        except Exception:
            pass
    rows = fetch_all(log=log)
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetchedAt": time.time(), "rows": rows}, f)
    return rows


def live_quote(ticker, log=print):
    """Real current price/change for one ticker, on demand -- only ever
    called while a detail panel for that ticker is actually open, not on a
    clock, so this stays a handful of calls rather than a standing poll."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("FMP_API_KEY is not set")
    client = FMPClient(api_key)
    q = client.quote_one(ticker)
    if not q:
        raise RuntimeError(f"no quote for {ticker}")
    return {
        "ticker": ticker, "price": q.get("price"), "change": q.get("change"),
        "changePercentage": q.get("changePercentage"), "dayLow": q.get("dayLow"),
        "dayHigh": q.get("dayHigh"), "open": q.get("open"),
        "previousClose": q.get("previousClose"), "volume": q.get("volume"),
        "timestamp": q.get("timestamp"),
    }


# Every SPDR sector fund publishes its full holdings as a free, no-auth
# XLSX at this same URL shape, one issuer format covering all 11 sectors --
# verified live against XLK (22.9KB, real weights, no login needed). This
# is the first (and so far only) issuer wired up; other issuers use their
# own formats and get added one at a time as they're worth tracking, per
# the plan discussed with Neil -- see HOLDINGS_FETCHERS below.
SPDR_TICKERS = {"XLK", "XLF", "XLC", "XLE", "XLV", "XLI", "XLB", "XLY", "XLP", "XLU", "XLRE"}
SPDR_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
            "fund-data/etfs/us/holdings-daily-us-en-{ticker}.xlsx")

HOLDINGS_CACHE_DIR = os.path.join(config.ROOT_DIR, "data", "us", "_cache", "theme_holdings")
HOLDINGS_CACHE_TTL_SECONDS = 20 * 3600  # holdings shift slowly; once a day is plenty


def _spdr_holdings(ticker, log=print):
    url = SPDR_URL.format(ticker=ticker.lower())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = response.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    # Header row ("Name", "Ticker", ...) location varies a little release to
    # release, so it's found by content rather than assumed to be row 5.
    header_idx = next((i for i, r in enumerate(rows)
                        if r and r[0] == "Name" and r[1] == "Ticker"), None)
    if header_idx is None:
        raise RuntimeError(f"unrecognized SPDR holdings format for {ticker}")
    out = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0] or not row[1]:
            continue
        name, sym, weight = row[0], row[1], row[4]
        if sym in ("-", "Cash", "CASH") or weight is None:
            continue
        out.append({"sym": sym, "name": str(name).title(), "weight": float(weight)})
    return sorted(out, key=lambda r: -r["weight"])[:15]


# ticker -> (issuer function, cache key) -- add an issuer here once it has
# a fetcher; anything not listed just reports "not available yet" rather
# than guessing.
HOLDINGS_FETCHERS = {t: _spdr_holdings for t in SPDR_TICKERS}


def holdings_for(ticker, log=print, force=False):
    """Top constituents for one ticker, or None if no issuer fetcher exists
    for it yet. Cached per-ticker on disk since a holdings file is a
    several-KB download that changes at most once a day."""
    fetcher = HOLDINGS_FETCHERS.get(ticker.upper())
    if not fetcher:
        return None
    cache_path = os.path.join(HOLDINGS_CACHE_DIR, ticker.upper() + ".json")
    if not force and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("fetchedAt", 0) < HOLDINGS_CACHE_TTL_SECONDS:
                return cached["rows"]
        except Exception:
            pass
    rows = fetcher(ticker, log=log)
    os.makedirs(HOLDINGS_CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"fetchedAt": time.time(), "rows": rows}, f)
    return rows


def _ema_series(values, period):
    """Plain EMA over a list, None carried through as a hold rather than a
    reset. Same definition render.py's _ema_series / dashboard.js's
    emaSeries() use, so a 10/20/50 line here means the same thing it does
    on every other chart in this app."""
    k = 2 / (period + 1)
    out = []
    prev = None
    for v in values:
        if v is None:
            out.append(prev)
            continue
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def history_for(ticker, log=print):
    """On-demand daily OHLC + 10/20/50 EMA for one ticker's detail chart --
    not cached with the rest, since only the ticker someone actually
    clicks needs it."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("FMP_API_KEY is not set")
    client = FMPClient(api_key)
    hist = client.historical_eod(ticker)
    hist = sorted(hist, key=lambda r: r.get("date") or "")
    hist = [r for r in hist if r.get("date") and r.get("close") is not None]
    closes = [r["close"] for r in hist]
    ema10 = _ema_series(closes, 10)
    ema20 = _ema_series(closes, 20)
    ema50 = _ema_series(closes, 50)
    return [{"time": r["date"], "open": r.get("open"), "high": r.get("high"),
              "low": r.get("low"), "close": r["close"],
              "ema10": ema10[i], "ema20": ema20[i], "ema50": ema50[i]}
             for i, r in enumerate(hist)]
