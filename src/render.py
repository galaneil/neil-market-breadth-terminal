"""
render.py — assembles data/<country>/*.jsonl history into HTML pages via Jinja2.

Two kinds of output, per country:
  - index.html: the full combined dashboard (all panels on one page).
  - panel-*.html: one standalone page per individual panel (each index,
    sectors, industries, each breadth metric), for embedding separately
    (e.g. one Notion /embed block per panel instead of one long scrolling
    embed of the whole dashboard). Each panel page only embeds its own
    slice of data, so it stays lightweight even though the combined page
    is large (a full year of sector/industry history for every group).

Both share the same docs/dashboard.css and docs/dashboard.js — dashboard.js
reads data-keys attributes off each panel's container element and only
wires up what's actually present on the page, so one script serves every
page variant.

Country layout: the US renders to docs/ and India to docs/in/. That asymmetry
is deliberate (see config.py) — the US embed URLs are already live in Notion.
India's pages therefore need `asset_prefix` of "../" to reach the shared CSS/JS
at the docs root, while the US pages use "".
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader

import config
import flags
import store

# Series that exist for every country under the same filename. Index series are
# added per country, since the keys differ (nasdaq/sp500/russell2000 vs
# sensex/nifty500/...).
COMMON_SERIES_FILES = {
    "environment": "environment.jsonl",
    "sector_ranks": "sector_ranks.jsonl",
    "industry_ranks": "industry_ranks.jsonl",
    "breadth_adv_decl": "breadth_adv_decl.jsonl",
    "breadth_new_hilo": "breadth_new_hilo.jsonl",
    # Same file, second view. The net figure answers "which side is winning";
    # it cannot tell 200-vs-190 from 12-vs-2, which are opposite markets with
    # the same net of 10. This key carries the two raw counts so both are
    # readable on their own.
    "breadth_hilo_counts": "breadth_new_hilo.jsonl",
    "breadth_pct_up20": "breadth_pct_up20.jsonl",
    "breadth_pct_up30": "breadth_pct_up30.jsonl",
    "breadth_pct_down20": "breadth_pct_down20.jsonl",
    "breadth_pct_down30": "breadth_pct_down30.jsonl",
}

BREADTH_LABELS = {
    "breadth_adv_decl": "Net Advancers − Decliners",
    "breadth_new_hilo": "Net New Highs − New Lows",
    "breadth_hilo_counts": "52-Week Highs & Lows",
    "breadth_pct_up20": "% Up 20%+ (5D)",
    "breadth_pct_up30": "% Up 30%+ (5D)",
    "breadth_pct_down20": "% Down 20%+ (5D)",
    "breadth_pct_down30": "% Down 30%+ (5D)",
}

_ENV = Environment(loader=FileSystemLoader(os.path.join(config.SRC_DIR, "templates")))


def series_files(country):
    files = dict(COMMON_SERIES_FILES)
    for key in config.COUNTRIES[country]["index_tickers"]:
        files[f"index_{key}"] = f"index_{key}.jsonl"
    return files


def index_labels(country):
    """{series_key: label} — e.g. {"index_sp500": "S&P 500"}."""
    return {
        f"index_{key}": label
        for key, label in config.COUNTRIES[country]["index_labels"].items()
    }


def index_tickers(country):
    """{series_key: raw ticker} — e.g. {"index_sp500": "^GSPC"} — so the card
    can say which underlying symbol it's actually tracking, not just the
    friendly name."""
    return {
        f"index_{key}": symbol
        for key, symbol in config.COUNTRIES[country]["index_tickers"].items()
    }


def load_all_series(country):
    return {
        key: store.read_jsonl(os.path.join(config.data_dir(country), filename))
        for key, filename in series_files(country).items()
    }


# A series smaller than this is cheaper to inline than to fetch — a second
# HTTP round trip costs more than the bytes saved.
INLINE_BYTE_LIMIT = 32 * 1024

# Sessions kept in the tail file the page paints from. One trading year: long
# enough that 1W/1M/3M/6M/YTD are all answered from it with no second request.
TAIL_SESSIONS = 252


def externalize_series(country, payload):
    """Move bulky series out of the HTML into fetched JSON files.

    Inlining put every byte in front of first paint: the browser could not
    show a single number until six years of industry ranks had downloaded and
    parsed, which is what made the Notion embeds feel broken.

    Small series stay inline — a round trip is not worth saving 4KB. Big ones
    are written twice: a tail the page paints from immediately, and the full
    history fetched afterwards, because "Since Inception" is the default
    timeframe and a tail alone would quietly mislabel one year as everything.

    Mutates and returns `payload`.
    """
    series = payload.get("series")
    if not series:
        return payload

    out_dir = os.path.join(config.docs_dir(country), "series")
    os.makedirs(out_dir, exist_ok=True)

    manifest, inline = {}, {}
    for key, rows in series.items():
        blob = json.dumps(rows, separators=(",", ":"))
        if len(blob) <= INLINE_BYTE_LIMIT or len(rows) <= TAIL_SESSIONS:
            inline[key] = rows
            continue

        with open(os.path.join(out_dir, f"{key}.json"), "w",
                  encoding="utf-8") as f:
            f.write(blob)
        tail = rows[-TAIL_SESSIONS:]
        with open(os.path.join(out_dir, f"{key}.tail.json"), "w",
                  encoding="utf-8") as f:
            json.dump(tail, f, separators=(",", ":"))

        manifest[key] = {
            "tail": f"series/{key}.tail.json",
            "full": f"series/{key}.json",
            "rows": len(rows),
            "tailRows": len(tail),
        }

    payload["series"] = inline
    if manifest:
        payload["seriesManifest"] = manifest
    return payload


def asset_version():
    """Fingerprint of the shared CSS/JS, appended to their URLs as ?v=.

    GitHub Pages serves these with a long cache lifetime, so without it a
    browser that has visited before keeps running the JS it already has —
    a deployed fix simply does not arrive. This bit me while verifying the
    composition chart: the file on Pages was correct and the page was
    executing a stale copy. Notion embeds are the same browser, so every
    panel would have had the same problem.

    Content-hashed rather than timestamped, so an unchanged file keeps its
    URL and stays cached.
    """
    h = hashlib.md5()
    for name in ("dashboard.css", "dashboard.js"):
        path = os.path.join(config.DOCS_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:10]


def asset_prefix(country):
    """Relative path from a country's pages back to the shared docs root, where
    dashboard.css / dashboard.js / vendor/ live."""
    sub = config.COUNTRIES[country]["docs_subdir"]
    return "../" * len(sub.strip("/").split("/")) if sub else ""


def country_links(country, filename):
    """Nav entries pointing at the same panel in each country, so a viewer can
    flip between markets without hunting for the other URL."""
    links = []
    for code, cfg in config.COUNTRIES.items():
        sub = cfg["docs_subdir"]
        if code == country:
            href = filename
        elif sub:
            href = asset_prefix(country) + sub + "/" + filename
        else:
            href = asset_prefix(country) + filename
        links.append({"code": code, "label": cfg["short"], "title": cfg["label"],
                      "flag": flags.flag(code),
                      "href": href, "active": code == country})
    return links


def _load_classification(country):
    path = os.path.join(config.data_dir(country), "classification.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def render_dashboard(country):
    cfg = config.COUNTRIES[country]
    series = load_all_series(country)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    data_json = json.dumps(externalize_series(country, {
        "generated_at": generated_at,
        "country": country,
        "indexLabels": index_labels(country),
        "indexTickers": index_tickers(country),
        "build": asset_version(),
        "assetPrefix": asset_prefix(country),
        "series": series,
    }), separators=(",", ":"))

    template = _ENV.get_template("dashboard.html.j2")
    html = template.render(
        generated_at=generated_at,
        data_json=data_json,
        country_label=cfg["label"],
        country_flag=flags.flag(country),
        index_keys_csv=",".join(f"index_{k}" for k in cfg["index_tickers"]),
        asset_prefix=asset_prefix(country),
        asset_version=asset_version(),
        country_links=country_links(country, "index.html"),
    )

    out_dir = config.docs_dir(country)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def _index_body(key):
    return f'<div class="card-grid" id="indices-grid" data-keys="{key}"></div>'


def _summary_body():
    return '<div id="environment-panel"></div>'


def _replay_body():
    return """
