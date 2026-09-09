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
    {"label": "Market Environment", "path": "panel-summary.html"},
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
    {"label": "Market Replay", "path": "panel-replay.html"},
    {"label": "Stock Lookup", "path": "panel-stock.html"},
]

SIGNALS_PANELS = [
    {"label": "Signals", "path": "panel-signals.html"},
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

OUTPUT_DIR = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
STOPS_FILE = os.path.join(OUTPUT_DIR, "stops.json")

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
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if f"{country}: starting pipeline" in line:
                started, finished = True, False
            elif f"{country}: pipeline done" in line or f"{country}: pipeline FAILED" in line:
                finished = True
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


def sync_from_origin(log=print):
    """Returns {"ok": bool, "message": str} — the caller persists this so a
    blocked sync (e.g. dirty working tree) is visible in the hub UI instead of
    failing silently forever. This exact silent failure is what let the hub
    sit stale for days once: a local test run left uncommitted files in the
    way, the pull skipped every 30 minutes with nothing surfaced, and nobody
    noticed until the dates were badly behind."""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            cwd=config.ROOT_DIR, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            msg = result.stdout.strip() or "already up to date"
            log(f"  git sync: {msg}")
            return {"ok": True, "message": msg}
        # Diverged history or local edits in the way. Never forced past
        # this — staying stale is the safe failure, overwriting isn't.
        reason = result.stderr.strip()[:200] or "not a fast-forward"
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
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            page = HUB_PAGE.replace("%%NAV_JSON%%", _hub_nav_json())
            self._send(200, page, "text/html; charset=utf-8")
            return
        if route.path == "/portfolio":
            self._send(200, PORTFOLIO_PAGE, "text/html; charset=utf-8")
            return
        if route.path == "/journal":
            self._send(200, JOURNAL_PAGE, "text/html; charset=utf-8")
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
                self._send(200, json.dumps(notion_sync.fetch_trades(log=log)), "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
            return
        if route.path == "/api/journal/schema":
            try:
                import notion_sync
                self._send(200, json.dumps(notion_sync.fetch_database_schema(notion_sync.TRADES_DB)),
                          "application/json")
            except Exception as error:
                self._send(500, json.dumps({"error": str(error)}), "application/json")
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root { --bg:#f5f6f8; --panel:#fff; --text:#1a1d24; --dim:#6b7280;
    --line:#e2e5ea; --up:#16a34a; --down:#dc2626; --warn:#ca8a04; --violet:#7c3aed;
    --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15; --violet:#a78bfa;
      --accent:#60a5fa; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-size:14px;
    font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-variant-numeric:tabular-nums; }
  main { padding:18px; max-width:1300px; margin:0 auto; }
  h1 { font-size:17px; margin:0 0 4px; }
  .sub { font-size:12px; color:var(--dim); margin:0 0 16px; max-width:680px; line-height:1.5; }

  .kpi-strip{display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:16px;}
  @media (max-width:900px){.kpi-strip{grid-template-columns:repeat(3,1fr);}}
  .kpi-tile{background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:11px 13px;}
  .kpi-k{font-size:10px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim); margin-bottom:4px;}
  .kpi-v{font-size:18px; font-weight:800;}
  .kpi-v.up{color:var(--up);} .kpi-v.down{color:var(--down);}

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
    padding:8px 10px; border-bottom:1px solid var(--line); font-weight:600; cursor:pointer;}
  td{padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:middle;}
  tr:last-child td{border-bottom:none;}
  tr.clickable{cursor:pointer;} tr.clickable:hover{background:color-mix(in srgb, var(--accent) 6%, transparent);}
  .sym{font-weight:700;}
  .acct{font-size:10px; font-weight:700; padding:2px 6px; border-radius:4px; background:var(--line); color:var(--dim);}
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
  .detail-page{background:var(--panel); border:1px solid var(--line); border-radius:14px; max-width:620px; width:100%;}
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
</style></head><body>
<main>
  <h1>Trade Journal</h1>
  <p class="sub">Reads your Notion trade log directly — the full record, open and closed. Editing a chip or the thesis writes straight back to that same Notion page. Chart screenshots still open in Notion itself (Notion's paste is already the best tool for that); everything else edits right here.</p>
  <div id="load-err" hidden></div>

  <div class="kpi-strip">
    <div class="kpi-tile"><div class="kpi-k">Open positions</div><div class="kpi-v" id="kpi-open">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Win rate (closed)</div><div class="kpi-v" id="kpi-winrate">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Avg gain</div><div class="kpi-v up" id="kpi-avggain">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Avg loss</div><div class="kpi-v down" id="kpi-avgloss">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">Total logged</div><div class="kpi-v" id="kpi-total">&mdash;</div></div>
    <div class="kpi-tile"><div class="kpi-k">No entry setup</div><div class="kpi-v" id="kpi-nosetup" style="color:var(--warn)">&mdash;</div></div>
  </div>

  <div class="filter-pills" id="filter-pills">
    <button class="filter-pill active" data-filter="all">All</button>
    <button class="filter-pill" data-filter="open">Open</button>
    <button class="filter-pill" data-filter="closed">Closed</button>
    <button class="filter-pill gap" data-filter="nosetup">&#9888; No entry setup</button>
    <button class="filter-pill gap" data-filter="noexit">&#9888; Closed, no exit setup</button>
  </div>
  <input id="search" placeholder="Filter by ticker&hellip;">

  <table>
    <thead><tr><th>Ticker</th><th>Account</th><th>Opened</th><th>Setup</th><th>P&amp;L%</th><th>Completeness</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" hidden>No trades match this filter.</div>
</main>

<div class="overlay" id="overlay" hidden>
  <div class="detail-page">
    <div class="detail-head"><b id="d-ticker"></b><button class="detail-close" id="d-close">&times;</button></div>
    <div class="detail-body">
      <div class="detail-grid">
        <div><div class="k">Account</div><div class="v" id="d-account"></div></div>
        <div><div class="k">Date Opened / Closed</div><div class="v" id="d-dates"></div></div>
        <div><div class="k">Entry / Exit Price</div><div class="v" id="d-prices"></div></div>
        <div><div class="k">Shares</div><div class="v" id="d-shares"></div></div>
        <div><div class="k">Cost Value <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-cost"></div></div>
        <div><div class="k">Initial Stop $ / % <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-stop"></div></div>
        <div><div class="k">PnL% / Hold Days <i style="color:var(--violet);font-style:normal;font-size:10px">&fnof;</i></div><div class="v" id="d-pnl"></div></div>
        <div><a class="notion-link" id="d-notion-link" href="#" target="_blank" rel="noopener">Open / edit chart in Notion &rarr;</a></div>
      </div>

      <div class="field-label">Entry Setup</div>
      <select class="edit-select" id="d-entrysetup"></select>
      <div class="field-label">Exit Setup</div>
      <select class="edit-select" id="d-exitsetup"></select>
      <div class="field-label">Buy Quality</div>
      <select class="edit-select" id="d-buyquality"></select>
      <div class="field-label">Sell Quality</div>
      <select class="edit-select" id="d-sellquality"></select>
      <div class="field-label">Entry Thesis</div>
      <textarea class="edit-text" id="d-thesis"></textarea>
      <div class="save-status" id="d-save-status"></div>
    </div>
  </div>
</div>

<script>
let ALL_TRADES = [];
let SCHEMA = {};
let activeFilter = "all";
let searchTerm = "";

function fmtDate(d) { return d || "&mdash;"; }
function fmtMoney(n) { return n == null ? "&mdash;" : "$" + Number(n).toLocaleString(undefined, {maximumFractionDigits: 2}); }
function fmtPct(n) { return n == null ? "&mdash;" : (n >= 0 ? "+" : "") + (Number(n) * 100).toFixed(1) + "%"; }

function computeKpis(trades) {
  const open = trades.filter(t => !t.dateClosed);
  const closed = trades.filter(t => t.dateClosed);
  const withPnl = closed.filter(t => t.pnlPct != null);
  const wins = withPnl.filter(t => t.pnlPct > 0);
  const losses = withPnl.filter(t => t.pnlPct <= 0);
  const avg = (arr) => arr.length ? arr.reduce((s, t) => s + t.pnlPct, 0) / arr.length : null;
  document.getElementById("kpi-open").textContent = open.length;
  document.getElementById("kpi-winrate").textContent = withPnl.length
    ? Math.round(wins.length / withPnl.length * 100) + "%" : "&mdash;";
  document.getElementById("kpi-avggain").innerHTML = fmtPct(avg(wins));
  document.getElementById("kpi-avgloss").innerHTML = fmtPct(avg(losses));
  document.getElementById("kpi-total").textContent = trades.length;
  document.getElementById("kpi-nosetup").textContent = trades.filter(t => !t.entrySetup).length;
}

function passesFilter(t) {
  if (searchTerm && !(t.ticker || "").toLowerCase().includes(searchTerm)) return false;
  if (activeFilter === "open") return !t.dateClosed;
  if (activeFilter === "closed") return !!t.dateClosed;
  if (activeFilter === "nosetup") return !t.entrySetup;
  if (activeFilter === "noexit") return !!t.dateClosed && !t.exitSetup;
  return true;
}

function completenessBadge(t) {
  if (!t.entrySetup) return '<span class="gap-badge">no entry setup</span>';
  if (t.dateClosed && !t.exitSetup) return '<span class="gap-badge">no exit setup</span>';
  return '<span class="ok-badge">&#10003; complete</span>';
}

function draw() {
  const rows = ALL_TRADES.filter(passesFilter);
  const tbody = document.getElementById("rows");
  document.getElementById("empty").hidden = rows.length > 0;
  tbody.innerHTML = rows.map((t, i) => {
    const idx = ALL_TRADES.indexOf(t);
    const pnlCls = t.pnlPct == null ? "" : (t.pnlPct >= 0 ? "up" : "down");
    return '<tr class="clickable" data-idx="' + idx + '">'
      + '<td class="sym">' + (t.ticker || "&mdash;") + '</td>'
      + '<td><span class="acct">' + t.account + '</span></td>'
      + '<td>' + fmtDate(t.dateOpened) + '</td>'
      + '<td>' + (t.entrySetup ? '<span class="setup-chip">' + t.entrySetup + '</span>' : '<span style="color:var(--dim);font-size:11.5px">&mdash;</span>') + '</td>'
      + '<td class="pnl ' + pnlCls + '">' + fmtPct(t.pnlPct) + '</td>'
      + '<td>' + completenessBadge(t) + '</td>'
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
  document.getElementById("d-ticker").textContent = t.ticker;
  document.getElementById("d-account").textContent = t.account;
  document.getElementById("d-dates").innerHTML = fmtDate(t.dateOpened) + " &rarr; " + fmtDate(t.dateClosed);
  document.getElementById("d-prices").innerHTML = fmtMoney(t.entryPrice) + " &rarr; " + fmtMoney(t.exitPrice);
  document.getElementById("d-shares").innerHTML = t.shares ?? "&mdash;";
  document.getElementById("d-cost").innerHTML = fmtMoney(t.costValue);
  document.getElementById("d-stop").innerHTML = fmtMoney(t.initialStop) + " / " + fmtPct(t.initialStopPct);
  document.getElementById("d-pnl").innerHTML = fmtPct(t.pnlPct) + " / " + (t.holdDays ?? "&mdash;") + "d";
  document.getElementById("d-notion-link").href = t.notionUrl || "#";
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

document.getElementById("d-entrysetup").addEventListener("change", e => saveField("Entry Setup", e.target.value, "entrySetup"));
document.getElementById("d-exitsetup").addEventListener("change", e => saveField("Exit Setup", e.target.value, "exitSetup"));
document.getElementById("d-buyquality").addEventListener("change", e => saveField("Buy Quality", e.target.value, "buyQuality"));
document.getElementById("d-sellquality").addEventListener("change", e => saveField("Sell Quality", e.target.value, "sellQuality"));
document.getElementById("d-thesis").addEventListener("blur", e => saveField("Entry Thesis", e.target.value, "entryThesis"));
document.getElementById("d-close").addEventListener("click", () => document.getElementById("overlay").hidden = true);
document.getElementById("overlay").addEventListener("click", e => { if (e.target.id === "overlay") e.target.hidden = true; });

document.querySelectorAll(".filter-pill").forEach(p => p.addEventListener("click", () => {
  document.querySelectorAll(".filter-pill").forEach(x => x.classList.remove("active"));
  p.classList.add("active");
  activeFilter = p.dataset.filter;
  draw();
}));
document.getElementById("search").addEventListener("input", e => { searchTerm = e.target.value.trim().toLowerCase(); draw(); });

Promise.all([
  fetch("/api/journal/trades").then(r => r.json()),
  fetch("/api/journal/schema").then(r => r.json()),
]).then(([trades, schema]) => {
  if (trades && trades.error) throw new Error(trades.error);
  ALL_TRADES = trades;
  SCHEMA = schema;
  computeKpis(trades);
  draw();
}).catch(err => {
  const el = document.getElementById("load-err");
  el.hidden = false;
  el.textContent = "Could not load the trade journal: " + err.message;
});
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
    :root { --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
      --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15;
      --accent:#60a5fa; }
  }
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
    def indices_group(cfg, prefix):
        labels = cfg.get("index_labels", {})
        children = [
            {"label": labels.get(key, key.title()),
             "url": prefix + f"panel-index-{key}.html",
             "note": ""}
            for key in cfg.get("index_tickers", {})
        ]
        return {"label": "Indices", "children": children}

    def resolve(entry, cfg, prefix):
        if entry.get("dynamic") == "indices":
            return indices_group(cfg, prefix)
        if "children" in entry:
            return {"label": entry["label"], "children": [
                {"label": c["label"], "url": prefix + c["path"],
                 "note": c.get("note", "")} for c in entry["children"]]}
        return {"label": entry["label"], "url": prefix + entry["path"]}

    countries = []
    for code, cfg in config.COUNTRIES.items():
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


HUB_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading System</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%233b82f6'/%3E%3Cpath d='M6 16l4-6 3 4 5-8' stroke='white' stroke-width='2' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root { --bg:#0d0f14; --panel:#171b24; --line:#262b36; --text:#e7e9ee;
    --dim:#9096a3; --accent:#3b82f6; --accent-dim:#1d4ed8;
    --mb:#5b9dff; --sig:#e0a94e; --algo:#a78bfa; --port:#4ade80; --sys:#a1a1aa;
    --icon-glow:rgba(91,157,255,.5);
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f5f6f8; --panel:#fff; --line:#e2e5ea; --text:#1a1d24;
      --dim:#6b7280; --accent:#2563eb; --accent-dim:#dbeafe;
      --mb:#2563eb; --sig:#b45309; --algo:#7c3aed; --port:#15803d; --sys:#52525b;
      --icon-glow:rgba(37,99,235,.45);
    }
  }
  * { box-sizing:border-box; }
  html, body { height:100%; margin:0; overflow:hidden; }
  body { display:flex; background:var(--bg); color:var(--text);
    font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px; }

  #sidebar { width:230px; flex:none; background:var(--panel);
    border-right:1px solid var(--line); display:flex; flex-direction:column;
    overflow-y:auto; }
  #brand { padding:16px 16px 10px; font-weight:700; font-size:15px;
    display:flex; align-items:center; gap:8px; }
  #brand .dot { width:8px; height:8px; border-radius:50%; background:var(--accent); }

  #country-switch { display:flex; gap:5px; padding:0 12px 12px; }
  #country-switch button { flex:1; background:var(--bg); border:1px solid var(--line);
    color:var(--dim); border-radius:7px; padding:6px 4px; font-size:12px;
    font-family:inherit; cursor:pointer; }
  #country-switch button.active { background:var(--accent); border-color:var(--accent);
    color:#fff; }

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
</style></head><body>

