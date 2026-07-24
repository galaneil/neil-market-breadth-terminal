"""
render.py — assembles all data/*.jsonl history into docs/index.html via Jinja2.

The full history of every metric is embedded inline as JSON so the page works
standalone (no server, no API calls at view time) — Chart.js (vendored at
docs/vendor/) and dashboard.js (docs/dashboard.js, static) do all client-side
rendering: timeframe filtering, chart drawing, table sorting, drill-down.
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


def load_all_series():
    return {
        key: store.read_jsonl(os.path.join(config.DATA_DIR, filename))
        for key, filename in SERIES_FILES.items()
    }


def render_dashboard():
    series = load_all_series()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    data_json = json.dumps({"generated_at": generated_at, "series": series}, separators=(",", ":"))

    env = Environment(loader=FileSystemLoader(os.path.join(config.SRC_DIR, "templates")))
    template = env.get_template("dashboard.html.j2")
    html = template.render(generated_at=generated_at, data_json=data_json)

    os.makedirs(config.DOCS_DIR, exist_ok=True)
    out_path = os.path.join(config.DOCS_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


if __name__ == "__main__":
    path = render_dashboard()
    print(f"Rendered dashboard to {path}")
