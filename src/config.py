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
SECTOR_CACHE_PATH = os.path.join(CACHE_DIR, "sector_window.json")

# Rolling internal calc-buffer windows (NOT chart history — see cache.py docstring)
PRICE_WINDOW_DAYS = 280      # covers 252-trading-day 52w hi/lo + 20d moves with margin
SECTOR_WINDOW_DAYS = 25      # only need trailing 20 trading days + margin

# Breadth internals thresholds
NEW_HIGH_LOW_WINDOW = 252    # ~52 trading weeks
PCT_MOVE_LOOKBACK_DAYS = 5
PCT_MOVE_THRESHOLDS = [20, 30]  # "% up/down 20%+ / 30%+ in the last 5 days"

FMP_SECTORS = [
    "Basic Materials", "Communication Services", "Consumer Cyclical",
    "Consumer Defensive", "Energy", "Financial Services", "Healthcare",
    "Industrials", "Real Estate", "Technology", "Utilities",
]

COUNTRIES = {
    "US": {
        "label": "United States",
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "index_tickers": {
            "nasdaq": "^IXIC",
            "sp500": "^GSPC",
            "russell2000": "^RUT",
        },
        "sectors": FMP_SECTORS,
    },
}

DEFAULT_COUNTRY = "US"