<div id="replay-panel">
  <div class="replay-controls">
    <button class="icon-btn" id="replay-prev" title="Previous session">&lsaquo;</button>
    <input type="date" id="replay-date">
    <button class="icon-btn" id="replay-next" title="Next session">&rsaquo;</button>
    <button class="icon-btn" id="replay-latest">Latest</button>
    <input type="search" id="replay-ticker" placeholder="Ticker, e.g. NVDA" list="replay-tickers" autocomplete="off">
    <datalist id="replay-tickers"></datalist>
    <span id="replay-ticker-result"></span>
  </div>
  <div id="replay-body"></div>
</div>
""".strip()


def _stock_body():
    # The suggestion list is built client-side from the classification map the
    # page already carries, rather than emitted here as a few thousand
    # duplicate <option> tags.
    return """
<div id="stock-panel">
  <div class="replay-controls">
    <input type="search" id="stock-ticker" placeholder="Ticker, e.g. SNDK" list="stock-tickers" autocomplete="off">
    <datalist id="stock-tickers"></datalist>
    <button class="icon-btn" id="stock-prev" title="Previous session">&lsaquo;</button>
    <input type="date" id="stock-date">
    <button class="icon-btn" id="stock-next" title="Next session">&rsaquo;</button>
    <button class="icon-btn" id="stock-latest">Latest</button>
    <span id="stock-status"></span>
  </div>
  <div id="stock-body"></div>
