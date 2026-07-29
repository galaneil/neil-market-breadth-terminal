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
    "breadth_pct_up20": "breadth_pct_up20.jsonl",
    "breadth_pct_up30": "breadth_pct_up30.jsonl",
    "breadth_pct_down20": "breadth_pct_down20.jsonl",
    "breadth_pct_down30": "breadth_pct_down30.jsonl",
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


def render_panel(country, filename, title, body_html, data_keys, series, generated_at,
                 chart_lib="chartjs"):
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

    for key, label in BREADTH_LABELS.items():
        filename = f"panel-{key.replace('_', '-')}.html"
        paths.append(render_panel(country, filename, label, _breadth_body(key),
                                  [key], series, generated_at))

    return paths


def render_country(country):
    dashboard = render_dashboard(country)
    panels = render_all_panels(country)
    return dashboard, panels


if __name__ == "__main__":
    for code in config.COUNTRIES:
        dashboard, panels = render_country(code)
        print(f"{code}: {dashboard} + {len(panels)} panel pages")
