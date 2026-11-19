"""
portfolio_server.py — a live portfolio page, served to yourself only.

HOW THIS IS EMBEDDABLE IN NOTION WITHOUT BEING HOSTED
---------------------------------------------------------------------------
A Notion embed is an iframe, and the iframe is rendered by YOUR browser. So a
page served from 127.0.0.1 resolves to this machine: Notion's servers never
fetch it, and anyone else opening the same Notion page sees an empty box. That
is the whole security argument — the data is embedded without being published.

Chrome treats http://localhost as a trustworthy origin, so it is exempt from
the mixed-content blocking that would otherwise stop an http iframe inside an
https page. That exemption is what makes this work at all.

The server binds 127.0.0.1 explicitly, NOT 0.0.0.0. On a cafe network the
difference is whether the room can read your positions.

WHY THERE IS AN INTRADAY LOG
---------------------------------------------------------------------------
No broker will sell you a historical intraday equity curve. Flex reports one
NAV per day; SmartAPI has no NAV history endpoint at all. So a 1D curve can
only exist if something samples NAV while the market is open and keeps it.
That is what nav_log() does, and it is why 1D starts working the day you
begin running this rather than retroactively.

Longer timeframes come from the broker's own daily NAV where it has one
(IBKR), and from the accumulated samples where it does not (Angel One).

Usage:
    python src/portfolio_server.py                 # IBKR only
    python src/portfolio_server.py --angel         # prompts for PIN + TOTP
"""

import base64
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import broker_api
import flags

HOST, PORT = "127.0.0.1", 8787

# The market breadth terminal is also published to GitHub Pages and stays
# published — Notion embeds still point at those URLs and nothing here should
# break them. This just serves the SAME files locally too, so the hub can show
# them in a tab without a second copy or a second build step.
DOCS_ROOT = config.DOCS_DIR

# The sidebar's contents. A plain {"label", "path"} entry is one panel. A
# {"label", "children": [...]} entry is several related panels folded under
# one sidebar row — sectors/industries are the same question (which groups
# are leading) at two granularities, and advance-decline/new-hi-lo are both
# breadth internals read together, so each pair reads as one topic rather
# than two competing sidebar rows. Selecting the row shows a small in-content
# toggle for its children plus a one-line note on what each one is, rather
# than expanding the sidebar itself.
# Three separate sidebar sections, each its own labelled list (NOT the
# "children" grouping mechanism used below for Sector & Industry / Indices --
# that mechanism stacks every child on one page when the group itself is
# clicked, which is right for a handful of tightly-related sub-views but
# wrong for a whole category of otherwise-unrelated panels. Market Breadth /
# Signals / Algorithms are plain flat lists under their own header, same as
# Portfolio and System already are.
HUB_PANELS = [
    # Native: the landing page. Today's verdict, what needs you, your watchlist, what's new in Signals.
    {"label": "Home", "native": "/today"},
    # Native hub page (not a docs/ page): it shows the user's own book next to
    # the market data, which must never be published to GitHub Pages.
    {"label": "Market Environment", "native": "/home"},
    # Each country tracks a different set of indices (US: 3, India: 4), so
    # this entry's children are NOT listed here — they are built per-country
    # in _hub_nav_json() from config.COUNTRIES, which is the one place that
    # already knows what each market's indices are.
    {"label": "Indices", "dynamic": "indices"},
    {"label": "Sector & Industry", "children": [
        {"label": "Sectors", "path": "panel-sectors.html",
         "note": "Broad GICS-style sector groups."},
        {"label": "Industries", "path": "panel-industries.html",
         "note": "TradingView's finer-grained industry groups — more names, "
                 "narrower categories."},
        {"label": "Lookup", "path": "panel-sector-lookup.html",
         "note": "Search one sector or industry — its own rank history and "
                 "the member stocks actually driving it."},
    ]},
    {"label": "Money Flows", "path": "panel-groups.html"},
    # Used to be a group of two raw-counts pages with no read on what they
    # meant. Now a single page with a regime badge and a verdict computed
    # over a window you pick, backed by the same two series. The individual
    # pages still render (reachable by direct URL) in case either is
    # embedded elsewhere; only the hub's own nav points at the unified one.
    {"label": "Breadth Internals", "path": "panel-breadth-internals.html"},
    # "Hi/Lo Counts & Screener" used to sit here too — its counts-over-time
    # card is redundant now: Money Flows shows the sector/industry breakdown
    # of the same counts, and Screener already covers the ticker-level view.
    # The page itself (panel-breadth-hilo-counts.html) is left rendering and
    # reachable by direct URL, since it may still be embedded in Notion —
    # this only removes it from the hub's own navigation.
    {"label": "Screener", "path": "panel-screener.html"},
    # Native (hub-only): scans the local price files, so it is not published.
    {"label": "Movers", "native": "/movers"},
    # Native (hub-only): big historical up-moves with entry points, for building the setups list by hand.
    {"label": "Setup finder", "native": "/setup-finder"},
    {"label": "Market Replay", "path": "panel-replay.html"},
    {"label": "Stock Lookup", "path": "panel-stock.html"},
]

SIGNALS_PANELS = [
    # Native: same data as the published panel-signals.html (read from it), laid out like the Setups page.
    {"label": "Signals", "native": "/signals"},
    {"label": "Watchlist", "path": "panel-watchlist.html"},
    {"label": "Feedback Log", "path": "panel-feedback-log.html"},
]

ALGORITHMS_PANELS = [
    {"label": "TMLE Leaders", "path": "panel-tmle-leaders.html"},
    {"label": "TMLE Emerging", "path": "panel-tmle-emerging.html"},
]

# A separate group from HUB_PANELS, rendered in its own sidebar section below
# Portfolio rather than mixed into Market Breadth — it's reference material
# about the system itself, not a data panel someone browses day to day.
SYSTEM_PANELS = [
    {"label": "System Architecture", "path": "panel-architecture.html"},
    {"label": "Data Freshness", "path": "panel-freshness.html"},
]

MOVERS_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "movers_page.html")
JOURNAL_REVIEW_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_review.js")
SETUP_FINDER_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_finder_page.html")
HOME_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home_page.html")
OUTPUT_DIR = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
STOPS_FILE = os.path.join(OUTPUT_DIR, "stops.json")
# Stop price + reason a setup's backtest was run with. Kept locally (not in Notion)
# until Stop Price / Stop Reason fields exist on the Setups database.
SETUP_STOPS_FILE = os.path.join(OUTPUT_DIR, "setup_stops.json")
TODAY_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "today_page.html")
SIGNALS_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_page.html")
_SIGNALS_CACHE = {}


def _signals_data(code):
    """The Signals lists for one market, read from the published panel's embedded data (re-read when it changes)."""
    cfg = config.COUNTRIES.get(code) or config.COUNTRIES["US"]
    sub = cfg.get("docs_subdir", "")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", sub, "panel-signals.html") if sub \
        else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "panel-signals.html")
    mt = os.path.getmtime(path)
    hit = _SIGNALS_CACHE.get(code)
    if hit and hit[0] == mt:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>', html, re.S)
    d = json.loads(m.group(1))
    keys = ("signals", "earningsSignals", "cupSignals", "vcpSignals", "breakdownSignals")
    syms = {x.get("sym") for k in keys for x in (d.get(k) or [])}
    cl = d.get("classification") or {}
    prof = d.get("signalProfiles") or {}
    log = d.get("signalsLog") or []
    out = {k: d.get(k) or [] for k in keys}
    out.update({"country": code, "classification": {k: cl[k] for k in syms if k in cl},
                "profiles": {k: (prof.get(k) or {}).get("n") for k in syms if prof.get(k)},
                "scanDate": log[-1]["date"] if log else None,
                "tickerPrefix": "/docs/" + (sub + "/" if sub else "") + "tickers/"})
    _SIGNALS_CACHE[code] = (mt, out)
    return out


SETUPS_PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setups_page.html")
# The Setups page opens from this saved copy at once and then checks Notion in the background.
SETUPS_CACHE_FILE = os.path.join(OUTPUT_DIR, "setups_cache.json")
# Names waiting to be logged that do not come from the Setup Finder: added on the page, or synced from the
# TradingView list "Setups to log" when Neil asks Claude to. Skipped ids live here too.
SETUPS_QUEUE_FILE = os.path.join(OUTPUT_DIR, "setups_queue.json")
SYMBOL_NAMES_FILE = os.path.join(OUTPUT_DIR, "symbol_names.json")
# Sector, industry and logo for names the Setups page logs from outside the terminal's list (ARM, ASML, TSM...).
SETUPS_SYMBOLS_FILE = os.path.join(OUTPUT_DIR, "setups_symbols.json")
_SETUPS_LOCK = threading.Lock()