<div id="sidebar">
  <div id="brand"><span class="dot"></span> Trading System</div>
  <div id="country-switch"></div>
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
  <div id="topbar">
    <div id="crumb"></div>
    <button id="reload-btn" title="Reload the current panel">Reload</button>
  </div>
  <div id="frame-wrap"><iframe id="frame" title="panel"></iframe></div>
  <div id="stack-wrap" hidden></div>
</div>

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
  "Screener": icon('<path d="M3 5h18l-7 8v6l-4 2v-8z"/>'
    + '<circle cx="12" cy="2.2" r="1.1" fill="currentColor" stroke="none"/>'),
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
    .concat([portfolioItem, journalItem])
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

function setChrome(url, label, scoped, topLabel) {
  currentUrl = url;
  activeTop = topLabel != null ? topLabel : label;
  // Portfolio and grouped panels are not scoped by the US/IN switch the same
  // way a plain leaf is, so the crumb only claims a country when it is one.
  document.getElementById("crumb").innerHTML = scoped === false ? "<b>" + label + "</b>"
    : "<b>" + country.flag + " " + country.label + "</b> &nbsp;/&nbsp; " + label;
  highlightActive();
  localStorage.setItem("hub-last",
    JSON.stringify({top: activeTop, country: country.code}));
}

