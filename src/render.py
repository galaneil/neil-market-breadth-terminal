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


def load_all_series(country):
    return {
        key: store.read_jsonl(os.path.join(config.data_dir(country), filename))
        for key, filename in series_files(country).items()
    }


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

    data_json = json.dumps({
        "generated_at": generated_at,
        "country": country,
        "indexLabels": index_labels(country),
        "series": series,
    }, separators=(",", ":"))

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


def build_replay_payload(country, series, generated_at):
    labels = index_labels(country)
    return {
        "generated_at": generated_at,
        "country": country,
        "indexLabels": labels,
        "environment": series.get("environment", []),
        "indices": {
            key: [
                {"date": r["date"], "close": r["close"],
                 "a10": r["above_ema10"], "a20": r["above_ema20"], "a50": r["above_ema50"]}
                for r in series.get(key, [])
            ]
            for key in labels
        },
        "sectors": _compact_groups(series.get("sector_ranks", []), "sectors", "sector"),
        "industries": _compact_groups(series.get("industry_ranks", []), "industries", "industry"),
        "classification": _load_classification(country),
        "sectorMembers": sector_member_counts(country),
    }


def _tmle_leaders_body():
    return """
<div class="empty-note">Scored across the current advance, not a calendar or trailing year — see the stage column. Only <b>Advancing</b> names within 25% of their high are actionable; everything else is shown for context but never ranked.</div>
<div class="tf-toggle" id="tmle-filter">
  <button class="tf-btn active" data-filter="actionable">Actionable only</button>
  <button class="tf-btn" data-filter="all">Everything scored</button>
</div>
<div class="table-wrap"><table id="tmle-leaders-table">
  <thead><tr>
    <th data-sort="rank">#</th><th data-sort="ticker">Ticker</th>
    <th data-sort="composite">Score</th>
    <th data-sort="F1">F1</th><th data-sort="F4">F4</th>
    <th data-sort="F4B">F4B</th><th data-sort="F5">F5</th>
    <th data-sort="gain" title="Rise from the low this advance started at">Up from low</th>
    <th data-sort="drawdown" title="How far below the highest close of this advance">Off high</th>
    <th data-sort="episode_days" title="Trading sessions since this advance began">Sessions in move</th>
    <th data-sort="stage">Stage</th>
  </tr></thead><tbody></tbody>
</table></div>
""".strip()


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

    # Attach each name's sector/industry here rather than shipping the whole
    # classification map to these pages — 250 rows need 250 lookups, not 3,300.
    classification = _load_classification(country)
    for item in leaders:
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

    return {"date": latest["date"], "leaders": leaders, "emerging": emerging[:60]}


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
""".strip()


def _groups_body():
    return """
<div class="empty-note">Which groups are making new highs, and which are making new lows. Click any row to send that group to the screener.</div>
<div class="screener-controls">
  <div class="tf-toggle" id="hilo-index"></div>
  <div class="tf-toggle" id="hilo-window"></div>
  <div class="tf-toggle" id="hilo-timeframe"></div>
</div>
<div class="composition-head">
  <div class="tf-toggle" id="hilo-view"></div>
  <div class="tf-toggle" id="hilo-groupby"></div>
  <div class="tf-toggle" id="hilo-groupmode"></div>
</div>
<div class="digest" id="hilo-digest"></div>
<div class="composition-note" id="hilo-heat-caption"></div>
<div class="heatmap" id="hilo-heatmap"></div>
<div class="composition">
  <div class="chart-wrap chart-tall"><canvas id="hilo-composition-canvas"></canvas></div>
</div>
<div class="heat-legend">
  <span class="heat-swatch heat-neg"></span> more lows
  <span class="heat-swatch heat-mid"></span> balanced
  <span class="heat-swatch heat-pos"></span> more highs
  <span class="heat-legend-note">Colour is scaled to the strongest group on screen, so the map stays readable at any window. Click a tile to filter.</span>
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
    }


def _breadth_body(key):
    return f'<div class="card-grid" id="breadth-grid" data-keys="{key}"></div>'


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
        needs_chartjs=(chart_lib == "chartjs"),
        needs_lightweight=(chart_lib == "lightweight"),
    )


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
                                  [key], series, generated_at, chart_lib="lightweight"))

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

    paths.append(render_panel(
        country, "panel-groups.html", "Highs & Lows by Group",
        _groups_body(), [], series, generated_at,
        extra=_screener_payload(country),
    ))

    paths.append(render_panel(
        country, "panel-screener.html", "New Highs & Lows Screener",
        _screener_body(), [], series, generated_at,
        extra=_screener_payload(country),
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
    }

    paths = []
    paths.append(_write_panel(
        country, "panel-tmle-leaders.html", "Market Leaders", _tmle_leaders_body(),
        dict(common, leaders=tmle_data["leaders"]), generated_at,
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
        generated_at, needs_chartjs=True,
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