def _setup_stops():
    try:
        with open(SETUP_STOPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _json_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _json_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


_NAME_FILL = {"busy": False}


def _names_fill_async(pairs):
    """Company names for tickers the page has not seen yet, fetched one at a time in the background and kept on disk."""
    if _NAME_FILL["busy"] or not pairs:
        return
    _NAME_FILL["busy"] = True

    def run():
        try:
            import symbol_info
            for code, ticker in pairs[:60]:
                try:
                    name = symbol_info.info(code, ticker).get("name")
                except Exception:
                    name = None
                if name:
                    with _SETUPS_LOCK:
                        names = _json_read(SYMBOL_NAMES_FILE, {})
                        names[code + ":" + ticker.upper()] = name
                        _json_write(SYMBOL_NAMES_FILE, names)
                time.sleep(0.3)
        finally:
            _NAME_FILL["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def _decorate_setup_entry(entry, names=None, stops=None):
    """Logo, company name, any stop saved locally by the old page, and the numbers measured from prices."""
    import setup_context
    import setup_measure
    code = "IN" if entry.get("country") == "India" else "US"
    ticker = (entry.get("ticker") or "").upper()
    entry["logoid"] = setup_context.classify(code, ticker)[2] if ticker else None
    names = names if names is not None else _json_read(SYMBOL_NAMES_FILE, {})
    entry["name"] = names.get(code + ":" + ticker)
    if ticker and not entry["logoid"]:
        outside = _json_read(SETUPS_SYMBOLS_FILE, {}).get(code + ":" + ticker) or {}
        entry["logoid"] = outside.get("logoid")
        entry["name"] = entry["name"] or outside.get("name")
        entry["outside"] = bool(outside)
    local = (stops if stops is not None else _setup_stops()).get(entry.get("pageId")) or {}
    if entry.get("stop") is None and local.get("stop"):
        entry["stop"], entry["stopLocal"] = local["stop"], True
        entry["stopReason"] = entry.get("stopReason") or local.get("reason") or None
        if entry.get("entryPrice") is None and local.get("entryPrice"):
            entry["entryPrice"] = local["entryPrice"]
    entry["D"] = (setup_measure.derive(code, ticker, entry.get("buyDate"), entry.get("entryPrice"), entry.get("stop"))
                  if ticker and entry.get("buyDate") else None)
    return entry


def _setups_live():
    """Read every entry from Notion, add what the page needs, and keep a copy on disk for the next open."""
    import notion_sync
    import setup_context
    payload = notion_sync.fetch_setup_entries(log=log)
    if payload.get("error"):
        return payload
    names, stops, missing = _json_read(SYMBOL_NAMES_FILE, {}), _setup_stops(), []
    for entry in payload.get("entries", []):
        _decorate_setup_entry(entry, names, stops)
        if not entry.get("name") and entry.get("ticker"):
            missing.append(("IN" if entry.get("country") == "India" else "US", entry["ticker"].upper()))
    payload["envNow"] = {c: setup_context.market_env(c) for c in config.COUNTRIES}
    payload["cachedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _SETUPS_LOCK:
        _json_write(SETUPS_CACHE_FILE, payload)
    _names_fill_async(missing)
    return payload


def _setups_cache_edit(entry=None, remove=None, types=None):
    """Change one row of the saved copy after a save, so the next open shows it without a full Notion read."""
    with _SETUPS_LOCK:
        cache = _json_read(SETUPS_CACHE_FILE, None)
        if not cache:
            return
        rows = cache.get("entries", [])
        if remove:
            rows = [r for r in rows if r.get("pageId") != remove]
        if entry:
            rows = [r for r in rows if r.get("pageId") != entry.get("pageId")]
            rows.insert(0, entry)
        cache["entries"] = rows
        if types is not None:
            cache["types"] = types
        _json_write(SETUPS_CACHE_FILE, cache)


def _setups_queue(code):
    """Names waiting to be logged: Setup Finder rows marked keep and not saved yet, plus the listed names."""
    import setup_context
    import setup_measure
    q = _json_read(SETUPS_QUEUE_FILE, {"listed": [], "skipped": []})
    skipped = set(q.get("skipped") or [])
    items = []
    if code == "US":
        try:
            import setup_finder
            edits = setup_finder._read().get("edits", {})
        except Exception:
            edits = {}
        for key, e in edits.items():
            if e.get("status") != "keep" or e.get("savedPageId") or ("finder:" + key) in skipped:
                continue
            t, start = (key.split("|") + [""])[:2]
            entry = e.get("entry") or setup_measure.first_breakout_after("US", t, start)
            items.append({"id": "finder:" + key, "src": "finder", "t": t, "entry": entry, "entryPx": e.get("entryPx"),
                          "folder": e.get("setup") or None, "note": e.get("note") or None, "added": e.get("editedAt")})
    for it in q.get("listed") or []:
        if (it.get("country") or "US") != code or ("list:" + it.get("t", "")) in skipped:
            continue
        items.append({"id": "list:" + it["t"], "src": it.get("src") or "manual", "t": it["t"], "entry": it.get("entry"),
                      "folder": it.get("folder"), "list": it.get("list"), "added": it.get("added"), "note": it.get("note")})
    names = _json_read(SYMBOL_NAMES_FILE, {})
    for it in items:
        sec, ind, logo, _ = setup_context.classify(code, it["t"])
        it.update(sector=sec, industry=ind, logoid=logo, name=names.get(code + ":" + it["t"].upper()))
        it["D"] = setup_measure.derive(code, it["t"], it.get("entry"), it.get("entryPx"))
    return items

# ── Keeping the local checkout current, automatically ───────────────────────
#
# Everything the hub reads from disk falls into two kinds, and each one goes
# stale for a different reason:
#
#   data/*.jsonl, docs/*.html   git-tracked. The nightly Action always
#                               refreshes these on origin/main; they only sit
#                               stale HERE because nobody ran `git pull` on
#                               this particular checkout. A plain fast-forward
#                               pull fixes it, safely: it does nothing at all
#                               if history has diverged or local edits are in
#                               the way, rather than risking either.
#
#   docs/tickers/, docs/in/tickers/   deliberately gitignored — the nightly
#                               Action rewrites thousands of per-ticker files
#                               every run, which is not worth permanent git
#                               history. A git pull can never refresh these;
#                               only re-running the whole pipeline locally, or
#                               pulling the copy the Action already produced
#                               on gh-pages, does. That branch cannot be git-
#                               cloned on Windows at all — one ticker is named
#                               CON.json, a reserved device name at the
#                               filesystem level, not a git limitation — so
#                               this fetches the tarball and extracts it
#                               itself, skipping only that one file.
#
# Both run once at server startup and then on a repeating timer, so the hub
# stays current on its own instead of depending on someone noticing a stale
# date and asking for it to be fixed again.
GITHUB_REPO = "galaneil/neil-market-breadth-terminal"
SYNC_STATE_FILE = os.path.join(OUTPUT_DIR, "sync_state.json")
SYNC_LOOP_MINUTES = 30          # how often the loop wakes up to check
TICKER_SYNC_HOURS = 20          # how old ticker history must be to redo the
                                # ~1-minute, ~100MB download — a bit under a
                                # day, so it can never fall a full day behind
_RESERVED_WINDOWS_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.[^.]*)?$", re.IGNORECASE)
_TICKER_PREFIXES = {"tickers/": "tickers", "in/tickers/": os.path.join("in", "tickers")}


def _country_data_status(code):
    """{asOf, updatedAt, stale} for one country, read straight off the same
    file the pages themselves render from — no separate tracking to fall out
    of sync with reality. asOf is the latest date actually IN the data (what
    session it covers); updatedAt is when that file last changed on disk
    (when the refresh that produced it actually ran). staleDays counts
    calendar days between asOf and today, so the sidebar can flag "this is
    old" without needing to know either market's holiday calendar — a
    generous cutoff (see DATA_STALE_DAYS) absorbs ordinary weekends."""
    path = os.path.join(config.data_dir(code), "environment.jsonl")
    if not os.path.exists(path):
        return {"asOf": None, "updatedAt": None, "staleDays": None}
    as_of = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                as_of = json.loads(line).get("date")
    updated_at = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    stale_days = None
    if as_of:
        as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
        stale_days = (datetime.now(timezone.utc).date() - as_of_date).days
    return {
        "asOf": as_of,
        "updatedAt": updated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "staleDays": stale_days,
    }


# ── Data Freshness tab ───────────────────────────────────────────────────
#
# One row per data FILE, not per tab -- most tabs share a small number of
# upstream funnels (see each row's own "feeds" list below), so a per-tab
# table would just show the same staleness repeated under 5 different
# names. This groups by what would actually need re-fetching to fix it.
#
# Every funnel here traces back to one of three real fetches (price data,
# index data, TradingView classification) -- nothing is independently
# fetchable at finer granularity than "run this country's pipeline", so the
# one-click action is the same for every red row in a country: sync first
# (cheap, catches up if origin already has it), full refresh if that's not
# enough (slow, actually re-fetches).

def _jsonl_status(country, filename):
    """Same read _country_data_status does, generalized to any jsonl."""
    path = os.path.join(config.data_dir(country), filename)
    if not os.path.exists(path):
        return {"asOf": None, "staleDays": None}
    as_of = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                as_of = json.loads(line).get("date")
    stale_days = None
    if as_of:
        as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
        stale_days = (datetime.now(timezone.utc).date() - as_of_date).days
    return {"asOf": as_of, "staleDays": stale_days}


def _ticker_canary_status(country, canary_ticker):
    """Per-ticker files have no single date to check (there are ~1,000-3,500
    of them) -- one well-known, always-priced name stands in as a canary.
    Not a full picture (a handful of thin names lagging behind everything
    else, the actual shape of most incidents this week, wouldn't show up
    here) but catches the case that matters most: the whole file set never
    got refreshed at all."""
    path = os.path.join(config.ticker_dir(country), canary_ticker + ".json")
    if not os.path.exists(path):
        return {"asOf": None, "staleDays": None}
    try:
        with open(path, encoding="utf-8") as f:
            dates = json.load(f).get("dates") or []
    except (OSError, ValueError):
        return {"asOf": None, "staleDays": None}
    if not dates:
        return {"asOf": None, "staleDays": None}
    as_of = dates[-1]
    as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date()
    stale_days = (datetime.now(timezone.utc).date() - as_of_date).days
    return {"asOf": as_of, "staleDays": stale_days}


def _mtime_status(country, filename):
    """classification.json and similar have no per-row date at all -- file
    mtime against the same DATA_STALE_DAYS tolerance is the best available
    signal, coarser than the jsonl checks (a same-day re-render bumps mtime
    even if the content underneath didn't actually change) but still catches
    the case of "this hasn't been touched in over a week"."""
    path = os.path.join(config.data_dir(country), filename)
    if not os.path.exists(path):
        return {"asOf": None, "staleDays": None}
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    stale_days = (datetime.now(timezone.utc) - mtime).days
    return {"asOf": mtime.strftime("%Y-%m-%d"), "staleDays": stale_days}


# {label, check(country) -> {asOf, staleDays}, feeds, tmleOnly}
FUNNELS = [
    {"label": "Market Environment", "feeds": ["Market Environment", "Signals"],
     "check": lambda c: _jsonl_status(c, "environment.jsonl")},
    {"label": "Indices", "feeds": ["Indices", "Market Environment"],
     "check": lambda c: _jsonl_status(c, "index_" + list(config.COUNTRIES[c]["index_tickers"])[0] + ".jsonl")},
    {"label": "Sector & Industry Ranks", "feeds": ["Sector & Industry", "Money Flows", "Signals"],
     "check": lambda c: _jsonl_status(c, "sector_ranks.jsonl")},
    {"label": "Screener / Money Flows bundle", "feeds": ["Screener", "Money Flows", "Signals", "Watchlist"],
     "check": lambda c: _jsonl_status(c, "hilo_counts.jsonl")},
    {"label": "Breadth", "feeds": ["Breadth Internals", "6 breadth panels"],
     "check": lambda c: _jsonl_status(c, "breadth_adv_decl.jsonl")},
    {"label": "TMLE Leaders", "feeds": ["TMLE Leaders", "TMLE Emerging", "Signals (breakout)"], "tmleOnly": True,
     "check": lambda c: _jsonl_status(c, "tmle_leaders.jsonl")},
    {"label": "TradingView Classification", "feeds": ["nearly every tab — sector/industry/logo/mkt cap"],
     "mtimeBased": True, "check": lambda c: _mtime_status(c, "classification.json")},
    {"label": "Per-ticker prices", "feeds": ["Stock Lookup", "Screener rows", "Signals (earnings/cup)", "TMLE"],
     "check": lambda c: _ticker_canary_status(c, "AAPL" if c == "US" else "RELIANCE")},
]


def _pipeline_running(country):
    """Best-effort: is a local refresh for this country active right now.
    Reads the tail of the same log run_daily_refresh.py already writes --
    a "starting" line with no later "done"/"FAILED" line for that country
    means it's still going."""
    log_path = os.path.join(OUTPUT_DIR, "daily-refresh.log")
    if not os.path.exists(log_path):
        return False
    started = finished = False
    started_at = None
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if f"{country}: starting pipeline" in line:
                started, finished = True, False
                try:
                    started_at = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    started_at = None
            elif f"{country}: pipeline done" in line or f"{country}: pipeline FAILED" in line:
                finished = True
    # A run that crashed never writes its "done" line. Without an age limit that one orphaned "starting" line
    # (US, 14 Sep) made every US row read "syncing" for five weeks and hid whether the data was actually fresh.
    # The longest real run is under two hours, so anything older than that is not running.
    if started and not finished and started_at is not None and (datetime.now() - started_at).total_seconds() > 4 * 3600:
        return False
    return started and not finished


def freshness_report():
    report = {}
    for code in config.COUNTRIES:
        rows = []
        running = _pipeline_running(code)
        # The country's own environment.jsonl is the authoritative "what
        # session should everything reflect by now" -- it already accounts
        # for weekends and holidays correctly, because it only ever advances
        # when a real session actually closed. Comparing every OTHER
        # funnel against calendar "today" (with a blanket few-day cushion
        # to paper over weekends) is what let a genuinely two-day-stale
        # per-ticker file read as green: the cushion was hiding exactly the
        # gap it was supposed to only excuse on weekends. Comparing against
        # this instead catches a real lag on any day, weekend or not.
        expected = _jsonl_status(code, "environment.jsonl").get("asOf")
        for funnel in FUNNELS:
            if funnel.get("tmleOnly") and not config.COUNTRIES[code].get("run_tmle"):
                continue
            try:
                status = funnel["check"](code)
            except Exception as error:
                status = {"asOf": None, "staleDays": None, "error": str(error)}
            as_of = status.get("asOf")
            stale_days = status.get("staleDays")

            if running:
                level = "syncing"
            elif funnel.get("mtimeBased"):
                # No real session date to compare (classification.json has
                # no per-row date) -- fall back to the coarser day-count
                # tolerance, same as before.
                level = "red" if stale_days is None or stale_days > DATA_STALE_DAYS else "green"
            elif as_of is None or expected is None:
                level = "red"
            else:
                level = "green" if as_of >= expected else "red"
                if level == "red" and as_of:
                    stale_days = (datetime.strptime(expected, "%Y-%m-%d")
                                  - datetime.strptime(as_of, "%Y-%m-%d")).days

            rows.append({
                "label": funnel["label"], "feeds": funnel["feeds"],
                "asOf": as_of, "staleDays": stale_days, "level": level,
            })
        report[code] = {"rows": rows, "syncing": running, "expectedAsOf": expected}
    return report


def trigger_sync_and_backfill(country, log=print):
    """The one-click action behind every red row: try the cheap fix first
    (pull whatever origin already has), and only fall back to an actual
    re-fetch if that alone doesn't close the gap. A full pipeline run takes
    anywhere from ~25 minutes (India) to ~50+ (US) -- far too long to hold
    an HTTP request open for, so that half runs detached and the caller
    polls /api/freshness afterward to watch it land, same as the page
    already does for the "syncing" (yellow) state.
    """
    origin_result = sync_from_origin(log=log)
    try:
        sync_tickers_from_ghpages(log=log)
    except Exception as error:
        log(f"  ticker sync failed: {error}")

    report = freshness_report()
    still_red = [r["label"] for r in report.get(country, {}).get("rows", []) if r["level"] == "red"]
    if not still_red:
        return {"action": "synced", "message": "Caught up from what was already published — no full refresh needed."}

    if _pipeline_running(country):
        return {"action": "already_running",
                "message": f"A {country} refresh is already in progress."}

    log(f"  sync alone didn't close the gap for {country} ({', '.join(still_red)}) — "
        f"starting a full refresh in the background")
    script = os.path.join(config.ROOT_DIR, "scripts", "run_daily_refresh.py")
    subprocess.Popen(
        [sys.executable, script, country],
        cwd=config.ROOT_DIR,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"action": "triggered_full_refresh",
            "message": f"Sync alone wasn't enough — a full {country} refresh just started in the "
                       "background. This can take 25-50+ minutes; the table above will update once it lands."}


# A weekend alone puts asOf 2-3 calendar days behind "today" with nothing
# wrong at all (Friday's close, checked Monday morning before that night's
# refresh, is 3 days old and completely correct). Beyond this, something
# really is behind — the nightly Action failed, or this checkout hasn't
# synced — rather than just "it's the weekend."
DATA_STALE_DAYS = 4


def _resolve_generated_conflicts(log=print):
    """After a stopped `git pull --rebase`: settle conflicts that are only in generated files, or abort.
    Returns True when the rebase finished, otherwise a short reason (the rebase has been aborted)."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=config.ROOT_DIR, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "GIT_EDITOR": "true"})
    try:
        rebase_dir = git("rev-parse", "--git-path", "rebase-merge").stdout.strip()
        if not rebase_dir or not os.path.isdir(os.path.join(config.ROOT_DIR, rebase_dir)):
            return None          # no rebase in progress: nothing to settle
        for _ in range(40):
            conflicted = [f for f in git("diff", "--name-only", "--diff-filter=U").stdout.split() if f]
            if not conflicted:
                step = git("rebase", "--continue")
                if step.returncode == 0:
                    return True
                continue
            if any(not (f.startswith("docs/") or f.startswith("data/")) for f in conflicted):
                git("rebase", "--abort")
                return "local code changes conflict with the published update: " + ", ".join(conflicted[:3])
            for f in conflicted:
                git("checkout", "--ours", "--", f)     # during a rebase, "ours" is the upstream side
                git("add", "--", f)
            step = git("rebase", "--continue")
            if step.returncode == 0:
                return True
        git("rebase", "--abort")
        return "too many conflicting steps; rebase aborted"
    except Exception as error:
        try:
            git("rebase", "--abort")
        except Exception:
            pass
        return f"conflict handling failed: {error}"


def sync_from_origin(log=print):
    """Returns {"ok": bool, "message": str} — the caller persists this so a
    blocked sync (e.g. dirty working tree) is visible in the hub UI instead of
    failing silently forever. This exact silent failure is what let the hub
    sit stale for days once: a local test run left uncommitted files in the
    way, the pull skipped every 30 minutes with nothing surfaced, and nobody
    noticed until the dates were badly behind."""
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],   # rebase: local unpushed commits must not block data updates
            cwd=config.ROOT_DIR, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            msg = result.stdout.strip() or "already up to date"
            log(f"  git sync: {msg}")
            return {"ok": True, "message": msg}
        # A rebase that stops on a conflict used to be left half-done, which blocked every later pull
        # ("you have unmerged files") and kept the hub on stale data for days. The nightly jobs rewrite the
        # generated pages (docs/) and data files (data/), so when local commits touch the same files the
        # conflict is expected: take the upstream copy of those and carry on. Anything in code is not ours
        # to guess at, so that case aborts the rebase cleanly and says so.
        fixed = _resolve_generated_conflicts(log)
        if fixed is True:
            return {"ok": True, "message": "synced (conflicts in generated files resolved to the latest published copy)"}
        reason = fixed or result.stderr.strip()[:200] or "not a fast-forward"
        log(f"  git sync skipped: {reason}")
        return {"ok": False, "message": reason}
    except Exception as error:
        log(f"  git sync failed: {error}")
        return {"ok": False, "message": str(error)}


def _load_sync_state():
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sync_state(state):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def sync_tickers_from_ghpages(log=print):
    # Despite the name (kept for the sync_state.json key and log lines
    # already written), this pulls from data-tickers, not gh-pages -- see
    # daily-us.yml for why: gh-pages is published by a disconnected workflow
    # whose own checkout never has these gitignored files, so anything only
    # published there was silently never actually reachable. data-tickers is
    # a dedicated branch only the daily pipeline (local or Actions) writes
    # to, specifically so this download always has something real to find.
    url = (f"https://codeload.github.com/{GITHUB_REPO}"
          "/tar.gz/refs/heads/data-tickers")
    archive_path = os.path.join(OUTPUT_DIR, "_ghpages_sync.tar.gz")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log("  downloading current ticker history from gh-pages (~1 min)...")
    urllib.request.urlretrieve(url, archive_path)

    written = skipped = 0
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                # First path segment is the tarball's synthetic
                # "<repo>-gh-pages/" root, which every entry carries.
                parts = member.name.split("/", 1)
                if len(parts) < 2:
                    continue
                rel = parts[1]
                local_prefix = sub_rel = None
                for prefix, local in _TICKER_PREFIXES.items():
                    if rel.startswith(prefix):
                        local_prefix, sub_rel = local, rel[len(prefix):]
                        break
                if local_prefix is None:
                    continue          # everything else on gh-pages is html/
                                       # css/js already current via git
                basename = os.path.basename(sub_rel)
                stem = os.path.splitext(basename)[0]
                if _RESERVED_WINDOWS_NAME.match(stem) or _RESERVED_WINDOWS_NAME.match(basename):
                    skipped += 1
                    continue
                dest = os.path.join(DOCS_ROOT, local_prefix, sub_rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as out:
                    out.write(tar.extractfile(member).read())
                written += 1
    finally:
        os.remove(archive_path)
    log(f"  tickers synced: {written} files ({skipped} skipped — Windows-reserved name)")


def run_auto_sync(force_tickers=False, log=print):
    origin_result = sync_from_origin(log=log)

    state = _load_sync_state()
    state["origin"] = {**origin_result, "ts": time.time()}
    _save_sync_state(state)
    age_hours = None
    if state.get("tickers"):
        age_hours = (time.time() - state["tickers"]) / 3600

    if force_tickers or age_hours is None or age_hours >= TICKER_SYNC_HOURS:
        try:
            sync_tickers_from_ghpages(log=log)
            state["tickers"] = time.time()
            _save_sync_state(state)
        except Exception as error:
            log(f"  ticker sync failed: {error}")
    else:
        log(f"  tickers synced {age_hours:.1f}h ago, skipping")

    # IBKR's Flex report needs no login, so this is the one broker leg of the
    # Trade Journal that really can run on a clock -- once a day is plenty
    # for an end-of-day statement. Angel One reconciles opportunistically
    # instead, right when a session exists (see _fetch_fresh).
    broker_age_hours = None
    if state.get("ibkrTrades"):
        broker_age_hours = (time.time() - state["ibkrTrades"]) / 3600
    if broker_age_hours is None or broker_age_hours >= 24:
        try:
            import broker_sync
            result = broker_sync.reconcile_ibkr(log=log)
            log(f"  IBKR trade sync: {len(result['created'])} logged, "
                f"{len(result['needs_date'])} need a buy date confirmed, "
                f"{len(result['possibly_closed'])} possibly closed")
            state["ibkrTrades"] = time.time()
            _save_sync_state(state)
        except Exception as error:
            log(f"  IBKR trade sync failed: {error}")

    # Stops resting at IBKR (needs the gateway logged in) are copied onto open trades that have no Initial Stop $ yet.
    try:
        import stop_capture
        stop_capture.capture_live(log=log)
    except Exception as error:
        log(f"  stop capture skipped: {error}")

    return origin_result


def _sync_loop(log):
    while True:
        try:
            run_auto_sync(log=log)
        except Exception as error:
            log(f"  auto-sync error: {error}")
        time.sleep(SYNC_LOOP_MINUTES * 60)

# How long a fetched view is reused. The page polls every 30s, which is right
# for a live feed and wildly wrong for a reports API — IBKR's Flex service
# answered a few of those with error 1018, "too many requests from this token",
# and would have throttled the token entirely if the page had been left open.
#
# So the interval follows the DATA, not the page: a live quote is stale in
# seconds, an end-of-day statement is not stale until tomorrow.
CACHE_SECONDS_LIVE = 20
CACHE_SECONDS_EOD = 30 * 60

_cache = {}
_lock = threading.Lock()
_sessions = {}

# Per-broker identity for the header. `short` is the fallback mark when no
# logo file is present; `color` is the broker's own brand colour so the chip
# is recognisable at a glance rather than being another grey pill. `flag` is
# a COUNTRY CODE, not the glyph itself — resolved through flags.py's inline
# SVGs in available(), same as the market breadth terminal's own nav already
# does. Windows renders the Unicode flag emoji as plain letters ("US", "IN")
# instead of a flag, which is exactly the "I don't want to see US/IN as text"
# complaint — flags.py exists for precisely this reason; the portfolio page
# just hadn't been switched over to it yet.
BROKER_META = {
    "ibkr":     {"short": "IBKR", "flag": "US", "color": "#d81222"},
    "sharekhan": {"short": "SK",  "flag": "IN", "color": "#00954f"},
    "angelone": {"short": "AO",   "flag": "IN", "color": "#ee4b2b"},
    # A second Angel One account (a family member's own SmartAPI app, under
    # their own login) — same broker, same login flow, a distinct env prefix
    # and session slot. See angelone.py's settings(env_prefix=...).
    "angelone2": {"short": "AO",  "flag": "IN", "color": "#ee4b2b"},
}

# Which broker ids are "an Angel One account" — anything that logs in via
# PIN + TOTP through angelone.py, as opposed to ibkr's separate gateway flow.
ANGELONE_BROKERS = {
    "angelone": "ANGELONE",
    "angelone2": "ANGELONE2",
}

# Which Angel One SESSION (this file's broker id) is which NOTION trade log
# (notion_sync.TRADE_DATABASES label) -- needed so broker_sync.py logs a fill
# into the right person's book. UNCONFIRMED: inferred from registration order
# (angelone = first configured = Shital, angelone2 = second = Sumit), not
# verified against anything that actually names an account holder. Wrong here
# means a trade lands in the wrong sibling's log -- flagged, not guessed past.
ANGELONE_ACCOUNT_LABEL = {
    "angelone": "ShG-AO",
    "angelone2": "SuG-AO",
}

# Drop <broker>.png (or .svg) in here and the header uses it instead of the
# lettered chip. Kept as files you supply rather than artwork shipped in the
# repo — they are other companies' trademarks, and this way they stay yours.
LOGO_DIR = os.path.join(OUTPUT_DIR, "logos")

NAMES_FILE = os.path.join(OUTPUT_DIR, "portfolio_names.json")


def load_names():
    """{broker: your label}. Free text, because "IBKR" is the broker and not
    the portfolio — the account is what you are actually naming."""
    if not os.path.exists(NAMES_FILE):
        return {}
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            return {k: str(v) for k, v in json.load(f).items() if v}
    except Exception:
        return {}


def save_name(broker, label):
    names = load_names()
    label = (label or "").strip()
    if label:
        names[broker] = label[:60]
    else:
        names.pop(broker, None)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=2, sort_keys=True)
    return names


# Real brand marks for the brokers this app talks to directly, from the same
# TradingView logo CDN every position's own logo already comes from (see
# _stock_pin_html / logoCell — s3-symbol-logo.tradingview.com/{logoid}.svg).
# IBKR and Angel One are each themselves exchange-listed (NASDAQ:IBKR,
# NSE:ANGELONE), so their brand logo lives at the same address a position in
# that ticker would use — found via the same tradingview_screener query
# tv_industry.py already runs, just against the broker's own symbol instead
# of a portfolio holding.
BROKER_TV_LOGOID = {
    "ibkr": "interactive-brokers-group",
    "angelone": "angel-broking",
}


def logo_for(broker):
    """A logo URL for this broker, local file first, else its real brand mark.

    Checks the exact broker id first (logos/angelone2.svg, for a family
    member's account that wants its own mark), then the institution's shared
    file (logos/angelone.svg) with any trailing digit stripped — one dropped-
    in file brands every account at that institution. Only once neither
    exists does it fall back to the broker's own TradingView logo — a file
    you supply always wins, so this is a default, not the last word.
    """
    import re
    family = re.sub(r"\d+$", "", broker)
    candidates = [broker] + ([family] if family != broker else [])
    for name in candidates:
        for ext in ("svg", "png", "jpg", "jpeg", "webp"):
            if os.path.exists(os.path.join(LOGO_DIR, f"{name}.{ext}")):
                return f"/logo/{name}.{ext}"
    logoid = BROKER_TV_LOGOID.get(broker) or BROKER_TV_LOGOID.get(family)
    if logoid:
        return f"https://s3-symbol-logo.tradingview.com/{logoid}.svg"
    return None


GATEWAY_DIR = os.path.join(OUTPUT_DIR, "ibkr-gateway")
GATEWAY_PORT = 5000


def gateway_running():
    """True if something is listening on the gateway port."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", GATEWAY_PORT)) == 0


def start_gateway():
    """Launch the gateway if it is not already up.

    The Connect button used to link straight to https://localhost:5000, which
    is a dead port whenever the gateway is not running — you clicked it and got
    "connection refused" with nothing explaining why. Starting it here means
    the button is an action rather than a hopeful link.

    It still cannot log you in: IBKR requires a human in a browser for that.
    """
    if gateway_running():
        return True
    script = os.path.join(GATEWAY_DIR, "bin", "run.bat")
    if not os.path.exists(script):
        raise RuntimeError(f"gateway not installed at {GATEWAY_DIR}")

    import subprocess
    subprocess.Popen(
        ["cmd", "/c", "bin\\run.bat", "root\\conf.yaml"],
        cwd=GATEWAY_DIR,
        stdout=open(os.path.join(OUTPUT_DIR, "gateway.log"), "a"),
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(40):                     # it takes ~10-15s to bind
        time.sleep(0.5)
        if gateway_running():
            return True
    return False


class NeedsLogin(RuntimeError):
    """Not an error so much as a state: the broker is set up but not signed in."""


def log(msg):
    print(msg, flush=True)


def load_stops():
    if not os.path.exists(STOPS_FILE):
        return {}
    try:
        with open(STOPS_FILE, encoding="utf-8") as f:
            return {k.upper(): float(v) for k, v in json.load(f).items() if v}
    except Exception:
        return {}


def nav_log(broker, nav):
    """Append one NAV sample, and return today's samples for the 1D curve."""
    if nav is None:
        return []
    path = os.path.join(OUTPUT_DIR, f"nav_{broker}.jsonl")
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    samples = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("ts", "").startswith(today):
                        samples.append([row["ts"], row["nav"]])

    # One sample a minute is plenty for a day curve and keeps the file small.
    stamp = now.isoformat(timespec="seconds")
    if not samples or (now - datetime.fromisoformat(samples[-1][0])
                       ).total_seconds() >= 60:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": stamp, "nav": nav}) + "\n")
        samples.append([stamp, nav])
    return samples


def daily_history(broker, broker_history):
    """Daily NAV closes: the broker's own where it has them, plus the last
    sample of each day we recorded ourselves for the days it does not."""
    by_date = {d: v for d, v in (broker_history or [])}

    path = os.path.join(OUTPUT_DIR, f"nav_{broker}.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                day = (row.get("ts") or "")[:10]
                # Broker figures win — they are the settled number. Ours only
                # fill days the broker never reported.
                if day and day not in by_date:
                    by_date[day] = row["nav"]
    return sorted(by_date.items())


def fetch(broker):
    """A broker's portfolio, cached briefly, with the curves attached."""
    with _lock:
        hit = _cache.get(broker)
        if hit:
            age = time.time() - hit[0]
            ttl = (CACHE_SECONDS_LIVE if hit[1].get("live")
                   else CACHE_SECONDS_EOD)
            fresh_enough = age < ttl
            # An end-of-day view is cached for half an hour, which is right
            # for Flex and wrong the moment the gateway comes back: you would
            # log in and the page would keep insisting it was end-of-day until
            # the cache aged out. So a stale non-live IBKR view is dropped as
            # soon as the gateway answers. One tickle, cheap.
            if fresh_enough and broker == "ibkr" and not hit[1].get("live"):
                try:
                    import ibkr_cp
                    if ibkr_cp.available():
                        fresh_enough = False
                except Exception:
                    pass
            if fresh_enough:
                return hit[1]

    try:
        view = _fetch_fresh(broker)
    except Exception as error:
        # A broker being briefly unreachable is normal — a rate limit, a
        # dropped session, a laptop waking up. Blanking the page for that
        # loses information the last successful fetch already had, so the
        # stale view is served with a note instead.
        with _lock:
            hit = _cache.get(broker)
        if not hit:
            raise
        stale = dict(hit[1])
        stale["stale"] = True
        stale["stale_reason"] = str(error)[:200]
        stale["stale_since"] = hit[1].get("fetched_at")
        log(f"  {broker}: refresh failed ({error}); serving last good data")
        return stale

    with _lock:
        _cache[broker] = (time.time(), view)
    return view


def _fetch_fresh(broker):
    stops = load_stops()
    if broker == "ibkr":
        # Live when the gateway is up and logged in, Flex when it is not.
        # The gateway cannot run unattended, so this is not a temporary state
        # to be cleaned up later — it is how the page stays useful overnight.
        try:
            view = broker_api.ibkr_live(stops=stops, log=log)
        except Exception as error:
            log(f"  gateway unavailable ({error}); falling back to Flex")
            view = broker_api.ibkr(stops=stops, log=lambda m: None)
    elif broker in ANGELONE_BROKERS:
        session = _sessions.get(broker)
        if not session:
            raise NeedsLogin("Angel One needs a login")
        view = broker_api.angelone(session, stops=stops, log=lambda m: None, broker_id=broker)
        # A session existing right now is the one moment Angel One CAN be
        # reconciled against Notion (see broker_sync.py) -- there is no clock
        # to hook otherwise. Best-effort: a failure here must never break the
        # portfolio view itself.
        account_label = ANGELONE_ACCOUNT_LABEL.get(broker)
        if account_label:
            try:
                import broker_sync
                token, api_key = session
                result = broker_sync.reconcile_angelone(token, api_key, account_label, log=log)
                if result["created"] or result["closed"]:
                    log(f"  {account_label} trade sync: {len(result['created'])} logged, "
                        f"{len(result['closed'])} closed")
            except Exception as error:
                log(f"  {account_label} trade sync failed: {error}")
    else:
        raise RuntimeError(f"unknown broker {broker!r}")

    # Only sample a LIVE feed. An end-of-day broker's NAV is a settled figure
    # that already carries its own date; recording it again under today's date
    # invents a flat session that never happened — which is exactly what it
    # did on the first run, stamping yesterday's close onto today.
    view["nav_intraday"] = (nav_log(broker, view["account"]["nav"])
                            if view.get("live") else [])
    view["nav_history"] = daily_history(broker, view.get("nav_history"))
    view["fetched_at"] = datetime.now().strftime("%H:%M:%S")

    # Where to go to make this broker live. Only offered when it is not, so
    # the button is an answer to a visible problem rather than clutter.
    if broker == "ibkr" and not view.get("live"):
        view["connect_url"] = "https://localhost:5000"
        view["connect_label"] = "Connect to IBKR"
    return view


def configured(broker):
    """True if this broker has credentials on disk, connected or not.

    A broker with credentials but no session still belongs in the list — it is
    the difference between "you have not set this up" and "this needs a login",
    and only the second one deserves a Connect button.
    """
    if broker == "ibkr":
        return True
    if broker in ANGELONE_BROKERS:
        try:
            import angelone
            angelone.settings(env_prefix=ANGELONE_BROKERS[broker])
            return True
        except Exception:
            return False
    return False


def available():
    """Every broker the page can show, with its identity for the header."""
    ids = ["ibkr"]
    for broker in list(ANGELONE_BROKERS) + ["sharekhan"]:
        if _sessions.get(broker) or configured(broker):
            ids.append(broker)

    names = load_names()
    out = []
    for broker in ids:
        meta = BROKER_META.get(broker, {})
        out.append({
            "id": broker,
            "short": meta.get("short", broker.upper()),
            # An inline SVG, not the raw country code — see BROKER_META's
            # comment on why this can't be a Unicode flag emoji on Windows.
            "flag": flags.flag(meta.get("flag", "")),
            "color": meta.get("color", "#6b7280"),
            "logo": logo_for(broker),
            # Brokers whose session is held in memory need a login before they
            # can answer; IBKR's lives in the gateway, so it is always "ready"
            # here and reports its own state through the live flag instead.
            "connected": (broker == "ibkr") or bool(_sessions.get(broker)),
            # The saved label if there is one; otherwise blank, so the field
            # shows a placeholder rather than a name you did not choose.
            "name": names.get(broker, ""),
        })
    return out


# ── Signal feedback log ──────────────────────────────────────────────────
#
# One JSONL file per country, one row per RATED signal (not per day like
# signals_log.jsonl -- a rating is keyed on sym+date+signalType and lives
# indefinitely until you change it, not capped or rolled off). Lives only on
# this local server: docs/ is static files with nothing to write to, so
# rating and browsing this log both require the local hub, by construction
# rather than by an access check anywhere.

def _feedback_path(country):
    return os.path.join(config.data_dir(country), "signal_feedback.jsonl")


def _load_feedback(country):
    path = _feedback_path(country)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_feedback_all():
    """Every rating across both countries, each row carrying its own
    country -- the panel shows one combined log rather than a separate one
    per market, since a rating is a personal judgment call, not market data."""
    out = []
    for code in config.COUNTRIES:
        out.extend(_load_feedback(code))
    out.sort(key=lambda r: r.get("ratedAt") or "", reverse=True)
    return out


def _upsert_feedback(country, entry):
    """Replaces any existing row for the same (sym, date, signalType);
    appends otherwise. `entry` must already carry sym/date/signalType/good."""
    path = _feedback_path(country)
    rows = _load_feedback(country)
    key = (entry["sym"], entry["date"], entry["signalType"])
    rows = [r for r in rows
            if (r.get("sym"), r.get("date"), r.get("signalType")) != key]
    rows.append(entry)
    rows.sort(key=lambda r: r.get("date") or "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass                       # the access log is noise here

    def _send(self, code, body, content_type):
        if content_type.startswith("text/html") and isinstance(body, str):
            body = _with_theme(body)
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # The page is embedded in an iframe on notion.so, so it must not
        # forbid framing. It is only reachable from this machine regardless.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_file(self, path):
        kind = {
            ".html": "text/html; charset=utf-8", ".js": "text/javascript",
            ".css": "text/css", ".json": "application/json",
            ".png": "image/png", ".svg": "image/svg+xml",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        with open(path, "rb") as f:
            blob = f.read()
        if kind.startswith("text/html"):
            blob = _with_theme(blob.decode("utf-8", "replace")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/nts-icon.png", "/favicon.ico"):
            # The app window and taskbar icon (Windows picks a PNG/ICO up more reliably than an SVG).
            icon = os.path.join(OUTPUT_DIR, "neils_trading_system.png" if route.path.endswith(".png") else "neils_trading_system.ico")
            if os.path.exists(icon):
                self._serve_file(icon)
                return
        if route.path in ("/", "/index.html"):
            page = HUB_PAGE.replace("%%NAV_JSON%%", _hub_nav_json())
            self._send(200, page, "text/html; charset=utf-8")
            return
        if route.path.startswith("/api/logo/"):
            # Logos are fetched from TradingView once and kept on disk, so the
            # panel doesn't depend on a third-party host answering every row.
            slug = os.path.basename(route.path[len("/api/logo/"):])
            slug = "".join(ch for ch in slug if ch.isalnum() or ch in "-_")[:80]
            folder = os.path.join(LOGO_DIR, "tv")
            path = os.path.join(folder, slug + ".svg")
            blob = None
            if slug:
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        blob = f.read()
                else:
                    try:
                        import urllib.request
                        req = urllib.request.Request("https://s3-symbol-logo.tradingview.com/" + slug + ".svg",
                                                     headers={"User-Agent": "Mozilla/5.0"})
                        blob = urllib.request.urlopen(req, timeout=8).read()
                        os.makedirs(folder, exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(blob)
                    except Exception:
                        blob = None
            if blob:
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=604800")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
            else:
                self._send(404, "no logo", "text/plain")
            return
        if route.path == "/api/movers/watch":
            import movers
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            code = code if code in config.COUNTRIES else "US"
            days = int(float((q.get("days") or ["21"])[0]))
            lists = movers.watch_lists(code)
            name = (q.get("list") or [""])[0]
            if name:
                self._send(200, json.dumps({"lists": {k: len(v) for k, v in lists.items()},
                                            "name": name, "symbols": lists.get(name, []),
                                            "rows": movers.lookup(code, lists.get(name, []), days)}),
                           "application/json")
            else:
                self._send(200, json.dumps({"lists": {k: len(v) for k, v in lists.items()}}), "application/json")
            return
        if route.path == "/api/movers/tv-export":
            import movers
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            code = code if code in config.COUNTRIES else "US"
            name = (q.get("list") or [""])[0]
            body = movers.tv_export(code, movers.watch_lists(code).get(name, []))
            fname = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name)[:40] or "watchlist"
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="' + fname + '.txt"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route.path == "/movers":
            with open(MOVERS_PAGE_FILE, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if route.path == "/journal-review.js":
            with open(JOURNAL_REVIEW_JS, encoding="utf-8") as f:
                self._send(200, f.read(), "application/javascript; charset=utf-8")
            return
        if route.path == "/api/journal/review":
            import monthly_review
            q = parse_qs(route.query)
            account = (q.get("account") or ["NG-IBKR"])[0]
            months = [m for m in ((q.get("months") or [""])[0]).split(",") if m]
            try:
                out = {"available": monthly_review.available(account)} if not months else monthly_review.build(months, account)
                self._send(200, json.dumps(out), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/setup-finder":
            with open(SETUP_FINDER_PAGE_FILE, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if route.path == "/api/setup-finder/state":
            import setup_finder
            self._send(200, json.dumps(setup_finder.state()), "application/json")
            return
        if route.path == "/api/setup-finder/scan":
            import setup_finder
            try:
                p = json.loads((parse_qs(route.query).get("p") or ["{}"])[0])
                self._send(200, json.dumps(setup_finder.scan("US", p)), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/movers/sectors":
            import movers
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            self._send(200, json.dumps(movers.sectors(code if code in config.COUNTRIES else "US")),
                       "application/json")
            return
        if route.path == "/api/movers":
            import movers
            q = parse_qs(route.query)
            g = lambda k, d="": (q.get(k) or [d])[0]
            code = g("country", "US").upper()
            code = code if code in config.COUNTRIES else "US"
            try:
                picked = [s for s in g("sectors").split("|") if s] or None
                res = movers.scan(code, int(float(g("days", "21"))), float(g("pct", "20")),
                                  g("dir", "up") if g("dir", "up") in ("up", "down", "either") else "up",
                                  picked, float(g("minPrice", "0")), float(g("minDv", "0")) * 1e6, g("unclassified") == "1")
                self._send(200, json.dumps(res), "application/json")
            except ValueError as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path in ("/today", "/signals"):
            with open(TODAY_PAGE_FILE if route.path == "/today" else SIGNALS_PAGE_FILE, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if route.path == "/api/signals":
            try:
                code = (parse_qs(route.query).get("country") or ["US"])[0].upper()
                self._send(200, json.dumps(_signals_data(code if code in config.COUNTRIES else "US")), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/home":
            # Read per request so edits to the page show up on reload.
            with open(HOME_PAGE_FILE, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if route.path == "/api/home":
            import home_data
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            if code not in config.COUNTRIES:
                code = "US"
            self._send(200, json.dumps(home_data.build(code)), "application/json")
            return
        if route.path == "/api/home/ticker":
            import home_data
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            if code not in config.COUNTRIES:
                code = "US"
            self._send(200, json.dumps(home_data.ticker_view((q.get("symbol") or [""])[0], code)),
                       "application/json")
            return
        if route.path == "/api/home/book":
            import home_data
            # ?part=risk (fast: positions + quotes) or ?part=journal (slow:
            # Notion chart scan) so the page can draw the first without the second.
            part = (parse_qs(route.query).get("part") or [""])[0]
            body = {}
            if part in ("", "risk"):
                body["risk"] = home_data.book_risk()
            if part in ("", "journal"):
                body["journal"] = home_data.journal_summary()
            self._send(200, json.dumps(body), "application/json")
            return
        if route.path == "/portfolio":
            self._send(200, PORTFOLIO_PAGE, "text/html; charset=utf-8")
            return
        if route.path == "/journal":
            self._send(200, JOURNAL_PAGE, "text/html; charset=utf-8")
            return
        if route.path == "/setups":
            with open(SETUPS_PAGE_FILE, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        # Everything under /docs/... is the market breadth terminal, served
        # from the exact files GitHub Pages publishes. Never edited here —
        # only read, so the Pages build and the local hub can never drift
        # apart from each other.
        if route.path.startswith("/docs/"):
            rel = route.path[len("/docs/"):].split("?")[0]
            target = os.path.normpath(os.path.join(DOCS_ROOT, rel))
            # normpath collapses ../ segments; this check refuses to serve
            # anything that walked outside DOCS_ROOT once it has.
            if not target.startswith(os.path.normpath(DOCS_ROOT)):
                self._send(403, "forbidden", "text/plain")
                return
            if os.path.isfile(target):
                self._serve_file(target)
                return
            self._send(404, "not found", "text/plain")
            return
        if route.path == "/api/sync/status":
            self._send(200, json.dumps(_load_sync_state()), "application/json")
            return
        if route.path == "/api/data-status":
            self._send(200, json.dumps({
                code: _country_data_status(code) for code in config.COUNTRIES
            }), "application/json")
            return
        if route.path == "/api/signal-feedback":
            self._send(200, json.dumps(_load_feedback_all()), "application/json")
            return
        if route.path == "/api/freshness":
            self._send(200, json.dumps(freshness_report()), "application/json")
            return
        if route.path == "/api/journal/trades":
            try:
                import notion_sync
                import setup_context
                trades = notion_sync.fetch_trades(log=log)
                for t in trades:
                    t["logoid"] = (setup_context.classify("US", t.get("ticker"))[2]
                                   or setup_context.classify("IN", t.get("ticker"))[2])
                # Charts are drawn automatically, so whether a screenshot was pasted is no longer checked
                # here (it took one Notion call per trade and nothing flags on it any more).
                self._send(200, json.dumps(trades), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/mfe":
            try:
                import home_data
                import journal_mfe
                self._send(200, json.dumps(journal_mfe.rows(home_data._trades())), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/backtest":
            try:
                import home_data
                import journal_mfe
                q = parse_qs(route.query)
                use_stop = (q.get("stop") or ["1"])[0] != "0"
                self._send(200, json.dumps(journal_mfe.backtest(home_data._trades(), use_stop)), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/group":
            try:
                import home_data
                import journal_mfe
                self._send(200, json.dumps(journal_mfe.group_strength(home_data._trades())), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/stops":
            try:
                import stop_capture
                self._send(200, json.dumps(stop_capture.summary()), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/last":
            import journal_mfe
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            code = code if code in config.COUNTRIES else "US"
            out = {}
            for sym in ((q.get("symbols") or [""])[0].split(","))[:80]:
                hist = journal_mfe._history(code, sym)
                if hist and hist.get("close"):
                    out[sym] = {"close": hist["close"][-1], "date": hist["dates"][-1]}
            self._send(200, json.dumps(out), "application/json")
            return
        if route.path == "/api/journal/path":
            import journal_mfe
            q = parse_qs(route.query)
            g = lambda k: (q.get(k) or [""])[0]
            code = g("country").upper()
            code = code if code in config.COUNTRIES else "US"
            self._send(200, json.dumps(journal_mfe.path(code, g("symbol"), g("from"), g("to"))),
                       "application/json")
            return
        if route.path == "/api/journal/pending":
            try:
                import notion_sync
                trades = notion_sync.fetch_trades(log=lambda *a: None)
                # charts are imported automatically; only a missing setup counts as a gap
                count = sum(1 for t in trades if not t.get("entrySetup") or (t.get("dateClosed") and not t.get("exitSetup")))
                self._send(200, json.dumps({"count": count}), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"count": 0, "error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/chart":
            try:
                import notion_sync
                q = parse_qs(route.query)
                page_id = (q.get("pageId") or [""])[0]
                if not page_id:
                    raise ValueError("pageId is required")
                self._send(200, json.dumps(notion_sync.fetch_page_images(page_id)),
                           "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/schema":
            try:
                import notion_sync
                self._send(200, json.dumps(notion_sync.fetch_trade_schema(log=log)),
                          "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/chart":
            try:
                import notion_sync
                q = parse_qs(route.query)
                page_id = (q.get("pageId") or [""])[0]
                mode = (q.get("mode") or ["blocks"])[0]
                if not page_id:
                    raise ValueError("pageId is required")
                imgs = notion_sync.fetch_trade_charts(page_id, mode)
                self._send(200, json.dumps([{"url": i.get("url"),
                    "ref": i.get("blockId")} for i in imgs]), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/data":
            try:
                if (parse_qs(route.query).get("cached") or [""])[0] == "1":
                    cached = _json_read(SETUPS_CACHE_FILE, None)
                    if cached and cached.get("entries") is not None:
                        cached["fromCache"] = True
                        self._send(200, json.dumps(cached), "application/json")
                        return
                self._send(200, json.dumps(_setups_live()), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/queue":
            try:
                code = (parse_qs(route.query).get("country") or ["US"])[0].upper()
                code = "IN" if code in ("IN", "INDIA") else "US"
                self._send(200, json.dumps({"items": _setups_queue(code)}), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/info":
            import symbol_info
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            code = "IN" if code in ("IN", "INDIA") else "US"
            info = symbol_info.info(code, (q.get("ticker") or [""])[0])
            if info.get("name") and info.get("ticker"):
                with _SETUPS_LOCK:
                    names = _json_read(SYMBOL_NAMES_FILE, {})
                    if names.get(code + ":" + info["ticker"]) != info["name"]:
                        names[code + ":" + info["ticker"]] = info["name"]
                        _json_write(SYMBOL_NAMES_FILE, names)
            self._send(200, json.dumps(info), "application/json")
            return
        if route.path == "/api/setups/prices":
            import journal_mfe
            import setup_measure
            q = parse_qs(route.query)
            code = (q.get("country") or ["US"])[0].upper()
            code = "IN" if code in ("IN", "INDIA") else "US"
            setup_measure.ensure_history(code, (q.get("ticker") or [""])[0])
            hist = journal_mfe._history(code, (q.get("ticker") or [""])[0])
            if not hist:
                self._send(200, json.dumps({"error": "no price history for that ticker"}), "application/json")
            else:
                out = {k: hist[k] for k in ("dates", "open", "high", "low", "close")}
                out["volume"] = hist.get("volume")
                self._send(200, json.dumps(out), "application/json")
            return
        if route.path == "/api/setups/stop":
            q = parse_qs(route.query)
            self._send(200, json.dumps(_setup_stops().get((q.get("pageId") or [""])[0]) or {}), "application/json")
            return
        if route.path == "/api/setups/chart":
            try:
                import notion_sync
                q = parse_qs(route.query)
                page_id = (q.get("pageId") or [""])[0]
                if not page_id:
                    raise ValueError("pageId is required")
                self._send(200, json.dumps(notion_sync.fetch_entry_charts(page_id)),
                           "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/schema":
            try:
                import notion_sync
                self._send(200, json.dumps(notion_sync.fetch_database_schema(
                    notion_sync.SETUP_ENTRIES_DB)), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/lookup":
            try:
                import setup_context
                q = parse_qs(route.query)
                country = (q.get("country") or ["US"])[0].upper()
                term = (q.get("q") or [""])[0].strip().upper()
                hits = []
                if country in config.COUNTRIES and term:
                    for sym, row in setup_context._classification(country).items():
                        if term in sym:
                            hits.append({"ticker": sym, "sector": row[0] if row else None,
                                         "industry": row[1] if len(row) > 1 else None,
                                         "logoid": row[2] if len(row) > 2 else None})
                    hits.sort(key=lambda h: (not h["ticker"].startswith(term), h["ticker"]))
                    hits = hits[:20]
                    # Not in the terminal's list (ADRs such as ARM, ASML, TSM): ask TradingView, and offer it on top.
                    if term.isalnum() and len(term) <= 6 and not any(h["ticker"] == term for h in hits):
                        import symbol_info
                        found = symbol_info.resolve(country, term)
                        if found:
                            with _SETUPS_LOCK:
                                known = _json_read(SETUPS_SYMBOLS_FILE, {})
                                known[country + ":" + term] = found
                                _json_write(SETUPS_SYMBOLS_FILE, known)
                            hits.insert(0, {"ticker": term, "sector": found.get("sector"), "industry": found.get("industry"),
                                            "logoid": found.get("logoid"), "name": found.get("name"), "outside": True})
                self._send(200, json.dumps(hits), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/context":
            try:
                import setup_context
                q = parse_qs(route.query)
                country = (q.get("country") or ["US"])[0].upper()
                ticker = (q.get("ticker") or [""])[0]
                buy_date = (q.get("date") or [""])[0]
                if country not in config.COUNTRIES or not ticker or not buy_date:
                    raise ValueError("country, ticker and date are all required")
                outside = _json_read(SETUPS_SYMBOLS_FILE, {}).get(country + ":" + ticker.upper()) or {}
                self._send(200, json.dumps(
                    setup_context.context_at(country, ticker, buy_date, sector=outside.get("sector"),
                                             industry=outside.get("industry"), logoid=outside.get("logoid"))),
                    "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/brokers":
            self._send(200, json.dumps(available()), "application/json")
            return
        if route.path == "/api/portfolio":
            which = (parse_qs(route.query).get("broker") or ["ibkr"])[0]
            try:
                self._send(200, json.dumps(fetch(which)), "application/json")
            except NeedsLogin as error:
                self._send(200, json.dumps({"needs_login": True,
                                            "broker": which,
                                            "message": str(error)}),
                           "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}),
                           "application/json")
            return
        if route.path.startswith("/logo/"):
            name = os.path.basename(route.path[len("/logo/"):])
            path = os.path.join(LOGO_DIR, name)
            # basename() above keeps this inside LOGO_DIR; without it a path
            # like /logo/../../.env would walk straight out of the folder.
            if os.path.isfile(path):
                kind = {"svg": "image/svg+xml", "png": "image/png",
                        "webp": "image/webp"}.get(name.rsplit(".", 1)[-1].lower(),
                                                  "image/jpeg")
                with open(path, "rb") as f:
                    blob = f.read()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            self._send(404, "no logo", "text/plain")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        route = urlparse(self.path)
        if route.path == "/api/sync":
            # The background loop already does this automatically — this is
            # the self-service version, for "I don't want to wait 30 minutes
            # or ask someone to fix it" rather than a routine path.
            try:
                origin_result = run_auto_sync(force_tickers=True, log=log)
                self._send(200, json.dumps({
                    "ok": origin_result["ok"],
                    "error": None if origin_result["ok"] else origin_result["message"],
                }), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"ok": False, "error": str(error)}),
                          "application/json")
            return

        if route.path == "/api/gateway/start":
            try:
                ok = start_gateway()
                self._send(200, json.dumps({
                    "ok": ok, "url": "https://localhost:5000",
                    "error": None if ok else
                             "gateway did not start — see Portfolio Local/gateway.log",
                }), "application/json")
            except Exception as error:
                self._send(200, json.dumps({"ok": False, "error": str(error)}),
                           "application/json")
            return

        if route.path == "/api/connect":
            # PIN and TOTP arrive here, are exchanged for a session token, and
            # are never written to disk or kept in memory afterwards. Reachable
            # only from this machine — the server binds 127.0.0.1.
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                broker = body.get("broker")
                if broker not in ANGELONE_BROKERS:
                    raise ValueError(f"{broker} does not log in this way")

                import angelone
                pin = body.get("pin") or ""
                totp = (body.get("totp") or "").strip()
                if not pin or not totp:
                    raise ValueError("PIN and TOTP are both required")

                api_key, client_code = angelone.settings(env_prefix=ANGELONE_BROKERS[broker])
                _sessions[broker] = angelone.login(pin, totp, api_key, client_code)
                del pin, body
                _cache.pop(broker, None)
                log(f"  {broker} connected")
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as error:
                # Angel One's own message is the useful one ("Invalid totp",
                # "Invalid credentials"), so it is passed through rather than
                # replaced with something generic.
                self._send(200, json.dumps({"ok": False, "error": str(error)}),
                           "application/json")
            return

        if route.path == "/api/freshness/sync":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                country = (body.get("country") or "").upper()
                if country not in config.COUNTRIES:
                    raise ValueError(f"unknown country {country!r}")
                result = trigger_sync_and_backfill(country, log=log)
                self._send(200, json.dumps(result), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/journal/update":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                fields = body.get("fields") or {}
                if not page_id or not fields:
                    raise ValueError("pageId and fields are both required")
                import notion_sync
                notion_sync.update_trade(page_id, fields, log=log)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/journal/create-from-broker":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                account_label = body.get("account")
                ticker = body.get("ticker")
                date_opened = body.get("dateOpened")
                entry_price = body.get("entryPrice")
                shares = body.get("shares")
                if not all([account_label, ticker, date_opened, entry_price, shares]):
                    raise ValueError("account, ticker, dateOpened, entryPrice and shares are all required")
                import notion_sync
                account = next((a for a in notion_sync.TRADE_DATABASES
                               if a["label"] == account_label), None)
                if not account:
                    raise ValueError(f"unknown account {account_label!r}")
                parent_id = body.get("parentId")
                result = notion_sync.create_trade(
                    account, ticker, date_opened, entry_price, shares, log=log,
                    initial_stop=body.get("initialStop") or None, parent_id=parent_id or None,
                    entry_setup="Pyramid Add" if parent_id else None)
                self._send(200, json.dumps(result), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/journal/close-from-broker":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                date_closed = body.get("dateClosed")
                exit_price = body.get("exitPrice")
                if not all([page_id, date_closed, exit_price is not None]):
                    raise ValueError("pageId, dateClosed and exitPrice are all required")
                import notion_sync
                notion_sync.close_trade(page_id, date_closed, float(exit_price), log=log)
                account = next((a for a in notion_sync.TRADE_DATABASES if a["label"] == body.get("account")), None)
                if account:      # a position closed by hand closes its pyramid adds too
                    import broker_sync
                    broker_sync._close_children(account, {"id": page_id}, date_closed, float(exit_price), log)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/journal/sync-brokers":
            try:
                import broker_sync
                try:
                    start_gateway()          # so the login page is there if the live read needs it
                except Exception as error:
                    log(f"  gateway not started: {error}")
                results = [broker_sync.reconcile_ibkr(log=log)]
                # India (Angel One) is only synced when a session already exists; an account that is not logged in
                # is left out of the result rather than reported as an error.
                for broker, label in ANGELONE_ACCOUNT_LABEL.items():
                    session = _sessions.get(broker)
                    if session:
                        token, api_key = session
                        results.append(broker_sync.reconcile_angelone(token, api_key, label, log=log))
                self._send(200, json.dumps({"results": results}), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/journal/chart":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                mode = body.get("chartMode") or "blocks"
                if not page_id:
                    raise ValueError("pageId is required")
                import notion_sync
                if body.get("action") == "delete":
                    imgs = notion_sync.remove_trade_chart(page_id, mode, body.get("ref"), log=log)
                else:
                    data_b64 = body.get("dataB64") or ""
                    if not data_b64:
                        raise ValueError("dataB64 is required")
                    blob = base64.b64decode(data_b64.split(",", 1)[-1])
                    if len(blob) > 12 * 1024 * 1024:
                        raise ValueError("image is larger than 12 MB")
                    ctype = body.get("contentType") or "image/png"
                    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                           "image/gif": "gif"}.get(ctype, "png")
                    imgs = notion_sync.add_trade_chart(
                        page_id, mode, body.get("filename") or f"chart.{ext}", ctype, blob, log=log)
                self._send(200, json.dumps({"ok": True, "images": [
                    {"url": i.get("url"), "ref": i.get("blockId")} for i in imgs]}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/setups/type":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                import notion_sync
                self._send(200, json.dumps(notion_sync.create_setup_type(body.get("name"), log=log)),
                           "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/create":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                country = (body.get("country") or "").upper()
                country = "IN" if country in ("IN", "INDIA") else country
                ticker = (body.get("ticker") or "").upper()
                buy_date = body.get("buyDate")
                if (country not in config.COUNTRIES or not ticker
                        or not buy_date or not body.get("setupId")):
                    raise ValueError("country, ticker, buyDate and setupId are all required")
                import setup_context
                import setup_measure
                import notion_sync
                der = setup_measure.derive(country, ticker, buy_date, body.get("entryPrice"), body.get("stop"))
                if not der.get("error"):
                    buy_date = der["date"]          # a weekend or holiday date moves to the next session
                outside = _json_read(SETUPS_SYMBOLS_FILE, {}).get(country + ":" + ticker) or {}
                ctx = setup_context.context_at(country, ticker, buy_date, sector=body.get("sector") or outside.get("sector"),
                                               industry=body.get("industry") or outside.get("industry"), logoid=outside.get("logoid"))
                if not der.get("error"):
                    ctx.update({k: v for k, v in setup_measure.notion_fields(der).items() if v is not None})
                fields = {
                    "setupId": body.get("setupId"),
                    "ticker": ticker,
                    "country": "US" if country == "US" else "India",
                    "buyDate": buy_date,
                    "entryThesis": body.get("entryThesis"),
                    "exitRule": body.get("exitRule"),
                    "entryPrice": body.get("entryPrice") or (der.get("entryPx") if not der.get("error") else None),
                    "stop": body.get("stop"),
                    "stopReason": body.get("stopReason"),
                    "checklist": body.get("checklist") or None,
                    "lines": body.get("lines") or None,
                }
                if body.get("baseLengthDays") is not None:
                    fields["baseLengthDays"] = body.get("baseLengthDays")
                result = notion_sync.create_setup_entry(fields, ctx, log=log)
                result["context"] = ctx
                try:
                    entry = _decorate_setup_entry(notion_sync.fetch_setup_entry(result["pageId"]))
                    _setups_cache_edit(entry=entry)
                    result["entry"] = entry
                except Exception as error:
                    log(f"  setups: could not read back {result.get('pageId')}: {error}")
                self._send(200, json.dumps(result), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/derived":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                import setup_measure
                out = {}
                for it in (body.get("items") or [])[:2000]:
                    code = "IN" if (it.get("country") or "").upper() in ("IN", "INDIA") else "US"
                    out[str(it.get("key"))] = setup_measure.derive(code, it.get("ticker"), it.get("buyDate") or None,
                                                                   it.get("entryPx") or None, it.get("stop") or None)
                self._send(200, json.dumps(out), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/delete":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                if not page_id:
                    raise ValueError("pageId is required")
                import notion_sync
                restore = bool(body.get("restore"))
                notion_sync.archive_setup_entry(page_id, restore=restore, log=log)
                entry = None
                if restore:
                    entry = _decorate_setup_entry(notion_sync.fetch_setup_entry(page_id))
                    _setups_cache_edit(entry=entry)
                else:
                    _setups_cache_edit(remove=page_id)
                self._send(200, json.dumps({"ok": True, "entry": entry}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/criteria":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                type_id = body.get("typeId")
                if not type_id:
                    raise ValueError("typeId is required")
                import notion_sync
                clean = notion_sync.set_type_criteria(type_id, body.get("criteria") or [], log=log)
                cache = _json_read(SETUPS_CACHE_FILE, None)
                if cache:
                    for t in cache.get("types", []):
                        if t.get("id") == type_id:
                            t["criteria"] = clean
                    _setups_cache_edit(types=cache.get("types", []))
                self._send(200, json.dumps({"criteria": clean}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/queue":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                action, iid = body.get("action"), body.get("id") or ""
                with _SETUPS_LOCK:
                    q = _json_read(SETUPS_QUEUE_FILE, {"listed": [], "skipped": []})
                    q.setdefault("listed", []); q.setdefault("skipped", [])
                    if action == "add":
                        t = "".join(ch for ch in (body.get("t") or "").upper().split(":")[-1] if ch.isalnum() or ch in ".-_")[:16]
                        code = "IN" if (body.get("country") or "").upper() in ("IN", "INDIA") else "US"
                        if not t:
                            raise ValueError("a ticker is required")
                        if not any(x.get("t") == t and (x.get("country") or "US") == code for x in q["listed"]):
                            q["listed"].append({"t": t, "country": code, "src": "signal" if body.get("src") == "signal" else "manual",
                                                "folder": body.get("folder"), "entry": body.get("entry") or None,
                                                "note": (body.get("note") or "").strip()[:400] or None,
                                                "added": datetime.now().strftime("%Y-%m-%d")})
                        q["skipped"] = [x for x in q["skipped"] if x != "list:" + t]
                    elif action == "skip":
                        if iid and iid not in q["skipped"]:
                            q["skipped"].append(iid)
                    elif action == "unskip":
                        q["skipped"] = [x for x in q["skipped"] if x != iid]
                    elif action in ("done", "undone"):
                        if iid.startswith("finder:"):
                            import setup_finder
                            if action == "done":
                                setup_finder.save_edit(iid[len("finder:"):], {"savedPageId": body.get("pageId"), "savedUrl": body.get("notionUrl"),
                                                                              "savedSetup": body.get("setup"), "status": "keep"})
                            else:
                                setup_finder.save_edit(iid[len("finder:"):], {"savedPageId": None, "savedUrl": None, "savedSetup": None, "status": "keep"})
                        elif iid.startswith("list:"):
                            if action == "done":
                                q.setdefault("done", []).append({**next((x for x in q["listed"] if "list:" + x.get("t", "") == iid), {}),
                                                                "pageId": body.get("pageId")})
                                q["listed"] = [x for x in q["listed"] if "list:" + x.get("t", "") != iid]
                            else:
                                back = next((x for x in reversed(q.get("done", [])) if "list:" + x.get("t", "") == iid), None)
                                if back:
                                    back = {k: v for k, v in back.items() if k != "pageId"}
                                    q["listed"].append(back)
                                    q["done"] = [x for x in q["done"] if x is not None and "list:" + x.get("t", "") != iid]
                    else:
                        raise ValueError("unknown action")
                    _json_write(SETUPS_QUEUE_FILE, q)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/setups/chart":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                if body.get("clear"):
                    import notion_sync
                    notion_sync.clear_entry_chart(page_id, log=log)
                    self._send(200, json.dumps({"ok": True, "files": []}), "application/json")
                    return
                data_b64 = body.get("dataB64") or ""
                if not page_id or not data_b64:
                    raise ValueError("pageId and dataB64 are both required")
                blob = base64.b64decode(data_b64.split(",", 1)[-1])
                if len(blob) > 12 * 1024 * 1024:
                    raise ValueError("image is larger than 12 MB")
                ctype = body.get("contentType") or "image/png"
                ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                       "image/gif": "gif"}.get(ctype, "png")
                import notion_sync
                files = notion_sync.attach_chart(
                    page_id, body.get("filename") or f"chart.{ext}", ctype, blob, log=log)
                self._send(200, json.dumps({"ok": True, "files": files}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path in ("/api/setup-finder/edit", "/api/setup-finder/filter"):
            import setup_finder
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if route.path.endswith("/edit"):
                    out = {"edit": setup_finder.save_edit(body.get("key") or "", body.get("fields") or {})}
                else:
                    out = {"filters": setup_finder.save_filter(body.get("name") or "", body.get("params") or {}, bool(body.get("delete")))}
                self._send(200, json.dumps(out), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/movers/watch":
            import movers
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                code = (body.get("country") or "US").upper()
                code = code if code in config.COUNTRIES else "US"
                action = body.get("action")
                symbols = body.get("symbols") or []
                if action == "import":     # a pasted TradingView export
                    symbols, action = movers.tv_parse(body.get("text") or ""), "add"
                if action not in ("add", "remove", "create", "delete", "rename"):
                    raise ValueError("unknown action")
                lists = movers.watch_edit(code, action, body.get("list") or "My Watchlist", symbols, body.get("newName"))
                self._send(200, json.dumps({"lists": {k: len(v) for k, v in lists.items()},
                                            "added": len(symbols)}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/stop":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                if not page_id:
                    raise ValueError("pageId is required")
                stops = _setup_stops()
                stops[page_id] = {"stop": body.get("stop"), "reason": (body.get("reason") or "").strip(),
                                  "entryDate": body.get("entryDate"), "entryPrice": body.get("entryPrice")}
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                with open(SETUP_STOPS_FILE, "w", encoding="utf-8") as f:
                    json.dump(stops, f, indent=1)
                self._send(200, json.dumps({"ok": True}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/setups/update":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                page_id = body.get("pageId")
                allowed = {"setupId", "entryThesis", "exitRule", "baseLengthDays", "entryPrice", "stop", "stopReason", "checklist", "buyDate", "lines"}
                fields = {k: v for k, v in (body.get("fields") or {}).items() if k in allowed}
                if not page_id or not (fields or body.get("resync")):
                    raise ValueError("pageId and fields are both required")
                context = None
                entry = body.get("entry") or {}
                if body.get("resync") or {"buyDate", "entryPrice", "stop"} & set(fields):
                    if entry.get("country") and entry.get("ticker") and (fields.get("buyDate") or entry.get("buyDate")):
                        import setup_context
                        import setup_measure
                        code = "US" if entry["country"] == "US" else "IN"
                        bd = fields.get("buyDate") or entry["buyDate"]
                        der = setup_measure.derive(code, entry["ticker"], bd, fields.get("entryPrice", entry.get("entryPrice")),
                                                   fields.get("stop", entry.get("stop")))
                        if not der.get("error"):
                            bd = der["date"]
                        if "buyDate" in fields:
                            fields["buyDate"] = bd
                            fields["title"] = f"{entry['ticker'].upper()} · {bd}"
                        outside = _json_read(SETUPS_SYMBOLS_FILE, {}).get(code + ":" + entry["ticker"].upper()) or {}
                        context = setup_context.context_at(code, entry["ticker"], bd, sector=entry.get("sector") or outside.get("sector"),
                                                           industry=entry.get("industry") or outside.get("industry"), logoid=outside.get("logoid"))
                        if not der.get("error"):
                            context.update({k: v for k, v in setup_measure.notion_fields(der).items() if v is not None})
                import notion_sync
                notion_sync.update_setup_entry(page_id, fields, context=context, log=log)
                fresh = None
                try:
                    fresh = _decorate_setup_entry(notion_sync.fetch_setup_entry(page_id))
                    _setups_cache_edit(entry=fresh)
                except Exception as error:
                    log(f"  setups: could not read back {page_id}: {error}")
                self._send(200, json.dumps({"ok": True, "context": context, "entry": fresh}), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/signal-feedback":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                country = (body.get("country") or "").upper()
                sym = body.get("sym")
                date = body.get("date")
                signal_type = body.get("signalType")
                if country not in config.COUNTRIES or not sym or not date or not signal_type:
                    raise ValueError("country, sym, date and signalType are all required")
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                existing = next((r for r in _load_feedback(country)
                                 if (r.get("sym"), r.get("date"), r.get("signalType"))
                                    == (sym, date, signal_type)), None)
                entry = {
                    "sym": sym, "date": date, "signalType": signal_type,
                    "country": country, "good": bool(body.get("good")),
                    "note": (body.get("note") or "").strip(),
                    "ratedAt": existing["ratedAt"] if existing else now,
                    "editedAt": now,
                }
                _upsert_feedback(country, entry)
                self._send(200, json.dumps(_load_feedback_all()), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}), "application/json")
            return

        if route.path == "/api/name":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                names = save_name(body.get("broker", ""), body.get("name", ""))
                self._send(200, json.dumps(names), "application/json")
            except Exception as error:
                self._send(400, json.dumps({"error": str(error)}),
                           "application/json")
            return
        self._send(404, "not found", "text/plain")


JOURNAL_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Journal</title>
<script src="/docs/vendor/lightweight-charts.js"></script>
<script src="/docs/std_chart.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root { --bg:#f5f6f8; --panel:#fff; --text:#1a1d24; --dim:#6b7280;
    --line:#e2e5ea; --up:#16a34a; --down:#dc2626; --warn:#ca8a04; --violet:#7c3aed;
    --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15; --violet:#a78bfa;
      --accent:#60a5fa; }
  }
  :root[data-theme="dark"] { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15; --violet:#a78bfa;
      --accent:#60a5fa; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-size:14px;
    font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-variant-numeric:tabular-nums; }
  main { padding:18px; max-width:1300px; margin:0 auto; }
  h1 { font-size:17px; margin:0 0 4px; }
  .sub { font-size:12px; color:var(--dim); margin:0 0 16px; max-width:680px; line-height:1.5; }

  .kpi-strip{display:grid; grid-template-columns:repeat(8,1fr); gap:8px; margin-bottom:10px;}
  @media (max-width:1000px){.kpi-strip{grid-template-columns:repeat(4,1fr);}}
  @media (max-width:560px){.kpi-strip{grid-template-columns:repeat(2,1fr);}}
  .kpi-tile{background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 12px;}
  .kpi-k{font-size:9.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--dim); margin-bottom:4px;}
  .kpi-v{font-size:17px; font-weight:800;}
  .kpi-v.up{color:var(--up);} .kpi-v.down{color:var(--down);}
  #breakdown{margin-bottom:16px;}
  .bd-wrap{display:grid; grid-template-columns:1fr 1fr; gap:14px;}
  @media (max-width:760px){.bd-wrap{grid-template-columns:1fr;}}
  .bd-title{font-size:10px; text-transform:uppercase; letter-spacing:.03em; color:var(--dim); margin-bottom:5px;}
  .bd-table{width:100%; border-collapse:collapse; font-size:11.5px; background:var(--panel);
    border:1px solid var(--line); border-radius:9px; overflow:hidden;}
  .bd-table th{font-size:9px; padding:5px 8px;}
  .bd-table td{padding:5px 8px; border-bottom:1px solid var(--line);}
  .bd-table tr:last-child td{border-bottom:none;}
  .bd-table .r{text-align:right;}
  .bd-table td.win{color:var(--up); font-weight:700;} .bd-table td.lose{color:var(--down); font-weight:700;}

  .filter-pills{display:flex; gap:7px; margin-bottom:14px; flex-wrap:wrap;}
  .filter-pill{font-size:12px; font-weight:600; border:1px solid var(--line); background:var(--panel); color:var(--dim);
    border-radius:20px; padding:6px 12px; cursor:pointer;}
  .filter-pill.active{background:var(--accent); border-color:var(--accent); color:#fff;}
  .filter-pill .n{font-size:10.5px; opacity:.8;}
  .filter-pill.gap{border-color:color-mix(in srgb, var(--warn) 50%, var(--line)); color:var(--warn);}
  .filter-pill.gap.active{background:var(--warn); border-color:var(--warn); color:#1a1d24;}

  #search{padding:7px 10px; font-size:13px; font-family:inherit; width:200px; margin-bottom:12px;
    background:var(--panel); border:1px solid var(--line); border-radius:7px; color:var(--text);}
  #search:focus{outline:none; border-color:var(--accent);}

  table{width:100%; border-collapse:collapse; font-size:12.5px; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; overflow:hidden;}
  th{text-align:left; font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--dim);
    padding:8px 10px; border-bottom:1px solid var(--line); font-weight:600; white-space:nowrap;}
  th[data-sort]{cursor:pointer; user-select:none;}
  th[data-sort]:hover{color:var(--text);}
  th.r, td.r{text-align:right;}
  td{padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:middle;}
  tr:last-child td{border-bottom:none;}
  tr.child td{background:color-mix(in srgb, var(--violet) 5%, transparent);}
  tr.child td:first-child{padding-left:22px;}
  .add-pill{font-size:9.5px; font-weight:700; color:var(--violet); background:rgba(124,58,237,.14);
    padding:1px 6px; border-radius:4px; margin-right:6px;}
  tr.clickable{cursor:pointer;} tr.clickable:hover{background:color-mix(in srgb, var(--accent) 6%, transparent);}
  .sym{font-weight:700;}
  .sym-cell{display:inline-flex; align-items:center; gap:7px;}
  .jlogo{border-radius:5px; flex:none; display:inline-flex; align-items:center; justify-content:center;
    font-size:8px; font-weight:800; color:#fff; overflow:hidden;}
  .jlogo img{width:100%; height:100%; object-fit:cover;}
  .acct{font-size:10px; font-weight:700; padding:2px 6px; border-radius:4px; background:var(--line); color:var(--dim);
    display:inline-flex; align-items:center; gap:4px;}
  .acct-broker{width:11px; height:11px; object-fit:contain; border-radius:2px;}
  .nav-badge{background:var(--warn,#ca8a04); color:#1a1d24; font-size:9.5px; font-weight:800; border-radius:9px;
    padding:1px 6px; margin-left:auto; flex:none;}
  .chart-box{margin:4px 0 8px; border:1px solid var(--line); border-radius:9px; overflow:hidden; background:var(--bg); min-height:44px;}
  .chart-box img{display:block; width:100%; height:auto; border-bottom:1px solid var(--line);}
  .chart-box img:last-child{border-bottom:none;}
  .chart-item{position:relative;}
  .chart-del{position:absolute; top:6px; right:6px; width:22px; height:22px; border-radius:6px; border:none;
    background:rgba(0,0,0,.55); color:#fff; font-size:14px; line-height:1; cursor:pointer;}
  .chart-del:hover{background:var(--down);}
  .chart-note{font-size:11px; color:var(--dim); padding:13px; text-align:center;}
  .paste-zone{border:1px dashed var(--line); border-radius:9px; padding:10px; text-align:center; font-size:11px;
    color:var(--dim); cursor:pointer; margin-bottom:6px;}
  .paste-zone:hover, .paste-zone:focus{border-color:var(--violet); color:var(--text); outline:none;}
  .setup-chip{font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:5px; background:rgba(124,58,237,.14); color:var(--violet);}
  .gap-badge{font-size:10px; font-weight:700; padding:2px 7px; border-radius:5px; background:rgba(240,85,75,.14); color:var(--down);}
  .ok-badge{font-size:10px; color:var(--dim);}
  .pnl.up{color:var(--up); font-weight:700;} .pnl.down{color:var(--down); font-weight:700;}
  #empty{padding:30px; text-align:center; color:var(--dim); font-size:12.5px;}
  #load-err{padding:12px 14px; background:color-mix(in srgb, var(--down) 10%, transparent);
    border:1px solid color-mix(in srgb, var(--down) 30%, transparent); border-radius:9px; color:var(--down);
    font-size:12.5px; margin-bottom:14px;}
  #load-err[hidden]{display:none;}

  .overlay{position:fixed; inset:0; background:rgba(0,0,0,.5); display:flex; align-items:flex-start; justify-content:center;
    padding:40px 20px; z-index:50; overflow-y:auto;}
  .overlay[hidden]{display:none;}
  .detail-page{background:var(--panel); border:1px solid var(--line); border-radius:14px; max-width:clamp(620px, 56vw, 1040px); width:100%;}
  .detail-head{padding:18px 22px; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between;}
  .detail-head b{font-size:16px;}
  .detail-close{background:none; border:none; font-size:20px; color:var(--dim); cursor:pointer;}
  .detail-body{padding:18px 22px;}
  .detail-grid{display:grid; grid-template-columns:1fr 1fr; gap:10px 20px; margin-bottom:16px; font-size:12.5px;}
  .detail-grid .k{color:var(--dim); font-size:11px;}
  .detail-grid .v{font-weight:600; margin-top:1px;}
  .edit-select, .edit-text{width:100%; font-size:12.5px; font-family:inherit; padding:6px 8px; border-radius:6px;
    border:1px solid var(--line); background:var(--bg); color:var(--text); margin-top:2px;}
  .edit-text{min-height:70px; resize:vertical;}
  .field-label{font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--dim); margin:14px 0 4px;}
  .save-status{font-size:11px; color:var(--up); margin-top:4px; height:14px;}
  .notion-link{font-size:12px; color:var(--accent); text-decoration:none;}
  #ins{margin-bottom:16px;}
  .itabs{display:flex; gap:4px; border-bottom:1px solid var(--line); margin:8px 0 14px; overflow-x:auto;}
  .itab{font-size:12.5px; font-weight:600; color:var(--dim); padding:8px 4px; margin-right:20px; border-bottom:2px solid transparent; cursor:pointer; white-space:nowrap;}
  .itab.on{color:var(--accent); border-bottom-color:var(--accent);}
  .itab small{display:block; font-size:10px; font-weight:500; color:var(--dim);}
  .icard{background:var(--panel); border:1px solid var(--line); border-radius:11px; padding:14px 16px; margin-bottom:12px;}
  .icard h3{font-size:13px; margin:0 0 3px;} .icard .isub{font-size:11.5px; color:var(--dim); margin:0 0 10px; max-width:760px; line-height:1.5;}
  .crow{display:grid; grid-template-columns:160px 1fr 72px 56px; align-items:center; gap:10px; padding:6px 6px; font-size:12.5px; border-radius:8px;}
  .crow.hd{color:var(--dim); font-size:10px; text-transform:uppercase; letter-spacing:.03em; border-bottom:1px solid var(--line); border-radius:0; padding-bottom:5px;}
  .crow.click{cursor:pointer;} .crow.click:hover{background:color-mix(in srgb, var(--accent) 8%, transparent);}
  .crow.sel{background:color-mix(in srgb, var(--accent) 10%, transparent); outline:1px solid var(--accent);}
  .ctrack{height:16px; border-radius:5px; background:var(--line); overflow:hidden;}
  .cfill{height:100%; display:flex; align-items:center; font-size:10px; font-weight:700; color:#fff; padding-left:6px; min-width:34px;}
  .found{font-size:12px; color:var(--dim); line-height:1.55; background:color-mix(in srgb, var(--warn) 10%, transparent); border:1px dashed color-mix(in srgb, var(--warn) 45%, transparent); border-radius:9px; padding:9px 12px; margin-top:10px;}
  .found.g{background:color-mix(in srgb, var(--up) 10%, transparent); border-color:color-mix(in srgb, var(--up) 45%, transparent);}
  .drill{margin-top:10px; border:1px solid var(--line); border-radius:10px; background:var(--bg); padding:10px 12px;}
  .drill h4{margin:0 0 6px; font-size:12px; display:flex; justify-content:space-between;} .drill h4 span.x{color:var(--dim); cursor:pointer; font-weight:600; font-size:11px;}
  .drill-scroll{max-height:260px; overflow:auto;}
  .drill table{width:100%; border-collapse:collapse; font-size:12px;} .drill th{text-align:left; font-size:10px; color:var(--dim); text-transform:uppercase; padding:4px 6px; border-bottom:1px solid var(--line);}
  .drill td{padding:5px 6px; border-bottom:1px solid var(--line);} .drill .r{text-align:right;} .drill tr.tc{cursor:pointer;} .drill tr.tc:hover td{background:color-mix(in srgb, var(--accent) 8%, transparent);}
  .i2{display:grid; grid-template-columns:1fr 1fr; gap:10px;} @media (max-width:760px){.i2{grid-template-columns:1fr;}}
  .mgrid{display:grid; grid-template-columns:repeat(3,1fr); gap:10px;} @media (max-width:860px){.mgrid{grid-template-columns:1fr 1fr;}}
  .mcard{border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:var(--panel); cursor:pointer;} .mcard:hover{border-color:var(--accent);} .mcard.sel{border-color:var(--violet); box-shadow:0 0 0 1px var(--violet);}
  .mcard .mt{font-size:12px; font-weight:700; display:flex; justify-content:space-between;} .mcard .mt span{font-weight:500; color:var(--dim); font-size:10.5px;} .mcard .mn{font-size:11px; color:var(--dim); margin:3px 0 6px;}
  table.lbt{width:100%; border-collapse:collapse; font-size:12.5px; background:var(--panel); border:1px solid var(--line); border-radius:9px;}
  table.lbt th{text-align:left; font-size:10.5px; color:var(--dim); text-transform:uppercase; padding:7px 9px; border-bottom:1px solid var(--line); cursor:pointer; user-select:none;}
  table.lbt td{padding:7px 9px; border-bottom:1px solid var(--line);} table.lbt .r{text-align:right;} table.lbt tr.tc{cursor:pointer;} table.lbt tr.tc:hover td{background:color-mix(in srgb, var(--accent) 8%, transparent);}
  .tagp{font-size:10px; font-weight:700; padding:1px 7px; border-radius:99px; margin-left:6px; background:rgba(240,85,75,.14); color:var(--down);} .tagp.g{background:rgba(46,204,113,.14); color:var(--up);}
  #mfe{margin-bottom:16px;}
  .mfe-tiles{display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:8px 0 10px;}
  @media (max-width:700px){.mfe-tiles{grid-template-columns:repeat(2,1fr);}}
  .mfe-note{font-size:11.5px; color:var(--dim); line-height:1.55; margin:0 0 10px; max-width:760px;}
  .mfe-bar{position:relative; height:8px; background:var(--line); border-radius:4px; min-width:90px;}
  .mfe-bar i{position:absolute; left:0; top:0; bottom:0; border-radius:4px; background:var(--up); opacity:.85;}
  .mfe-bar i.low{background:var(--warn);}
  .jtabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:0 0 14px;}
  .jtab{font:inherit;font-size:13px;font-weight:600;padding:9px 14px;border:0;background:none;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;}
  .jtab:hover{color:var(--text);} .jtab.on{color:var(--accent);border-bottom-color:var(--accent);}
  .dgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;} .dgrid .jw{grid-column:span 6;} .dgrid .jw.w12{grid-column:span 12;} .dgrid .jw.w7{grid-column:span 7;} .dgrid .jw.w5{grid-column:span 5;}
  @media (max-width:900px){.dgrid .jw,.dgrid .jw.w7,.dgrid .jw.w5{grid-column:span 12;}}
  .jw{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .jw h3{margin:0 0 10px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);}
  .jw .kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px 16px;} .jw .kv .k{font-size:11px;color:var(--dim);} .jw .kv .v{font-size:19px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums;} .jw .kv .s{font-size:11px;color:var(--dim);}
  .jw .strip{display:flex;gap:6px;flex-wrap:wrap;} .jw .strip span{min-width:64px;padding:5px 6px;border-radius:8px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;font-weight:700;font-size:13px;cursor:pointer;line-height:1.2;} .jw .strip span small{font-size:10px;font-weight:600;opacity:.85;} .jw .strip span:hover{filter:brightness(.95);outline:2px solid var(--accent);outline-offset:1px;}
  .jw .strip .W{background:rgba(34,197,94,.16);color:var(--up);border:1px solid rgba(34,197,94,.4);} .jw .strip .L{background:rgba(239,68,68,.14);color:var(--down);border:1px solid rgba(239,68,68,.4);}
  .dseg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;} .dseg button{border:0;background:transparent;color:var(--dim);font:600 12px inherit;font-family:inherit;padding:5px 11px;cursor:pointer;} .dseg button.on{background:var(--accent);color:#fff;}
  .dbox{border:1px dashed var(--line);border-radius:10px;padding:11px 13px;font-size:12px;color:var(--dim);line-height:1.5;margin-top:10px;}
  .dtbl{width:100%;border-collapse:collapse;font-size:12.5px;} .dtbl th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--dim);padding:7px 9px;border-bottom:1px solid var(--line);} .dtbl td{padding:7px 9px;border-bottom:1px solid var(--line);} .dtbl .r{text-align:right;} .dtbl tbody tr.dclick{cursor:pointer;} .dtbl tbody tr.dclick:hover td{background:color-mix(in srgb,var(--accent) 8%,transparent);}
  .tagrow{display:flex;flex-wrap:wrap;gap:6px;} .tagchip{border:1px solid var(--line);background:var(--panel);border-radius:99px;padding:3px 10px;font-size:11.5px;font-weight:600;cursor:pointer;color:var(--text);} .tagchip.on{background:var(--accent);border-color:var(--accent);color:#fff;}
  .rv-bar{display:flex;align-items:center;gap:12px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line);} .rv-stat{flex:1;font-size:12px;color:var(--dim);}
  .rv-btn{font:700 12.5px inherit;font-family:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;}
  .newsetup{display:flex;gap:6px;margin-top:6px;} .newsetup input{flex:1;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit;}
  #d-path{margin:4px 0 12px; border:1px solid var(--line); border-radius:9px; padding:8px 10px 4px;}
  #d-path[hidden]{display:none;}
</style></head><body>
<main>
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap">
    <div>
      <h1>Trade Journal</h1>
      <p class="sub">Reads all three trade logs (NG-IBKR, ShG-AO, SuG-AO) directly — the full record, open and closed. New trades and closes are logged from each broker's own execution history; editing a chip or the thesis writes straight back to Notion; paste a chart (Ctrl/Cmd+V) right in the detail view. Chart and Entry Thesis are the only two things left to fill in by hand.</p>
    </div>
    <button id="log-trade-btn" style="font-size:12px;font-weight:700;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;white-space:nowrap;margin-right:8px">+ Log a trade</button>
    <button id="sync-brokers-btn" style="font-size:12px;font-weight:700;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;white-space:nowrap">&#8635; Sync from brokers</button>
  </div>
  <div id="sync-banner" hidden style="font-size:12px;background:color-mix(in srgb, var(--accent) 10%, transparent);border:1px solid color-mix(in srgb, var(--accent) 30%, transparent);border-radius:9px;padding:10px 13px;margin-bottom:12px;line-height:1.6"></div>
  <div id="load-err" hidden></div>
  <div class="jtabs" id="jtabs"><button class="jtab" data-t="dash">Dashboard</button><button class="jtab" data-t="review">Monthly review</button><button class="jtab" data-t="trades">Trades</button></div>
  <div id="tab-dash" hidden></div>
  <div id="tab-review" hidden></div>
  <div id="tab-trades">

  <div class="kpi-strip">
    <div class="kpi-tile"><div class="kpi-k">Open positions</div><div class="kpi-v" id="kpi-open">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Closed trades</div><div class="kpi-v" id="kpi-closed">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Win rate</div><div class="kpi-v" id="kpi-winrate">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Expectancy / trade</div><div class="kpi-v" id="kpi-expectancy">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Avg win</div><div class="kpi-v up" id="kpi-avggain">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Avg loss</div><div class="kpi-v down" id="kpi-avgloss">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Total P&amp;L</div><div class="kpi-v" id="kpi-totalpnl">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Avg hold (days)</div><div class="kpi-v" id="kpi-hold">&mdash;</div></div>
  </div>
  <p class="sub" style="margin:-6px 0 14px">Win / P&amp;L% / Hold Days / Outcome are Notion's own formulas, read as-is; the aggregates below are computed from those over <b>closed</b> trades only, matching how you derived them in Notion. Every tile respects the filter and search above.</p>

  <details id="breakdown">
    <summary style="font-size:12px;font-weight:700;cursor:pointer;margin-bottom:8px">Performance by setup &mdash; entry &amp; exit counts, win rate, avg P&amp;L%</summary>
    <div class="bd-wrap">
      <div><div class="bd-title">Entry Setup</div><table class="bd-table" id="bd-entry"></table></div>
      <div><div class="bd-title">Exit Setup</div><table class="bd-table" id="bd-exit"></table></div>
    </div>
  </details>

  <details id="ins" open>
    <summary style="font-size:12px;font-weight:700;cursor:pointer;margin-bottom:4px">Trade insights &mdash; computed from your own log, price history and the terminal's group ranks</summary>
    <div class="itabs" id="itabs">
      <div class="itab on" data-p="calib">Self-rating calibration<small>do you know a good trade when you're in it?</small></div>
      <div class="itab" data-p="behav">Exit behavior &amp; hold time<small>what actually leaks</small></div>
      <div class="itab" data-p="board">Ticker leaderboard<small>names you keep re-fighting</small></div>
      <div class="itab" data-p="group">Group strength at entry<small>was the industry already leading?</small></div>
      <div class="itab" data-p="mfec">MFE &amp; capture<small>how much of the move you kept</small></div>
      <div class="itab" data-p="rule">Rule discipline<small>too early or too late vs your rule</small></div>
    </div>
    <div id="ipane"></div>
  </details>

  <details id="mfe">
    <summary style="font-size:12px;font-weight:700;cursor:pointer;margin-bottom:8px">MFE &amp; capture &mdash; how much of each winner's peak you kept</summary>
    <p class="mfe-note"><b>MFE</b> is the best open profit a trade showed between your entry and exit (daily highs). <b>Capture</b> is your realised P&amp;L% divided by that peak: 100% means you sold at the top, 40% means you gave back 60% of it. <b>MAE</b> is the deepest drawdown from entry (daily lows). Click a row for the price path. Respects the account filter and search.</p>
    <div class="mfe-tiles" id="mfe-tiles"></div>
    <div style="overflow-x:auto"><table class="bd-table" id="mfe-table"></table></div>
  </details>

  <details id="bt">
    <summary style="font-size:12px;font-weight:700;cursor:pointer;margin-bottom:8px">Exit-rule backtest &mdash; what if you had sold by a rule instead</summary>
    <p class="mfe-note">Each closed trade is replayed from your <b>real entry price and date</b> using daily bars, and sold by the rule instead of when you actually sold. Rules run past your real exit if they hadn't fired yet. <label style="white-space:nowrap"><input type="checkbox" id="bt-stop" checked> also honour my initial stop (fills at the stop, or the open if it gapped)</label>. Pyramid adds are replayed as their own entries. <b>S</b> = stopped out, <b>H</b> = rule never fired (marked to the latest close). Respects the account filter and search.</p>
    <div style="overflow-x:auto"><table class="bd-table" id="bt-rules"></table></div>
    <div class="bd-title" style="margin-top:14px">Per trade &mdash; click a column to sort, a row for detail</div>
    <div style="overflow-x:auto"><table class="bd-table" id="bt-table"></table></div>
  </details>

  <div class="filter-pills" id="account-pills"></div>
  <div class="filter-pills" id="filter-pills">
    <button class="filter-pill active" data-filter="all">All</button>
    <button class="filter-pill" data-filter="open">Open</button>
    <button class="filter-pill" data-filter="closed">Closed</button>
    <button class="filter-pill gap" data-filter="nosetup">&#9888; No entry setup <span class="n" id="cnt-nosetup"></span></button>
    <button class="filter-pill gap" data-filter="noexit">&#9888; Closed, no exit setup <span class="n" id="cnt-noexit"></span></button>
  </div>
  <input id="search" placeholder="Filter by ticker&hellip;">

  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>Ticker</th>
      <th data-sort="dateOpened" data-label="Opened">Opened</th>
      <th id="th-closed" data-sort="dateClosed" data-label="Closed">Closed</th>
      <th>Setup</th>
      <th data-sort="entryPrice" data-label="Entry" class="r">Entry</th>
      <th data-sort="shares" data-label="Shares" class="r">Shares</th>
      <th data-sort="costValue" data-label="Cost Value" class="r">Cost Value</th>
      <th>Stop</th>
      <th data-sort="pnlPct" data-label="P&amp;L%" class="r">P&amp;L%</th>
      <th>Completeness</th>
      <th id="th-account">Account</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  </div>
  <div id="empty" hidden>No trades match this filter.</div>
  </div>
</main>

<div class="overlay" id="lt-overlay" hidden>
  <div class="detail-page" style="max-width:520px">
    <div class="detail-head"><b>Log a trade</b><button class="detail-close" id="lt-close">&times;</button></div>
    <div class="detail-body">
      <p style="font-size:12px;color:var(--dim);margin:0 0 10px;line-height:1.5">For a buy or sell you made today that IBKR&rsquo;s report does not have yet. When the report arrives, the sync recognises it (same ticker, day and price) and does not add it a second time.</p>
      <div class="dseg" id="lt-mode" style="margin-bottom:12px"><button data-m="buy" class="on">I bought</button><button data-m="sell">I sold</button></div>
      <div id="lt-buy">
        <div class="field-label">Account</div><select class="edit-select" id="lt-acct"></select>
        <div class="field-label">Ticker</div><input class="edit-select" id="lt-tk" placeholder="e.g. STLD" autocomplete="off" style="text-transform:uppercase">
        <div class="field-label">Buy date</div><input class="edit-select" id="lt-date" type="date">
        <div style="display:flex;gap:10px"><div style="flex:1"><div class="field-label">Price</div><input class="edit-select" id="lt-px" type="number" step="0.01"></div><div style="flex:1"><div class="field-label">Shares</div><input class="edit-select" id="lt-sh" type="number" step="1"></div><div style="flex:1"><div class="field-label">Stop (optional)</div><input class="edit-select" id="lt-stop" type="number" step="0.01"></div></div>
        <label id="lt-addrow" style="display:none;margin-top:10px;font-size:12.5px"><input type="checkbox" id="lt-add" checked> This adds to my open <b id="lt-addtk"></b> position (pyramid add)</label>
      </div>
      <div id="lt-sell" hidden>
        <div class="field-label">Open position</div><select class="edit-select" id="lt-pos"></select>
        <div class="field-label">Sell date</div><input class="edit-select" id="lt-sdate" type="date">
        <div class="field-label">Sell price</div><input class="edit-select" id="lt-spx" type="number" step="0.01">
      </div>
      <div class="rv-bar"><span id="lt-msg" class="rv-stat"></span><button class="rv-btn" id="lt-go" type="button">Save to journal</button></div>
    </div>
  </div>
</div>

<div class="overlay" id="overlay" hidden>
  <div class="detail-page">
    <div class="detail-head"><b id="d-ticker" class="sym-cell"></b><button class="detail-close" id="d-close">&times;</button></div>
    <div class="detail-body">
      <div class="chart-box" id="d-chart"><div class="chart-note">Loading chart&hellip;</div></div>
      <div id="d-path" hidden></div>
      <div class="paste-zone" id="d-paste" tabindex="0">Paste a chart (Ctrl/Cmd+V) or click to choose an image &mdash; adds to this trade's Notion page</div>
      <input type="file" id="d-file" accept="image/*" hidden>
      <div class="detail-grid">
        <div><div class="k">Account</div><div class="v" id="d-account"></div></div>
        <div><div class="k">Date Opened / Closed</div><div class="v" id="d-dates"></div></div>
        <div><div class="k">Entry / Exit Price</div><div class="v" id="d-prices"></div></div>
        <div><div class="k">Shares</div><div class="v" id="d-shares"></div></div>
        <div><div class="k">Cost Value <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-cost"></div></div>
        <div><div class="k">Initial Stop $ / % <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-stop"></div></div>
        <div><div class="k">P&amp;L ($)</div><div class="v" id="d-pnl-usd"></div></div>
        <div><div class="k">P&amp;L % <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-pnl"></div></div>
        <div><div class="k">Hold days <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-hold"></div></div>
        <div><div class="k">MFE / capture / MAE</div><div class="v" id="d-mfe">&mdash;</div></div>
        <div><a class="notion-link" id="d-notion-link" href="#" target="_blank" rel="noopener">Open this trade in Notion &rarr;</a></div>
      </div>

      <div class="field-label">Entry Setup</div>
      <select class="edit-select" id="d-entrysetup"></select>
      <div class="field-label">Exit Setup</div>
      <select class="edit-select" id="d-exitsetup"></select>
      <div class="field-label">Buy Quality</div>
      <select class="edit-select" id="d-buyquality"></select>
      <div class="field-label">Sell Quality</div>
      <select class="edit-select" id="d-sellquality"></select>
      <div class="field-label">Entry Thesis (pre-trade notes)</div>
      <textarea class="edit-text" id="d-thesis"></textarea>
      <div class="field-label">Post-trade notes</div>
      <textarea class="edit-text" id="d-post" placeholder="What changed after the trade, and what will you carry forward?"></textarea>
      <div class="field-label">Emotion</div>
      <select class="edit-select" id="d-emotion"></select>
      <div class="field-label">Process tags (pick any that apply)</div>
      <div class="tagrow" id="d-tags"></div>
      <div class="save-status" id="d-save-status"></div>
      <div class="rv-bar"><span id="d-rvstat" class="rv-stat"></span><button class="rv-btn" id="d-rv-next" type="button">Save &amp; next</button></div>
    </div>
  </div>
</div>

<script>
let ALL_TRADES = [];
let SCHEMA = {};
let BT = null;         // {rules, byId}
let BT0 = null;        // same, stops off: the pure rule-vs-you comparison
let GRP = {};          // pageId -> {industry, rank, of, quartile}
let insTab = "calib", insDrill = {};
let btSort = { key: "actual", dir: -1 };
let MFE = {};          // pageId -> {mfe, mae, capture, gaveBack, peakDate}
let mfeSort = { key: "gaveBack", dir: -1 };
let activeFilter = "all";
let searchTerm = "";

function esc(s){ var d=document.createElement("div"); d.textContent=(s==null?"":String(s)); return d.innerHTML; }
function logoColor(s){ let h=0; for(let i=0;i<(s||"").length;i++) h=(h*31+s.charCodeAt(i))%360; return "hsl("+h+",42%,45%)"; }
const ACCOUNT_META = {
  "NG-IBKR": { flag: "🇺🇸", brokerLogo: "interactive-brokers-group" },
  "ShG-AO":  { flag: "🇮🇳", brokerLogo: "angel-broking" },
  "SuG-AO":  { flag: "🇮🇳", brokerLogo: "angel-broking" },
};
function acctChip(account){
  const m = ACCOUNT_META[account] || {};
  const broker = m.brokerLogo
    ? '<img class="acct-broker" src="https://s3-symbol-logo.tradingview.com/' + m.brokerLogo + '.svg" alt="">' : "";
  return '<span class="acct">' + (m.flag || "") + ' ' + broker + esc(account) + '</span>';
}
function jlogo(sym, logoid, px){
  px = px || 18;
  const init = esc((sym||"").slice(0,2));
  const inner = logoid
    ? '<img src="https://s3-symbol-logo.tradingview.com/' + esc(logoid) + '.svg" onerror="this.replaceWith(document.createTextNode(\'' + init + '\'))">'
    : init;
  return '<span class="jlogo" style="width:'+px+'px;height:'+px+'px;background:'+logoColor(sym||"")+'">'+inner+'</span>';
}

const CUR = { USD: "$", INR: "₹" };
function fmtDate(d) { return d || "&mdash;"; }
function fmtMoney(n, cur) { const s = CUR[cur] || "$"; return n == null ? "&mdash;" : (n < 0 ? "-" + s : s) + Math.abs(Number(n)).toLocaleString(undefined, {maximumFractionDigits: 2}); }
function fmtPct(n) { return n == null ? "&mdash;" : (n >= 0 ? "+" : "") + (Number(n) * 100).toFixed(1) + "%"; }
function mean(arr) { return arr.length ? arr.reduce((s, x) => s + x, 0) / arr.length : null; }

// Notion's own field definitions, replicated:
//   Win     = 1 if (Exit - Entry) > 0 else 0        (closed trades only)
//   PnL%    = (Exit - Entry) / Entry                (a fraction; Notion rounds to 4dp)
//   PnL     = (Exit - Entry) * Shares               (dollars)
//   Outcome = Profit / Loss / Breakeven by sign of PnL
// Everything here is over CLOSED trades, the way the Notion roll-ups are.
function computeKpis(trades) {
  const open = trades.filter(t => !t.dateClosed && !t.parentId);   // positions, not each add
  const closed = trades.filter(t => t.dateClosed);
  const rated = closed.filter(t => t.win != null);        // has both prices
  const wins = rated.filter(t => t.win === 1);
  const losers = closed.filter(t => t.pnl != null && t.pnl < 0);
  const winRate = rated.length ? wins.length / rated.length : null;
  const avgWin = mean(wins.map(t => t.pnlPct).filter(x => x != null));
  const avgLoss = mean(losers.map(t => t.pnlPct).filter(x => x != null));
  const expectancy = (winRate != null && avgWin != null && avgLoss != null)
    ? winRate * avgWin + (1 - winRate) * avgLoss : null;
  const avgHold = mean(closed.map(t => t.holdDays).filter(x => x != null && x > 0));
  const curs = [...new Set(closed.map(t => t.currency || "USD"))];
  const totalPnl = closed.reduce((s, t) => s + (t.pnl || 0), 0);

  document.getElementById("kpi-open").textContent = open.length;
  document.getElementById("kpi-closed").textContent = closed.length;
  document.getElementById("kpi-winrate").textContent = winRate == null ? "—" : Math.round(winRate * 100) + "%";
  const exp = document.getElementById("kpi-expectancy");
  exp.innerHTML = fmtPct(expectancy);
  exp.className = "kpi-v" + (expectancy == null ? "" : expectancy >= 0 ? " up" : " down");
  document.getElementById("kpi-avggain").innerHTML = fmtPct(avgWin);
  document.getElementById("kpi-avgloss").innerHTML = fmtPct(avgLoss);
  const tp = document.getElementById("kpi-totalpnl");
  if (!closed.length) { tp.innerHTML = "—"; tp.className = "kpi-v"; }
  else if (curs.length > 1) { tp.innerHTML = '<span style="font-size:12px;font-weight:600">mixed &mdash; filter by account</span>'; tp.className = "kpi-v"; }
  else { tp.innerHTML = fmtMoney(totalPnl, curs[0]); tp.className = "kpi-v" + (totalPnl >= 0 ? " up" : " down"); }
  document.getElementById("kpi-hold").textContent = avgHold == null ? "—" : avgHold.toFixed(0);

  drawBreakdown("bd-entry", trades, "entrySetup");
  drawBreakdown("bd-exit", trades, "exitSetup");
  drawMfe(trades);
  drawBt(trades);
  drawIns(trades);
}

// ---------------- Trade insights (tabs) ----------------
document.getElementById("itabs").addEventListener("click", e => {
  const t = e.target.closest(".itab"); if (!t) return;
  insTab = t.dataset.p; insDrill = {};
  document.querySelectorAll(".itab").forEach(x => x.classList.toggle("on", x === t));
  draw();
});
const pp = v => v == null ? "&mdash;" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const wrCol = w => w >= 0.5 ? "var(--up)" : w >= 0.3 ? "var(--warn)" : "var(--down)";
function grpStats(list) {
  const rated = list.filter(t => t.pnlPct != null);
  return { n: list.length, wr: rated.length ? rated.filter(t => t.pnlPct > 0).length / rated.length : 0, avg: mean(rated.map(t => t.pnlPct)) };
}
function calRows(id, groups, drillTitle) {
  // groups: [{name, list}] -> bar rows; a click opens that group's trades
  return groups.filter(g => g.list.length).map((g, i) => {
    const s = grpStats(g.list);
    return '<div class="crow click' + (insDrill[id] === g.name ? " sel" : "") + '" data-d="' + id + '" data-g="' + esc(g.name) + '"><div>' + esc(g.name) + '</div>'
      + '<div class="ctrack"><div class="cfill" style="width:' + Math.max(6, s.wr * 100) + '%;background:' + wrCol(s.wr) + '">' + (s.wr * 100).toFixed(1) + '%</div></div>'
      + '<div class="r ' + (s.avg >= 0 ? "up" : "down") + '" style="text-align:right;font-weight:700">' + pp(s.avg) + '</div><div style="text-align:right;color:var(--dim)">' + s.n + '</div></div>';
  }).join("");
}
const CAL_HD = '<div class="crow hd"><div>Group</div><div>Win rate</div><div style="text-align:right">Avg P&amp;L%</div><div style="text-align:right">Trades</div></div>';
function tradeTable(list, extra) {
  return '<div class="drill-scroll"><table><thead><tr><th>Ticker</th><th>Opened</th><th>Closed</th>' + (extra ? extra.head : "") + '<th class="r">P&amp;L%</th></tr></thead><tbody>'
    + list.map(t => '<tr class="tc" data-i="' + ALL_TRADES.indexOf(t) + '"><td class="sym">' + esc(t.ticker) + '</td><td>' + fmtDate(t.dateOpened) + '</td><td>' + fmtDate(t.dateClosed) + '</td>'
      + (extra ? extra.cells(t) : "") + '<td class="r ' + (t.pnlPct >= 0 ? "up" : "down") + '" style="font-weight:700">' + pp(t.pnlPct) + '</td></tr>').join("") + '</tbody></table></div>';
}
function drillBox(id, title, list, extra) {
  if (insDrill[id] == null) return '<div class="drill" id="dr-' + id + '" hidden></div>';
  const sorted = list.slice().sort((a, b) => b.pnlPct - a.pnlPct);
  return '<div class="drill" id="dr-' + id + '"><h4><span>' + esc(title) + ' &mdash; ' + list.length + ' trades, best to worst</span><span class="x" data-close="' + id + '">close &times;</span></h4>' + tradeTable(sorted, extra) + '</div>';
}
function wireIns(pane, trades, groupsById) {
  pane.querySelectorAll(".crow.click").forEach(r => r.onclick = () => { insDrill[r.dataset.d] = insDrill[r.dataset.d] === r.dataset.g ? null : r.dataset.g; drawIns(trades); });
  pane.querySelectorAll("[data-close]").forEach(x => x.onclick = () => { insDrill[x.dataset.close] = null; drawIns(trades); });
  pane.querySelectorAll("tr.tc").forEach(tr => tr.onclick = () => openDetail(ALL_TRADES[Number(tr.dataset.i)]));
}
function cell(v, cls) { return '<td class="r ' + (cls || "") + '">' + v + '</td>'; }

function drawIns(trades) {
  const pane = document.getElementById("ipane");
  if (!pane) return;
  const closed = trades.filter(t => t.dateClosed && t.pnlPct != null);
  const core = closed.filter(t => !t.parentId);
  if (!closed.length) { pane.innerHTML = '<div class="found">No closed trades match this filter.</div>'; return; }
  let html = "";
  const drillFor = (id, title, groups) => { const g = groups.find(x => x.name === insDrill[id]); return g ? drillBox(id, title + ": " + g.name, g.list) : '<div class="drill" hidden></div>'; };
  if (insTab === "calib") {
    const by = (key, order) => order.map(name => ({ name, list: closed.filter(t => t[key] === name) }));
    const buy = by("buyQuality", ["Right Buy", "Marginal", "Wrong Buy"]), sell = by("sellQuality", ["Good Sell", "Marginal", "Bad Sell"]);
    const bS = buy.map(g => grpStats(g.list)), rated = buy.reduce((s, g) => s + g.list.length, 0);
    html += '<div class="icard"><h3>Buy Quality vs. what actually happened</h3><p class="isub">The rating you gave each entry in the moment (' + rated + ' of ' + closed.length + ' closed trades rated) against the real outcome. Click a row for the trades.</p>'
      + CAL_HD + calRows("buy", buy) + drillFor("buy", "Buy Quality", buy);
    if (buy[0].list.length && buy[2].list.length) {
      const good = bS[0].wr > bS[2].wr + 0.1;
      html += '<div class="found ' + (good ? "g" : "") + '"><b>' + (good ? "Well calibrated" : "Weak calibration") + '</b> &mdash; "Right Buy" wins ' + (bS[0].wr * 100).toFixed(0) + '% (' + pp(bS[0].avg) + ' avg) vs "Wrong Buy" ' + (bS[2].wr * 100).toFixed(0) + '% (' + pp(bS[2].avg) + '). '
        + (good ? "Your read on entry quality holds up; the leak is elsewhere." : "Your in-the-moment rating isn't separating winners from losers yet.") + '</div>';
    }
    html += '</div><div class="icard"><h3>Sell Quality vs. what actually happened</h3><p class="isub">Same check for exits: Good Sell / Marginal / Bad Sell.</p>' + CAL_HD + calRows("sell", sell) + drillFor("sell", "Sell Quality", sell) + '</div>';
    pane.innerHTML = html; wireIns(pane, trades); return;
  }
  if (insTab === "behav") {
    const names = [...new Set(closed.map(t => t.exitSetup).filter(Boolean))];
    const ex = names.map(n => ({ name: n, list: closed.filter(t => t.exitSetup === n) })).sort((a, b) => grpStats(b.list).avg - grpStats(a.list).avg);
    const bk = [["31+ days", 31, 1e9], ["15-30 days", 15, 30], ["8-14 days", 8, 14], ["4-7 days", 4, 7], ["2-3 days", 2, 3], ["0-1 day", 0, 1]];
    const hold = bk.map(([name, lo, hi]) => ({ name, list: core.filter(t => t.holdDays != null && t.holdDays >= lo && t.holdDays <= hi) }));
    html += '<div class="icard"><h3>Exit setup &mdash; the real breakdown</h3><p class="isub">Which exit behaviours make money and which are the real leaks, from the Exit Setup you tagged. Sorted best to worst average. Click a row for the trades.</p>'
      + CAL_HD + calRows("exit", ex) + drillFor("exit", "Exit", ex);
    const worst = ex.filter(g => g.list.length >= 3).sort((a, b) => grpStats(a.list).avg - grpStats(b.list).avg)[0], best = ex.filter(g => g.list.length >= 3)[0];
    if (worst && best) html += '<div class="found"><b>Reading it</b> &mdash; best exit behaviour: <b>' + esc(best.name) + '</b> (' + pp(grpStats(best.list).avg) + ' avg, n=' + best.list.length + '); biggest leak: <b>' + esc(worst.name) + '</b> (' + pp(grpStats(worst.list).avg) + ' avg, n=' + worst.list.length + ').</div>';
    html += '</div><div class="icard"><h3>Hold time vs. outcome</h3><p class="isub">Closed core trades (pyramid adds excluded) bucketed by days held.</p>' + CAL_HD + calRows("hold", hold) + drillFor("hold", "Held", hold) + '</div>';
    pane.innerHTML = html; wireIns(pane, trades); return;
  }
  if (insTab === "board") {
    const map = {};
    closed.forEach(t => { (map[t.ticker] = map[t.ticker] || []).push(t); });
    const curs = [...new Set(closed.map(t => t.currency || "USD"))];
    const cur = curs.includes("USD") ? "USD" : curs[0];
    let rows = Object.keys(map).filter(k => map[k].length >= 3).map(k => {
      const s = grpStats(map[k]);
      return { tk: k, n: s.n, wr: s.wr, avgp: s.avg, tot: map[k].filter(t => (t.currency || "USD") === cur).reduce((a, t) => a + (t.pnl || 0), 0), list: map[k] };
    });
    const key = insDrill.lbKey || "n", dir = insDrill.lbDir || -1;
    rows.sort((a, b) => key === "tk" ? dir * a.tk.localeCompare(b.tk) : dir * (a[key] - b[key]));
    const th = (k, l, r) => '<th data-k="' + k + '"' + (r ? ' class="r"' : "") + '>' + l + (key === k ? (dir === -1 ? " ▼" : " ▲") : "") + '</th>';
    html += '<div class="icard"><h3>Tickers you\'ve traded 3+ times</h3><p class="isub">Names you trade well repeatedly, and names you keep re-entering that keep costing you. $ totals are ' + cur + ' only (accounts in other currencies are left out, never mixed). Click a header to sort, a row for its trades.</p>'
      + '<table class="lbt"><thead><tr>' + th("tk", "Ticker") + th("n", "Trades", 1) + th("wr", "Win %", 1) + th("avgp", "Avg P&amp;L%", 1) + th("tot", "Total " + (cur === "USD" ? "$" : cur), 1) + '</tr></thead><tbody>'
      + (rows.length ? rows.map(r => '<tr class="tc" data-t="' + esc(r.tk) + '"><td class="sym">' + esc(r.tk) + (r.tot >= 800 ? '<span class="tagp g">works</span>' : r.tot <= -500 ? '<span class="tagp">re-fought</span>' : "") + '</td>'
        + cell(r.n) + cell((r.wr * 100).toFixed(1) + "%") + cell(pp(r.avgp), r.avgp >= 0 ? "up" : "down") + cell(fmtMoney(r.tot, cur), r.tot >= 0 ? "up" : "down")).join("</tr>") + "</tr>" : '<tr><td colspan="5" style="color:var(--dim)">No ticker has 3+ closed trades under this filter.</td></tr>')
      + '</tbody></table>';
    const open = insDrill.lbOpen && map[insDrill.lbOpen];
    if (open) html += '<div class="drill"><h4><span>' + esc(insDrill.lbOpen) + ' &mdash; ' + open.length + ' trades</span><span class="x" data-lbclose="1">close &times;</span></h4>' + tradeTable(open.slice().sort((a, b) => b.pnlPct - a.pnlPct)) + '</div>';
    html += '</div>';
    pane.innerHTML = html;
    pane.querySelectorAll("th[data-k]").forEach(h => h.onclick = () => { const k = h.dataset.k; insDrill.lbDir = (insDrill.lbKey || "n") === k ? -(insDrill.lbDir || -1) : (k === "tk" ? 1 : -1); insDrill.lbKey = k; drawIns(trades); });
    pane.querySelectorAll("table.lbt tr.tc").forEach(r => r.onclick = () => { insDrill.lbOpen = insDrill.lbOpen === r.dataset.t ? null : r.dataset.t; drawIns(trades); });
    const lc = pane.querySelector("[data-lbclose]"); if (lc) lc.onclick = () => { insDrill.lbOpen = null; drawIns(trades); };
    pane.querySelectorAll(".drill tr.tc").forEach(tr => tr.onclick = () => openDetail(ALL_TRADES[Number(tr.dataset.i)]));
    return;
  }
  if (insTab === "group") {
    const withG = closed.filter(t => GRP[t.pageId]);
    const names = ["Top quartile", "2nd quartile", "3rd quartile", "Bottom quartile"];
    const gs = names.map((name, i) => ({ name, list: withG.filter(t => GRP[t.pageId].quartile === i + 1) }));
    const extra = { head: '<th>Industry</th><th class="r">Rank that day</th>', cells: t => '<td>' + esc(GRP[t.pageId].industry) + '</td><td class="r">#' + GRP[t.pageId].rank + ' / ' + GRP[t.pageId].of + '</td>' };
    const g = gs.find(x => x.name === insDrill.group);
    html += '<div class="icard"><h3>Was the group already strong when you bought?</h3><p class="isub">Each trade\'s ticker is mapped to its industry, then to that industry\'s rank on your entry date from the terminal\'s own history, and bucketed by how strong the group was (' + withG.length + ' of ' + closed.length + ' closed trades matched). Click a row for the trades.</p>'
      + CAL_HD + calRows("group", gs) + (g ? drillBox("group", g.name, g.list, extra) : "");
    const top = gs[0].list.length;
    if (withG.length) html += '<div class="found"><b>How much you buy leaders</b> &mdash; ' + top + ' of ' + withG.length + ' matched trades (' + Math.round(top / withG.length * 100) + '%) were bought when the industry sat in the top quartile of rank. Compare the win rates above to see whether group strength actually separates your winners from your losers, or is a filter you already apply.</div>';
    html += '</div>';
    pane.innerHTML = html; wireIns(pane, trades); return;
  }
  if (insTab === "mfec") {
    const rows = core.filter(t => MFE[t.pageId]);
    if (!rows.length) { pane.innerHTML = '<div class="found">Loading price history…</div>'; return; }
    const winners = rows.filter(t => t.pnlPct > 0), caps = winners.map(t => MFE[t.pageId].capture).filter(x => x != null);
    const rt = rows.filter(t => MFE[t.pageId].mfe >= 0.03 && t.pnlPct < 0);
    const wMae = winners.map(t => MFE[t.pageId].mae).sort((a, b) => a - b);
    const med = wMae.length ? wMae[Math.floor(wMae.length / 2)] : null, deep = winners.filter(t => MFE[t.pageId].mae <= -0.05).length;
    const tile = (k, v, sub, cls) => '<div class="kpi-tile"><div class="kpi-k">' + k + '</div><div class="kpi-v ' + (cls || "") + '">' + v + '</div><div style="font-size:11px;color:var(--dim);margin-top:2px">' + sub + '</div></div>';
    html += '<div class="icard"><h3>Maximum Favorable Excursion &mdash; how much of the move you actually kept</h3><p class="isub">MFE is the best price each trade reached (daily high) between entry and exit, from the terminal\'s real price history. <b>Capture</b> only makes sense for winners; for losers the useful number is the <b>giveback</b>: the profit cushion that round-tripped into a loss. ' + rows.length + ' of ' + core.length + ' closed core trades have price history.</p>'
      + '<div class="i2">' + tile("Avg capture, winners (n=" + caps.length + ")", caps.length ? Math.round(mean(caps) * 100) + "%" : "&mdash;", "Realised P&amp;L% &divide; peak MFE%. Exit price counts toward the peak.", "up")
      + tile("Round-trippers: had 3%+ cushion, closed a loss", rt.length + " / " + rows.length + " (" + Math.round(rt.length / rows.length * 100) + "%)", "Average giveback " + (rt.length ? (mean(rt.map(t => MFE[t.pageId].gaveBack)) * 100).toFixed(1) + " points" : "&mdash;"), "down") + '</div>';
    const names = [...new Set(rows.map(t => t.exitSetup || "— none"))];
    const groups = names.map(n => ({ name: n, list: rows.filter(t => (t.exitSetup || "— none") === n) })).filter(g => g.list.length >= 2).sort((a, b) => b.list.length - a.list.length);
    const AX0 = -25, AX1 = 45, pos = v => Math.max(0, Math.min(100, (v * 100 - AX0) / (AX1 - AX0) * 100));
    html += '<div style="font-size:12.5px;font-weight:700;margin:14px 0 4px">By exit type <span style="font-weight:400;color:var(--dim);font-size:11px">peak reached vs. what you kept &mdash; click a card, then a trade</span></div>'
      + '<div style="display:flex;gap:16px;font-size:11px;color:var(--dim);margin-bottom:8px"><span><i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--violet);margin-right:5px"></i>avg peak (MFE)</span><span><i style="display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--accent);margin-right:5px"></i>avg kept (actual)</span></div><div class="mgrid">'
      + groups.map(g => {
        const mfe = mean(g.list.map(t => MFE[t.pageId].mfe)), kept = mean(g.list.map(t => t.pnlPct)), lo = Math.min(pos(mfe), pos(kept)), hi = Math.max(pos(mfe), pos(kept));
        return '<div class="mcard' + (insDrill.mfeG === g.name ? " sel" : "") + '" data-g="' + esc(g.name) + '"><div class="mt">' + esc(g.name) + '<span>n=' + g.list.length + '</span></div><div class="mn">peak ' + pp(mfe) + ' &middot; kept ' + pp(kept) + ' &middot; gap ' + ((mfe - kept) * 100).toFixed(1) + 'pp</div>'
          + '<svg viewBox="0 0 100 22" width="100%" height="30"><line x1="0" x2="100" y1="11" y2="11" stroke="currentColor" stroke-opacity=".18"/><line x1="' + pos(0) + '" x2="' + pos(0) + '" y1="3" y2="19" stroke="currentColor" stroke-opacity=".35" stroke-dasharray="1.5 1.5"/>'
          + '<line x1="' + lo + '" x2="' + hi + '" y1="11" y2="11" stroke="var(--dim)" stroke-width="2.4"/><circle cx="' + pos(mfe) + '" cy="11" r="3.4" fill="var(--violet)"/><circle cx="' + pos(kept) + '" cy="11" r="3.4" fill="var(--accent)"/></svg></div>';
      }).join("") + '</div>';
    const sel = groups.find(g => g.name === insDrill.mfeG);
    if (sel) {
      const L = sel.list.slice().sort((a, b) => MFE[b.pageId].gaveBack - MFE[a.pageId].gaveBack);
      html += '<div class="drill"><h4><span>' + esc(sel.name) + ' &mdash; ' + L.length + ' trades, biggest gap first (click for the price path)</span><span class="x" data-mclose="1">close &times;</span></h4><div class="drill-scroll"><table><thead><tr><th>Ticker</th><th>Opened &rarr; closed</th><th class="r">Peak</th><th class="r">Worst dip</th><th class="r">Kept</th><th class="r">Left on table</th></tr></thead><tbody>'
        + L.map(t => { const m = MFE[t.pageId]; return '<tr class="tc" data-i="' + ALL_TRADES.indexOf(t) + '"><td class="sym">' + esc(t.ticker) + '</td><td>' + fmtDate(t.dateOpened) + ' &rarr; ' + fmtDate(t.dateClosed) + '</td><td class="r up">' + pp(m.mfe) + '</td><td class="r down">' + pp(m.mae) + '</td><td class="r ' + (t.pnlPct >= 0 ? "up" : "down") + '">' + pp(t.pnlPct) + '</td><td class="r">' + (m.gaveBack * 100).toFixed(1) + 'pp</td></tr>'; }).join("") + '</tbody></table></div></div>';
    }
    html += '</div><div class="icard"><h3>Stop calibration &mdash; how far do your eventual winners dip first?</h3><p class="isub">Maximum Adverse Excursion (worst drawdown from entry) on the ' + winners.length + ' trades that ended as winners.</p><div class="i2">'
      + tile("Median MAE, eventual winners", pp(med), "The typical dip before it worked", "down") + tile("Winners that were down 5%+ first", deep + " / " + winners.length + " (" + (winners.length ? Math.round(deep / winners.length * 100) : 0) + "%)", "A stop tighter than about 5% would have cut these") + '</div></div>';
    pane.innerHTML = html;
    pane.querySelectorAll(".mcard").forEach(c => c.onclick = () => { insDrill.mfeG = insDrill.mfeG === c.dataset.g ? null : c.dataset.g; drawIns(trades); });
    const mc = pane.querySelector("[data-mclose]"); if (mc) mc.onclick = () => { insDrill.mfeG = null; drawIns(trades); };
    pane.querySelectorAll("tr.tc").forEach(tr => tr.onclick = () => openDetail(ALL_TRADES[Number(tr.dataset.i)]));
    return;
  }
  if (insTab === "rule") {
    if (!BT0) { pane.innerHTML = '<div class="found">Replaying your rule on every trade…</div>'; return; }
    const rows = core.filter(t => BT0.byId[t.pageId]).map(t => ({ t, r: BT0.byId[t.pageId].yourrule }));
    const dayGap = (a, b) => Math.round((new Date(a) - new Date(b)) / 864e5);
    const early = rows.filter(x => x.r.kind === "holding" || x.t.dateClosed < x.r.date), late = rows.filter(x => x.r.kind !== "holding" && x.t.dateClosed > x.r.date), same = rows.length - early.length - late.length;
    const edge = list => list.length ? mean(list.map(x => x.t.pnlPct - x.r.pct)) : null;
    const tile = (label, list, days, cls) => '<div class="kpi-tile"><div class="kpi-k">' + label + '</div><div class="kpi-v ' + cls + '">' + list.length + ' / ' + rows.length + ' (' + Math.round(list.length / (rows.length || 1) * 100) + '%)</div><div style="font-size:11px;color:var(--dim);margin-top:2px">Avg ' + (list.length ? Math.abs(mean(list.map(days))).toFixed(1) : "&mdash;") + ' days ' + (cls === "up" ? "early" : "late") + ' &middot; you ' + pp(mean(list.map(x => x.t.pnlPct))) + ' vs rule ' + pp(mean(list.map(x => x.r.pct))) + ' &middot; edge <b class="' + (edge(list) >= 0 ? "up" : "down") + '">' + (edge(list) == null ? "&mdash;" : (edge(list) * 100 >= 0 ? "+" : "") + (edge(list) * 100).toFixed(2) + "pp") + '</b></div></div>';
    html += '<div class="icard"><h3>Your stated sell rule, simulated against every trade</h3><p class="isub">Your rule: two closes below the 20 EMA, or a close below the 50 EMA. It is run day by day from each real entry (no stop applied) to find the date it would have fired, then compared with what you did. ' + rows.length + ' closed core trades.</p><div class="i2">'
      + tile("Sold BEFORE the rule would've fired", early, x => dayGap(x.r.date, x.t.dateClosed), "up") + tile("Held PAST where the rule would've fired", late, x => dayGap(x.t.dateClosed, x.r.date), "down") + '</div>'
      + '<div class="found ' + (edge(early) >= 0 ? "g" : "") + '"><b>Direct answer</b> &mdash; ' + Math.round(early.length / (rows.length || 1) * 100) + '% of the time you exit earlier than the rule ' + (edge(early) >= 0 ? "and that discretion is net positive" : "and that discretion has cost you") + ' (' + (edge(early) == null ? "&mdash;" : (edge(early) * 100 >= 0 ? "+" : "") + (edge(early) * 100).toFixed(2) + "pp") + '). In the ' + Math.round(late.length / (rows.length || 1) * 100) + '% where you held past it the effect is ' + (edge(late) == null ? "&mdash;" : (edge(late) * 100 >= 0 ? "+" : "") + (edge(late) * 100).toFixed(2) + "pp") + '. ' + same + ' trades exited on the rule\'s own day.</div></div>';
    pane.innerHTML = html; return;
  }
}

function drawBt(trades) {
  const rt = document.getElementById("bt-rules"), tt = document.getElementById("bt-table");
  if (!BT) { rt.innerHTML = '<tbody><tr><td style="color:var(--dim)">Replaying trades\u2026</td></tr></tbody>'; tt.innerHTML = ""; return; }
  const rows = trades.filter(t => t.dateClosed && BT.byId[t.pageId] && t.pnlPct != null);
  if (!rows.length) { rt.innerHTML = '<tbody><tr><td style="color:var(--dim)">No closed trades match this filter.</td></tr></tbody>'; tt.innerHTML = ""; return; }
  const curs = [...new Set(rows.map(t => t.currency || "USD"))];
  const dollars = (fn) => curs.length === 1 ? fmtMoney(rows.reduce((s, t) => s + fn(t) * (t.costValue || 0), 0), curs[0]) : "&mdash;";
  const cls = v => v == null ? "" : v >= 0 ? "win" : "lose";
  const stat = (get) => {
    const v = rows.map(get), w = v.filter(x => x > 0);
    return { avg: mean(v), wr: w.length / v.length };
  };
  const act = stat(t => t.pnlPct);
  const line = (name, get, isActual) => {
    const s = stat(get), diff = isActual ? null : s.avg - act.avg;
    const beat = isActual ? null : rows.filter(t => get(t) > t.pnlPct).length;
    return '<tr' + (isActual ? ' style="font-weight:700"' : "") + '><td>' + name + '</td>'
      + '<td class="r ' + cls(s.avg) + '">' + fmtPct(s.avg) + '</td>'
      + '<td class="r">' + Math.round(s.wr * 100) + '%</td>'
      + '<td class="r ' + cls(diff) + '">' + (isActual ? "&mdash;" : fmtPct(diff)) + '</td>'
      + '<td class="r">' + (isActual ? "&mdash;" : beat + " / " + rows.length) + '</td>'
      + '<td class="r ' + cls(isActual ? 1 : diff) + '">' + (isActual ? dollars(t => t.pnlPct) : dollars(t => get(t) - t.pnlPct)) + '</td></tr>';
  };
  rt.innerHTML = '<thead><tr><th>Exit</th><th class="r">Avg P&L%</th><th class="r">Win %</th><th class="r">vs your exits (avg)</th><th class="r">Beat your exit</th>'
    + '<th class="r">' + (curs.length === 1 ? "$ (on cost value)" : "$ (filter to one account)") + '</th></tr></thead><tbody>'
    + line("Your actual exits (" + rows.length + " trades)", t => t.pnlPct, true)
    + BT.rules.map(r => line('<span title="' + esc(r.desc) + '">' + esc(r.label) + '</span>', t => BT.byId[t.pageId][r.key].pct, false)).join("") + '</tbody>';

  const val = (t, k) => k === "ticker" ? (t.ticker || "") : k === "dateClosed" ? t.dateClosed : k === "actual" ? t.pnlPct : BT.byId[t.pageId][k].pct;
  const sorted = rows.slice().sort((a, b) => {
    const av = val(a, btSort.key), bv = val(b, btSort.key);
    return typeof av === "string" ? btSort.dir * av.localeCompare(bv) : btSort.dir * (av - bv);
  });
  const th = (k, label, r) => '<th data-bk="' + k + '"' + (r ? ' class="r"' : "") + ' style="cursor:pointer">' + label + (btSort.key === k ? (btSort.dir === -1 ? " \u25BC" : " \u25B2") : "") + '</th>';
  tt.innerHTML = '<thead><tr>' + th("ticker", "Ticker") + th("dateClosed", "Closed") + th("actual", "Yours", 1)
    + BT.rules.map(r => th(r.key, esc(r.label), 1)).join("") + '</tr></thead><tbody>'
    + sorted.slice(0, 300).map(t => {
      const b = BT.byId[t.pageId];
      return '<tr class="clickable" data-i="' + ALL_TRADES.indexOf(t) + '"><td class="sym"><span class="sym-cell">' + jlogo(t.ticker, t.logoid, 16) + esc(t.ticker) + '</span></td><td>' + fmtDate(t.dateClosed) + '</td>'
        + '<td class="r ' + cls(t.pnlPct) + '">' + fmtPct(t.pnlPct) + '</td>'
        + BT.rules.map(r => {
          const x = b[r.key];
          return '<td class="r ' + cls(x.pct) + '" title="' + (x.kind === "stop" ? "stopped out " : x.kind === "holding" ? "never fired; marked to latest close " : "exit ") + x.date + '">' + fmtPct(x.pct) + (x.kind === "stop" ? ' <span style="color:var(--dim)">S</span>' : x.kind === "holding" ? ' <span style="color:var(--dim)">H</span>' : "") + '</td>';
        }).join("") + '</tr>';
    }).join("") + '</tbody>';
  tt.querySelectorAll("th[data-bk]").forEach(h => h.onclick = () => {
    const k = h.dataset.bk;
    if (btSort.key === k) btSort.dir *= -1; else { btSort.key = k; btSort.dir = -1; }
    drawBt(trades);
  });
  tt.querySelectorAll("tr.clickable").forEach(tr => tr.onclick = () => openDetail(ALL_TRADES[Number(tr.dataset.i)]));
}

function loadBt0() {
  fetch("/api/journal/backtest?stop=0").then(r => r.json()).then(d => {
    if (!d.trades) return;
    BT0 = { rules: d.rules, byId: {} };
    d.trades.forEach(x => { BT0.byId[x.pageId] = x.results; });
    draw();
  }).catch(() => {});
}
function loadGroups() {
  fetch("/api/journal/group").then(r => r.json()).then(rows => {
    if (!Array.isArray(rows)) return;
    rows.forEach(r => { GRP[r.pageId] = r; });
    draw();
  }).catch(() => {});
}

function loadBacktest() {
  const stop = document.getElementById("bt-stop").checked ? 1 : 0;
  BT = null; draw();
  fetch("/api/journal/backtest?stop=" + stop).then(r => r.json()).then(d => {
    if (!d.trades) return;
    BT = { rules: d.rules, byId: {} };
    d.trades.forEach(x => { BT.byId[x.pageId] = x.results; });
    draw();
  }).catch(() => {});
}
document.getElementById("bt-stop").addEventListener("change", loadBacktest);

function drawMfe(trades) {
  const rows = trades.filter(t => t.dateClosed && MFE[t.pageId]).map(t => ({ t, m: MFE[t.pageId] }));
  const tiles = document.getElementById("mfe-tiles"), table = document.getElementById("mfe-table");
  if (!rows.length) {
    tiles.innerHTML = "";
    table.innerHTML = '<tbody><tr><td style="color:var(--dim)">' + (Object.keys(MFE).length ? "No closed trades match this filter." : "Loading price history\u2026") + '</td></tr></tbody>';
    return;
  }
  const winners = rows.filter(r => r.t.pnlPct != null && r.t.pnlPct > 0);
  const caps = winners.map(r => r.m.capture).filter(x => x != null);
  const tile = (k, v, cls) => '<div class="kpi-tile"><div class="kpi-k">' + k + '</div><div class="kpi-v ' + (cls || "") + '">' + v + '</div></div>';
  const avgCap = mean(caps);
  tiles.innerHTML =
      tile("Avg MFE (all closed)", fmtPct(mean(rows.map(r => r.m.mfe))), "up")
    + tile("Avg capture (winners)", avgCap == null ? "&mdash;" : Math.round(avgCap * 100) + "%")
    + tile("Avg given back (winners)", fmtPct(mean(winners.map(r => r.m.gaveBack).filter(x => x != null))), "down")
    + tile("Avg MAE", fmtPct(mean(rows.map(r => r.m.mae))), "down");
  const val = r => mfeSort.key === "ticker" ? (r.t.ticker || "")
                 : mfeSort.key === "pnlPct" ? r.t.pnlPct
                 : mfeSort.key === "dateClosed" ? r.t.dateClosed : r.m[mfeSort.key];
  rows.sort((a, b) => {
    const av = val(a), bv = val(b);
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return typeof av === "string" ? mfeSort.dir * av.localeCompare(bv) : mfeSort.dir * (av - bv);
  });
  const th = (k, label, r) => '<th data-mk="' + k + '"' + (r ? ' class="r"' : "") + ' style="cursor:pointer">' + label
    + (mfeSort.key === k ? (mfeSort.dir === -1 ? " \u25BC" : " \u25B2") : "") + '</th>';
  table.innerHTML = '<thead><tr>' + th("ticker", "Ticker") + th("dateClosed", "Closed")
    + th("pnlPct", "P&L%", 1) + th("mfe", "MFE", 1) + th("capture", "Capture", 1)
    + th("gaveBack", "Gave back", 1) + th("mae", "MAE", 1) + '<th>Peak on</th></tr></thead><tbody>'
    + rows.slice(0, 300).map(r => {
      const c = r.m.capture, w = c == null ? 0 : Math.max(0, Math.min(1, c)) * 100;
      return '<tr class="clickable" data-i="' + ALL_TRADES.indexOf(r.t) + '"><td class="sym"><span class="sym-cell">' + jlogo(r.t.ticker, r.t.logoid, 16) + esc(r.t.ticker) + '</span></td>'
        + '<td>' + fmtDate(r.t.dateClosed) + '</td>'
        + '<td class="r ' + (r.t.pnlPct == null ? "" : r.t.pnlPct >= 0 ? "win" : "lose") + '">' + fmtPct(r.t.pnlPct) + '</td>'
        + '<td class="r">' + fmtPct(r.m.mfe) + '</td>'
        + '<td class="r"><div style="display:flex;align-items:center;gap:8px;justify-content:flex-end"><span>' + (c == null ? "&mdash;" : Math.round(c * 100) + "%") + '</span><span class="mfe-bar"><i class="' + (c != null && c < 0.5 ? "low" : "") + '" style="width:' + w + '%"></i></span></div></td>'
        + '<td class="r">' + fmtPct(r.m.gaveBack) + '</td>'
        + '<td class="r">' + fmtPct(r.m.mae) + '</td>'
        + '<td>' + fmtDate(r.m.peakDate) + '</td></tr>';
    }).join("") + '</tbody>';
  table.querySelectorAll("th[data-mk]").forEach(h => h.onclick = () => {
    const k = h.dataset.mk;
    if (mfeSort.key === k) mfeSort.dir *= -1; else { mfeSort.key = k; mfeSort.dir = -1; }
    drawMfe(trades);
  });
  table.querySelectorAll("tr.clickable").forEach(tr => tr.onclick = () => openDetail(ALL_TRADES[Number(tr.dataset.i)]));
}

let pathChart = null;
// The trade's chart: the standard chart (candles, EMA 10/20/50, volume with its 30-day average,
// 52-week lines as of the entry date) with the buy and sell arrows running out into empty space.
function drawPath(t) {
  const box = document.getElementById("d-path");
  if (pathChart) { pathChart.destroy(); pathChart = null; }
  box.hidden = true; box.innerHTML = "";
  if (!t.dateOpened || !window.StdChart) return;
  // An open trade has no close date: the chart then runs to the latest bar with just the entry arrow.
  fetch("/api/journal/path?country=" + encodeURIComponent(t.country || "US") + "&symbol=" + encodeURIComponent(t.ticker || "")
        + "&from=" + t.dateOpened + "&to=" + (t.dateClosed || "")).then(r => r.json()).then(d => {
    if (!d.series || currentTrade !== t) return;
    const s = d.series, entryIdx = Math.max(0, s.dates.findIndex(x => x >= t.dateOpened));
    const lines = [], m = MFE[t.pageId];
    if (t.initialStop) lines.push({p: t.initialStop, color: "#dc2626", title: "stop"});
    if (m) lines.push({p: m.peak, color: "#16a34a", title: "peak"});
    box.hidden = false;
    pathChart = StdChart.mount(box, {t: t.ticker, name: t.name || t.ticker, d: s.dates, o: s.open, h: s.high, l: s.low, c: s.close, v: s.volume,
      logoUrl: t.logoid ? "/api/logo/" + encodeURIComponent(t.logoid) : "", ht: Math.round(Math.max(340, Math.min(620, innerHeight * 0.44))), tf: t.dateClosed ? "Trade" : "6M", entryIdx: entryIdx, lines: lines,
      trades: [{d: t.dateOpened, a: "Buy", p: t.entryPrice, q: t.shares}, t.dateClosed ? {d: t.dateClosed, a: "Sell", p: t.exitPrice, q: t.shares} : null].filter(x => x && x.p)});
  }).catch(() => {});
}

function drawBreakdown(elId, trades, key) {
  const groups = {};
  trades.forEach(t => {
    const g = t[key] || "— none";
    (groups[g] = groups[g] || []).push(t);
  });
  const rows = Object.keys(groups).map(name => {
    const g = groups[name];
    const closed = g.filter(t => t.dateClosed);
    const rated = closed.filter(t => t.win != null);
    const wr = rated.length ? rated.filter(t => t.win === 1).length / rated.length : null;
    const avgP = mean(closed.map(t => t.pnlPct).filter(x => x != null));
    return { name, n: g.length, closed: closed.length, wr, avgP };
  }).sort((a, b) => b.n - a.n);
  document.getElementById(elId).innerHTML =
    '<thead><tr><th>Setup</th><th class="r">Trades</th><th class="r">Closed</th><th class="r">Win %</th><th class="r">Avg P&L%</th></tr></thead><tbody>'
    + rows.map(r =>
      '<tr><td>' + r.name + '</td><td class="r">' + r.n + '</td><td class="r">' + r.closed + '</td>'
      + '<td class="r">' + (r.wr == null ? "—" : Math.round(r.wr * 100) + "%") + '</td>'
      + '<td class="r ' + (r.avgP == null ? "" : r.avgP >= 0 ? "win" : "lose") + '">' + fmtPct(r.avgP) + '</td></tr>'
    ).join("") + '</tbody>';
}

let activeAccount = "all";
// Charts are imported automatically now, so a missing chart or thesis is not a gap. Only a missing setup is.
function isPending(t) { return !t.entrySetup || (!!t.dateClosed && !t.exitSetup); }
function passesFilter(t) {
  if (activeAccount !== "all" && t.account !== activeAccount) return false;
  if (searchTerm && !(t.ticker || "").toLowerCase().includes(searchTerm)) return false;
  if (activeFilter === "open") return !t.dateClosed;
  if (activeFilter === "closed") return !!t.dateClosed;
  if (activeFilter === "nosetup") return !t.entrySetup;
  if (activeFilter === "noexit") return !!t.dateClosed && !t.exitSetup;
  if (activeFilter === "pending") return isPending(t);
  return true;
}

function completenessBadge(t) {
  const bits = [];
  if (!t.entrySetup) bits.push('no entry setup');
  if (t.dateClosed && !t.exitSetup) bits.push('no exit setup');
  return bits.length ? '<span class="gap-badge">' + bits.join(', ') + '</span>'
                      : '<span class="ok-badge">&#10003; complete</span>';
}
function updatePillCounts() {
  document.getElementById("cnt-nosetup").textContent = ALL_TRADES.filter(t => !t.entrySetup).length || "";
  document.getElementById("cnt-noexit").textContent = ALL_TRADES.filter(t => t.dateClosed && !t.exitSetup).length || "";
}

let sortState = { key: null, dir: -1 };
function sortRows(rows) {
  if (!sortState.key) {
    // Default: most-recently-active trade first -- a closed trade sorts by
    // its close date, an open one by its open date, so a new sync landing
    // either kind surfaces at the top without extra clicks.
    rows.sort((a, b) => (b.dateClosed || b.dateOpened || "").localeCompare(a.dateClosed || a.dateOpened || ""));
    return rows;
  }
  const k = sortState.key, dir = sortState.dir;
  rows.sort((a, b) => {
    const av = a[k], bv = b[k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return typeof av === "string" ? dir * av.localeCompare(bv) : dir * (av - bv);
  });
  return rows;
}
function updateSortIndicators() {
  document.querySelectorAll("th[data-sort]").forEach(th => {
    const active = sortState.key === th.dataset.sort;
    th.textContent = th.dataset.label + (active ? (sortState.dir === -1 ? " ▼" : " ▲") : "");
  });
}
document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  if (sortState.key === key) sortState.dir *= -1;
  else { sortState.key = key; sortState.dir = -1; }
  updateSortIndicators();
  draw();
}));
updateSortIndicators();

function draw() {
  const rows = sortRows(ALL_TRADES.filter(passesFilter));
  computeKpis(rows);
  // Pyramid adds sit directly under their core row, whatever the sort.
  const ids = new Set(rows.map(r => r.pageId));
  const kids = {};
  rows.forEach(r => { if (r.parentId && ids.has(r.parentId)) (kids[r.parentId] = kids[r.parentId] || []).push(r); });
  const nested = [];
  rows.forEach(r => {
    if (r.parentId && ids.has(r.parentId)) return;
    nested.push(r);
    (kids[r.pageId] || []).sort((a, b) => (a.dateOpened || "").localeCompare(b.dateOpened || "")).forEach(k => nested.push(k));
  });

  // A single account is already implied by the account pill you're on --
  // showing the chip on every row just repeats it. Only "All accounts"
  // needs the column to tell rows apart.
  const showAccount = activeAccount === "all";
  document.getElementById("th-account").style.display = showAccount ? "" : "none";
  // The Closed column is blank on every row while viewing Open, so it's
  // pure dead space there.
  const showClosed = activeFilter !== "open";
  document.getElementById("th-closed").style.display = showClosed ? "" : "none";

  const tbody = document.getElementById("rows");
  document.getElementById("empty").hidden = rows.length > 0;
  tbody.innerHTML = nested.map((t, i) => {
    const idx = ALL_TRADES.indexOf(t);
    const pnlCls = t.pnlPct == null ? "" : (t.pnlPct >= 0 ? "up" : "down");
    const stop = t.initialStop == null ? "&mdash;" : fmtMoney(t.initialStop, t.currency) + (t.initialStopPct != null ? ' <span style="color:var(--dim)">(' + fmtPct(-Math.abs(t.initialStopPct)) + ')</span>' : "");
    return '<tr class="clickable' + (t.parentId ? " child" : "") + '" data-idx="' + idx + '">'
      + '<td class="sym"><span class="sym-cell">' + (t.parentId ? '<span class="add-pill">&#8627; Add</span>' : "") + jlogo(t.ticker, t.logoid, 16) + (t.ticker || "&mdash;") + '</span></td>'
      + '<td>' + fmtDate(t.dateOpened) + '</td>'
      + '<td' + (showClosed ? "" : ' style="display:none"') + '>' + fmtDate(t.dateClosed) + '</td>'
      + '<td>' + (t.entrySetup ? '<span class="setup-chip">' + t.entrySetup + '</span>' : '<span style="color:var(--dim);font-size:11.5px">&mdash;</span>') + '</td>'
      + '<td class="r">' + fmtMoney(t.entryPrice, t.currency) + '</td>'
      + '<td class="r">' + (t.shares ?? "&mdash;") + '</td>'
      + '<td class="r">' + fmtMoney(t.costValue, t.currency) + '</td>'
      + '<td>' + stop + '</td>'
      + '<td class="pnl r ' + pnlCls + '">' + fmtPct(t.pnlPct) + '</td>'
      + '<td>' + completenessBadge(t) + '</td>'
      + '<td' + (showAccount ? "" : ' style="display:none"') + '>' + acctChip(t.account) + '</td>'
    + '</tr>';
  }).join("");
  tbody.querySelectorAll("tr").forEach(row => row.onclick = () => openDetail(ALL_TRADES[Number(row.dataset.idx)]));
}

function fillSelect(id, options, current) {
  const el = document.getElementById(id);
  el.innerHTML = '<option value="">&mdash;</option>' + options.map(o =>
    '<option value="' + o + '"' + (o === current ? " selected" : "") + '>' + o + '</option>').join("");
}

let currentTrade = null;
function openDetail(t) {
  currentTrade = t;
  document.getElementById("overlay").hidden = false;
  document.getElementById("d-ticker").innerHTML = jlogo(t.ticker, t.logoid, 22) + esc(t.ticker || "");
  loadTradeChart(t);
  document.getElementById("d-account").innerHTML = acctChip(t.account);
  document.getElementById("d-dates").innerHTML = fmtDate(t.dateOpened) + " &rarr; " + fmtDate(t.dateClosed);
  document.getElementById("d-prices").innerHTML = fmtMoney(t.entryPrice, t.currency) + " &rarr; " + fmtMoney(t.exitPrice, t.currency);
  document.getElementById("d-shares").innerHTML = t.shares ?? "&mdash;";
  document.getElementById("d-cost").innerHTML = fmtMoney(t.costValue, t.currency);
  document.getElementById("d-stop").innerHTML = fmtMoney(t.initialStop, t.currency) + " / " + fmtPct(t.initialStopPct);
  const pl = t.pnl == null ? null : Number(t.pnl);
  document.getElementById("d-pnl-usd").innerHTML = pl == null ? "&mdash;" : '<span style="color:var(--' + (pl >= 0 ? "up" : "down") + ')">' + fmtMoney(pl, t.currency) + '</span>';
  document.getElementById("d-pnl").innerHTML = fmtPct(t.pnlPct);
  document.getElementById("d-hold").innerHTML = t.holdDays == null ? "&mdash;" : t.holdDays + (t.holdDays === 1 ? " day" : " days");
  document.getElementById("d-notion-link").href = t.notionUrl || "#";
  const m = MFE[t.pageId];
  document.getElementById("d-mfe").innerHTML = m
    ? fmtPct(m.mfe) + " / " + (m.capture == null ? "&mdash;" : Math.round(m.capture * 100) + "%") + " / " + fmtPct(m.mae)
    : "&mdash;";
  drawPath(t);
  fillSelect("d-entrysetup", SCHEMA["Entry Setup"] || [], t.entrySetup);
  fillSelect("d-exitsetup", SCHEMA["Exit Setup"] || [], t.exitSetup);
  fillSelect("d-buyquality", SCHEMA["Buy Quality"] || [], t.buyQuality);
  fillSelect("d-sellquality", SCHEMA["Sell Quality"] || [], t.sellQuality);
  document.getElementById("d-thesis").value = t.entryThesis || "";
  document.getElementById("d-save-status").textContent = "";
}

function saveField(propName, value, elId) {
  if (!currentTrade) return;
  const status = document.getElementById("d-save-status");
  status.textContent = "Saving...";
  status.style.color = "var(--dim)";
  fetch("/api/journal/update", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pageId: currentTrade.pageId, fields: {[propName]: value}}),
  }).then(r => r.json()).then(r => {
    if (r.ok) {
      currentTrade[elId] = value;
      status.textContent = "Saved.";
      status.style.color = "var(--up)";
      draw();
    } else {
      status.textContent = "Failed: " + (r.error || "unknown error");
      status.style.color = "var(--down)";
    }
  }).catch(() => { status.textContent = "Server not reachable."; status.style.color = "var(--down)"; });
}

function loadTradeChart(t) {
  const box = document.getElementById("d-chart");
  box.innerHTML = '<div class="chart-note">Loading chart&hellip;</div>';
  fetch("/api/journal/chart?pageId=" + encodeURIComponent(t.pageId) + "&mode=" + (t.chartMode || "blocks"))
    .then(r => r.json()).then(imgs => renderTradeChart(imgs && imgs.error ? [] : imgs))
    .catch(() => renderTradeChart([]));
}
function renderTradeChart(imgs) {
  const box = document.getElementById("d-chart");
  if (!imgs || !imgs.length) {
    box.innerHTML = "";     // the chart below is drawn automatically; pasting a screenshot is optional
    return;
  }
  box.innerHTML = imgs.map(i =>
    '<div class="chart-item"><a href="' + esc(i.url) + '" target="_blank"><img src="' + esc(i.url) + '"></a>'
    + '<button class="chart-del" title="Delete this chart" data-r="' + esc(i.ref || "") + '">&times;</button></div>').join("");
  box.querySelectorAll(".chart-del").forEach(b => b.onclick = () => {
    if (!confirm("Delete this chart from the trade's Notion page?")) return;
    const status = document.getElementById("d-save-status");
    status.textContent = "Deleting…"; status.style.color = "var(--dim)";
    fetch("/api/journal/chart", { method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ pageId: currentTrade.pageId, chartMode: currentTrade.chartMode, action: "delete", ref: b.dataset.r }) })
      .then(r => r.json()).then(res => {
        if (res.error) { status.textContent = "Failed: " + res.error; status.style.color = "var(--down)"; return; }
        status.textContent = "Chart deleted."; status.style.color = "var(--up)"; renderTradeChart(res.images);
      });
  });
}
function uploadTradeChart(blob) {
  if (!blob || !currentTrade) return;
  const status = document.getElementById("d-save-status");
  status.textContent = "Uploading chart…"; status.style.color = "var(--dim)";
  const reader = new FileReader();
  reader.onload = () => {
    fetch("/api/journal/chart", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ pageId: currentTrade.pageId, chartMode: currentTrade.chartMode,
        filename: (blob.name || "chart.png"), contentType: blob.type || "image/png", dataB64: reader.result }),
    }).then(r => r.json()).then(res => {
      if (res.error) { status.textContent = "Failed: " + res.error; status.style.color = "var(--down)"; return; }
      status.textContent = "Chart added."; status.style.color = "var(--up)";
      renderTradeChart(res.images);
    }).catch(err => { status.textContent = String(err); status.style.color = "var(--down)"; });
  };
  reader.readAsDataURL(blob);
}
(function () {
  const pz = document.getElementById("d-paste");
  pz.onclick = () => document.getElementById("d-file").click();
  document.getElementById("d-file").onchange = function () { uploadTradeChart(this.files[0]); };
  function onPaste(ev) {
    const items = (ev.clipboardData || {}).items || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf("image") === 0) { ev.preventDefault(); uploadTradeChart(items[i].getAsFile()); return; }
    }
  }
  pz.addEventListener("paste", onPaste);
  document.querySelector(".detail-page").addEventListener("paste", onPaste);
})();

document.getElementById("d-entrysetup").addEventListener("change", e => saveField("Entry Setup", e.target.value, "entrySetup"));
document.getElementById("d-exitsetup").addEventListener("change", e => saveField("Exit Setup", e.target.value, "exitSetup"));
document.getElementById("d-buyquality").addEventListener("change", e => saveField("Buy Quality", e.target.value, "buyQuality"));
document.getElementById("d-sellquality").addEventListener("change", e => saveField("Sell Quality", e.target.value, "sellQuality"));
document.getElementById("d-thesis").addEventListener("blur", e => saveField("Entry Thesis", e.target.value, "entryThesis"));
document.getElementById("d-close").addEventListener("click", () => document.getElementById("overlay").hidden = true);
document.getElementById("overlay").addEventListener("click", e => { if (e.target.id === "overlay") e.target.hidden = true; });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !document.getElementById("overlay").hidden) document.getElementById("overlay").hidden = true;
});

document.querySelectorAll(".filter-pill").forEach(p => p.addEventListener("click", () => {
  document.querySelectorAll(".filter-pill").forEach(x => x.classList.remove("active"));
  p.classList.add("active");
  activeFilter = p.dataset.filter;
  draw();
}));
document.getElementById("search").addEventListener("input", e => { searchTerm = e.target.value.trim().toLowerCase(); draw(); });

document.getElementById("sync-brokers-btn").addEventListener("click", function () {
  const btn = this, banner = document.getElementById("sync-banner");
  btn.disabled = true; btn.textContent = "Syncing…";
  fetch("/api/journal/sync-brokers", { method: "POST" }).then(r => r.json()).then(res => {
    btn.disabled = false; btn.innerHTML = "&#8635; Sync from brokers";
    if (res.error) { banner.hidden = false; banner.textContent = "Sync failed: " + res.error; return; }
    renderSyncBanner(res.results);
    loadJournal();
    // Gateway not logged in: try to open its login page straight away (the banner button is the fallback if the browser blocks the pop-up).
    if ((res.results || []).some(r => r.live && !r.live.used)) { try { window.open("https://localhost:5000/", "_blank", "noopener"); } catch (e) {} }
  }).catch(err => { btn.disabled = false; btn.innerHTML = "&#8635; Sync from brokers";
    banner.hidden = false; banner.textContent = "Sync failed: " + err; });
});

document.addEventListener("click", async function (e) {
  const b = e.target.closest(".gw-login");
  if (!b) return;
  b.disabled = true; const t = b.textContent; b.textContent = "Starting gateway…";
  try {
    const j = await (await fetch("/api/gateway/start", { method: "POST" })).json();
    if (j.ok) window.open(j.url || "https://localhost:5000/", "_blank", "noopener"); else alert(j.error || "Could not start the gateway");
  } catch (err) { alert("Could not reach the hub: " + err); }
  b.disabled = false; b.textContent = t;
});

function renderSyncBanner(results) {
  const banner = document.getElementById("sync-banner");
  const lines = [];
  const needsDateRows = [];
  const possiblyClosedRows = [];
  results.forEach(r => {
    if (r.error) { lines.push(esc(r.account) + ": " + esc(r.error)); return; }
    const created = (r.created || []).length;
    const closed = (r.closed || []).length;
    const possiblyClosed = r.possibly_closed || [];
    let line = "<b>" + esc(r.account) + "</b>: " + created + " new trade" + (created === 1 ? "" : "s") + " logged";
    if (r.live) line += r.live.used
      ? ' <span style="color:var(--up)">&middot; live gateway read (' + r.live.added + ' execution' + (r.live.added === 1 ? "" : "s") + ' after ' + esc(r.live.reportThrough) + ')</span>'
      : ' <span style="color:var(--warn)">&middot; IBKR&rsquo;s report ends ' + esc(r.live.reportThrough || "yesterday") + ', and the live gateway is not logged in, so trades since then are not visible yet.</span> <button type="button" class="gw-login" style="font-size:12px;font-weight:700;padding:4px 10px;border-radius:7px;border:1px solid var(--warn);background:none;color:var(--warn);cursor:pointer">Log in to IBKR gateway</button> <span style="color:var(--dim)">then press Sync again</span>';
    if (closed) line += ", " + closed + " closed (real exit price from the trade book)";
    const added = r.added || [];
    if (added.length) line += ', <span style="color:var(--violet)">' + added.length + " pyramid add" + (added.length === 1 ? "" : "s")
      + " linked</span> (" + added.map(a => esc(a.ticker) + " +" + a.shares).join(", ") + ") — add a chart to each";
    if (possiblyClosed.length) line += ', <span style="color:var(--warn)">' + possiblyClosed.length + " possibly closed</span> — no longer an open position at the broker";
    lines.push(line);
    (r.needs_date || []).forEach(nd => needsDateRows.push(Object.assign({ account: r.account }, nd)));
    possiblyClosed.forEach(pc => possiblyClosedRows.push(Object.assign({ account: r.account }, pc)));
  });
  let html = lines.join("<br>");
  if (needsDateRows.length) {
    html += '<div style="margin-top:9px;font-weight:700">New positions found — Flex doesn\'t report an open date, just confirm one:</div>';
    html += needsDateRows.map((nd, i) =>
      '<div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">'
      + '<b>' + esc(nd.ticker) + '</b> <span style="color:var(--dim)">' + nd.shares + ' @ ' + fmtMoney(nd.entryPrice, "USD") + '</span>'
      + '<input type="date" id="nd-date-' + i + '" style="font-size:12px;padding:4px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text)">'
      + '<button class="log-nd-btn" data-i="' + i + '" style="font-size:11.5px;font-weight:700;padding:5px 10px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer">Log it</button>'
      + '<span id="nd-status-' + i + '"></span></div>'
    ).join("");
  }
  if (possiblyClosedRows.length) {
    html += '<div style="margin-top:9px;font-weight:700">No longer open at the broker — confirm the exit to close it out:</div>';
    html += possiblyClosedRows.map((pc, i) =>
      '<div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">'
      + '<b>' + esc(pc.ticker) + '</b>'
      + '<input type="date" id="pc-date-' + i + '" style="font-size:12px;padding:4px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text)">'
      + '<input type="number" step="0.01" placeholder="exit price" id="pc-price-' + i + '" style="width:90px;font-size:12px;padding:4px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text)">'
      + '<button class="close-pc-btn" data-i="' + i + '" style="font-size:11.5px;font-weight:700;padding:5px 10px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer">Close it</button>'
      + '<a class="notion-link" href="' + esc(pc.notionUrl) + '" target="_blank" style="font-size:11.5px">open in Notion</a>'
      + '<span id="pc-status-' + i + '"></span></div>'
    ).join("");
  }
  banner.hidden = false;
  banner.innerHTML = html;
  needsDateRows.forEach((nd, i) => {
    document.querySelector('.log-nd-btn[data-i="' + i + '"]').addEventListener("click", function () {
      const dateVal = document.getElementById("nd-date-" + i).value;
      const status = document.getElementById("nd-status-" + i);
      if (!dateVal) { status.textContent = "pick a date"; status.style.color = "var(--down)"; return; }
      this.disabled = true; status.textContent = "logging…"; status.style.color = "var(--dim)";
      fetch("/api/journal/create-from-broker", { method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ account: nd.account, ticker: nd.ticker, dateOpened: dateVal,
          entryPrice: nd.entryPrice, shares: nd.shares }) })
        .then(r => r.json()).then(res => {
          if (res.error) { status.textContent = res.error; status.style.color = "var(--down)"; this.disabled = false; return; }
          status.textContent = "logged ✓"; status.style.color = "var(--up)";
          loadJournal();
        });
    });
  });
  possiblyClosedRows.forEach((pc, i) => {
    document.querySelector('.close-pc-btn[data-i="' + i + '"]').addEventListener("click", function () {
      const dateVal = document.getElementById("pc-date-" + i).value;
      const priceVal = document.getElementById("pc-price-" + i).value;
      const status = document.getElementById("pc-status-" + i);
      if (!dateVal || !priceVal) { status.textContent = "date + price needed"; status.style.color = "var(--down)"; return; }
      this.disabled = true; status.textContent = "closing…"; status.style.color = "var(--dim)";
      fetch("/api/journal/close-from-broker", { method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ pageId: pc.pageId, dateClosed: dateVal, exitPrice: priceVal }) })
        .then(r => r.json()).then(res => {
          if (res.error) { status.textContent = res.error; status.style.color = "var(--down)"; this.disabled = false; return; }
          status.textContent = "closed ✓"; status.style.color = "var(--up)";
          loadJournal();
        });
    });
  });
}

function loadJournal() {
  return Promise.all([
    fetch("/api/journal/trades").then(r => r.json()),
    fetch("/api/journal/schema").then(r => r.json()),
  ]).then(([trades, schema]) => {
    if (trades && trades.error) throw new Error(trades.error);
    ALL_TRADES = trades;
    SCHEMA = schema;
    updatePillCounts();
    const accounts = [...new Set(trades.map(t => t.account))];
    if (accounts.length > 1) {
      document.getElementById("account-pills").innerHTML =
        '<button class="filter-pill active" data-a="all">All accounts</button>'
        + accounts.map(a => '<button class="filter-pill" data-a="' + esc(a) + '">' + acctChip(a)
          + ' <span class="n">' + trades.filter(t => t.account === a).length + '</span></button>').join("");
      document.querySelectorAll("#account-pills .filter-pill").forEach(p => p.addEventListener("click", () => {
        document.querySelectorAll("#account-pills .filter-pill").forEach(x => x.classList.remove("active"));
        p.classList.add("active"); activeAccount = p.dataset.a; draw();
      }));
    }
    draw();
    fetch("/api/journal/mfe").then(r => r.json()).then(rows => {
      if (!Array.isArray(rows)) return;
      rows.forEach(r => { MFE[r.pageId] = r; });
      draw();
    }).catch(() => {});
    loadBacktest();
    loadBt0();
    loadGroups();
  }).catch(err => {
    const el = document.getElementById("load-err");
    el.hidden = false;
    el.textContent = "Could not load the trade journal: " + err.message;
  });
}

// ================= Dashboard tab + review mode =================
const REVIEW_EMOTIONS = ["Calm", "FOMO", "Fear", "Overconfident", "Frustrated"];
const REVIEW_TAGS = ["Followed plan", "Chased entry", "Sized too big", "Cut early", "Held too long", "Moved stop", "Added to winner"];
const NEW_SETUP = "__new__";
let dashAcct = null, dashStreak = 10, dashOpenSel = null, dashLive = [], dashLast = {};
let jtab = (function () { try { return localStorage.getItem("journal-tab") || "dash"; } catch (e) { return "dash"; } })();

function setJTab(t) {
  jtab = t; try { localStorage.setItem("journal-tab", t); } catch (e) {}
  document.querySelectorAll("#jtabs .jtab").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  document.getElementById("tab-dash").hidden = t !== "dash"; document.getElementById("tab-trades").hidden = t !== "trades";
  document.getElementById("tab-review").hidden = t !== "review";
  if (t === "dash") drawDash();
  if (t === "review" && window.drawReview) window.drawReview();
}
document.querySelectorAll("#jtabs .jtab").forEach(b => b.addEventListener("click", () => setJTab(b.dataset.t)));

const _drawTrades = draw;
draw = function () { _drawTrades(); if (jtab === "dash") drawDash(); };

function reviewQueue() {
  return ALL_TRADES.filter(t => t.dateClosed && isPending(t) && !t.reviewed && (activeAccount === "all" || t.account === activeAccount))
    .sort((a, b) => (b.dateClosed || "").localeCompare(a.dateClosed || ""));
}

function dashStats(list) {
  const cl = list.filter(t => t.dateClosed && t.pnl != null).sort((a, b) => (a.dateClosed || "").localeCompare(b.dateClosed || ""));
  const wins = cl.filter(t => t.pnl > 0), loss = cl.filter(t => t.pnl <= 0);
  const gw = wins.reduce((s, t) => s + t.pnl, 0), gl = Math.abs(loss.reduce((s, t) => s + t.pnl, 0)), total = cl.reduce((s, t) => s + t.pnl, 0);
  let eq = 0, peak = 0, mdd = 0; const curve = {};
  cl.forEach(t => { eq += t.pnl; peak = Math.max(peak, eq); mdd = Math.min(mdd, eq - peak); curve[t.dateClosed] = eq; });
  let bestW = 0, worstL = 0, runW = 0, runL = 0;
  cl.forEach(t => { if (t.pnl > 0) { runW++; runL = 0; } else { runL++; runW = 0; } bestW = Math.max(bestW, runW); worstL = Math.max(worstL, runL); });
  const months = {}; cl.forEach(t => { const m = t.dateClosed.slice(0, 7); months[m] = (months[m] || 0) + t.pnl; });
  const withStop = cl.filter(t => t.initialStop && t.entryPrice && t.exitPrice && t.entryPrice > t.initialStop);
  const rs = withStop.map(t => (t.exitPrice - t.entryPrice) / (t.entryPrice - t.initialStop));
  const big = (arr, f) => arr.reduce((b, t) => (b == null || f(t) > f(b)) ? t : b, null);
  return { cl, n: cl.length, wins, loss, total, pf: gl ? gw / gl : null, exp: cl.length ? total / cl.length : 0, winRate: cl.length ? wins.length / cl.length * 100 : 0,
    avgW: wins.length ? gw / wins.length : 0, avgL: loss.length ? -gl / loss.length : 0, avgWp: mean(wins.map(t => t.pnlPct).filter(x => x != null)), avgLp: mean(loss.map(t => t.pnlPct).filter(x => x != null)),
    holdW: mean(wins.map(t => t.holdDays).filter(x => x != null)), holdL: mean(loss.map(t => t.holdDays).filter(x => x != null)), bigW: big(wins, t => t.pnl), bigL: big(loss, t => -t.pnl),
    mdd, bestW, worstL, curve, months, withStop: withStop.length, noStop: cl.length - withStop.length, expR: rs.length ? mean(rs) : null, wl: gl && wins.length && loss.length ? (gw / wins.length) / (gl / loss.length) : null };
}

function killDash() { dashLive.forEach(h => { try { h.remove ? h.remove() : h.destroy(); } catch (e) {} }); dashLive = []; }
function drawDash() {
  const host = document.getElementById("tab-dash"); if (!host || !ALL_TRADES.length) return;
  killDash();
  const accounts = [...new Set(ALL_TRADES.map(t => t.account))];
  if (!dashAcct || accounts.indexOf(dashAcct) < 0) dashAcct = accounts[0];
  const list = ALL_TRADES.filter(t => t.account === dashAcct), cur = (list[0] || {}).currency || "USD", S = dashStats(list);
  const m = (v, d) => fmtMoney(v, cur).replace(/\.\d+$/, d ? "$&" : "");
  const kv = (k, v, s) => '<div><div class="k">' + k + '</div><div class="v">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : '') + '</div>';
  const col = v => v >= 0 ? "up" : "down", sign = v => (v >= 0 ? "+" : "");
  const queue = reviewQueue().filter(t => t.account === dashAcct);
  const open = list.filter(t => !t.dateClosed && t.entryPrice);
  const recent = S.cl.slice(-dashStreak);
  host.innerHTML =
    '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px"><div class="dseg" id="dacct">' + accounts.map(a => '<button data-a="' + esc(a) + '"' + (a === dashAcct ? ' class="on"' : '') + '>' + esc(a) + '</button>').join("") + '</div>' +
    '<span style="font-size:12px;color:var(--dim)">' + S.n + ' closed trades &middot; ' + cur + ' &middot; realised P&amp;L from fill prices</span></div>' +
    '<div class="dgrid">' +
    '<div class="jw w12"><h3>Performance overview</h3><div class="kv">' + kv("Realised P&amp;L", '<span class="' + col(S.total) + '">' + m(S.total) + '</span>', "all closed trades") + kv("Trades", S.n, S.wins.length + "W / " + S.loss.length + "L") + kv("Win rate", S.winRate.toFixed(1) + "%") + kv("Profit factor", S.pf == null ? "&mdash;" : S.pf.toFixed(2), "gross wins &divide; gross losses") + kv("Expectancy", '<span class="' + col(S.exp) + '">' + m(S.exp, 2) + '</span>', "per trade") + kv("Avg win / loss", '<span class="up">' + m(S.avgW) + '</span> / <span class="down">' + m(S.avgL) + '</span>') + '</div></div>' +
    '<div class="jw w7"><h3>Equity curve</h3><div id="d-eq" style="height:230px"></div></div>' +
    '<div class="jw w5"><h3 style="display:flex;justify-content:space-between;align-items:center">Recent trades<span class="dseg" id="dstreak"><button data-n="5"' + (dashStreak === 5 ? ' class="on"' : '') + '>5</button><button data-n="10"' + (dashStreak === 10 ? ' class="on"' : '') + '>10</button></span></h3><div class="strip">' +
      recent.map(t => '<span class="' + (t.pnl > 0 ? "W" : "L") + '" data-p="' + esc(t.pageId) + '" title="' + esc(t.ticker) + " " + fmtMoney(t.pnl, cur) + " " + fmtPct(t.pnlPct) + " &middot; closed " + t.dateClosed + ' &middot; click to open"><b>' + (t.pnl > 0 ? "W" : "L") + '</b><small>' + esc(t.ticker) + '</small><small>' + fmtPct(t.pnlPct) + '</small></span>').join("") + '</div>' +
      '<div style="font-size:12px;color:var(--dim);margin-top:8px">Current streak: <b class="' + (recent.length && recent[recent.length - 1].pnl > 0 ? "up" : "down") + '">' + (function () { let k = 0, w = recent.length && recent[recent.length - 1].pnl > 0; for (let i = S.cl.length - 1; i >= 0 && (S.cl[i].pnl > 0) === w; i--) k++; return k + (w ? "W" : "L"); })() + '</b> &middot; oldest &rarr; latest, click one to open that trade</div></div>' +
    '<div class="jw"><h3>Win / loss profile</h3><div class="kv">' + kv("Avg win", '<span class="up">' + m(S.avgW) + '</span>', S.avgWp == null ? "" : "+" + (S.avgWp * 100).toFixed(1) + "%") + kv("Avg loss", '<span class="down">' + m(S.avgL) + '</span>', S.avgLp == null ? "" : (S.avgLp * 100).toFixed(1) + "%") + kv("Avg hold, winners", S.holdW == null ? "&mdash;" : S.holdW.toFixed(1) + " days") + kv("Avg hold, losers", S.holdL == null ? "&mdash;" : S.holdL.toFixed(1) + " days") +
      kv("Biggest win", S.bigW ? '<span class="up">' + m(S.bigW.pnl) + '</span>' : "&mdash;", S.bigW ? esc(S.bigW.ticker) + " &middot; " + S.bigW.dateClosed : "") + kv("Biggest loss", S.bigL ? '<span class="down">' + m(S.bigL.pnl) + '</span>' : "&mdash;", S.bigL ? esc(S.bigL.ticker) + " &middot; " + S.bigL.dateClosed : "") + '</div></div>' +
    '<div class="jw"><h3>Risk &amp; consistency</h3><div class="kv">' + kv("Max drawdown", '<span class="down">' + m(S.mdd) + '</span>', "realised, peak to trough") + kv("Streaks", "best " + S.bestW + "W", "worst " + S.worstL + "L") + '</div><div style="font-size:11.5px;color:var(--dim);margin:12px 0 4px">Realised P&amp;L by month</div><div id="d-mo" style="height:110px"></div></div>' +
    '<div class="jw"><h3>Trading edge</h3><div class="kv">' + kv("Win/loss $ ratio", S.wl == null ? "&mdash;" : S.wl.toFixed(2)) + kv("Expectancy", m(S.exp, 2)) + kv("Expectancy (R)", S.expR == null ? "&mdash;" : sign(S.expR) + S.expR.toFixed(2) + "R", S.withStop + " trades with a real stop") + '</div>' +
      '<div class="dbox"><b style="color:var(--warn)">Add stops</b> &mdash; ' + S.noStop + ' of ' + S.n + ' closed trades have no initial stop, so R-multiples cover only ' + S.withStop + '. <span id="d-stopwhy">Checking where stops can come from&hellip;</span></div></div>' +
    '<div class="jw"><h3>Needs review</h3><div style="display:flex;align-items:center;gap:14px"><div style="font-size:34px;font-weight:800;color:var(--warn)">' + queue.length + '</div><div><b>closed trades are missing a setup</b><div style="font-size:12px;color:var(--dim)">Work through them one at a time: setup, notes, emotion, tags, then Save &amp; next.</div></div></div><button class="rv-btn" id="d-start" style="margin-top:12px"' + (queue.length ? "" : " disabled") + '>Start reviewing &rarr;</button></div>' +
    '<div class="jw w12"><h3>Open positions &amp; near stops</h3>' + (open.length ? '<table class="dtbl"><thead><tr><th>Symbol</th><th class="r">Shares</th><th class="r">Avg</th><th class="r">Last</th><th class="r">P&amp;L %</th><th class="r">Stop</th><th class="r">From stop</th></tr></thead><tbody id="d-open"></tbody></table><div id="d-opd"></div>' :
      '<div class="dbox" style="margin-top:0">No open positions in ' + esc(dashAcct) + ' right now. Open positions appear here with the last close, % from the stop, and a chart when you click a row.</div>') + '</div>' +
    '</div>';
  host.querySelectorAll("#dacct button").forEach(b => b.onclick = () => { dashAcct = b.dataset.a; dashOpenSel = null; drawDash(); });
  fetch("/api/journal/stops").then(r => r.json()).then(s => {
    const el = document.getElementById("d-stopwhy"); if (!el || s.error) return; const st = s.status || {};
    el.innerHTML = 'IBKR&rsquo;s daily report has no order history, so stops you placed never reach the journal. ' +
      (st.gateway === "connected" ? 'The live gateway is connected, so stops resting at IBKR are copied onto open trades.' : 'The live gateway is not logged in, so resting stops cannot be read right now (' + esc(st.gateway || "never checked") + ').') +
      (st.flexOrders ? ' The report includes orders (' + (st.flexStopOrders || 0) + ' stop orders).' : ' IBKR’s Flex sections do not include placed orders, so stops from before today cannot be recovered automatically; from now on they are captured while the gateway is logged in.') +
      ' Open trades without a stop: ' + s.openNoStop + ' of ' + s.open + '.';
  }).catch(() => {});
  host.querySelectorAll("#dstreak button").forEach(b => b.onclick = () => { dashStreak = +b.dataset.n; drawDash(); });
  host.querySelectorAll(".strip span[data-p]").forEach(el => el.onclick = () => { const t = ALL_TRADES.find(x => x.pageId === el.dataset.p); if (t) openDetail(t); });
  const st = document.getElementById("d-start"); if (st) st.onclick = () => { const q = reviewQueue().filter(t => t.account === dashAcct); if (q.length) openDetail(q[0]); };
  // equity curve + monthly bars
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), th = { layout: { background: { color: "transparent" }, textColor: css("--dim"), fontSize: 10 }, grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } }, rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line") } };
  const eqEl = document.getElementById("d-eq");
  if (eqEl && window.LightweightCharts) {
    const c = LightweightCharts.createChart(eqEl, Object.assign({ width: eqEl.clientWidth, height: 230 }, th)); dashLive.push(c);
    const a = c.addAreaSeries({ lineColor: css("--accent"), topColor: "rgba(37,99,235,.28)", bottomColor: "rgba(37,99,235,.02)", lineWidth: 2, priceLineVisible: false });
    a.setData(Object.keys(S.curve).sort().map(d => ({ time: d, value: S.curve[d] }))); c.timeScale().fitContent();
    new ResizeObserver(() => { try { c.applyOptions({ width: eqEl.clientWidth }); } catch (e) {} }).observe(eqEl);
  }
  const moEl = document.getElementById("d-mo");
  if (moEl && window.LightweightCharts) {
    const c = LightweightCharts.createChart(moEl, Object.assign({ width: moEl.clientWidth, height: 110 }, th, { rightPriceScale: { visible: false } })); dashLive.push(c);
    const h = c.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    h.setData(Object.keys(S.months).sort().map(k => ({ time: k + "-15", value: S.months[k], color: S.months[k] >= 0 ? css("--up") : css("--down") }))); c.timeScale().fitContent();
    new ResizeObserver(() => { try { c.applyOptions({ width: moEl.clientWidth }); } catch (e) {} }).observe(moEl);
  }
  // open positions: last close from the local price files, near-stop flag, click for a chart
  if (open.length) {
    const code = open[0].country || "US";
    fetch("/api/journal/last?country=" + code + "&symbols=" + encodeURIComponent(open.map(t => t.ticker).join(","))).then(r => r.json()).then(L => { dashLast = L; drawOpen(open, cur); }).catch(() => drawOpen(open, cur));
  }
}
function drawOpen(open, cur) {
  const tb = document.getElementById("d-open"); if (!tb) return;
  tb.innerHTML = open.map(t => {
    const last = (dashLast[t.ticker] || {}).close, pl = last && t.entryPrice ? (last / t.entryPrice - 1) : null, fs = last && t.initialStop ? (last / t.initialStop - 1) : null;
    return '<tr class="dclick" data-p="' + esc(t.pageId) + '"' + (t.pageId === dashOpenSel ? ' style="background:color-mix(in srgb,var(--accent) 9%,transparent)"' : '') + '><td><b>' + esc(t.ticker) + '</b></td><td class="r">' + (t.shares == null ? "&mdash;" : t.shares) + '</td><td class="r">' + fmtMoney(t.entryPrice, cur) + '</td><td class="r">' + (last ? fmtMoney(last, cur) : "&mdash;") + '</td><td class="r ' + (pl == null ? "" : pl >= 0 ? "up" : "down") + '">' + fmtPct(pl) + '</td><td class="r">' + (t.initialStop ? fmtMoney(t.initialStop, cur) : "&mdash;") + '</td><td class="r ' + (fs != null && fs < 0.05 ? "down" : "") + '">' + (fs == null ? "&mdash;" : (fs * 100).toFixed(1) + "%" + (fs < 0.05 ? " &middot; near" : "")) + '</td></tr>';
  }).join("");
  tb.querySelectorAll("tr").forEach(tr => tr.onclick = () => { dashOpenSel = dashOpenSel === tr.dataset.p ? null : tr.dataset.p; drawOpen(open, cur); });
  const box = document.getElementById("d-opd"); box.innerHTML = "";
  const t = open.find(x => x.pageId === dashOpenSel); if (!t || !window.StdChart) return;
  const lines = []; if (t.entryPrice) lines.push({ p: t.entryPrice, color: "#2563eb", title: "avg buy" }); if (t.initialStop) lines.push({ p: t.initialStop, color: "#dc2626", title: "stop" });
  box.style.marginTop = "12px";
  fetch("/api/setups/prices?country=" + (t.country || "US") + "&ticker=" + encodeURIComponent(t.ticker)).then(r => r.json()).then(D => {
    if (D.error) { box.innerHTML = '<div class="dbox">' + esc(D.error) + '</div>'; return; }
    dashLive.push(StdChart.mount(box, { t: t.ticker, name: t.ticker, d: D.dates, o: D.open, h: D.high, l: D.low, c: D.close, v: D.volume || null, ht: 360, tf: "6M", lines: lines, entryDate: t.dateOpened,
      trades: t.dateOpened ? [{ d: t.dateOpened, a: "Buy", p: t.entryPrice, q: t.shares }] : [], logoUrl: t.logoid ? "/api/logo/" + encodeURIComponent(t.logoid) : "" }));
  }).catch(() => {});
}

// ---- review mode in the trade sheet
function reviewFill(t) {
  fillSelect("d-emotion", REVIEW_EMOTIONS, t.emotion);
  document.getElementById("d-post").value = t.postNotes || "";
  const tags = t.processTags || [];
  document.getElementById("d-tags").innerHTML = REVIEW_TAGS.map(x => '<span class="tagchip' + (tags.indexOf(x) >= 0 ? " on" : "") + '" data-t="' + esc(x) + '">' + esc(x) + '</span>').join("");
  ["d-entrysetup", "d-exitsetup"].forEach(id => { const el = document.getElementById(id); el.insertAdjacentHTML("beforeend", '<option value="' + NEW_SETUP + '">+ New setup…</option>'); });
  const q = reviewQueue(), at = q.findIndex(x => x.pageId === t.pageId);
  document.getElementById("d-rvstat").textContent = t.reviewed ? "Reviewed" : (at >= 0 ? (at + 1) + " of " + q.length + " to review" : (q.length ? q.length + " trades to review" : "Nothing left to review"));
  document.getElementById("d-rv-next").textContent = q.filter(x => x.pageId !== t.pageId).length ? "Save & next" : "Save & finish";
  document.querySelectorAll(".newsetup").forEach(n => n.remove());
}
const _openDetail = openDetail;
openDetail = function (t) { _openDetail(t); reviewFill(t); };

function newSetupInput(selId, prop, key) {
  const sel = document.getElementById(selId); document.querySelectorAll(".newsetup").forEach(n => n.remove());
  const box = document.createElement("div"); box.className = "newsetup";
  box.innerHTML = '<input type="text" placeholder="Name of the new ' + (prop === "Entry Setup" ? "entry" : "exit") + ' setup, e.g. Oversold Bounce" maxlength="60"><button class="rv-btn" type="button">Add</button>';
  sel.insertAdjacentElement("afterend", box);
  const inp = box.querySelector("input"), go = () => {
    const name = inp.value.trim(); if (!name) return;
    SCHEMA[prop] = (SCHEMA[prop] || []).concat(SCHEMA[prop] && SCHEMA[prop].indexOf(name) >= 0 ? [] : [name]);
    fillSelect(selId, SCHEMA[prop], name); sel.insertAdjacentHTML("beforeend", '<option value="' + NEW_SETUP + '">+ New setup…</option>'); sel.value = name;
    box.remove(); saveField(prop, name, key);
  };
  box.querySelector("button").onclick = go; inp.onkeydown = e => { if (e.key === "Enter") go(); }; inp.focus();
  sel.value = (currentTrade && currentTrade[key]) || "";
}
document.getElementById("d-entrysetup").addEventListener("change", e => { if (e.target.value === NEW_SETUP) { e.stopImmediatePropagation(); newSetupInput("d-entrysetup", "Entry Setup", "entrySetup"); } }, true);
document.getElementById("d-exitsetup").addEventListener("change", e => { if (e.target.value === NEW_SETUP) { e.stopImmediatePropagation(); newSetupInput("d-exitsetup", "Exit Setup", "exitSetup"); } }, true);
document.getElementById("d-post").addEventListener("blur", e => { if (currentTrade && e.target.value !== (currentTrade.postNotes || "")) saveField("Post-trade Notes", e.target.value, "postNotes"); });
document.getElementById("d-emotion").addEventListener("change", e => saveField("Emotion", e.target.value, "emotion"));
document.getElementById("d-tags").addEventListener("click", e => {
  const c = e.target.closest(".tagchip"); if (!c || !currentTrade) return;
  c.classList.toggle("on"); const tags = [...document.querySelectorAll("#d-tags .tagchip.on")].map(x => x.dataset.t);
  saveField("Process Tags", tags, "processTags");
});
document.getElementById("d-rv-next").addEventListener("click", () => {
  if (!currentTrade) return; const t = currentTrade;
  const post = document.getElementById("d-post").value; if (post !== (t.postNotes || "")) saveField("Post-trade Notes", post, "postNotes");
  const q = reviewQueue().filter(x => x.pageId !== t.pageId);
  saveField("Reviewed", true, "reviewed");
  setTimeout(() => { if (q.length) openDetail(q[0]); else document.getElementById("overlay").hidden = true; if (jtab === "dash") drawDash(); }, 450);
});


// ================= Log a trade by hand =================
(function () {
  const $ = id => document.getElementById(id), today = () => new Date().toISOString().slice(0, 10);
  let mode = "buy";
  const msg = (t, c) => { $("lt-msg").textContent = t; $("lt-msg").style.color = c || "var(--dim)"; };
  function openLT() {
    const accts = [...new Set(ALL_TRADES.map(t => t.account))];
    $("lt-acct").innerHTML = accts.map(a => '<option>' + esc(a) + '</option>').join("");
    $("lt-date").value = today(); $("lt-sdate").value = today(); ["lt-tk", "lt-px", "lt-sh", "lt-stop", "lt-spx"].forEach(i => $(i).value = "");
    fillPositions(); msg(""); $("lt-overlay").hidden = false; $("lt-tk").focus();
  }
  function openRows() { return ALL_TRADES.filter(t => !t.dateClosed && !t.parentId && t.account === $("lt-acct").value); }
  function fillPositions() {
    $("lt-pos").innerHTML = openRows().map(t => '<option value="' + esc(t.pageId) + '">' + esc(t.ticker) + ' &middot; ' + (t.shares ?? "?") + ' sh @ ' + t.entryPrice + ' &middot; opened ' + t.dateOpened + '</option>').join("") || '<option value="">No open positions in this account</option>';
  }
  function checkAdd() {
    const tk = $("lt-tk").value.trim().toUpperCase(), row = openRows().find(t => (t.ticker || "").toUpperCase() === tk);
    $("lt-addrow").style.display = row ? "block" : "none"; if (row) $("lt-addtk").textContent = tk;
  }
  $("log-trade-btn").addEventListener("click", openLT);
  $("lt-close").addEventListener("click", () => { $("lt-overlay").hidden = true; });
  $("lt-acct").addEventListener("change", () => { fillPositions(); checkAdd(); });
  $("lt-tk").addEventListener("input", checkAdd);
  $("lt-mode").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return; mode = b.dataset.m;
    document.querySelectorAll("#lt-mode button").forEach(x => x.classList.toggle("on", x === b));
    $("lt-buy").hidden = mode !== "buy"; $("lt-sell").hidden = mode !== "sell"; msg("");
  });
  $("lt-go").addEventListener("click", () => {
    $("lt-go").disabled = true; msg("Saving…");
    let req;
    if (mode === "buy") {
      const tk = $("lt-tk").value.trim().toUpperCase(), row = openRows().find(t => (t.ticker || "").toUpperCase() === tk);
      if (!tk || !$("lt-date").value || !$("lt-px").value || !$("lt-sh").value) { msg("Ticker, date, price and shares are needed.", "var(--down)"); $("lt-go").disabled = false; return; }
      req = fetch("/api/journal/create-from-broker", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        account: $("lt-acct").value, ticker: tk, dateOpened: $("lt-date").value, entryPrice: +$("lt-px").value, shares: +$("lt-sh").value,
        initialStop: $("lt-stop").value ? +$("lt-stop").value : null, parentId: row && $("lt-add").checked ? row.pageId : null }) });
    } else {
      const id = $("lt-pos").value; if (!id || !$("lt-sdate").value || !$("lt-spx").value) { msg("Pick the position, a date and a price.", "var(--down)"); $("lt-go").disabled = false; return; }
      req = fetch("/api/journal/close-from-broker", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        pageId: id, dateClosed: $("lt-sdate").value, exitPrice: +$("lt-spx").value, account: $("lt-acct").value }) });
    }
    req.then(r => r.json()).then(res => {
      $("lt-go").disabled = false;
      if (res.error) { msg(res.error, "var(--down)"); return; }
      msg("Saved ✓", "var(--up)"); setTimeout(() => { $("lt-overlay").hidden = true; loadJournal(); }, 500);
    }).catch(() => { $("lt-go").disabled = false; msg("Server not reachable.", "var(--down)"); });
  });
})();

setJTab(jtab);
</script>
<script src="/journal-review.js"></script>
<script>
if (jtab === "review" && window.drawReview) window.drawReview();

loadJournal();
</script>
</body></html>"""


PORTFOLIO_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio — live, local</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root { --bg:#f5f6f8; --panel:#fff; --text:#1a1d24; --dim:#6b7280;
    --line:#e2e5ea; --up:#16a34a; --down:#dc2626; --warn:#ca8a04;
    --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15;
      --accent:#60a5fa; }
  }
  :root[data-theme="dark"] { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15;
      --accent:#60a5fa; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-size:14px;
    font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .mono, td.num, th.num, .stat-value { font-family:"IBM Plex Mono",ui-monospace,"SFMono-Regular",Consolas,monospace; }
  main { padding:18px; max-width:1500px; margin:0 auto; }
  .top { display:flex; justify-content:space-between; align-items:center;
    flex-wrap:wrap; gap:10px; margin-bottom:14px; }
  h1 { font-size:17px; margin:0; }
  .ident { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
  #flag { display:inline-flex; align-items:center; }
  #flag svg.flag { width:20px; height:14px; border-radius:2px; display:block; }
  #brokers .btn svg.flag { width:15px; height:10px; border-radius:1.5px; vertical-align:-1px; margin-right:3px; }
  .name { font-size:17px; font-weight:600; color:var(--text);
    background:transparent; border:1px solid transparent; border-radius:6px;
    padding:3px 7px; min-width:180px; font-family:inherit; }
  .name:hover { border-color:var(--line); }
  .name:focus { outline:none; border-color:var(--accent); background:var(--panel); }
  .name::placeholder { color:var(--dim); font-weight:500; }
  .chip { display:inline-flex; align-items:center; font-size:11px;
    font-weight:700; letter-spacing:.03em; color:#fff; padding:3px 8px;
    border-radius:6px; }
  .logo { height:22px; width:auto; display:block; }
  .saved { font-size:11px; color:var(--up); }
  .tag { display:inline-block; font-size:10px; padding:2px 7px;
    border-radius:999px; margin-left:7px; text-transform:uppercase;
    letter-spacing:.04em; }
  .tag.live { background:color-mix(in srgb,var(--up) 18%,transparent); color:var(--up); }
  .src { font-size:10px; color:var(--dim); }
  .connect { display:inline-flex; align-items:center; gap:5px; font-size:12px;
    font-weight:600; text-decoration:none; background:var(--accent); color:#fff;
    padding:4px 11px; border-radius:7px; }
  .connect:hover { filter:brightness(1.08); }
  .connect[hidden] { display:none; }
  button.connect { border:none; cursor:pointer; font-family:inherit; }
  .login { background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:14px 16px; margin-bottom:16px; max-width:420px; }
  .login[hidden] { display:none; }
  .login input { display:block; width:100%; margin:8px 0; padding:7px 9px;
    font-size:14px; font-family:inherit; background:var(--bg);
    border:1px solid var(--line); border-radius:6px; color:var(--text); }
  .login input:focus { outline:none; border-color:var(--accent); }
  .table-tools { display:flex; align-items:center; gap:10px; margin-bottom:8px; flex-wrap:wrap; }
  #filter { padding:6px 10px; font-size:13px; font-family:inherit; width:220px;
    background:var(--panel); border:1px solid var(--line); border-radius:7px;
    color:var(--text); }
  #filter:focus { outline:none; border-color:var(--accent); }
  .chips { display:flex; gap:5px; flex-wrap:wrap; }
  #sector-filter { padding:6px 9px; font-size:12.5px; font-family:inherit;
    background:var(--panel); border:1px solid var(--line); border-radius:7px;
    color:var(--text); }
  #sector-filter:focus { outline:none; border-color:var(--accent); }
  tr.pos-row { cursor:pointer; }
  tr.pos-row:hover td { background:color-mix(in srgb,var(--accent) 6%,transparent); }
  tr.pos-row.open td { background:color-mix(in srgb,var(--accent) 9%,transparent); }
  tr.expand-row td { background:var(--bg); padding:12px 15px; }
  .expand-stats { display:flex; gap:26px; flex-wrap:wrap; }
  .expand-stat { display:flex; flex-direction:column; gap:2px; }
  .expand-stat-label { font-size:10px; color:var(--dim); text-transform:uppercase;
    letter-spacing:.04em; }
  .expand-stat-val { font-size:15.5px; font-weight:600; font-variant-numeric:tabular-nums; }
  th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  th.sortable:hover { color:var(--text); }
  th.sorted { color:var(--accent); }
  .tag.eod { background:color-mix(in srgb,var(--warn) 18%,transparent); color:var(--warn); }
  .btn { background:var(--panel); border:1px solid var(--line); color:var(--dim);
    border-radius:7px; padding:4px 10px; font-size:12px; cursor:pointer; }
  .btn.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px; margin-bottom:16px; }
  .stat { background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:12px 14px; }
  .stat-label { font-size:10px; color:var(--dim); text-transform:uppercase;
    letter-spacing:.04em; }
  .stat-value { font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; }
  .stat-sub { font-size:11px; color:var(--dim); }
  .panel { background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:12px 14px; margin-bottom:16px; }
  .curve { width:100%; height:200px; display:block; }
  .ax { fill:var(--dim); font-size:11px; }
  .tf { display:flex; gap:5px; flex-wrap:wrap; }
  .tf .btn:disabled { opacity:.35; cursor:not-allowed; }
  .tf .btn.partial { border-style:dashed; color:var(--warn); }
  .tf .btn.partial.active { background:var(--warn); border-color:var(--warn);
    color:#1a1d24; }
  table { width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th,td { padding:8px 11px; text-align:left; border-bottom:1px solid var(--line); }
  th { font-size:10px; text-transform:uppercase; letter-spacing:.04em;
    color:var(--dim); font-weight:600; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:none; }
  .dim { color:var(--dim); font-size:11px; }
  .pos-cell { display:flex; align-items:center; gap:8px; }
  .pos-logo { width:22px; height:22px; border-radius:5px; flex:none; object-fit:cover; }
  .pos-logo-fallback { width:22px; height:22px; border-radius:5px; flex:none;
    display:flex; align-items:center; justify-content:center; color:#fff;
    font-size:9.5px; font-weight:700; }
  .pos-name { display:flex; flex-direction:column; line-height:1.25; }
  .pos-name .ind { font-weight:400; color:var(--dim); font-size:10.5px; }
  .exp-row { display:grid; grid-template-columns:150px 1fr 46px; align-items:center;
    gap:10px; padding:4px 0; font-size:12.5px; }
  .exp-track { position:relative; height:12px; background:var(--line); border-radius:4px; }
  .exp-fill { position:absolute; inset:0 auto 0 0; background:var(--accent,#3b82f6); border-radius:4px; }
  .exp-val { text-align:right; font-variant-numeric:tabular-nums; color:var(--dim); }
  .up { color:var(--up); } .down { color:var(--down); }
  .warn { color:var(--warn); }
  .warn-line { background:color-mix(in srgb,var(--warn) 12%,transparent);
    border-left:3px solid var(--warn); padding:7px 11px; margin-bottom:10px;
    font-size:12px; border-radius:0 6px 6px 0; }
</style></head><body><main>

<div class="top">
  <div class="ident">
    <span id="flag" class="flag"></span>
    <span id="mark"></span>
    <input id="name" class="name" spellcheck="false" maxlength="60"
           placeholder="Name this portfolio" title="Click to rename — saved on this machine">
    <span id="mode" class="tag"></span>
    <button id="connect" class="connect" hidden></button>
    <button id="connect-btn" class="connect" hidden></button>
  </div>
  <div id="login" class="login" hidden>
    <form id="login-form" autocomplete="off">
      <b>Connect to Angel One</b>
      <div class="dim">Used to sign in and then discarded — neither value is stored.</div>
      <input id="pin" type="password" placeholder="PIN" autocomplete="off">
      <input id="totp" type="text" inputmode="numeric" maxlength="6"
             placeholder="6-digit code" autocomplete="off">
      <div>
        <button type="submit" class="connect">Connect</button>
        <button type="button" id="login-cancel" class="btn">Cancel</button>
      </div>
      <div id="login-error" class="warn" style="font-size:12px"></div>
    </form>
  </div>
  <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
    <span id="brokers" class="tf"></span>
    <span id="ccy" class="tf"></span>
    <span class="dim" id="stamp"></span>
  </div>
</div>

<div id="warnings"></div>
<div class="stats" id="stats"></div>

<div class="panel">
  <div style="display:flex;justify-content:space-between;align-items:baseline;
              flex-wrap:wrap;gap:8px;margin-bottom:8px">
    <div><b style="font-size:12px">Equity curve</b>
      <span class="dim" id="curve-meta"></span></div>
    <div class="tf" id="tf"></div>
  </div>
  <div id="curve-wrap"></div>
</div>

<div class="panel" id="exposure-panel" hidden>
  <b style="font-size:12px">Sector exposure</b>
  <div class="dim" style="font-size:11px;margin:2px 0 10px">Share of invested value — not
    weighed against an index yet, just what's actually concentrated in the book.</div>
  <div id="exposure-rows"></div>
</div>

<div class="table-tools">
  <input id="filter" placeholder="Filter positions…" spellcheck="false">
  <div class="chips" id="chips"></div>
  <select id="sector-filter"><option value="">All sectors</option></select>
  <button class="btn" id="avg-toggle">Averages</button>
  <span class="dim" id="filter-note"></span>
</div>
<div class="stats" id="averages-panel" hidden></div>
<table>
  <thead><tr id="head"></tr></thead>
  <tbody id="rows"></tbody>
</table>
<div class="dim" id="foot" style="margin-top:10px;line-height:1.6"></div>

<script>
const REFRESH_MS = 30000;
let broker = new URLSearchParams(location.search).get("broker") || "ibkr";
let tf = "1M", data = null;

const TF = ["1D","1W","1M","MTD","3M","6M","YTD","1Y"];
const DAYS = {"1W":7,"1M":30,"3M":91,"6M":182,"1Y":365};

let showUsd = false;

// Indian numbers group by lakh and crore (1,00,000) and Western ones by
// thousand (100,000). Same digits, different reading — en-IN does it natively,
// so the portfolio reads the way its market does.
const SYMBOL = { INR: "₹", USD: "$", CAD: "C$" };
const LOCALE = c => c === "INR" ? "en-IN" : "en-US";

function fmt(v, code, dp) {
  if (v == null) return "—";
  const digits = dp === undefined ? 2 : dp;
  return (SYMBOL[code] || "") + Math.abs(v).toLocaleString(LOCALE(code),
    {minimumFractionDigits:digits, maximumFractionDigits:digits})
    .replace(/^/, v < 0 ? "-" : "");
}

const money = v => v == null ? "—" : v.toLocaleString(
  LOCALE(data && data.currency), {minimumFractionDigits:2, maximumFractionDigits:2});
const pct = (v,d=1) => v == null ? "—" : (v*100).toFixed(d) + "%";
const cls = v => (v||0) > 0 ? "up" : ((v||0) < 0 ? "down" : "");

// Amounts held in the account's BASE currency, converted for display.
const conv = v => (v == null) ? null
  : (showUsd && data.usd_rate ? v * data.usd_rate : v);
const ccy = () => (showUsd && data.usd_rate) ? "USD" : data.currency;
const cash = v => fmt(conv(v), ccy());
// Per-share figures stay in the instrument's own currency and are never
// converted — a stop belongs to the market you set it in.
const px = (v, p) => fmt(v, (p && p.currency) || data.currency);

function renderCcy() {
  const opts = [data.currency];
  if (data.usd_rate) opts.push("USD");
  document.getElementById("ccy").innerHTML = opts.length < 2 ? "" :
    opts.map(c => '<button class="btn'
      + ((c === "USD") === showUsd ? ' active' : '') + '" data-c="'+c+'">'
      + c + '</button>').join("");
  document.querySelectorAll("#ccy .btn").forEach(b =>
    b.onclick = () => { showUsd = b.dataset.c === "USD"; render(); });
}

function cutoff(tf, last) {
  const d = new Date(last);
  if (tf === "WTD") {
    const c = new Date(d);
    // getDay(): 0=Sun..6=Sat. Back up to this week's Monday.
    c.setDate(c.getDate() - ((c.getDay() + 6) % 7));
    return c;
  }
  if (tf === "MTD") return new Date(d.getFullYear(), d.getMonth(), 1);
  if (tf === "YTD") return new Date(d.getFullYear(), 0, 1);
  if (DAYS[tf]) { const c = new Date(d); c.setDate(c.getDate() - DAYS[tf]); return c; }
  return null;
}

function seriesFor(tf) {
  // 1D is the only view built from intraday samples; everything longer reads
  // daily closes, where one point per day is the honest resolution.
  if (tf === "1D") return (data.nav_intraday || []).map(p => [p[0], p[1]]);
  const daily = data.nav_history || [];
  if (!daily.length) return [];
  const c = cutoff(tf, daily[daily.length-1][0]);
  return c ? daily.filter(p => new Date(p[0]) >= c) : daily;
}

function drawCurve(points) {
  const wrap = document.getElementById("curve-wrap");
  const meta = document.getElementById("curve-meta");
  if (points.length < 2) {
    meta.textContent = "";
    wrap.innerHTML = '<div class="dim" style="padding:26px 4px;text-align:center;'
      + 'line-height:1.6">Not enough history at this timeframe yet — '
      + points.length + ' point' + (points.length===1?'':'s') + '.<br>'
      + 'This fills in as the server keeps running.</div>';
    return;
  }
  const W = 1100, H = 200, L = 8, R = 8, T = 12, B = 22;
  const vals = points.map(p => p[1]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  let span = (hi - lo) || (hi * 0.01) || 1;
  const pad = span * 0.12; lo -= pad; hi += pad; span = hi - lo;
  const pw = W - L - R, ph = H - T - B;
  const x = i => L + pw * i / (points.length - 1);
  const y = v => T + ph - ph * (v - lo) / span;

  const line = points.map((p,i) => x(i).toFixed(1)+","+y(p[1]).toFixed(1)).join(" ");
  const area = L+","+(T+ph)+" "+line+" "+(L+pw)+","+(T+ph);
  const first = vals[0], last = vals[vals.length-1], up = last >= first;
  const col = up ? "var(--up)" : "var(--down)";

  let peak = vals[0], dd = 0;
  for (const v of vals) { peak = Math.max(peak, v); if (peak) dd = Math.min(dd, v/peak - 1); }

  const lbl = s => tf === "1D" ? s.slice(11,16) : s.slice(0,10);
  meta.innerHTML = points.length + " points · "
    + '<span class="' + (up?"up":"down") + '">' + pct(last/first - 1, 2) + "</span>"
    + ' · <span class="down">' + pct(dd,2) + "</span> max drawdown";

  wrap.innerHTML =
    '<svg class="curve" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'
    + '<polygon points="'+area+'" fill="'+col+'" opacity="0.10"/>'
    + '<polyline points="'+line+'" fill="none" stroke="'+col+'" stroke-width="2"'
    + ' stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle cx="'+x(points.length-1).toFixed(1)+'" cy="'+y(last).toFixed(1)
    + '" r="3.5" fill="'+col+'"/>'
    + '<text x="'+L+'" y="'+(H-6)+'" class="ax">'+lbl(points[0][0])+'</text>'
    + '<text x="'+(L+pw)+'" y="'+(H-6)+'" class="ax" text-anchor="end">'
    + lbl(points[points.length-1][0])+'</text></svg>';
}

function renderTF() {
  // A window is only honest if the data actually reaches back to its cutoff.
  // With two NAV points every button from 1W to 1Y draws the same two-point
  // line, which looks like an answer and is not one — so say so on the button
  // rather than let the label imply a year of history.
  const daily = data.nav_history || [];
  const earliest = daily.length ? new Date(daily[0][0]) : null;
  const last = daily.length ? daily[daily.length-1][0] : null;

  document.getElementById("tf").innerHTML = TF.map(t => {
    const pts = seriesFor(t);
    if (pts.length < 2)
      return '<button class="btn" data-tf="'+t+'" disabled title="only '
        + pts.length + ' point of history">'+t+'</button>';
    const c = last ? cutoff(t, last) : null;
    const partial = t !== "1D" && c && earliest && earliest > c;
    return '<button class="btn'+(t===tf?' active':'')+(partial?' partial':'')
      + '" data-tf="'+t+'"'
      + (partial ? ' title="history only goes back to '+daily[0][0]
                   +' — this shows everything there is, not a full '+t+'"' : '')
      + '>'+t+(partial?'*':'')+'</button>';
  }).join("");
  document.querySelectorAll("#tf .btn").forEach(b =>
    b.onclick = () => { tf = b.dataset.tf; renderTF(); drawCurve(seriesFor(tf)); });
}

// Column definitions in one place: header, how to read the value for sorting,
// and how to draw the cell. Sorting has to use the NUMBER, not the formatted
// string, or "1,00,000" sorts before "9" — which is exactly the bug that
// hand-written table sorters ship with.
// A deterministic colour per symbol (not random per render), so a name's
// fallback avatar doesn't change colour every refresh.
function logoColor(sym) {
  let h = 0;
  for (let i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) | 0;
  return "hsl(" + (Math.abs(h) % 360) + ",55%,42%)";
}
// HTML only — no inline onerror="" attribute, which has to escape its own
// quotes past the surrounding HTML attribute and past the outer template
// string. That broke exactly this way the first time: the fallback markup's
// own style="..." quotes closed the onerror attribute early and spilled the
// rest onto the page as visible text. wireLogos() below attaches the actual
// fallback behaviour after the table is in the DOM, via the DOM API, where
// there is no string to escape into in the first place.
// Real logos, tried in order, before ever falling back to initials:
//  1. TradingView's own logo slug (classification.json's 3rd tuple element,
//     tagged onto the position server-side) — the only source that actually
//     covers India; FMP has no logo image for any NSE-listed ticker.
//  2. FMP's image, for IBKR (US) positions only, in case a ticker's TV
//     logoid is missing (delisted from that day's classified universe, etc.)
//     but FMP still recognises the symbol.
function logoCell(p) {
  const initials = (p.symbol || "??").replace(/[^A-Z]/gi, "").slice(0, 2).toUpperCase();
  const candidates = [];
  if (p.logoid) candidates.push("https://s3-symbol-logo.tradingview.com/" + encodeURIComponent(p.logoid) + ".svg");
  if (data.broker === "ibkr") candidates.push("https://images.financialmodelingprep.com/symbol/" + encodeURIComponent(p.symbol) + ".png");
  if (!candidates.length) {
    return '<div class="pos-logo-fallback" style="background:' + logoColor(p.symbol || "") + '">' + initials + '</div>';
  }
  return '<img class="pos-logo" data-symbol="' + p.symbol + '" data-initials="' + initials
    + '" data-candidates="' + candidates.join("|") + '" data-next="1" alt="" src="' + candidates[0] + '">';
}

function wireLogos() {
  document.querySelectorAll("img.pos-logo").forEach(img => {
    img.onerror = () => {
      const candidates = (img.dataset.candidates || "").split("|").filter(Boolean);
      const next = parseInt(img.dataset.next || "1", 10);
      if (next < candidates.length) {
        img.dataset.next = String(next + 1);
        img.src = candidates[next];
        return;
      }
      const fallback = document.createElement("div");
      fallback.className = "pos-logo-fallback";
      fallback.style.background = logoColor(img.dataset.symbol || "");
      fallback.textContent = img.dataset.initials || "";
      img.replaceWith(fallback);
    };
  });
}

const COLUMNS = [
  {key:"symbol", label:"Position", num:false,
   cell:p => '<div class="pos-cell">' + logoCell(p) +
     '<div class="pos-name"><b>' + p.symbol + '</b>' +
     (p.industry ? '<span class="ind">' + p.industry + '</span>' : '') +
     '</div></div>'},
  {key:"sector", label:"Sector", num:false,
   cell:p => p.sector || '<span class="dim">&mdash;</span>'},
  {key:"quantity", label:"Qty",
   cell:p => (p.quantity||0).toLocaleString(LOCALE(data.currency))},
  {key:"mark", label:"Mark",
   // A mark with no live quote behind it is labelled, so a stale price is
   // never presented as a live tick.
   cell:p => px(p.mark, p) + (p.price_source && p.price_source !== "last"
       ? '<div class="src">'+p.price_source+'</div>' : '')},
  {key:"cost_price", label:"Avg cost", cell:p => px(p.cost_price, p)},
  {key:"cost", label:"Cost value", cell:p => cash(p.cost)},
  {key:"value", label:"Market value", cell:p => cash(p.value)},
  {key:"pct_nav", label:"% NAV", cell:p => pct(p.pct_nav)},
  {key:"unrealized", label:"Unrealized",
   cell:p => '<span class="'+cls(p.unrealized)+'">' + cash(p.unrealized)
     + '<div class="dim">'+pct(p.unrealized_pct)+'</div></span>'},
  {key:"stop", label:"Stop",
   cell:p => p.stop!=null ? px(p.stop,p) : '<span class="warn">not set</span>'},
  {key:"risk", label:"Risk",
   cell:p => p.risk!=null
     ? '<span class="down">'+cash(p.risk)+'</span>'
       + ' <span class="dim">('+pct(p.risk_pct_nav)+')</span>'
     : '<span class="warn">—</span>'},
];

// Share of INVESTED value per sector — not NAV, so idle cash doesn't dilute
// the read of what the book is actually concentrated in. Positions with no
// sector tag (not in that day's classified universe) are grouped under
// "Unclassified" rather than silently dropped, so the shares still sum to
// the full invested total.
function renderExposure() {
  const panel = document.getElementById("exposure-panel");
  const positions = (data.positions || []).filter(p => p.value);
  if (!positions.length) { panel.hidden = true; return; }
  panel.hidden = false;

  const bySector = {};
  let total = 0;
  positions.forEach(p => {
    const key = p.sector || "Unclassified";
    bySector[key] = (bySector[key] || 0) + p.value;
    total += p.value;
  });
  const rows = Object.keys(bySector).map(name => ({ name, value: bySector[name] }))
    .sort((a, b) => b.value - a.value);
  const max = Math.max(...rows.map(r => r.value), 1);

  document.getElementById("exposure-rows").innerHTML = rows.map(r => {
    const sharePct = total ? (r.value / total * 100) : 0;
    const barPct = Math.round((r.value / max) * 100);
    return '<div class="exp-row"><span>' + r.name + '</span>'
      + '<div class="exp-track"><div class="exp-fill" style="width:' + barPct + '%"></div></div>'
      + '<span class="exp-val">' + sharePct.toFixed(1) + '%</span></div>';
  }).join("");
}

let sortKey = "value", sortDesc = true;
let posFilter = "all", sectorFilter = "";
let expandedSymbol = null;
const perfCache = {};

function renderChips() {
  const CHIPS = [
    ["all", "All"], ["winners", "Winners"], ["losers", "Losers"],
    ["no-stop", "No stop set"],
  ];
  document.getElementById("chips").innerHTML = CHIPS.map(([k, label]) =>
    '<button class="btn' + (posFilter === k ? ' active' : '')
    + '" data-f="' + k + '">' + label + '</button>').join("");
  document.querySelectorAll("#chips .btn").forEach(b =>
    b.onclick = () => { posFilter = b.dataset.f; renderTable(); });
}

function passesChip(p) {
  if (posFilter === "winners") return (p.unrealized || 0) > 0;
  if (posFilter === "losers") return (p.unrealized || 0) < 0;
  if (posFilter === "no-stop") return p.stop == null;
  return true;
}

// The per-ticker price history that already backs the market breadth terminal's
// own stock chart and TMLE panels — the portfolio page reads the very same
// files through the /docs proxy, rather than fetching anything of its own.
// India's positions live under docs/in/tickers/, US under docs/tickers/; any
// symbol outside that day's classified universe (an ETF, a delisted name,
// pretty much anything on Angel One that isn't a benchmark constituent) 404s,
// which is reported as "no price history" rather than left to hang.
function tickerUrl(symbol) {
  const sub = data.broker.indexOf("angelone") === 0 ? "in/" : "";
  return "/docs/" + sub + "tickers/" + encodeURIComponent(symbol) + ".json";
}

async function loadPerf(symbol) {
  const key = data.broker + "|" + symbol;
  if (perfCache[key]) return perfCache[key];
  const p = (async () => {
    const res = await fetch(tickerUrl(symbol));
    if (!res.ok) return null;
    const j = await res.json();
    const dates = j.dates || [], closes = j.close || [];
    if (!dates.length) return null;
    const last = dates[dates.length - 1], lastClose = closes[closes.length - 1];
    const since = tf2 => {
      const c = cutoff(tf2, last);
      let i = 0;
      while (i < dates.length && new Date(dates[i]) < c) i++;
      if (i >= dates.length || closes[i] == null || lastClose == null) return null;
      return lastClose / closes[i] - 1;
    };
    return { wtd: since("WTD"), mtd: since("MTD"), ytd: since("YTD"), asOf: last };
  })();
  perfCache[key] = p;
  return p;
}

function expandRowHtml(symbol) {
  return '<tr class="expand-row" data-expand-for="' + symbol + '">'
    + '<td colspan="' + COLUMNS.length + '"><div class="expand-stats" id="perf-'
    + symbol.replace(/[^A-Za-z0-9]/g, "_") + '">Loading price history…</div></td></tr>';
}

function fillExpandRow(symbol) {
  const id = "perf-" + symbol.replace(/[^A-Za-z0-9]/g, "_");
  loadPerf(symbol).then(perf => {
    const el = document.getElementById(id);
    if (!el) return; // row was collapsed before the fetch resolved
    if (!perf) {
      el.innerHTML = '<span class="dim">No price history for ' + symbol
        + ' in the classified universe — likely an ETF or a name outside '
        + 'that day\'s index constituents.</span>';
      return;
    }
    const stat = (label, v) => '<div class="expand-stat"><div class="expand-stat-label">'
      + label + '</div><div class="expand-stat-val ' + cls(v) + '">' + pct(v, 2)
      + '</div></div>';
    el.innerHTML = stat("WTD", perf.wtd) + stat("MTD", perf.mtd) + stat("YTD", perf.ytd)
      + '<div class="expand-stat"><div class="expand-stat-label">As of</div>'
      + '<div class="expand-stat-val dim" style="font-size:12.5px;font-weight:500">'
      + perf.asOf + '</div></div>';
  });
}

// Plain arithmetic mean over whichever rows renderTable() just filtered to —
// same rows the table itself is showing, so the numbers stay in sync with
// whatever filter/search/sort is active rather than always covering the
// whole book.
function avgOf(rows, key) {
  const vals = rows.map(p => p[key]).filter(v => v != null && !isNaN(v));
  if (!vals.length) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

let showAverages = false;

function renderAverages(rows) {
  const panel = document.getElementById("averages-panel");
  panel.hidden = !showAverages;
  if (!showAverages) return;
  if (!rows.length) {
    panel.innerHTML = '<div class="stat"><div class="dim">No positions to average.</div></div>';
    return;
  }
  const withStop = rows.filter(p => p.stop != null);
  const avgRisk = withStop.length ? avgOf(withStop, "risk") : null;
  const avgUnreal = avgOf(rows, "unrealized"), avgUnrealPct = avgOf(rows, "unrealized_pct");
  // Mark and cost price are PER-SHARE figures in each instrument's own
  // currency — averaging across tickers listed in different currencies has
  // no single honest unit to show it in, so these two stay unconverted
  // plain numbers rather than run through cash()'s base-currency symbol.
  const stats = [
    ["Avg qty", Math.round(avgOf(rows, "quantity") || 0).toLocaleString(LOCALE(data.currency))],
    ["Avg mark", (avgOf(rows, "mark") || 0).toFixed(2)],
    ["Avg cost price", (avgOf(rows, "cost_price") || 0).toFixed(2)],
    ["Avg cost value", cash(avgOf(rows, "cost"))],
    ["Avg position size", cash(avgOf(rows, "value"))],
    ["Avg % of NAV", pct(avgOf(rows, "pct_nav"))],
    ["Avg unrealized", '<span class="'+cls(avgUnreal)+'">' + cash(avgUnreal) + '</span>'],
    ["Avg unrealized %", '<span class="'+cls(avgUnrealPct)+'">' + pct(avgUnrealPct) + '</span>'],
    ["Avg risk", avgRisk == null ? "—"
      : cash(avgRisk) + (withStop.length < rows.length
          ? ' <span class="dim" style="font-size:11px">(' + withStop.length + '/' + rows.length + ' have a stop)</span>' : "")],
  ];
  panel.innerHTML = stats.map(s =>
    '<div class="stat"><div class="stat-label">' + s[0] + '</div>'
    + '<div class="stat-value" style="font-size:17px">' + s[1] + '</div></div>').join("");
}

document.getElementById("avg-toggle").onclick = () => {
  showAverages = !showAverages;
  document.getElementById("avg-toggle").classList.toggle("active", showAverages);
  renderTable();
};

function renderTable() {
  document.getElementById("head").innerHTML = COLUMNS.map(c =>
    '<th class="sortable'+(c.num===false?'':' num')
    + (c.key===sortKey?' sorted':'')+'" data-key="'+c.key+'">'
    + c.label + (c.key===sortKey ? (sortDesc?' ▾':' ▴') : '') + '</th>').join("");
  document.querySelectorAll("#head th").forEach(th => th.onclick = () => {
    if (sortKey === th.dataset.key) sortDesc = !sortDesc;
    else { sortKey = th.dataset.key; sortDesc = true; }
    renderTable();
  });

  renderChips();

  const sectorSel = document.getElementById("sector-filter");
  const sectors = Array.from(new Set(data.positions.map(p => p.sector || "Unclassified"))).sort();
  if (sectorSel.dataset.built !== sectors.join("|")) {
    sectorSel.innerHTML = '<option value="">All sectors</option>'
      + sectors.map(s => '<option value="'+s+'">'+s+'</option>').join("");
    sectorSel.value = sectorFilter;
    sectorSel.dataset.built = sectors.join("|");
    sectorSel.onchange = () => { sectorFilter = sectorSel.value; renderTable(); };
  }

  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  let rows = data.positions.filter(p =>
    (!q || (p.symbol||"").toLowerCase().includes(q))
    && passesChip(p)
    && (!sectorFilter || (p.sector || "Unclassified") === sectorFilter));

  rows = rows.slice().sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x == null && y == null) return 0;
    if (x == null) return 1;          // blanks last, whichever direction
    if (y == null) return -1;
    const r = (typeof x === "string") ? x.localeCompare(y) : (x - y);
    return sortDesc ? -r : r;
  });

  const filtered = q || posFilter !== "all" || sectorFilter;
  document.getElementById("filter-note").textContent =
    filtered ? rows.length + " of " + data.positions.length + " positions" : "";

  renderAverages(rows);

  document.getElementById("rows").innerHTML = rows.map(p => {
    const open = expandedSymbol === p.symbol;
    const row = '<tr class="pos-row'+(open?' open':'')+'" data-symbol="'+p.symbol+'">'
      + COLUMNS.map(c => '<td class="'+(c.num===false?'':'num')+'">'+c.cell(p)+'</td>').join("")
      + '</tr>';
    return row + (open ? expandRowHtml(p.symbol) : "");
  }).join("")
    || '<tr><td colspan="'+COLUMNS.length+'">'
       + (filtered ? 'No positions match.' : 'No open positions.')+'</td></tr>';
  wireLogos();

  document.querySelectorAll("tr.pos-row").forEach(tr => tr.onclick = () => {
    const sym = tr.dataset.symbol;
    expandedSymbol = expandedSymbol === sym ? null : sym;
    renderTable();
    if (expandedSymbol === sym) fillExpandRow(sym);
  });
}

function render() {
  const a = data.account, cur = data.currency;
  document.getElementById("mode").className = "tag " + (data.live ? "live" : "eod");
  document.getElementById("mode").textContent = data.live ? "live" : "end of day";

  // Offered only while the feed is not live. After logging in, the next poll
  // detects the gateway and the button disappears on its own.
  const connect = document.getElementById("connect");
  if (data.connect_url && !data.live) {
    connect.textContent = "⚡ " + (data.connect_label || "Connect");
    connect.title = "Starts the IBKR gateway if needed, then opens its login. "
      + "Accept the self-signed certificate, sign in, and come back — this "
      + "page picks it up within about 30 seconds.";
    connect.hidden = false;
    connect.onclick = async function (e) {
      e.preventDefault();
      const original = connect.textContent;
      connect.textContent = "starting gateway…";
      const j = await (await fetch("/api/gateway/start", {method:"POST"})).json();
      connect.textContent = original;
      if (j.ok) window.open(j.url, "_blank", "noopener");
      else document.getElementById("warnings").innerHTML =
        '<div class="warn-line">' + (j.error || "could not start the gateway")
        + '</div>';
    };
  } else {
    connect.hidden = true;
  }
  document.getElementById("stamp").textContent =
    (data.as_of || "") + " · updated " + (data.fetched_at || "");

  renderCcy();
  document.getElementById("stats").innerHTML = [
    ["NAV", cash(a.nav), ccy()],
    ["Invested", pct(a.invested_pct), cash(a.invested)],
    ["Unrealized",
     '<span class="'+cls(a.unrealized)+'">' + cash(a.unrealized) + "</span>",
     pct(a.unrealized_pct) + " on " + cash(a.cost) + " cost"],
    ["Cash", cash(a.cash), pct(1 - (a.invested_pct||0)) + " of NAV"],
    ["Portfolio heat", '<span class="down">' + pct(a.heat_pct) + "</span>",
     cash(a.heat) + " at risk"],
    ["Positions", a.positions, "largest " + pct(a.largest_pct)],
  ].map(s => '<div class="stat"><div class="stat-label">'+s[0]+'</div>'
    + '<div class="stat-value">'+s[1]+'</div>'
    + '<div class="stat-sub">'+s[2]+'</div></div>').join("");

  const w = [];
  if (data.stale)
    w.push("Showing the last good data from " + (data.stale_since || "earlier")
      + " — the latest refresh failed: " + (data.stale_reason || "unknown"));
  if (a.missing_stops.length)
    w.push("No stop recorded for " + a.missing_stops.join(", ")
      + " — heat excludes " + (a.missing_stops.length===1?"it":"them")
      + ", so the real figure is higher.");
  if (a.concentrated)
    w.push("Largest position is " + pct(a.largest_pct) + " of NAV.");
  document.getElementById("warnings").innerHTML =
    w.map(t => '<div class="warn-line">'+t+'</div>').join("");

  renderExposure();
  renderTable();

  document.getElementById("foot").innerHTML =
    "Value, Unrealized and Risk are shown in <b>" + ccy() + "</b>. "
    + "Mark, Cost and Stop stay in each instrument's own currency — a share "
    + "price and a stop belong to the market you trade them in, and "
    + "restating them makes them unrecognisable."
    + (data.usd_rate && showUsd
        ? " Converted at " + data.usd_rate.toFixed(6) + " "
          + data.currency + "/USD, the rate "
          + (data.broker === "ibkr" ? "IBKR used for this statement." : "from FMP.")
        : "")
    + "<br>Risk is measured from the current mark to your stop, not from "
    + "entry — what the stop would cost you today.";

  renderTF();
  drawCurve(seriesFor(tf));
}

let brokerList = [];

function meta() {
  return brokerList.find(b => b.id === broker) || {};
}

function renderIdentity() {
  const m = meta();
  document.getElementById("flag").innerHTML = m.flag || "";
  // A supplied logo file wins; otherwise a brand-coloured chip, which is
  // recognisable without shipping anyone else's trademarked artwork.
  document.getElementById("mark").innerHTML = m.logo
    ? '<img class="logo" src="' + m.logo + '" alt="' + (m.short||"") + '">'
    : '<span class="chip" style="background:' + (m.color||"#6b7280") + '">'
      + (m.short || "") + '</span>';

  const field = document.getElementById("name");
  if (document.activeElement !== field) field.value = m.name || "";
  field.placeholder = "Name this portfolio";
}

async function saveName() {
  const field = document.getElementById("name");
  const value = field.value.trim();
  await fetch("/api/name", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({broker: broker, name: value})
  });
  const b = brokerList.find(x => x.id === broker);
  if (b) b.name = value;
  field.blur();
}

async function loadBrokers() {
  brokerList = await (await fetch("/api/brokers")).json();
  document.getElementById("brokers").innerHTML = brokerList.map(b =>
    '<button class="btn'+(b.id===broker?' active':'')+'" data-b="'+b.id+'" '
    + 'title="'+(b.name||b.short)+'">'
    + (b.flag ? b.flag+" " : "")
    + (b.name || b.short) + '</button>').join("");
  document.querySelectorAll("#brokers .btn").forEach(el =>
    el.onclick = () => switchBroker(el.dataset.b));
  renderIdentity();
}

// loadBrokers() and tick() both hit the network and both eventually call
// render() — firing them unawaited (the old behaviour) let them resolve in
// whichever order the network happened to return them, and the table sat
// showing the PREVIOUS broker's positions with no indication anything was
// happening until whichever of the two finished last. Clearing the table to
// an explicit loading row up front, then awaiting each fetch in order, means
// a broker switch always shows visible progress instead of looking frozen.
async function switchBroker(id) {
  broker = id;
  data = null;
  document.getElementById("rows").innerHTML =
    '<tr><td colspan="' + COLUMNS.length + '">Loading…</td></tr>';
  document.getElementById("stats").innerHTML = "";
  document.getElementById("warnings").innerHTML = "";
  renderIdentity();
  await loadBrokers();
  await tick();
}

document.getElementById("name").addEventListener("blur", saveName);
document.getElementById("name").addEventListener("keydown", e => {
  if (e.key === "Enter") saveName();
  if (e.key === "Escape") { document.getElementById("name").value = meta().name || "";
                            document.getElementById("name").blur(); }
});

function showLogin(on) {
  document.getElementById("login").hidden = !on;
  document.getElementById("login-error").textContent = "";
  if (on) document.getElementById("pin").focus();
  else { document.getElementById("pin").value = "";
         document.getElementById("totp").value = ""; }
}

document.getElementById("login-cancel").onclick = () => showLogin(false);

document.getElementById("login-form").onsubmit = async function (e) {
  e.preventDefault();
  const err = document.getElementById("login-error");
  err.textContent = "connecting…";
  const r = await fetch("/api/connect", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      broker: broker,
      pin: document.getElementById("pin").value,
      totp: document.getElementById("totp").value
    })
  });
  const j = await r.json();
  if (j.ok) { showLogin(false); await loadBrokers(); await tick(); }
  else { err.textContent = j.error || "login failed"; }
};

async function tick() {
  try {
    const r = await fetch("/api/portfolio?broker=" + broker);
    const j = await r.json();
    if (j.needs_login) {
      // Not an error — it just has not been signed into yet.
      document.getElementById("warnings").innerHTML = "";
      document.getElementById("connect-btn").hidden = false;
      document.getElementById("connect-btn").textContent =
        "⚡ Connect to " + (meta().short === "AO" ? "Angel One" : meta().short);
      document.getElementById("stamp").textContent = "not connected";
      return;
    }
    document.getElementById("connect-btn").hidden = true;
    if (j.error) {
      document.getElementById("warnings").innerHTML =
        '<div class="warn-line">' + j.error + '</div>';
      return;
    }
    data = j; render();
  } catch (e) {
    document.getElementById("stamp").textContent = "server not reachable";
  }
}

document.getElementById("connect-btn").onclick = () => showLogin(true);
document.getElementById("filter").addEventListener("input", () => {
  if (data) renderTable();
});

(async () => { await loadBrokers(); await tick(); })();
setInterval(tick, REFRESH_MS);
</script>
</main></body></html>"""


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if "--angel" in sys.argv:
        import angelone
        log("Connecting Angel One — nothing you type is stored.")
        pin, totp = angelone.prompt_credentials()
        _sessions["angelone"] = angelone.login(pin, totp)
        del pin
        log("  Angel One connected\n")

    # Runs once now and then every SYNC_LOOP_MINUTES for as long as the
    # server is up, so the hub stops depending on someone noticing a stale
    # date and asking for it to be fixed — see the block above main() for
    # what each half of this actually does and why it is safe to automate.
    threading.Thread(target=_sync_loop, args=(log,), daemon=True).start()

    import movers
    threading.Thread(target=movers.warm, daemon=True).start()   # build the Movers table in the background
    import setup_finder
    threading.Thread(target=setup_finder.warm, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    log(f"  serving {url}")
    log(f"  embed this in Notion:  {url}?broker=ibkr")
    for b in available()[1:]:
        log(f"                         {url}?broker={b['id']}")
    log("\n  Bound to 127.0.0.1 — reachable from this machine only.")
    log("  Leave this window open. Ctrl+C to stop.\n")

    # --no-open is for the logon launcher: opening a browser tab every time
    # the machine starts would be its own small annoyance.
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("\n  stopped")


def _hub_nav_json():
    """The sidebar's contents, generated from HUB_PANELS and config.COUNTRIES
    rather than hand-duplicated in the JS below — a panel added to one list
    should not require editing two places to appear in the hub."""
    cfg_code = [None]

    def indices_group(cfg, prefix):
        labels = cfg.get("index_labels", {})
        children = [
            {"label": labels.get(key, key.title()),
             "url": prefix + f"panel-index-{key}.html",
             "note": ""}
            for key in cfg.get("index_tickers", {})
        ]
        try:
            acc = os.path.join(config.data_dir(cfg_code[0]), "accdist_index.json")
            if os.path.exists(acc) and os.path.getsize(acc) > 4:
                children.append({"label": "Distribution days", "url": prefix + "panel-distribution.html",
                                 "note": "O'Neil distribution and accumulation days on the indices."})
        except Exception:
            pass
        return {"label": "Indices", "children": children}

    def resolve(entry, cfg, prefix):
        if entry.get("dynamic") == "indices":
            return indices_group(cfg, prefix)
        if "children" in entry:
            return {"label": entry["label"], "children": [
                {"label": c["label"], "url": prefix + c["path"],
                 "note": c.get("note", "")} for c in entry["children"]]}
        if entry.get("native"):
            return {"label": entry["label"], "url": entry["native"] + "?country=" + cfg.get("short", "US"),
                    "scoped": False}
        return {"label": entry["label"], "url": prefix + entry["path"]}

    countries = []
    for code, cfg in config.COUNTRIES.items():
        cfg_code[0] = code
        sub = cfg.get("docs_subdir", "")
        prefix = f"/docs/{sub}/" if sub else "/docs/"
        countries.append({
            "code": code, "label": cfg.get("short", code),
            "flag": flags.flag(code) if flags else "",
            "panels": [resolve(entry, cfg, prefix) for entry in HUB_PANELS],
            "signalsPanels": [resolve(entry, cfg, prefix) for entry in SIGNALS_PANELS],
            "algorithmsPanels": [resolve(entry, cfg, prefix) for entry in ALGORITHMS_PANELS],
            "systemPanels": [resolve(entry, cfg, prefix) for entry in SYSTEM_PANELS],
        })
    return json.dumps(countries)


# Light / Dark / Auto: chosen in the hub sidebar, stored as localStorage "nts-theme" (same origin for
# every page), applied to <html data-theme> before first paint. A change reloads open pages so charts
# pick up the new colours too.
THEME_HEAD = ('<script>(function(){var d=document.documentElement;try{var t=localStorage.getItem("nts-theme");'
              'if(t==="light"||t==="dark")d.setAttribute("data-theme",t);}catch(e){}'
              'addEventListener("storage",function(e){if(e.key==="nts-theme"&&window.top!==window)location.reload();});})();</script>')
# Warm neutrals to match the logo, laid over every page's own tokens (html:root outranks :root);
# accents (up, down, links, chart colours) stay each page's own.
THEME_HEAD += ('<style id="nts-warm">html:root{--bg:#f6f5f1;--panel:#fffefb;--panel-bg:#fffefb;--line:#e6e3da;--border:#e6e3da;--text:#1f1e1c;--ink:#1f1e1c;--dim:#6f6c64;--text-dim:#6f6c64;--faint:#a19d93;--grid:#efede6;--grid-line:#efede6}'
               '@media (prefers-color-scheme: dark){html:root:not([data-theme="light"]){--bg:#141311;--panel:#1c1b18;--panel-bg:#1c1b18;--line:#2e2c28;--border:#2e2c28;--text:#f1efe8;--ink:#f1efe8;--dim:#a19d93;--text-dim:#a19d93;--faint:#77736a;--grid:#23211e;--grid-line:#23211e}}'
               'html:root[data-theme="dark"]{--bg:#141311;--panel:#1c1b18;--panel-bg:#1c1b18;--line:#2e2c28;--border:#2e2c28;--text:#f1efe8;--ink:#f1efe8;--dim:#a19d93;--text-dim:#a19d93;--faint:#77736a;--grid:#23211e;--grid-line:#23211e}</style>')
_HEAD_RE = re.compile(r"<head[^>]*>", re.I)


def _with_theme(html):
    if "nts-theme" in html[:4000]:
        return html
    m = _HEAD_RE.search(html, 0, 4000)
    return html[:m.end()] + THEME_HEAD + html[m.end():] if m else html


HUB_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Neil's Trading System</title>
<link rel="icon" type="image/png" sizes="256x256" href="/nts-icon.png">
<link rel="apple-touch-icon" href="/nts-icon.png">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 34 34'%3E%3Crect width='34' height='34' rx='9' fill='%232C2C2A'/%3E%3Cpath d='M8 25V13l10 10V12' fill='none' stroke='%23F1EFE8' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cline x1='25' y1='4.5' x2='25' y2='20' stroke='%23FAC775' stroke-width='1.4' stroke-linecap='round'/%3E%3Crect x='22.5' y='7.5' width='5' height='9.5' rx='1' fill='%23EF9F27'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  /* Neil's Trading System: the A4 mark's charcoal and gold carry the chrome; section colours stay for wayfinding. */
  :root { --bg:#f6f5f1; --panel:#fffefb; --line:#e6e3da; --text:#1f1e1c;
    --dim:#6f6c64; --accent:#b7791f; --accent-dim:#fbefd9; --gold:#EF9F27; --gold-soft:#FAC775;
    --tile:#2C2C2A; --tile-ink:#F1EFE8; --tile-ring:transparent;
    --mb:#2563eb; --sig:#b45309; --algo:#7c3aed; --port:#15803d; --sys:#5f5e5a;
    --icon-glow:rgba(183,121,31,.4); --down:#c62828; --warn:#a15c07; color-scheme:light;
  }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
    --bg:#141311; --panel:#1c1b18; --line:#2e2c28; --text:#f1efe8;
    --dim:#a19d93; --accent:#EF9F27; --accent-dim:#3a2c12;
    --tile:#2C2C2A; --tile-ink:#F1EFE8; --tile-ring:#4a4740;
    --mb:#5b9dff; --sig:#e0a94e; --algo:#a78bfa; --port:#4ade80; --sys:#b4b2a9;
    --icon-glow:rgba(239,159,39,.45); --down:#f26b63; --warn:#e3b04b; color-scheme:dark;
  } }
  :root[data-theme="dark"] {
    --bg:#141311; --panel:#1c1b18; --line:#2e2c28; --text:#f1efe8;
    --dim:#a19d93; --accent:#EF9F27; --accent-dim:#3a2c12;
    --tile:#2C2C2A; --tile-ink:#F1EFE8; --tile-ring:#4a4740;
    --mb:#5b9dff; --sig:#e0a94e; --algo:#a78bfa; --port:#4ade80; --sys:#b4b2a9;
    --icon-glow:rgba(239,159,39,.45); --down:#f26b63; --warn:#e3b04b; color-scheme:dark;
  }
  * { box-sizing:border-box; }
  html, body { height:100%; margin:0; overflow:hidden; }
  body { display:flex; background:var(--bg); color:var(--text);
    font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px; }

  #sidebar { width:230px; flex:none; background:var(--panel);
    border-right:1px solid var(--line); display:flex; flex-direction:column;
    overflow-y:auto; }
  #brand { padding:16px 14px 12px 16px; display:flex; align-items:center; gap:10px; }
  #brand svg { flex:none; border-radius:9px; box-shadow:0 0 0 1px var(--tile-ring); }
  #brand .bn { min-width:0; line-height:1.15; }
  #brand .bn b { display:block; font-size:14.5px; font-weight:600; letter-spacing:-.01em; white-space:nowrap; }
  #brand .bn small { display:block; font-size:11.5px; color:var(--dim); white-space:nowrap; margin-top:2px; }
  #theme-switch { display:flex; gap:2px; margin:0 12px 12px; padding:2px; border:1px solid var(--line); border-radius:8px; background:var(--bg); }
  #theme-switch button { flex:1; border:0; background:transparent; color:var(--dim); border-radius:6px; padding:4px 0;
    font:500 11.5px "IBM Plex Sans",sans-serif; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:5px; }
  #theme-switch button svg { width:13px; height:13px; }
  #theme-switch button.on { background:var(--panel); color:var(--text); box-shadow:0 0 0 1px var(--line); }

  #country-switch { display:flex; gap:5px; padding:0 12px 12px; }
  #country-switch button { flex:1; background:var(--bg); border:1px solid var(--line);
    color:var(--dim); border-radius:7px; padding:6px 4px; font-size:12px;
    font-family:inherit; cursor:pointer; }
  #country-switch button.active { background:var(--tile); border-color:var(--tile);
    color:var(--tile-ink); box-shadow:inset 0 -2px 0 var(--gold); }

  /* Each group carries its own accent (--row-c, set per #<x>-nav below) so
     the sidebar's 6 sections are tellable apart at a glance instead of all
     six looking identical but for their text label — a colored dot plus a
     colored label, a matching icon tint, and a matching active-row rail. */
  .group-label { display:flex; align-items:center; gap:7px; padding:14px 16px 6px;
    margin-top:2px; position:relative; font-size:11px; text-transform:uppercase;
    letter-spacing:.07em; color:var(--row-c, var(--dim)); font-weight:800; cursor:pointer;
    user-select:none; }
  .group-label::before { content:""; position:absolute; left:16px; right:16px; top:0;
    height:1px; background:var(--line); }
  #pinned-section .group-label::before, #sidebar > .group-label:first-of-type::before { display:none; }
  .group-label .label-dot { width:6px; height:6px; border-radius:50%; flex:none;
    background:var(--row-c, var(--dim)); }
  .group-label .label-text { flex:1; }
  .group-label .label-fresh { width:6px; height:6px; border-radius:50%; flex:none;
    background:var(--down); box-shadow:0 0 0 2px color-mix(in srgb, var(--down) 25%, transparent); }
  .group-label .label-chevron { flex:none; color:var(--dim); font-size:10px;
    display:inline-block; transition:transform .15s; }
  .group-label.collapsed .label-chevron { transform:rotate(-90deg); }
  #pinned-section[hidden], .group-label[hidden] { display:none; }
  nav.collapsed { display:none; }

  nav a { display:flex; align-items:center; gap:9px; padding:8px 16px;
    color:var(--dim); text-decoration:none; font-size:13px; font-weight:500;
    border-left:4px solid transparent; cursor:pointer; transition:background .12s, border-color .12s; }
  nav a .nav-label { flex:1; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; }
  /* Icons use stroke="currentColor", but .nav-icon sets its OWN color here
     (the group's --row-c) rather than inheriting the row's dim/active text
     color the way it used to — that decouples icon tint from row state, so
     every icon carries its section's color whether or not that row is
     currently selected. A black drop-shadow gives constant depth; the
     row's own color joins it as a glow once active. */
  .nav-icon { flex:none; display:flex; opacity:.7; color:var(--row-c, var(--dim));
    filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.5)); transition:opacity .15s, filter .15s; }
  nav a:hover .nav-icon { opacity:.95; }
  nav a.active .nav-icon { opacity:1;
    filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.5)) drop-shadow(0 0 4px var(--icon-glow)); }
  nav a:hover { color:var(--text); background:color-mix(in srgb, var(--row-c, var(--accent)) 7%, transparent); }
  nav a.active { color:var(--text); border-left-color:var(--row-c, var(--accent));
    background:color-mix(in srgb, var(--row-c, var(--accent)) 20%, transparent); font-weight:700;
    box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--row-c, var(--accent)) 25%, transparent); }


  .pin-btn { background:none; border:none; color:var(--dim); opacity:0;
    font-size:13px; cursor:pointer; line-height:1; padding:2px; flex:none; }
  nav a:hover .pin-btn, .pin-btn.pinned { opacity:1; }
  .pin-btn.pinned { color:var(--accent); }

  /* A grouped nav row (e.g. "Breadth Internals") expands in place to list its
     members directly underneath it, rather than switching to a horizontal
     tab bar above the content — the sidebar shows what is on the page. */
  .sub-nav { display:none; padding:2px 0 6px; }
  .sub-nav.open { display:block; }
  .sub-nav a { padding:6px 16px 6px 39px; font-size:12.5px; }
  .sub-nav a .sub-note { display:block; font-size:11px; color:var(--dim);
    white-space:normal; margin-top:1px; }
  .sub-dot { width:4px; height:4px; border-radius:50%; background:currentColor;
    opacity:.6; flex:none; }

  #sidebar-foot { margin-top:auto; padding:12px 16px; font-size:11px; color:var(--dim);
    border-top:1px solid var(--line); }
  #sidebar-foot a { color:var(--accent); text-decoration:none; }

  #body { flex:1; display:flex; flex-direction:column; min-width:0; }
  #topbar { height:44px; flex:none; display:flex; align-items:center;
    justify-content:space-between; padding:0 16px; border-bottom:1px solid var(--line);
    background:var(--panel); }
  #crumb { font-size:13px; color:var(--dim); }
  #crumb b { color:var(--text); }
  #topbar button { background:none; border:1px solid var(--line); color:var(--dim);
    border-radius:6px; padding:4px 9px; font-size:12px; cursor:pointer;
    font-family:inherit; }
  #topbar button:hover { color:var(--text); }

  #frame-wrap { flex:1; position:relative; }
  #frame-wrap[hidden] { display:none; }
  #frame { position:absolute; inset:0; width:100%; height:100%; border:0;
    background:var(--bg); }

  /* Group mode: every member panel stacked on one scrollable page instead of
     a toggle, each labelled with what makes it different from its sibling. */
  #stack-wrap[hidden] { display:none; }
  #stack-wrap { flex:1; overflow-y:auto; padding:14px 16px; }
  .stack-block { border:1px solid var(--line); border-radius:10px;
    overflow:hidden; background:var(--panel); margin-bottom:16px;
    scroll-margin-top:14px; }
  .stack-block:last-child { margin-bottom:0; }
  .stack-head { padding:10px 14px; border-bottom:1px solid var(--line);
    display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .stack-head b { font-size:13px; }
  .stack-head span { font-size:12px; color:var(--dim); }
  .stack-frame { display:block; width:100%; height:760px; border:0;
    background:var(--bg); }

  /* Workspace tabs (one browser-style tab per open page, each with its own
     history and its own live frame) and the horizontal tabs of a grouped page. */
  #wstabs { display:flex; align-items:flex-end; gap:2px; padding:6px 8px 0; overflow-x:auto;
    background:color-mix(in srgb, var(--bg) 70%, var(--line)); border-bottom:1px solid var(--line); min-height:38px; flex:none; }
  .wtab { display:flex; align-items:center; gap:8px; height:31px; padding:0 8px 0 12px; min-width:110px; max-width:220px;
    border:1px solid transparent; border-bottom:0; border-radius:9px 9px 0 0; color:var(--dim); cursor:pointer;
    font-size:12.5px; font-weight:500; flex:0 1 190px; position:relative; }
  .wtab:hover { background:color-mix(in srgb, var(--accent) 8%, transparent); }
  .wtab.on { background:var(--panel); color:var(--text); border-color:var(--line); font-weight:600; }
  .wtab.on::after { content:""; position:absolute; left:0; right:0; bottom:-1px; height:1px; background:var(--panel); }
  .wtab .tt { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wtab .x { width:18px; height:18px; border-radius:5px; display:flex; align-items:center; justify-content:center; font-size:14px; color:var(--dim); }
  .wtab .x:hover { background:var(--line); color:var(--text); }
  #newtab { flex:none; width:28px; height:28px; margin:0 0 2px 4px; border:0; border-radius:8px; background:transparent;
    color:var(--dim); font-size:18px; cursor:pointer; }
  #newtab:hover { background:color-mix(in srgb, var(--accent) 12%, transparent); color:var(--accent); }
  #topbar-left { display:flex; align-items:center; gap:10px; min-width:0; }
  #navbtns { display:flex; gap:4px; }
  #navbtns button { width:28px; padding:3px 0; }
  #navbtns button:disabled { opacity:.35; cursor:default; }
  #ptabs { display:flex; gap:2px; padding:0 16px; background:var(--panel); border-bottom:1px solid var(--line); overflow-x:auto; flex:none; }
  #ptabs[hidden] { display:none; }
  .ptab { padding:10px 13px; font-size:13px; font-weight:600; color:var(--dim); border:0; background:none;
    border-bottom:2px solid transparent; cursor:pointer; white-space:nowrap; font-family:inherit; }
  .ptab:hover { color:var(--text); }
  .ptab.on { color:var(--accent); border-bottom-color:var(--accent); }
  .pframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:var(--bg); }
  #pop .menu { position:fixed; z-index:50; background:var(--panel); border:1px solid var(--line); border-radius:12px;
    box-shadow:0 8px 28px rgba(0,0,0,.35); padding:8px; width:260px; max-height:60vh; overflow:auto; }
  #pop .menu b { display:block; padding:4px 8px 6px; font-size:12px; color:var(--dim); }
  #pop .menu a { display:block; padding:7px 10px; border-radius:7px; font-size:13px; cursor:pointer; color:var(--text); }
  #pop .menu a:hover { background:color-mix(in srgb, var(--accent) 14%, transparent); }