function showLeaf() {
  document.getElementById("frame-wrap").hidden = false;
  document.getElementById("stack-wrap").hidden = true;
}

// Every published panel carries its own US/IN quick-links (plain <a href>,
// so they work in Notion with no hub around them) — clicking one navigates
// the iframe internally, which the hub never learns about. If the sidebar is
// then clicked back to a panel whose URL string happens to be exactly what
// the iframe's src ATTRIBUTE already says (unchanged since the hub last set
// it), assigning that same string again is a no-op in every browser: the
// frame silently keeps showing whatever it navigated to on its own, while the
// hub's own chrome — crumb, sidebar highlight — confidently shows the
// panel it THINKS is loaded. That mismatch is exactly what showed up as "US"
// in the sidebar with India's data on screen.
//
// contentWindow.location.replace() has no such fast path: it always performs
// a real navigation to the given URL regardless of the frame's current
// location, so the hub's click is authoritative every time. It only fails
// (cross-origin, or before the first document has loaded), in which case
// setting .src is correct anyway since there is nothing stale to override.
function loadFrame(url) {
  const frame = document.getElementById("frame");
  try {
    frame.contentWindow.location.replace(url);
  } catch (e) {
    frame.src = url;
  }
}

function go(url, label, scoped, topLabel) {
  showLeaf();
  loadFrame(url);
  setChrome(url, label, scoped, topLabel);
}