</div>
""".strip()


def _compact_groups(rows, items_field, name_field):
    """Rebuilds the per-day group tables as a name list plus numeric arrays.

    The replay page needs every date, and the industry history alone is 3.6MB
    as stored - mostly the same field names and group names repeated 252 times.
    Emitting names once and then [rank, 1d, 5d, 20d] per group per day cuts it
    to a fraction, which matters when the page has to load inside a Notion
    embed."""
    names, index_of = [], {}
    by_date = {}
    for row in rows:
        values = []
        for item in row.get(items_field, []):
            name = item.get(name_field)
            if name is None:
                continue
            if name not in index_of:
                index_of[name] = len(names)
                names.append(name)
            values.append([
                index_of[name],
                item.get("rank"),
                item.get("chg_1d"),
                item.get("chg_5d"),
                item.get("chg_20d"),
            ])
        by_date[row["date"]] = values
    return {"names": names, "byDate": by_date}


def write_replay_files(country, series):
    """One JSON per session under docs/<country>/replay/.

    The replay page used to carry every series inline. At a year that was a few
    megabytes; at six it is roughly eleven, all parsed before first paint, in a
    Notion iframe. Since the page only ever shows ONE date at a time, the data
    is stored the way it is read — one file per session, fetched on demand,
    exactly as the stock page fetches one ticker.

    Returns the list of session dates, which is all the page needs up front.
    """
    out_dir = os.path.join(config.docs_dir(country), "replay")
    os.makedirs(out_dir, exist_ok=True)

    labels = index_labels(country)
    env_by_date = {r["date"]: r for r in series.get("environment", [])}

    def group_by_date(rows, items_field, name_field):
        out = {}
        for row in rows:
            out[row["date"]] = [
                {"name": item.get(name_field), "rank": item.get("rank"),
                 "chg_1d": item.get("chg_1d"), "chg_5d": item.get("chg_5d"),
                 "chg_20d": item.get("chg_20d")}
                for item in row.get(items_field, []) if item.get(name_field)
            ]
        return out

    sectors_by_date = group_by_date(series.get("sector_ranks", []), "sectors", "sector")
    industries_by_date = group_by_date(series.get("industry_ranks", []), "industries", "industry")

    # Index rows carried forward: a session missing from one index's series
    # should still show its last known reading rather than a blank.
    index_rows = {key: sorted(series.get(key, []), key=lambda r: r["date"]) for key in labels}
    index_cursor = {key: 0 for key in labels}
    index_last = {key: None for key in labels}

    dates = sorted(env_by_date)
    for date_str in dates:
        indices_now = {}
        for key in labels:
            rows = index_rows[key]
            while index_cursor[key] < len(rows) and rows[index_cursor[key]]["date"] <= date_str:
                row = rows[index_cursor[key]]
                index_last[key] = {"close": row["close"], "a10": row["above_ema10"],
                                   "a20": row["above_ema20"], "a50": row["above_ema50"]}
                index_cursor[key] += 1
            if index_last[key]:
                indices_now[key] = index_last[key]

        payload = {
            "date": date_str,
            "environment": env_by_date.get(date_str),
            "indices": indices_now,
            "sectors": sectors_by_date.get(date_str, []),
            "industries": industries_by_date.get(date_str, []),
        }
        with open(os.path.join(out_dir, f"{date_str}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))

    return dates


def build_replay_payload(country, series, generated_at):
    labels = index_labels(country)
    dates = write_replay_files(country, series)
    return {
        "generated_at": generated_at,
        "country": country,
        "indexLabels": labels,
        # Just the session list; everything else arrives per date.
        "replayDates": dates,
        "replayDir": "replay",
        "classification": _load_classification(country),
        "sectorMembers": sector_member_counts(country),
        "tickerDir": config.TICKER_DIR_NAME,
    }


def _tmle_leaders_body():
    # The stock/score panel is the SAME markup as the standalone "Leader
    # Score" page (_tmle_stock_body()) — clicking a row here publishes a
    # ticker over the page's existing Sync bus, and renderTmleStock() is
    # already called unconditionally on every page (guarded by its own
    # "if (!host) return"), so embedding this markup is the entire change:
    # no new JS, no new data plumbing beyond what render_tmle_panels()
    # already computes for the standalone page.
    return """
<div id="tmle-digest"></div>
<div class="tf-toggle" id="tmle-filter">
  <button class="tf-btn active" data-filter="actionable">Buyable now</button>
  <button class="tf-btn" data-filter="broken">Broken ex-leaders</button>
  <button class="tf-btn" data-filter="all">Everything scored</button>
</div>
<div id="tmle-legend" class="tmle-legend"></div>
<div class="table-wrap"><table id="tmle-leaders-table">
  <thead><tr>
    <th data-sort="rank">#</th><th data-sort="ticker">Ticker</th>
    <th data-sort="composite">Score</th>
    <th data-sort="F1">F1</th><th data-sort="F4">F4</th>
    <th data-sort="F4B">F4B</th><th data-sort="F5">F5</th>
    <th data-sort="gain" title="Rise from the low this advance started at">Up from low</th>
    <th data-sort="drawdown" title="How far below the highest close of this advance">Off high</th>
    <th data-sort="episode_days" title="Trading sessions the advance lasted">Sessions</th>
    <th data-sort="stage">Stage</th>
  </tr></thead><tbody></tbody>
</table></div>
<div class="empty-note" style="margin-top:18px">Click a row for that name's score history and full factor breakdown.</div>
""".strip() + "\n" + _tmle_stock_body()


def _tmle_emerging_body():
    return """
<div class="empty-note">Ranked by how much the score has <b>climbed</b>, not by the score itself — leadership forming rather than leadership confirmed. Actionable names only.</div>
<div class="table-wrap"><table id="tmle-emerging-table">
  <thead><tr>
    <th data-sort="ticker">Ticker</th><th data-sort="composite">Score</th>
    <th data-sort="d4" title="Change in the composite score over 4 weeks, in points">4-week change (pts)</th>
    <th data-sort="d12" title="Change in the composite score over 12 weeks, in points">12-week change (pts)</th>
    <th data-sort="gain" title="Rise from the low this advance started at">Up from low</th>
    <th data-sort="drawdown" title="How far below the highest close of this advance">Off high</th>
    <th data-sort="episode_days" title="Trading sessions since this advance began">Sessions in move</th>
  </tr></thead><tbody></tbody>
</table></div>
""".strip()


def _tmle_stock_body():
    return """
<div id="tmle-stock-panel">
  <div class="replay-controls">
    <input type="search" id="tmle-ticker" placeholder="Ticker" list="tmle-tickers" autocomplete="off">
    <datalist id="tmle-tickers"></datalist>
    <span id="tmle-status"></span>
  </div>
  <div id="tmle-stock-body"></div>