</style></head><body>

<div id="sidebar">
  <div id="brand"><svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true"><rect width="34" height="34" rx="9" fill="var(--tile)"/><path d="M8 25 V13 L18 23 V12" fill="none" stroke="var(--tile-ink)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/><line x1="25" y1="4.5" x2="25" y2="20" stroke="#FAC775" stroke-width="1.3" stroke-linecap="round"/><rect x="22.5" y="7.5" width="5" height="9.5" rx="1" fill="#EF9F27"/></svg><div class="bn"><b>Neil&rsquo;s Trading System</b><small id="greet"></small></div></div>
  <div id="country-switch"></div>
  <div id="theme-switch" role="group" aria-label="Light or dark">
    <button type="button" data-th="light" title="Light"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/></svg>Light</button><button type="button" data-th="dark" title="Dark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>Dark</button><button type="button" data-th="" title="Follow Windows"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg>Auto</button>
  </div>
  <div id="pinned-section" hidden>
    <div class="group-label">Pinned</div>
    <nav id="pinned-nav"></nav>
  </div>
  <div class="group-label" style="--row-c:var(--mb)" data-group="panel-nav">
    <span class="label-chevron">&#9662;</span><span class="label-dot"></span>
    <span class="label-text">Market Breadth</span><span class="label-fresh" id="fresh-panel-nav" hidden></span>
  </div>
  <nav id="panel-nav" style="--row-c:var(--mb)"></nav>
  <div class="group-label" style="--row-c:var(--sig)" data-group="signals-nav">
    <span class="label-chevron">&#9662;</span><span class="label-dot"></span>
    <span class="label-text">Signals</span><span class="label-fresh" id="fresh-signals-nav" hidden></span>
  </div>
  <nav id="signals-nav" style="--row-c:var(--sig)"></nav>
  <div class="group-label" style="--row-c:var(--algo)" data-group="algorithms-nav">
    <span class="label-chevron">&#9662;</span><span class="label-dot"></span>
    <span class="label-text">Algorithms</span><span class="label-fresh" id="fresh-algorithms-nav" hidden></span>
  </div>
  <nav id="algorithms-nav" style="--row-c:var(--algo)"></nav>
  <div class="group-label" style="--row-c:var(--port)" data-group="portfolio-nav">
    <span class="label-chevron">&#9662;</span><span class="label-dot"></span>
    <span class="label-text">Portfolio</span>
  </div>
  <nav id="portfolio-nav" style="--row-c:var(--port)"></nav>
  <div class="group-label" style="--row-c:var(--sys)" data-group="system-nav">
    <span class="label-chevron">&#9662;</span><span class="label-dot"></span>
    <span class="label-text">System</span>
  </div>
  <nav id="system-nav" style="--row-c:var(--sys)"></nav>
  <div id="sidebar-foot">
    <div id="sync-warning" hidden style="padding:8px; border-radius:6px;
         background:color-mix(in srgb, var(--warn) 15%, transparent);
         border:1px solid var(--warn); color:var(--warn); font-size:11px; line-height:1.4;"></div>
  </div>