// A grouped row's members render together on one scrollable page rather than
// behind a toggle — the sidebar's expanded sub-list already shows what is in
// the group, so switching to a horizontal tab bar on top of that would just
// be the same information said twice.
function openGroup(item) {
  document.getElementById("frame-wrap").hidden = true;
  const stack = document.getElementById("stack-wrap");
  stack.hidden = false;
  stack.innerHTML = item.children.map(c =>
    '<div class="stack-block" id="' + stackId(c.url) + '">'
    + '<div class="stack-head"><b>' + c.label + '</b>'
    + (c.note ? '<span>' + c.note + '</span>' : '') + '</div>'
    + '<iframe class="stack-frame" src="' + c.url + '" title="' + c.label + '"></iframe>'
    + '</div>').join("");
  setChrome(item.children[0].url, item.label, undefined, item.label);
}

function stackId(url) {
  return "stack-" + url.replace(/[^a-zA-Z0-9]/g, "-");
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
    if (item.children) openGroup(item);
    else go(item.url, item.label, item.scoped, item.label);
    refreshNav();
  };
}

// Clicking a member's name in the sidebar's expanded sub-list scrolls that
// member's block into view rather than navigating anywhere — everything in
// the group is already on the page, so this is wayfinding, not routing.
function wireSubNav(wrap, item) {
  wrap.querySelectorAll(".sub-link").forEach(a => a.onclick = (e) => {
    e.stopPropagation();
    const target = document.getElementById(stackId(a.dataset.url));
    if (target) target.scrollIntoView({behavior: "auto", block: "start"});
  });
}

