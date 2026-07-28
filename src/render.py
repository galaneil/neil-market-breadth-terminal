"""
render.py — assembles data/*.jsonl history into HTML pages via Jinja2.

Two kinds of output:
  - docs/index.html: the full combined dashboard (all panels on one page).
  - docs/panel-*.html: one standalone page per individual panel (each index,
    sectors, industries, each breadth metric), for embedding separately
    (e.g. one Notion /embed block per panel instead of one long scrolling
    embed of the whole dashboard). Each panel page only embeds its own
    slice of data, so it stays lightweight even though the combined page
    is large (a full year of sector/industry history for every group).

Both share the same docs/dashboard.css and docs/dashboard.js — dashboard.js
reads data-keys attributes off each panel's container element and only
wires up what's actually present on the page, so one script serves every
page variant.
"""

import json
import os
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader

import config
import store

SERIES_FILES = {
    "environment": "environment.jsonl",
    "index_nasdaq": "index_nasdaq.jsonl",
    "index_sp500": "index_sp500.jsonl",
    "index_russell2000": "index_russell2000.jsonl",
    "sector_ranks": "sector_ranks.jsonl",
    "industry_ranks": "industry_ranks.jsonl",
    "breadth_adv_decl": "breadth_adv_decl.jsonl",
    "breadth_new_hilo": "breadth_new_hilo.jsonl",
    "breadth_pct_up20": "breadth_pct_up20.jsonl",
    "breadth_pct_up30": "breadth_pct_up30.jsonl",
    "breadth_pct_down20": "breadth_pct_down20.jsonl",
    "breadth_pct_down30": "breadth_pct_down30.jsonl",
}

INDEX_LABELS = {
    "index_nasdaq": "NASDAQ Composite",
    "index_sp500": "S&P 500",
    "index_russell2000": "Russell 2000",
}

BREADTH_LABELS = {
    "breadth_adv_decl": "Net Advancers − Decliners",
    "breadth_new_hilo": "Net New Highs − New Lows",
    "breadth_pct_up20": "% Up 20%+ (5D)",
    "breadth_pct_up30": "% Up 30%+ (5D)",
    "breadth_pct_down20": "% Down 20%+ (5D)",
    "breadth_pct_down30": "% Down 30%+ (5D)",
}

_ENV = Environment(loader=FileSystemLoader(os.path.join(config.SRC_DIR, "templates")))


def load_all_series():
    return {
        key: store.read_jsonl(os.path.join(config.DATA_DIR, filename))
        for key, filename in SERIES_FILES.items()
    }


def render_dashboard():
    series = load_all_series()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    data_json = json.dumps({"generated_at": generated_at, "series": series}, separators=(",", ":"))
    template = _ENV.get_template("dashboard.html.j2")
    html = template.render(generated_at=generated_at, data_json=data_json)

    os.makedirs(config.DOCS_DIR, exist_ok=True)
    out_path = os.path.join(config.DOCS_DIR, "index.html")
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