</div>

<div id="body">
  <div id="wstabs"></div>
  <div id="topbar">
    <div id="topbar-left">
      <div id="navbtns"><button id="back-btn" title="Back">&#8592;</button><button id="fwd-btn" title="Forward">&#8594;</button></div>
      <div id="crumb"></div>
    </div>
    <button id="reload-btn" title="Reload the current panel">Reload</button>
  </div>
  <div id="ptabs" hidden></div>
  <div id="frame-wrap"></div>
</div>
<div id="pop"></div>

<script>
// One small line-icon per sidebar row, keyed by its label. Plain shapes
// (a wallet, a funnel, a clock) rather than brand marks — these are sections
// of this app, not outside companies, so there is no logo to show. stroke=
// "currentColor" with no fill/color set on the svg itself means each icon
// just inherits the row's current text color — dim normally, full color on
// hover or when active — without any extra JS to keep them in sync.
function icon(d) {
  return '<span class="nav-icon"><svg viewBox="0 0 24 24" width="15" height="15" '
    + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round">' + d + '</svg></span>';
}
const ICONS = {
  "Home": icon('<rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M8.5 16V9.5l5 5V8.5"/>'
    + '<path d="M17 6.5v7" stroke-width="1.6"/>'),
  "Market Environment": icon('<path d="M2 17l4-1.2 3-8.8 4 7 3-4 3 2"/>'
    + '<circle cx="19" cy="12" r="1.3" fill="currentColor" stroke="none"/>'),
  "Indices": icon('<rect x="3" y="14" width="3.6" height="7" rx="1"/>'
    + '<rect x="10.2" y="9" width="3.6" height="12" rx="1"/><rect x="17.4" y="4" width="3.6" height="17" rx="1"/>'),
  "Sector & Industry": icon('<rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/>'
    + '<rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/>'
    + '<rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/>'),
  "Money Flows": icon('<path d="M3 12h4"/><path d="M7 12l5-6"/><path d="M7 12l5 6"/>'
    + '<path d="M12 6h7"/><path d="M12 18h7"/>'),
  "Breadth Internals": icon('<path d="M3 21V3"/><path d="M3 21h18"/>'
    + '<path d="M7 17v-6"/><path d="M12 17v-10"/><path d="M17 17v-3"/>'),
  "Hi/Lo Counts & Screener": icon('<polyline points="17 11 12 6 7 11"/>'
    + '<polyline points="7 13 12 18 17 13"/>'),
  "Movers": icon('<polyline points="3 17 9 11 13 15 21 7"/>'
    + '<polyline points="15 7 21 7 21 13"/>'),
  "Screener": icon('<path d="M3 5h18l-7 8v6l-4 2v-8z"/>'
    + '<circle cx="12" cy="2.2" r="1.1" fill="currentColor" stroke="none"/>'),
  "Setup finder": icon('<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.35-4.35"/><path d="M8 13l2.2-2.6 1.8 1.6 2.4-3.2"/>'),
  "Market Replay": icon('<circle cx="12" cy="12" r="9"/>'
    + '<path d="M10 8.3l5.2 3.7-5.2 3.7z" fill="currentColor" stroke="none"/>'),
  "Stock Lookup": icon('<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20 20l-4.35-4.35"/>'
    + '<path d="M7.3 12.2l1.6-3.2 1.6 2 2.2-4.3"/>'),
  "TMLE Leaders": icon('<path d="M3 8l4 3 5-7 5 7 4-3-2 10H5z"/><path d="M5 21h14"/>'),
  "TMLE Emerging": icon('<path d="M12 21V10"/>'
    + '<path d="M12 10c0-4.2-3.1-6.3-7.3-6.3.3 4.2 3.1 6.3 7.3 6.3z"/>'
    + '<path d="M12 14.5c0-3.1 3-5.2 6.3-5.2-.3 3.1-3.2 5.2-6.3 5.2z"/>'),
  "Signals": icon('<circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>'
    + '<path d="M8.5 8.5a5 5 0 0 1 7 0"/><path d="M5.5 5.5a9 9 0 0 1 13 0"/>'),
  "Watchlist": icon('<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
    + '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>'),
  "Feedback Log": icon('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/>'
    + '<path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>'),
  "System Architecture": icon('<circle cx="12" cy="12" r="3"/>'
    + '<path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>'),
  "Data Freshness": icon('<path d="M21 12a9 9 0 1 1-3.5-7.1"/><polyline points="21 3 21 9 15 9"/>'),
  "Live Portfolio": icon('<rect x="2" y="7" width="20" height="14" rx="2"/>'
    + '<path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>'),
  "Trade Journal": icon('<path d="M4 4h13l3 3v13H4z"/><path d="M8 9h9M8 13h9M8 17h5"/>'),
  "Setups": icon('<rect x="3" y="4" width="7" height="7" rx="1"/>'
    + '<rect x="14" y="4" width="7" height="7" rx="1"/><rect x="3" y="15" width="7" height="5" rx="1"/>'
    + '<path d="M17.5 15v5M15 17.5h5"/>'),
};