function itemHTML(item) {
  let html = '<div class="nav-item"><a data-top="' + item.label + '">'
    + (ICONS[item.label] || "") + '<span class="nav-label">' + item.label + '</span>'
    + pinButton(item) + '</a>';
  if (item.children) {
    html += '<div class="sub-nav" data-top="' + item.label + '">' + item.children.map(c =>
      '<a class="sub-link" data-url="' + c.url + '"><span class="sub-dot"></span>'
      + '<span class="nav-label">' + c.label
      + (c.note ? '<span class="sub-note">' + c.note + '</span>' : '')
      + '</span></a>').join("") + '</div>';
  }
  return html + '</div>';
}

function renderGroup(nav, items) {
  nav.innerHTML = items.map(itemHTML).join("");
  [...nav.children].forEach((wrap, i) => {
    wireItem(wrap.querySelector(":scope > a"), items[i]);
    if (items[i].children) wireSubNav(wrap, items[i]);
  });
  highlightActive();
}

function renderPinned() {
  const items = topLevelItems().filter(i => pins.includes(i.label));
  document.getElementById("pinned-section").hidden = items.length === 0;
  renderGroup(document.getElementById("pinned-nav"), items);
}

function renderPortfolioNav() {
  renderGroup(document.getElementById("portfolio-nav"), [portfolioItem, journalItem]);
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
      // Stay on whatever tab was open (Money Flows -> Money Flows, not back
      // to Market Environment) -- activeTop is set by setChrome() on every
      // navigation, so it already names exactly the row to look up again in
      // the country being switched to. Falls back to the first panel only
      // for the rare case that row doesn't exist there (e.g. TMLE, US-only).
      const wantedLabel = activeTop;
      country = COUNTRIES.find(c => c.code === b.dataset.c);
      renderCountrySwitch(); refreshNav();
      const match = topLevelItems().find(p => p.label === wantedLabel);
      const target = match || country.panels[0];
      if (target.children) openGroup(target);
      else go(target.url, target.label, target.scoped, target.label);
      refreshNav();
    });
}

