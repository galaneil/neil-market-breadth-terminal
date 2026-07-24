"""
config.py — single source of truth for paths, thresholds, and per-country setup.

Only "US" is populated today. Adding a second country (e.g. India: Nifty 50 /
Midcap / Smallcap) later means adding an entry to COUNTRIES with its own index
tickers/universe source, plus an NSE equivalent for the FMP-specific calls in
metrics/* — no changes to cache.py, store.py, or render.py's storage/rendering
logic, which are all country-agnostic.
"""

import os

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
CACHE_DIR = os.path.join(DATA_DIR, "_cache")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")

PRICE_CACHE_PATH = os.path.join(CACHE_DIR, "price_window.json")

# Rolling internal calc-buffer window (NOT chart history — see cache.py docstring).
# Sized as CHART_BACKFILL_DAYS + NEW_HIGH_LOW_WINDOW so that even the OLDEST day
# we backfill into the charts still has a full, accurate trailing 252-day lookback
# for the 52-week-high/low calc — not just the most recent ~28 days of it.
CHART_BACKFILL_DAYS = 252    # ~1 trading year of real chart history (1W..6M/YTD all work)
NEW_HIGH_LOW_WINDOW = 252    # ~52 trading weeks
PRICE_WINDOW_DAYS = CHART_BACKFILL_DAYS + NEW_HIGH_LOW_WINDOW + 20  # + small margin

# Breadth internals thresholds
PCT_MOVE_LOOKBACK_DAYS = 5
PCT_MOVE_THRESHOLDS = [20, 30]  # "% up/down 20%+ / 30%+ in the last 5 days"

# Sector/industry performance lookback windows (in trading days) reported per group
GROUP_CHG_WINDOWS = {"chg_1d": 1, "chg_5d": 5, "chg_20d": 20}
MIN_GROUP_MEMBERS = 5  # drop sectors/industries too small to be a meaningful bucket

COUNTRIES = {
    "US": {
        "label": "United States",
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "index_tickers": {
            "nasdaq": "^IXIC",
            "sp500": "^GSPC",
            "russell2000": "^RUT",
        },
    },
}

DEFAULT_COUNTRY = "US"