const COUNTRIES = %%NAV_JSON%%;
let country = COUNTRIES[0];
let currentUrl = null;
let activeTop = null;                 // top-level sidebar label currently open
let pins = JSON.parse(localStorage.getItem("hub-pins") || "[]");

// Flat lookup of every top-level entry (leaf or group), current country plus
// the synthetic Portfolio entry, so pinning and restoring do not care which
// section something came from.
function topLevelItems() {
  return country.panels
    .concat(country.signalsPanels || [])
    .concat(country.algorithmsPanels || [])
    .concat([portfolioItem, journalItem, setupsItem])
    .concat(country.systemPanels || []);
}

// Nav rows are rebuilt with innerHTML on pretty much every interaction (a pin
// toggling, a group opening), which wipes any .active/.open class set on the
// old elements. Both are therefore reapplied from state after every render
// rather than being something a click handler sets once.
function highlightActive() {
  document.querySelectorAll("nav a[data-top]")
    .forEach(a => a.classList.toggle("active", a.dataset.top === activeTop));
  document.querySelectorAll(".sub-nav")
    .forEach(el => el.classList.toggle("open", el.dataset.top === activeTop));
}

const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

// ---------- workspace tabs ----------
// Each tab is one open page with its own back/forward history. Its frame stays
// alive while another tab is in front, so switching tabs never reloads a page or
// loses its scroll position. A grouped page (Indices, Sector & Industry) shows its
// members as horizontal tabs under the title instead of stacking them down a page.
const WS = {tabs: [], cur: null, uid: 1};
const curTab = () => WS.tabs.find(t => t.id === WS.cur);
function itemsOf(c) {
  return c.panels.concat(c.signalsPanels || []).concat(c.algorithmsPanels || [])
    .concat([portfolioItem, journalItem, setupsItem]).concat(c.systemPanels || []);
}
function itemFor(code, label) {
  const c = COUNTRIES.find(x => x.code === code) || COUNTRIES[0];
  return itemsOf(c).find(p => p.label === label);
}
function urlsOf(item) { return item.children ? item.children.map(c => c.url) : [item.url]; }
function saveWS() {
  try {
    localStorage.setItem("hub-ws", JSON.stringify({cur: WS.tabs.findIndex(t => t.id === WS.cur),
      tabs: WS.tabs.map(t => ({code: t.code, top: t.top, sub: t.sub}))}));
  } catch (e) { /* storage blocked: tabs simply do not survive a restart */ }
}
function newTab(code, top, sub) {
  const t = {id: WS.uid++, code: code, top: top, sub: sub || 0, hist: [{code: code, top: top, sub: sub || 0}], hi: 0, frames: {}};
  WS.tabs.push(t); WS.cur = t.id; return t;
}
// to: {code, top, sub}; opt: {force: reload the frame, replace: do not add a history entry}
function navTo(to, opt) {
  const t = curTab(); opt = opt || {};
  const same = t.code === to.code && t.top === to.top && (t.sub || 0) === (to.sub || 0);
  t.code = to.code; t.top = to.top; t.sub = to.sub || 0;
  if (!opt.replace && !same) {
    t.hist = t.hist.slice(0, t.hi + 1); t.hist.push({code: t.code, top: t.top, sub: t.sub}); t.hi = t.hist.length - 1;
  }
  showTab(opt.force);
}
function mkFrame(t, url) {
  const f = document.createElement("iframe");
  f.className = "pframe"; f.title = "panel"; f.dataset.tab = t.id; f.src = url;
  document.getElementById("frame-wrap").appendChild(f); t.frames[url] = f; return f;
}
// Every published panel carries its own US/IN quick-links, so a frame can navigate
// on its own without the hub knowing. Re-assigning the same src string would then be
// a silent no-op in every browser and the hub's chrome would claim a page that is not
// on screen. location.replace() always performs a real navigation, so an explicit
// click in the sidebar is authoritative.
function reloadFrame(f, url) {
  try { f.contentWindow.location.replace(url); } catch (e) { f.src = url; }
}
function showTab(force) {
  const t = curTab(); if (!t) return;
  country = COUNTRIES.find(c => c.code === t.code) || country;
  let item = itemFor(t.code, t.top);
  if (!item) { item = country.panels[0]; t.top = item.label; t.sub = 0; }
  const kids = item.children || null;
  if (kids) t.sub = Math.max(0, Math.min(t.sub || 0, kids.length - 1));
  const keep = urlsOf(item), url = kids ? kids[t.sub].url : item.url;
  Object.keys(t.frames).forEach(u => { if (keep.indexOf(u) < 0) { t.frames[u].remove(); delete t.frames[u]; } });
  document.querySelectorAll("#frame-wrap iframe").forEach(f => { f.style.display = "none"; });
  let f = t.frames[url];
  if (!f) f = mkFrame(t, url); else if (force) reloadFrame(f, url);
  f.style.display = "block";
  currentUrl = url; activeTop = t.top;
  const pt = document.getElementById("ptabs");
  if (kids) {
    pt.hidden = false;
    pt.innerHTML = kids.map((c, i) => '<button class="ptab' + (i === t.sub ? " on" : "") + '" data-i="' + i + '" title="' + esc(c.note || "") + '">' + esc(c.label) + '</button>').join("");
    pt.querySelectorAll(".ptab").forEach(b => b.onclick = () => navTo({code: t.code, top: t.top, sub: +b.dataset.i}));
  } else { pt.hidden = true; pt.innerHTML = ""; }
  const sub = kids ? " &rsaquo; " + esc(kids[t.sub].label) : "";
  document.getElementById("crumb").innerHTML = item.scoped === false ? "<b>" + esc(item.label) + "</b>"
    : "<b>" + country.flag + " " + country.label + "</b> &nbsp;/&nbsp; " + esc(item.label) + sub;
  document.getElementById("back-btn").disabled = t.hi <= 0;
  document.getElementById("fwd-btn").disabled = t.hi >= t.hist.length - 1;
  renderWsTabs(); renderCountrySwitch(); refreshNav(); saveWS();
}
function tabTitle(t) {
  const it = itemFor(t.code, t.top); if (!it) return t.top;
  return t.top + (it.children ? " · " + it.children[Math.min(t.sub || 0, it.children.length - 1)].label : "");
}
function renderWsTabs() {
  const el = document.getElementById("wstabs");
  el.innerHTML = WS.tabs.map(t => '<div class="wtab' + (t.id === WS.cur ? " on" : "") + '" data-id="' + t.id + '"><span class="tt">'
    + esc(tabTitle(t)) + '</span><span class="x" data-x="' + t.id + '" title="Close tab">&times;</span></div>').join("")
    + '<button id="newtab" title="New tab">+</button>';
  el.querySelectorAll(".wtab").forEach(w => w.onclick = ev => {
    if (ev.target.dataset.x) { closeTab(+ev.target.dataset.x); return; }
    WS.cur = +w.dataset.id; showTab();
  });
  document.getElementById("newtab").onclick = ev => { ev.stopPropagation(); tabPicker(ev.currentTarget); };
}
function closeTab(id) {
  const i = WS.tabs.findIndex(t => t.id === id), t = WS.tabs[i];
  Object.keys(t.frames).forEach(u => t.frames[u].remove());
  WS.tabs.splice(i, 1);
  if (!WS.tabs.length) newTab(COUNTRIES[0].code, COUNTRIES[0].panels[0].label);
  else if (WS.cur === id) WS.cur = WS.tabs[Math.min(i, WS.tabs.length - 1)].id;
  showTab();
}
function tabPicker(anchor) {
  const r = anchor.getBoundingClientRect(), pop = document.getElementById("pop");
  pop.innerHTML = '<div class="menu" style="left:' + Math.max(8, Math.min(r.left, innerWidth - 280)) + 'px;top:' + (r.bottom + 4) + 'px"><b>Open in a new tab</b>'
    + itemsOf(country).map(p => '<a data-l="' + esc(p.label) + '">' + esc(p.label) + '</a>').join("") + '</div>';
  pop.querySelectorAll("a").forEach(a => a.onclick = () => { pop.innerHTML = ""; newTab(country.code, a.dataset.l); showTab(true); });
}
document.addEventListener("click", e => {
  if (!e.target.closest("#pop .menu") && !e.target.closest("#newtab")) { const p = document.getElementById("pop"); if (p) p.innerHTML = ""; }
});
// Sidebar click: change this tab's page; Ctrl/Cmd-click or middle-click opens it in a new tab.
function openItem(item, inNewTab) {
  if (inNewTab) { newTab(country.code, item.label); showTab(true); }
  else navTo({code: country.code, top: item.label, sub: 0}, {force: true});
}
function histStep(d) {
  const t = curTab(), j = t.hi + d; if (j < 0 || j >= t.hist.length) return;
  t.hi = j; const h = t.hist[j]; t.code = h.code; t.top = h.top; t.sub = h.sub; showTab();
}