def build_replay_payload(series, generated_at):
    classification_path = os.path.join(config.DATA_DIR, "classification.json")
    classification = {}
    if os.path.exists(classification_path):
        with open(classification_path) as f:
            classification = json.load(f)

    return {
        "generated_at": generated_at,
        "environment": series.get("environment", []),
        "indices": {
            key: [
                {"date": r["date"], "close": r["close"],
                 "a10": r["above_ema10"], "a20": r["above_ema20"], "a50": r["above_ema50"]}
                for r in series.get(key, [])
            ]
            for key in INDEX_LABELS
        },
        "sectors": _compact_groups(series.get("sector_ranks", []), "sectors", "sector"),
        "industries": _compact_groups(series.get("industry_ranks", []), "industries", "industry"),
        "classification": classification,
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


def render_panel(filename, title, body_html, data_keys, series, generated_at, chart_lib="chartjs"):
    """Renders one standalone single-panel page, embedding only `data_keys`
    from the full series set (keeps individual embed pages lightweight).

    `chart_lib` decides which charting library the page loads — index panels
    need lightweight-charts for HLC bars, everything else needs Chart.js.
    Loading only the one in use keeps a 12KB breadth panel from pulling in
    370KB of unused JavaScript, which matters inside a Notion embed."""
    scoped_series = {k: series.get(k, []) for k in data_keys}
    data_json = json.dumps({"generated_at": generated_at, "series": scoped_series}, separators=(",", ":"))

    template = _ENV.get_template("panel.html.j2")
    html = template.render(
        title=title, generated_at=generated_at, body_html=body_html, data_json=data_json,
        needs_chartjs=(chart_lib == "chartjs"), needs_lightweight=(chart_lib == "lightweight"),
    )

    out_path = os.path.join(config.DOCS_DIR, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def render_all_panels():
    """Generates one standalone page per index/sector/industry/breadth panel,
    so each can be embedded separately (e.g. in Notion) instead of only as
    part of the single combined dashboard. Returns the list of paths written."""
    os.makedirs(config.DOCS_DIR, exist_ok=True)
    series = load_all_series()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    paths = []

    paths.append(render_panel(
        "panel-summary.html", "Market Environment", _summary_body(),
        ["environment"], series, generated_at,
    ))

    # The replay page carries its own compacted payload rather than the raw
    # per-metric series, so it is built directly instead of via render_panel.
    replay_json = json.dumps(build_replay_payload(series, generated_at), separators=(",", ":"))
    replay_html = _ENV.get_template("panel.html.j2").render(
        title="Market Replay", generated_at=generated_at,
        body_html=_replay_body(), data_json=replay_json,
        needs_chartjs=False, needs_lightweight=False,
    )
    replay_path = os.path.join(config.DOCS_DIR, "panel-replay.html")
    with open(replay_path, "w", encoding="utf-8") as f:
        f.write(replay_html)
    paths.append(replay_path)

    # Stock context page. Carries only the classification map; each ticker's
    # prices are fetched on demand from docs/tickers/.
    classification_path = os.path.join(config.DATA_DIR, "classification.json")
    classification = {}
    if os.path.exists(classification_path):
        with open(classification_path) as f:
            classification = json.load(f)
    stock_json = json.dumps({
        "generated_at": generated_at,
        "classification": classification,
        "tickerDir": config.TICKER_DIR_NAME,
    }, separators=(",", ":"))
    stock_html = _ENV.get_template("panel.html.j2").render(
        title="Stock Context", generated_at=generated_at,
        body_html=_stock_body(), data_json=stock_json,
        needs_chartjs=False, needs_lightweight=True,
    )
    stock_path = os.path.join(config.DOCS_DIR, "panel-stock.html")
    with open(stock_path, "w", encoding="utf-8") as f:
        f.write(stock_html)
    paths.append(stock_path)

    for key, label in INDEX_LABELS.items():
        filename = f"panel-{key.replace('_', '-')}.html"
        paths.append(render_panel(filename, label, _index_body(key), [key], series, generated_at,
                                  chart_lib="lightweight"))

    paths.append(render_panel(
        "panel-sectors.html", "Sector Performance",
        _rank_body("Sector", "sector-table", "sector-drilldown", "sector"),
        ["sector_ranks"], series, generated_at,
    ))
    paths.append(render_panel(
        "panel-industries.html", "Industry Performance",
        _rank_body("Industry", "industry-table", "industry-drilldown", "industry"),
        ["industry_ranks"], series, generated_at,
    ))

    for key, label in BREADTH_LABELS.items():
        filename = f"panel-{key.replace('_', '-')}.html"
        paths.append(render_panel(filename, label, _breadth_body(key), [key], series, generated_at))

    return paths


if __name__ == "__main__":
    path = render_dashboard()
    print(f"Rendered combined dashboard to {path}")
    panel_paths = render_all_panels()
    print(f"Rendered {len(panel_paths)} individual panel pages")
