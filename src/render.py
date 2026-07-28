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