function pinButton(item) {
  const pinned = pins.includes(item.label);
  return '<button class="pin-btn' + (pinned ? ' pinned' : '') + '" data-pin="'
    + item.label + '" title="' + (pinned ? "Unpin" : "Pin to top")
    + '">' + (pinned ? "★" : "☆") + '</button>';
}

function refreshNav() {
  renderPanelNav(); renderSignalsNav(); renderAlgorithmsNav();
  renderPortfolioNav(); renderSystemNav(); renderPinned();
}

function wireItem(a, item) {
  a.querySelector(".pin-btn").onclick = (e) => {
    e.stopPropagation();
    pins = pins.includes(item.label)
      ? pins.filter(l => l !== item.label) : pins.concat([item.label]);
    localStorage.setItem("hub-pins", JSON.stringify(pins));
    refreshNav();
  };
  a.onclick = (e) => {
    if (e.target.closest(".pin-btn")) return;
    openItem(item, e.ctrlKey || e.metaKey);
  };
  a.onauxclick = (e) => {
    if (e.button !== 1 || e.target.closest(".pin-btn")) return;
    e.preventDefault(); openItem(item, true);
  };
}

function itemHTML(item) {
  const badge = item.label === "Trade Journal"
    ? '<span class="nav-badge" id="tj-badge" hidden>0</span>' : "";
  let html = '<div class="nav-item"><a data-top="' + item.label + '">'
    + (ICONS[item.label] || "") + '<span class="nav-label">' + item.label + '</span>' + badge
    + pinButton(item) + '</a>';
  return html + '</div>';
}