</div>
""".strip()


def _tmle_latest(country):
    """Most recent leaderboard row, plus the score changes that drive the
    emerging view. Both are derived here rather than in the browser: the full
    score history is megabytes, and the page only needs the answer."""
    data_dir = config.data_dir(country)
    leaders_rows = store.read_jsonl(os.path.join(data_dir, "tmle_leaders.jsonl"))
    score_rows = store.read_jsonl(os.path.join(data_dir, "tmle_scores.jsonl"))
    if not leaders_rows:
        return {"date": None, "leaders": [], "emerging": []}

    latest = leaders_rows[-1]
    leaders = latest.get("leaders", [])
    broken = latest.get("broken", [])

    # Attach each name's sector/industry here rather than shipping the whole
    # classification map to these pages — 250 rows need 250 lookups, not 3,300.
    classification = _load_classification(country)
    for item in leaders + broken:
        tags = classification.get(item["ticker"])
        if tags:
            item["sector"], item["industry"] = tags[0], tags[1]

    def scores_at(offset):
        """Composite by ticker `offset` checkpoints back (one per week)."""
        if len(score_rows) <= offset:
            return {}
        row = score_rows[-1 - offset]
        return dict(zip(row.get("t", []), row.get("c", [])))

    now, four, twelve = scores_at(0), scores_at(4), scores_at(12)
    emerging = []
    for item in leaders:
        ticker = item["ticker"]
        if not item.get("actionable"):
            continue
        current = now.get(ticker)
        if current is None:
            continue
        entry = dict(item)
        entry["d4"] = round(current - four[ticker], 1) if ticker in four else None
        entry["d12"] = round(current - twelve[ticker], 1) if ticker in twelve else None
        emerging.append(entry)
    # A name with no prior reading is new to the scored set, which is itself
    # interesting — sort those last rather than dropping them.
    emerging.sort(key=lambda e: (e["d4"] is not None, e["d4"] or 0), reverse=True)

    return {"date": latest["date"], "leaders": leaders, "broken": broken,
            "counts": latest.get("counts", {}), "emerging": emerging[:60]}


def _screener_body():
    return """
<div class="empty-note">Counts are <b>distinct companies</b> over the selected period, not daily totals — a stock printing highs five days running is one company, not five. Pick a lookback, a period, then filter and click any row to send the ticker to the other panels.</div>
<div class="screener-controls">
  <div class="tf-toggle" id="hilo-index"></div>
  <div class="tf-toggle" id="hilo-window"></div>
  <div class="tf-toggle" id="hilo-side"></div>
</div>
<div class="hilo-pair">
  <div class="hilo-side"><div class="card-value up" data-hi>&nbsp;</div>
    <div class="hilo-label" data-hi-label>&nbsp;</div></div>
  <div class="hilo-side"><div class="card-value down" data-lo>&nbsp;</div>
    <div class="hilo-label" data-lo-label>&nbsp;</div></div>
</div>
<div class="hilo-lead"></div>
<div class="tf-toggle" id="hilo-timeframe"></div>
<div class="tf-toggle" id="hilo-chart-view"></div>
<div class="empty-note" id="hilo-chart-verdict" style="margin-bottom:8px"></div>
<div class="chart-wrap"><canvas id="hilo-screener-canvas"></canvas></div>
<div class="screener-filters">
  <span class="filter-label">ADR</span>
  <div class="tf-toggle" id="hilo-adr"></div>
  <select id="hilo-sector"><option value="">All sectors</option></select>
  <select id="hilo-industry"><option value="">All industries</option></select>
  <input type="search" id="hilo-search" placeholder="Filter by ticker">
  <span id="hilo-result-count"></span>
</div>
<div class="table-wrap"><table id="hilo-screener-table">
  <thead><tr>
    <th data-sort="ticker">Ticker</th>
    <th data-sort="sector">Sector</th>
    <th data-sort="industry">Industry</th>
    <th data-sort="close">Close</th>
    <th data-sort="chg">1D %</th>
    <th data-sort="adr" title="Average daily range over 20 sessions: mean of high/low - 1. How much room the stock gives you intraday.">ADR %</th>
    <th data-sort="hits" title="Sessions in the selected period on which it printed a new high or low">Sessions</th>
    <th data-sort="last" title="Most recent session on which it printed one">Latest</th>
  </tr></thead><tbody></tbody>
</table></div>
""".strip() + "\n" + _stock_body()


def _moneyflows_body():
    # One index, one Longs/Shorts side, all three windows (13/26/52W) stacked
    # and pre-drilled to sector -> industry -> stocks, with a running log of
    # whatever's currently drilled in each window so a full pass never needs
    # writing down. Replaces the old heatmap/composition view; the ticker-
    # level lookup that used to live only in the Screener is folded in here
    # too (Screener itself stays, for direct ticker/ADR searches).
    return """
<div class="empty-note">Where new highs and new lows are concentrated, drilled from sector to
industry to the actual stocks — for one index and one side (Longs/Shorts) at a time, across all
three windows at once. Click any sector or industry bar to re-drill that window, double-click an
industry name to open its full Lookup; the log above remembers what you found in each.</div>
<div class="mf-controls">
  <div class="tf-toggle" id="mf-index"></div>
  <div class="mf-side" id="mf-side"></div>
  <div class="search-wrap" style="position:relative">
    <input class="industry-search" id="mf-search" placeholder="Jump to an industry…" autocomplete="off">
    <div class="search-dropdown" id="mf-dropdown"></div>
  </div>
</div>
<div class="pass-log" id="mf-log">
  <div class="pass-log-label"><span>This pass, kept automatically</span><span id="mf-log-ctx"></span></div>
  <div id="mf-log-rows"></div>
</div>
<div id="mf-blocks"></div>
""".strip() + "\n" + _stock_pin_html()


def _stock_pin_html():
    return """
<div class="stock-pin" id="stock-pin">
  <div class="stock-pin-head">
    <div><b id="pin-symbol"></b> <span id="pin-name"></span></div>
    <button class="stock-pin-close" id="pin-close" aria-label="Close">&times;</button>
  </div>
  <div class="stock-pin-body">
    <span class="stock-pin-price" id="pin-price"></span><span class="stock-pin-chg" id="pin-chg"></span>
    <span class="stock-pin-stage" id="pin-stage"></span>
    <div class="stock-pin-verdict" id="pin-verdict"></div>
    <div class="stock-pin-stats" id="pin-stats"></div>
    <div class="lw-chart" id="pin-chart" style="flex:1 1 auto; min-height:80px; margin-top:10px"></div>
  </div>
  <div class="stock-pin-foot">Pinned here — keeps the drill open behind it. <a href="#" id="pin-open">Open full Stock Lookup &rarr;</a></div>