const portfolioItem = {label: "Live Portfolio", url: "/portfolio", scoped: false};
const journalItem = {label: "Trade Journal", url: "/journal", scoped: false};

document.getElementById("reload-btn").onclick = () => {
  if (!document.getElementById("stack-wrap").hidden) {
    document.querySelectorAll(".stack-frame").forEach(f => {
      try { f.contentWindow.location.reload(); } catch (e) { f.src = f.src; }
    });
  } else if (currentUrl) {
    // Same no-op trap as go(): the frame's src attribute already equals
    // currentUrl, so setting it again does nothing in any browser.
    const frame = document.getElementById("frame");
    try { frame.contentWindow.location.reload(); }
    catch (e) { frame.src = currentUrl; }
  }
};

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

// Remembers the last panel across a restart or reload, rather than always
// dumping back to Market Environment — this is meant to stay open and be
// returned to, like a desktop app, not re-navigated from scratch each time.
renderCountrySwitch();
refreshNav();

const last = JSON.parse(localStorage.getItem("hub-last") || "null");
let restored = false;
if (last) {
  if (last.top === "Live Portfolio") {
    go("/portfolio", "Live Portfolio", false); restored = true;
  } else if (last.top === "Trade Journal") {
    go("/journal", "Trade Journal", false); restored = true;
  } else if (COUNTRIES.some(c => c.code === last.country)) {
    country = COUNTRIES.find(c => c.code === last.country);
    renderCountrySwitch(); refreshNav();
    const item = country.panels.find(p => p.label === last.top);
    if (item) {
      if (item.children) openGroup(item);
      else go(item.url, item.label, undefined, item.label);
      restored = true;
    }
  }
}
if (!restored) {
  const first = country.panels[0];
  if (first.children) openGroup(first);
  else go(first.url, first.label, undefined, first.label);
}
refreshNav();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
