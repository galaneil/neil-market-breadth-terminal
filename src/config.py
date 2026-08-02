"""
config.py — single source of truth for paths, thresholds, and per-country setup.

Two countries are wired up: US (prices from FMP) and India (prices from Yahoo
via yfinance). Everything downstream of the price fetch — cache.py, store.py,
metrics/*, render.py — is country-agnostic and works off the COUNTRIES entry it
is handed, so adding a third market means adding a dict entry and a price
source, not touching the metrics or rendering logic.

Path layout, and why it is asymmetric:

    data/us/*.jsonl        docs/panel-*.html      docs/tickers/
    data/in/*.jsonl        docs/in/panel-*.html   docs/in/tickers/

Data directories are symmetric. The published docs paths are NOT: the US pages
stay at the root because those URLs are already embedded in Notion, and moving
them to docs/us/ would silently break every existing embed. India is nested
under docs/in/ instead. `docs_subdir` carries that difference so no other
module has to know about it.
"""

import os

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")

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

# Environment thresholds (metrics/environment.py).
# Trend: of the index-vs-EMA factors, how many must be favourable. Expressed as
# a FRACTION of the total rather than a raw count, because the number of
# factors depends on how many indices a country tracks (3 US indices x 3 EMAs =
# 9 factors; 4 Indian indices x 3 = 12).
TREND_BULL_FRACTION = 7 / 9
TREND_BEAR_FRACTION = 2 / 9
# Participation: % of sectors/industries with a positive 20-day return.
PARTICIPATION_BULL_MIN = 65
PARTICIPATION_BEAR_MAX = 35
# Breadth internals are averaged over this many sessions before being read,
# since a single day's net figure whipsaws too much to describe a regime.
INTERNALS_LOOKBACK_DAYS = 10

# Leaders/laggards: how many to list each way, and which stored return window
# each label maps to. Deliberately no 1-day window — a single session says
# nothing about which groups are actually gaining or losing traction.
TOP_MOVERS_COUNT = 3
MOVER_WINDOWS = {"1w": "chg_5d", "1m": "chg_20d"}

# Names to track for the stock-context page beyond the index breadth universe.
# Neil trades plenty of names that aren't index members (CRDO, ALAB, AXTI, AAOI
# and friends). Largely superseded now that every classified ticker gets a
# price file, but kept so these are guaranteed present in the price cache.
WATCHLIST = [
    "AAOI", "AEIS", "ALAB", "ALKS", "APLS", "ARM", "ARWR", "AXTI", "BE", "BEAM",
    "CDE", "COHR", "CRDO", "CRH", "CW", "DOCN", "DRAM", "FLEX", "FN", "FTI",
    "GEV", "IESC", "IONQ", "LITE", "MDGL", "MOD", "MPWR", "MTZ", "NVT", "NXT",
    "POWL", "RVMD", "SATS", "SITM", "SMMT", "SNDK", "STRL", "TGTX", "TTMI",
    "TVTX", "TWST", "VIAV", "VICR", "VRT", "VST", "XPO",
]

# Per-ticker price files published for the stock-context page — one for every
# name TradingView classifies, so any traded ticker can be looked up rather
# than only a curated list. These are regenerated in full on every run and
# published to the gh-pages branch, which is replaced wholesale each time;
# committing thousands of daily-rewritten files into main would grow the repo
# by tens of megabytes a day and never release it.
TICKER_DIR_NAME = "tickers"
TICKER_HISTORY_DAYS = 252