</div>
""".strip()


def _screener_payload(country):
    """Everything the screener page needs, and nothing else.

    The stored counts file carries a per-sector breakdown for every session and
    window, which the screener's chart does not use — stripping it here halves
    the page weight, and a Notion embed has to parse all of it before first
    paint.
    """
    data_dir = config.data_dir(country)

    def _read_json(name, default):
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    counts = store.read_jsonl(os.path.join(data_dir, "hilo_counts.jsonl"))
    compact = []
    for row in counts:
        entry = {"date": row["date"]}
        for key in ("w13", "w26", "w52"):
            w = row.get(key) or {}
            entry[key] = {"hi": w.get("hi", 0), "lo": w.get("lo", 0)}
        compact.append(entry)

    # Member counts over the SCREENER's universe (every priced name), not the
    # breadth universe — the composition chart divides by these, and dividing a
    # 3,311-name count by 1,500-name weights would overstate every group.
    classification = _load_classification(country)
    quotes = _read_json("hilo_quotes.json", {})
    group_members = {"sector": {}, "industry": {}}
    for ticker, tags in classification.items():
        if ticker not in quotes or not tags:
            continue
        for idx, kind in ((0, "sector"), (1, "industry")):
            name = tags[idx] if len(tags) > idx else None
            if name:
                group_members[kind][name] = group_members[kind].get(name, 0) + 1

    return {
        "hiloCounts": compact,
        "hiloNames": _read_json("hilo_names.json", []),
        "quotes": quotes,
        "classification": classification,
        "sectorMembers": sector_member_counts(country),
        "groupMembers": group_members,
        "indexMembership": _read_json("index_membership.json", {}),
        "screenerIndexes": [
            {"code": code, "label": label}
            for code, label, _tv in config.SCREENER_INDEXES.get(country, [])
        ],
        # Needed by the embedded Stock Context panel below the table (see
        # _screener_body) — clicking a row now shows that name's chart right
        # there instead of only sending the ticker to a different page.
        "tickerDir": config.TICKER_DIR_NAME,
        "benchmarkKeys": config.COUNTRIES[country]["largecap_keys"],
    }


def _architecture_body(country):
    """A plain-language map of where every number on this site actually comes
    from, so a stale or surprising figure can be traced back to its source
    instead of taken on faith. Built from the same config values the pipeline
    itself runs on, not hand-typed — this can't drift out of sync with what
    the code actually does the way a separate wiki page would."""
    cfg = config.COUNTRIES[country]
    other = next(c for c in config.COUNTRIES if c != country)
    other_cfg = config.COUNTRIES[other]
    cutoff_h, cutoff_m = cfg["session_final_after_utc"]
    price_src = "Financial Modeling Prep (FMP)" if cfg["price_source"] == "fmp" else "Yahoo Finance (yfinance)"
    workflow = f"daily-{country.lower()}.yml"
    vol_map = cfg.get("index_volume", {})
    vol_lines = "".join(
        f"<li><code>{key}</code> ({cfg['index_labels'].get(key, key)}) "
        f"&mdash; {'own volume from ' + price_src if sym == cfg['index_tickers'][key] else 'proxy: ' + sym}</li>"
        for key, sym in vol_map.items()
    )
    no_vol = [k for k in cfg["index_tickers"] if k not in vol_map]
    no_vol_line = (
        f"<li>{', '.join(cfg['index_labels'][k] for k in no_vol)} &mdash; "
        "no volume panel; the underlying source has no usable figure for these "
        "(near-zero or always-zero), so it's omitted rather than shown misleadingly.</li>"
        if no_vol else ""
    )

    return f"""
<div class="empty-note">How this page's own data actually gets here, end to end &mdash; for tracing a number back to its source rather than taking it on faith.</div>

<div class="arch-section">
  <h3>1. Where the numbers come from</h3>
  <table class="arch-table">
    <tr><td>Prices ({cfg['label']})</td><td>{price_src}</td></tr>
    <tr><td>Prices ({other_cfg['label']})</td><td>{"Financial Modeling Prep (FMP)" if other_cfg["price_source"] == "fmp" else "Yahoo Finance (yfinance)"}</td></tr>
    <tr><td>Sector / industry classification</td><td>TradingView's public screener &mdash; both markets</td></tr>
    <tr><td>Breadth universe</td><td>{"S&amp;P 1500 constituents (iShares IVV/IJH/IJR holdings CSVs)" if country == "US" else "Top 1,000 NSE names by market cap (from the TradingView classification pull itself &mdash; India has no free constituent list)"}</td></tr>
  </table>
</div>