function renderGroup(nav, items) {
  nav.innerHTML = items.map(itemHTML).join("");
  [...nav.children].forEach((wrap, i) => {
    wireItem(wrap.querySelector(":scope > a"), items[i]);
  });
  highlightActive();
}

function renderPinned() {
  const items = topLevelItems().filter(i => pins.includes(i.label));
  document.getElementById("pinned-section").hidden = items.length === 0;
  renderGroup(document.getElementById("pinned-nav"), items);
}

function renderPortfolioNav() {
  renderGroup(document.getElementById("portfolio-nav"), [portfolioItem, journalItem, setupsItem]);
}

function renderSystemNav() {
  renderGroup(document.getElementById("system-nav"), country.systemPanels || []);
}

function renderPanelNav() {
  renderGroup(document.getElementById("panel-nav"), country.panels);
}

function renderSignalsNav() {
  renderGroup(document.getElementById("signals-nav"), country.signalsPanels || []);
}

function renderAlgorithmsNav() {
  renderGroup(document.getElementById("algorithms-nav"), country.algorithmsPanels || []);
}

function renderCountrySwitch() {
  document.getElementById("country-switch").innerHTML = COUNTRIES.map(c =>
    '<button class="' + (c.code === country.code ? 'active' : '') + '" data-c="'
    + c.code + '">' + c.flag + ' ' + c.label + '</button>').join("");
  document.querySelectorAll("#country-switch button").forEach(b =>
    b.onclick = () => {
      // Stay on whatever page this tab has open (Money Flows -> Money Flows); fall back
      // to the first panel only when that page does not exist in the other market.
      const c = COUNTRIES.find(x => x.code === b.dataset.c), wanted = curTab().top;
      navTo({code: c.code, top: itemFor(c.code, wanted) ? wanted : c.panels[0].label, sub: 0}, {force: true});
    });
}