COUNTRIES = {
    "US": {
        "label": "United States",
        "short": "US",
        "data_subdir": "us",
        "docs_subdir": "",          # root — protects the live Notion embed URLs
        "price_source": "fmp",
        "tv_market": "america",
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "min_market_cap": 300e6,
        "universe_limit": 5000,
        "index_tickers": {
            "nasdaq": "^IXIC",
            "sp500": "^GSPC",
            "russell2000": "^RUT",
        },
        "index_labels": {
            "nasdaq": "NASDAQ Composite",
            "sp500": "S&P 500",
            "russell2000": "Russell 2000",
        },
        # The index whose trading days define the calendar every other series
        # is aligned to.
        "calendar_index": "sp500",
        # Benchmark for the RS rating: both the trading calendar the ranking
        # happens on and the index the RS line is measured against.
        "rs_benchmark": "^IXIC",
        # The broad/large-cap indices, used for the "large caps only" read in
        # the environment summary (i.e. the same call with small caps excluded).
        "largecap_keys": ["nasdaq", "sp500"],
        # US cash session closes at 20:00 UTC under EDT and 21:00 UTC under EST.
        # Before this cutoff, today's daily bar is still forming and both FMP's
        # historical endpoint and its live quote hand back an in-progress value
        # that looks exactly like a finished session.
        "session_final_after_utc": (21, 30),
        # TMLE runs here only. Its fundamental factors depend on FMP income
        # statements, which have no India coverage on this plan, so scoring
        # India would silently mean a different (price-only) engine wearing the
        # same name. US first; it can scale once it has earned it.
        "run_tmle": True,
    },
    "IN": {
        "label": "India",
        "short": "IN",
        "data_subdir": "in",
        "docs_subdir": "in",
        "price_source": "yahoo",
        "tv_market": "india",
        "exchanges": ["NSE"],
        # India is capped by COUNT rather than by market cap: below the top 1000
        # (~Rs 3,200 crore) the tail thins into names that barely trade, and
        # illiquid constituents distort breadth counts far more than they add
        # coverage. 2,934 NSE names carry a market cap; we take the largest 1000.
        "min_market_cap": 0,
        "universe_limit": 1000,
        "index_tickers": {
            "sensex": "^BSESN",
            "nifty500": "^CRSLDX",
            "niftymidcap150": "NIFTYMIDCAP150.NS",
            "niftysmallcap250": "NIFTYSMLCAP250.NS",
        },
        "index_labels": {
            "sensex": "BSE Sensex",
            "nifty500": "Nifty 500",
            "niftymidcap150": "Nifty Midcap 150",
            "niftysmallcap250": "Nifty Smallcap 250",
        },
        "calendar_index": "nifty500",
        "rs_benchmark": "^CRSLDX",
        "largecap_keys": ["sensex", "nifty500"],
        # NSE/BSE close at 15:30 IST = 10:00 UTC year-round (India observes no
        # daylight saving), so a 10:30 UTC cutoff clears the bell with margin.
        "session_final_after_utc": (10, 30),
        "run_tmle": False,
    },
}

DEFAULT_COUNTRY = "US"


def data_dir(country):
    return os.path.join(DATA_DIR, COUNTRIES[country]["data_subdir"])


def cache_dir(country):
    return os.path.join(data_dir(country), "_cache")


def price_cache_path(country):
    return os.path.join(cache_dir(country), "price_window.json")


def docs_dir(country):
    sub = COUNTRIES[country]["docs_subdir"]
    return os.path.join(DOCS_DIR, sub) if sub else DOCS_DIR


def ticker_dir(country):
    return os.path.join(docs_dir(country), TICKER_DIR_NAME)


def trend_thresholds(n_factors):
    """Bull/bear factor counts scaled to however many index-vs-EMA factors this
    country actually has, so a 4-index market isn't held to a 3-index bar."""
    return (
        int(round(TREND_BULL_FRACTION * n_factors)),
        int(round(TREND_BEAR_FRACTION * n_factors)),
    )

# ── Screener index scopes ──────────────────────────────────────────────────
# The screener only ever looks at real index constituents. Screening the whole
# listed tape buries the names worth finding under shells and microcaps, and
# Neil does not trade that paper. Values are TradingView's own index names,
# matched exactly against the `indexes` field.
SCREENER_INDEXES = {
    "US": [
        ("SPX", "S&P 500", "S&P 500"),
        ("NDX", "Nasdaq 100", "NASDAQ 100"),
        ("RUT", "Russell 2000", "Russell 2000"),
    ],
    "IN": [
        ("N100", "Nifty 100", "Nifty 100"),
        ("NMID", "Midcap 150", "Nifty MidCap 150"),
        ("NSML", "Smallcap 250", "Nifty SmallCap 250"),
        ("N500", "Nifty 500", "Nifty 500"),
    ],
}

# ── ADR (Average Daily Range) ──────────────────────────────────────────────
# Mean of (high/low - 1) over this many sessions, as a percent. It answers a
# question a percentage move cannot: how much room a name gives you intraday.
# A 1% ADR stock making new highs is not tradeable the way a 6% ADR one is.
ADR_WINDOW = 20