<div class="arch-section">
  <h3>2. Pipeline order (src/main.py, one country at a time)</h3>
  <ol>
    <li>Pull TradingView classification (sector/industry tags, and India's universe)</li>
    <li>Build the breadth universe (S&amp;P 1500 for US; top-1000 by cap for India)</li>
    <li>Update the rolling price cache &mdash; new tickers get full history; existing ones get the last {config.RECENT_WINDOW_DAYS} days re-checked (not just today &mdash; see the reliability note below)</li>
    <li>Backfill index history (OHLC + EMA10/20/50 + volume where available) &mdash; fetched directly per index, independent of the breadth universe</li>
    <li>Backfill breadth internals (advance/decline, new highs/lows, % up/down) from the price cache</li>
    <li>Backfill sector and industry performance/rank</li>
    <li>Compute the Market Environment read (trend/participation/internals)</li>
    <li>Compute relative-strength ratings</li>
    <li>{"Run TMLE (leader-scoring engine) &mdash; US only, since it needs FMP fundamentals with no India coverage on this plan" if country == "US" else "(TMLE does not run for India &mdash; see note in step above)"}</li>
    <li>Render every page in docs/ from what was just written</li>
  </ol>
</div>

<div class="arch-section">
  <h3>3. Update schedule</h3>
  <table class="arch-table">
    <tr><td>{cfg['label']}</td><td>GitHub Action <code>{workflow}</code> &mdash; runs after {cfg['label']}'s own market close, independently of the other country</td></tr>
    <tr><td>Session cutoff</td><td>{cutoff_h:02d}:{cutoff_m:02d} UTC &mdash; today's bar is excluded entirely until the wall clock clears this, so a live/forming session is never mistaken for a finished one</td></tr>
    <tr><td>Self-healing</td><td>Every run recomputes the full trailing {config.CHART_BACKFILL_DAYS}-day window and upserts by date &mdash; a day that failed to price on one run fills itself in on the next successful one, automatically</td></tr>
  </table>
</div>

<div class="arch-section">
  <h3>4. Storage model</h3>
  <table class="arch-table">
    <tr><td><code>data/{cfg['data_subdir']}/*.jsonl</code></td><td>Permanent accumulating history, one row per session, git-tracked in <code>main</code>. This IS the chart history.</td></tr>
    <tr><td><code>docs/{cfg['docs_subdir'] + '/' if cfg['docs_subdir'] else ''}panel-*.html</code></td><td>Generated pages, one per panel, rebuilt fresh every run and git-tracked (so the last good render always survives a bad run).</td></tr>
    <tr><td><code>docs/{cfg['docs_subdir'] + '/' if cfg['docs_subdir'] else ''}tickers/</code></td><td>Per-ticker price files (chart depth back to 2020) &mdash; deliberately <b>not</b> git-tracked in main (thousands of files rewritten daily would bloat history for nothing); lives only on the <code>gh-pages</code> branch and this hub's local sync.</td></tr>
  </table>
</div>

<div class="arch-section">
  <h3>5. Known data caveats</h3>
  <ul>
    {vol_lines}
    {no_vol_line}
    <li>Russell 2000's own volume from FMP is byte-identical to the S&amp;P 500's every day &mdash; not real Russell volume, just a mirrored composite figure &mdash; which is why it uses IWM (the Russell 2000 ETF) as a proxy instead.</li>
  </ul>
</div>

<div class="arch-section">
  <h3>6. Reliability</h3>
  <p class="env-block-sub">FMP's real-time quote endpoint previously went dark for entire runs on several separate days, pricing under 1% of tickers while reporting success &mdash; breadth/sector/industry correctly refused to count those as sessions and fell behind, while index history (fetched differently) kept advancing, which is what caused different panels to show different "as of" dates with nothing explaining why. Today's close is now fetched via the same historical-EOD endpoint used everywhere else, and a run aborts loudly (committing nothing) if fewer than {config.MIN_DAILY_PRICE_COVERAGE:.0%} of tickers come back priced, rather than silently accepting a near-empty session.</p>
</div>
""".strip()


def _breadth_body(key):
    return f'<div class="card-grid" id="breadth-grid" data-keys="{key}"></div>'


def _sector_lookup_body():
    # What Stock Lookup already does for one ticker, done for a sector or
    # industry instead — search, step through sessions, its own rank history,
    # and (new here) which member stocks are actually driving it: highest
    # market cap, top gainers, top decliners, each its own colour. This is
    # what double-clicking an industry in Money Flows opens.
    return """
<div class="empty-note">Search any sector or industry, step through sessions, and see its own
rank history — plus which member stocks are actually driving it.</div>
<div class="mf-controls">
  <div class="tf-toggle" id="sl-kind"></div>
  <div class="search-wrap" style="position:relative">
    <input class="industry-search" id="sl-search" placeholder="Search any sector or industry…" autocomplete="off">
    <div class="search-dropdown" id="sl-dropdown"></div>
  </div>
  <button class="icon-btn" id="sl-prev" title="Previous session">&lsaquo;</button>
  <input type="date" id="sl-date">
  <button class="icon-btn" id="sl-next" title="Next session">&rsaquo;</button>
  <button class="icon-btn" id="sl-latest">Latest</button>
</div>
<div class="card">
  <div class="card-head">
    <div>
      <div class="card-title" id="sl-name" style="font-size:20px; font-weight:600; margin-bottom:0"></div>
      <div class="card-sub" id="sl-parent"></div>
    </div>
    <div style="text-align:right">
      <div class="card-value" id="sl-rank" style="font-size:26px"></div>
      <div class="card-sub" id="sl-rank-sub"></div>
    </div>
  </div>
  <div class="tmle-stats" id="sl-pcts" style="margin:12px 0"></div>
  <div class="chart-wrap"><canvas id="sl-rank-canvas"></canvas></div>
  <div id="sl-leader-groups" style="margin-top:14px"></div>
</div>
""".strip()


def _breadth_internals_body():
    # Regime badge + one verdict sentence before the raw counts, the same
    # pattern Market Environment already uses well — computed client-side
    # from the same two daily series the Advance/Decline and New Highs/Lows
    # panels already publish, over whichever window is selected, rather than
    # a second copy of metrics/environment.py's fixed 10-day read.
    return """
<div class="empty-note">What advance/decline and new highs/lows are actually saying, read
over a window you choose — not just the raw counts.</div>
<div class="tf-toggle" id="bi-window"></div>
<div id="bi-badge"></div>
<div class="digest-verdict" id="bi-verdict" style="margin: 8px 0 16px"></div>
<div class="card-grid">
  <div class="card">
    <div class="card-title">Advance / Decline</div>
    <div class="card-value" id="bi-adv-val"></div>
    <div class="card-sub" id="bi-adv-sub"></div>
    <div class="chart-wrap"><canvas id="bi-adv-canvas"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">New Highs / Lows</div>
    <div class="card-value" id="bi-hilo-val"></div>
    <div class="card-sub" id="bi-hilo-sub"></div>
    <div class="chart-wrap"><canvas id="bi-hilo-canvas"></canvas></div>
  </div>
</div>
""".strip()


def _rank_body(name_label, table_id, drilldown_id, sort_key):
    return f"""
<div class="empty-note">Ranked by 20-trading-day performance (smoother than 1-day, which whipsaws on noise) — click a column to sort by it instead.</div>
<div class="table-wrap">
  <table id="{table_id}">
    <thead><tr>
      <th data-sort="{sort_key}">{name_label}</th>
      <th data-sort="chg_1d">1D %</th>
      <th data-sort="chg_5d">5D %</th>
      <th data-sort="chg_20d">20D %</th>
      <th data-sort="rank">Rank</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</div>
<div class="drilldown" id="{drilldown_id}">
  <div class="drilldown-title"></div>
  <div class="tf-toggle" data-target="{drilldown_id}"></div>
  <div class="chart-wrap"><canvas id="{drilldown_id}-canvas"></canvas></div>
</div>
""".strip()


def _write_panel(country, filename, title, body_html, payload, generated_at,
                 needs_chartjs=False, needs_lightweight=False):
    payload = externalize_series(country, dict(payload or {}))
    payload["build"] = asset_version()
    # India's pages live in docs/in/, so they need "../" to reach the marker
    # at the docs root.
    payload["assetPrefix"] = asset_prefix(country)
    html = _ENV.get_template("panel.html.j2").render(
        title=title,
        generated_at=generated_at,
        body_html=body_html,
        data_json=json.dumps(payload, separators=(",", ":")),
        needs_chartjs=needs_chartjs,
        needs_lightweight=needs_lightweight,
        asset_prefix=asset_prefix(country),
        asset_version=asset_version(),
        country_links=country_links(country, filename),
        country_label=config.COUNTRIES[country]["label"],
        country_flag=flags.flag(country),
    )
    out_dir = config.docs_dir(country)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def sector_member_counts(country):
    """{sector: how many breadth-universe members it has}.

    Needed because a raw count of new highs rewards the biggest sector by
    membership rather than the strongest one. Finance is 348 of 1,500 US names,
    so it tops a raw count on an average day without leading anything. Dividing
    by these weights turns "most highs" into "most highs relative to its size",
    which is the question actually being asked.

    Counted over the BREADTH universe (the 1,500 names the highs and lows are
    measured across), not the full classified list — the classification covers
    every priced name, and dividing a breadth share by a whole-market weight
    understates every sector's true concentration.
    """
    path = os.path.join(config.data_dir(country), "breadth_sector_members.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    # Fallback for a country whose pipeline has not written the file yet.
    counts = {}
    for tags in _load_classification(country).values():
        if tags and tags[0]:
            counts[tags[0]] = counts.get(tags[0], 0) + 1
    return counts


def render_panel(country, filename, title, body_html, data_keys, series, generated_at,
                 chart_lib="chartjs", extra=None):
    """Renders one standalone single-panel page, embedding only `data_keys`
    from the full series set (keeps individual embed pages lightweight).

    `chart_lib` decides which charting library the page loads — index panels
    need lightweight-charts for HLC bars, everything else needs Chart.js.
    Loading only the one in use keeps a 12KB breadth panel from pulling in
    370KB of unused JavaScript, which matters inside a Notion embed."""
    payload = {
        "generated_at": generated_at,
        "country": country,
        "indexLabels": index_labels(country),
        "series": {k: series.get(k, []) for k in data_keys},
    }
    payload.update(extra or {})
    return _write_panel(
        country, filename, title, body_html, payload, generated_at,
        needs_chartjs=(chart_lib in ("chartjs", "both")),
        needs_lightweight=(chart_lib in ("lightweight", "both")),
    )


def write_build_marker():
    """A 30-byte file naming the current build.

    Notion embeds each panel in a cross-origin iframe, and a hard refresh of
    the Notion page does not reliably revalidate those. After a structural
    change the embed can sit on a stale copy indefinitely — which is exactly
    what happened when the replay history went from one year to six: the page
    was correct on Pages and the embed still showed the old date range.

    So each page checks this file on load and reloads itself once if the build
    it was rendered against is no longer current. Deliberately fetched with a
    timestamp so this one request can never itself be cached.
    """
    path = os.path.join(config.DOCS_DIR, "build.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"build": asset_version()}, f)
    return path


def render_all_panels(country):
    """Generates one standalone page per index/sector/industry/breadth panel,
    so each can be embedded separately (e.g. in Notion) instead of only as
    part of the single combined dashboard. Returns the list of paths written."""
    cfg = config.COUNTRIES[country]
    os.makedirs(config.docs_dir(country), exist_ok=True)
    series = load_all_series(country)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    paths = []

    paths.append(render_panel(
        country, "panel-summary.html", "Market Environment", _summary_body(),
        ["environment"], series, generated_at,
    ))

    # The replay page carries its own compacted payload rather than the raw
    # per-metric series, so it is built directly instead of via render_panel.
    paths.append(_write_panel(
        country, "panel-replay.html", "Market Replay", _replay_body(),
        build_replay_payload(country, series, generated_at), generated_at,
    ))

    # Stock context page. Carries only the classification map; each ticker's
    # prices are fetched on demand from <docs>/tickers/.
    paths.append(_write_panel(
        country, "panel-stock.html", "Stock Context", _stock_body(),
        {
            "generated_at": generated_at,
            "country": country,
            "classification": _load_classification(country),
            "tickerDir": config.TICKER_DIR_NAME,
            "indexLabels": index_labels(country),
            "benchmarkKeys": cfg["largecap_keys"],
        },
        generated_at, needs_lightweight=True,
    ))

    for key, label in index_labels(country).items():
        filename = f"panel-{key.replace('_', '-')}.html"
        paths.append(render_panel(country, filename, label, _index_body(key),
                                  [key], series, generated_at, chart_lib="lightweight",
                                  extra={"indexTickers": index_tickers(country)}))

    paths.append(render_panel(
        country, "panel-sectors.html", "Sector Performance",
        _rank_body("Sector", "sector-table", "sector-drilldown", "sector"),
        ["sector_ranks"], series, generated_at,
    ))
    paths.append(render_panel(
        country, "panel-industries.html", "Industry Performance",
        _rank_body("Industry", "industry-table", "industry-drilldown", "industry"),
        ["industry_ranks"], series, generated_at,
    ))

    quotes_path = os.path.join(config.data_dir(country), "hilo_quotes.json")
    quotes = {}
    if os.path.exists(quotes_path):
        with open(quotes_path, encoding="utf-8") as f:
            quotes = json.load(f)
    paths.append(_write_panel(
        country, "panel-sector-lookup.html", "Sector & Industry Lookup",
        _sector_lookup_body(),
        dict(sectorRanks=series.get("sector_ranks", []), industryRanks=series.get("industry_ranks", []),
             classification=_load_classification(country), quotes=quotes,
             generated_at=generated_at, country=country),
        generated_at, needs_chartjs=True,
    ))

    mf_extra = dict(_screener_payload(country))
    if cfg.get("run_tmle"):
        # Optional — only US has TMLE scores. The pin fetches this per
        # ticker and simply skips the stage/verdict section if the request
        # 404s, same graceful-degrade as everywhere else TMLE is optional.
        mf_extra["tmleDir"] = "tmle"
    paths.append(render_panel(
        country, "panel-groups.html", "Money Flows",
        _moneyflows_body(), ["industry_ranks"], series, generated_at,
        extra=mf_extra, chart_lib="both",
    ))

    paths.append(render_panel(
        country, "panel-screener.html", "New Highs & Lows Screener",
        _screener_body(), [], series, generated_at,
        extra=_screener_payload(country), chart_lib="both",
    ))

    paths.append(render_panel(
        country, "panel-breadth-internals.html", "Breadth Internals",
        _breadth_internals_body(), ["breadth_adv_decl", "breadth_new_hilo"],
        series, generated_at,
    ))

    members = sector_member_counts(country)
    for key, label in BREADTH_LABELS.items():
        filename = f"panel-{key.replace('_', '-')}.html"
        # Only the highs/lows counts panel needs the sector weights, and it is
        # the one panel that would mislead without them.
        extra = {"sectorMembers": members} if key == "breadth_hilo_counts" else None
        paths.append(render_panel(country, filename, label, _breadth_body(key),
                                  [key], series, generated_at, extra=extra))

    if cfg.get("run_tmle"):
        paths.extend(render_tmle_panels(country, generated_at))

    paths.append(render_panel(
        country, "panel-architecture.html", "System Architecture",
        _architecture_body(country), [], series, generated_at,
    ))

    return paths


def render_tmle_panels(country, generated_at):
    """The three leader-engine pages. Built only where TMLE runs."""
    import tmle.config as tmle_config

    tmle_data = _tmle_latest(country)
    factor_meta = {
        key: {"label": tmle_config.FACTOR_LABELS[key], "blurb": tmle_config.FACTOR_BLURBS[key]}
        for key in tmle_config.ACTIVE_FACTORS
    }
    common = {
        "generated_at": generated_at,
        "country": country,
        "asOf": tmle_data["date"],
        "factorMeta": factor_meta,
        "maxDrawdown": tmle_config.MAX_ACTIONABLE_DRAWDOWN,
        "tickerDir": config.TICKER_DIR_NAME,
    }

    # The leaders page now embeds the same score/chart panel the standalone
    # "Leader Score" page shows, so it needs that panel's data too:
    # classification (industry/sector for the header), tmleDir (where its
    # per-ticker score history is fetched from), and scored (the autocomplete
    # list plus what a click is validated against).
    paths = []
    paths.append(_write_panel(
        country, "panel-tmle-leaders.html", "Market Leaders", _tmle_leaders_body(),
        dict(common, leaders=tmle_data["leaders"], broken=tmle_data["broken"],
             counts=tmle_data["counts"], classification=_load_classification(country),
             tmleDir="tmle", scored=[item["ticker"] for item in tmle_data["leaders"]]),
        generated_at, needs_chartjs=True, needs_lightweight=True,
    ))
    paths.append(_write_panel(
        country, "panel-tmle-emerging.html", "Emerging Leaders", _tmle_emerging_body(),
        dict(common, emerging=tmle_data["emerging"]), generated_at,
    ))
    paths.append(_write_panel(
        country, "panel-tmle-stock.html", "Leader Score", _tmle_stock_body(),
        dict(common, classification=_load_classification(country),
             tmleDir="tmle",
             scored=[item["ticker"] for item in tmle_data["leaders"]]),
        generated_at, needs_chartjs=True, needs_lightweight=True,
    ))
    return paths


def render_country(country):
    dashboard = render_dashboard(country)
    panels = render_all_panels(country)
    return dashboard, panels


if __name__ == "__main__":
    for code in config.COUNTRIES:
        dashboard, panels = render_country(code)
        print(f"{code}: {dashboard} + {len(panels)} panel pages")
    print(f"build marker: {write_build_marker()}")