const portfolioItem = {label: "Live Portfolio", url: "/portfolio", scoped: false};
const journalItem = {label: "Trade Journal", url: "/journal", scoped: false};
const setupsItem = {label: "Setups", url: "/setups", scoped: false};

document.getElementById("reload-btn").onclick = () => {
  const t = curTab(), f = t && t.frames[currentUrl]; if (!f) return;
  // Same no-op trap as navigation: the frame's src attribute already equals currentUrl.
  try { f.contentWindow.location.reload(); } catch (e) { f.src = currentUrl; }
};
document.getElementById("back-btn").onclick = () => histStep(-1);
document.getElementById("fwd-btn").onclick = () => histStep(1);

// The auto-sync loop deliberately refuses to overwrite local changes rather
// than risk clobbering something — but that safe failure used to be
// invisible: it would skip silently every 30 minutes with nothing shown
// anywhere, so the hub sat on stale data for days before anyone noticed.
// This surfaces that exact state on load, so a blocked sync is a banner,
// not a support ticket.
(async () => {
  try {
    const state = await (await fetch("/api/sync/status")).json();
    const origin = state.origin;
    if (origin && origin.ok === false) {
      const el = document.getElementById("sync-warning");
      el.hidden = false;
      el.textContent = "Auto-sync is blocked, so data may be stale: " + origin.message +
        '. See System → Data Freshness once the conflict is resolved.';
    }
  } catch (err) { /* status endpoint unreachable — say nothing, not worth alarming over */ }
})();

// Click a group's own label to collapse/expand it in place -- state isn't
// persisted on purpose: it's for temporarily getting a long section (Market
// Breadth) out of the way, not a standing preference to remember.
document.querySelectorAll(".group-label[data-group]").forEach((label) => {
  label.onclick = () => {
    label.classList.toggle("collapsed");
    document.getElementById(label.dataset.group).classList.toggle("collapsed");
  };
});

// Only ever shown when a group actually has something stale -- a synced
// group shows no dot at all, so the dot appearing is itself the signal
// instead of a permanent row of reassurance dots next to every label.
// Best-effort mapping from a Data Freshness funnel's own label to which
// sidebar group it belongs to; a funnel not listed here (Screener/Money
// Flows bundle spans two groups, so it's counted for both) just doesn't
// contribute to any dot rather than guessing.
const FRESHNESS_GROUP_MAP = {
  "panel-nav": ["Market Environment", "Indices", "Sector & Industry Ranks",
               "Screener / Money Flows bundle", "Breadth", "TradingView Classification",
               "Per-ticker prices"],
  "signals-nav": ["Screener / Money Flows bundle"],
  "algorithms-nav": ["TMLE Leaders"],
};
async function refreshSidebarFreshness() {
  try {
    const report = await (await fetch("/api/freshness")).json();
    const staleByGroup = {};
    Object.values(report).forEach((c) => {
      (c.rows || []).forEach((row) => {
        if (row.level !== "red") return;
        Object.entries(FRESHNESS_GROUP_MAP).forEach(([group, labels]) => {
          if (labels.includes(row.label)) staleByGroup[group] = true;
        });
      });
    });
    Object.keys(FRESHNESS_GROUP_MAP).forEach((group) => {
      const el = document.getElementById("fresh-" + group);
      if (el) el.hidden = !staleByGroup[group];
    });
  } catch (err) { /* local-hub-only endpoint -- say nothing if unreachable */ }
}
refreshSidebarFreshness();
setInterval(refreshSidebarFreshness, 60000);

// How many logged trades are still missing a chart or an entry thesis --
// the backlog from "I took/closed this, haven't written it up yet." Reuses
// the same per-page chart-presence cache the Trade Journal itself warms, so
// this is only slow the very first time it runs in a session.
async function refreshJournalBadge() {
  try {
    const r = await (await fetch("/api/journal/pending")).json();
    const el = document.getElementById("tj-badge");
    if (!el) return;
    el.hidden = !r.count;
    el.textContent = r.count || 0;
  } catch (err) { /* local-hub-only endpoint -- say nothing if unreachable */ }
}
refreshJournalBadge();
setInterval(refreshJournalBadge, 60000);

// Remembers the last panel across a restart or reload, rather than always
// dumping back to Market Environment — this is meant to stay open and be
// returned to, like a desktop app, not re-navigated from scratch each time.
renderCountrySwitch();
refreshNav();

let restored = false;
try {
  const ws = JSON.parse(localStorage.getItem("hub-ws") || "null");
  if (ws && ws.tabs && ws.tabs.length) {
    ws.tabs.forEach(s => { if (itemFor(s.code, s.top)) newTab(s.code, s.top, s.sub); });
    if (WS.tabs.length) { WS.cur = WS.tabs[Math.max(0, Math.min(ws.cur, WS.tabs.length - 1))].id; restored = true; }
  }
} catch (e) { /* bad saved state: start fresh */ }
if (!restored) {
  const last = JSON.parse(localStorage.getItem("hub-last") || "null");
  if (last && itemFor(last.country || COUNTRIES[0].code, last.top)) { newTab(last.country || COUNTRIES[0].code, last.top); restored = true; }
}
if (!WS.tabs.length) newTab(COUNTRIES[0].code, COUNTRIES[0].panels[0].label);
showTab(true);

// Native pages (Market Environment) live inside the frame and cannot navigate the hub
// themselves; they post {hubGo: "<panel label>"} and the hub opens it in the current tab.
/* Greeting under the name, and the Light / Dark / Auto switch. The choice lives in localStorage
   ("nts-theme"), which every page reads as it loads (see THEME_HEAD in the server); open pages reload on change. */
(function () {
  const g = document.getElementById("greet");
  const tick = () => {
    const d = new Date(), h = d.getHours();
    g.textContent = (h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening") + " \u00b7 "
      + d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  };
  tick(); setInterval(tick, 60000);
  const apply = t => {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
    document.querySelectorAll("#theme-switch button").forEach(b => b.classList.toggle("on", b.dataset.th === (t || "")));
  };
  let cur = ""; try { cur = localStorage.getItem("nts-theme") || ""; } catch (e) {}
  apply(cur);
  document.getElementById("theme-switch").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    const t = b.dataset.th;
    try { if (t) localStorage.setItem("nts-theme", t); else localStorage.removeItem("nts-theme"); } catch (err) {}
    apply(t);
  });
})();

window.addEventListener("message", e => {
  if (e.origin !== location.origin || !e.data || !e.data.hubGo) return;
  const item = topLevelItems().find(p => p.label === e.data.hubGo);
  if (!item) return;
  // {child: "Nasdaq 100"} opens that member of a grouped page; {open: {...}} is handed to the page once it has loaded
  const sub = item.children && e.data.child ? Math.max(0, item.children.findIndex(c => c.label === e.data.child)) : 0;
  navTo({code: country.code, top: item.label, sub: sub}, {force: true});
  if (e.data.open) {
    const t0 = curTab(), f0 = t0 && t0.frames[currentUrl];
    if (f0) {
      const send0 = () => { try { f0.contentWindow.postMessage({mbtOpen: e.data.open}, location.origin); } catch (err) {} };
      f0.addEventListener("load", send0); setTimeout(send0, 1200); setTimeout(send0, 3500);
    }
  }
  // A landing-page click can say which stretch of an index to shade; hand it to every
  // member frame of the page (they are created now, so wait for each to load).
  if (Array.isArray(e.data.focus) && item.children) {
    const t = curTab();
    item.children.forEach(c => {
      const f = t.frames[c.url] || mkFrame(t, c.url);
      f.style.display = c.url === currentUrl ? "block" : "none";
      const send = () => { try { f.contentWindow.postMessage({mbtFocus: e.data.focus}, location.origin); } catch (err) {} };
      f.addEventListener("load", send);
      setTimeout(send, 1500);
    });
  }
});
</script>
</body></html>"""


if __name__ == "__main__":
    main()
