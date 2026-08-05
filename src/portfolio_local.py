"""
portfolio_local.py — the portfolio panel, rendered locally and never published.

SECURITY POSTURE
---------------------------------------------------------------------------
Positions, cost basis and account equity must never reach the public repo or
the Pages site. Three independent things enforce that, so no single mistake
exposes anything:

  1. Output goes to OUTPUT_DIR, which sits OUTSIDE the git repository. The
     parent folder is not a repo, so there is no index that could stage it
     even by accident.
  2. Nothing is written into data/ or docs/ — the two trees the pipeline
     commits and publishes.
  3. .gitignore carries the portfolio paths anyway, in case a future version
     of this file is careless.

The only thing that lives inside the repo is this code, which contains no
account data. The Flex token stays in .env, which is gitignored.

The page is opened from disk, so it is visible only on this machine. It is
deliberately not embeddable in Notion — Notion would need a URL it can reach,
and that means a public host.

WHAT IT SHOWS
---------------------------------------------------------------------------
Broker truth from IBKR, converted to base currency, joined to whatever the
terminal already knows about each holding (RS rating, stage, industry). Risk
per position needs a stop, which IBKR cannot supply — Flex reports what
executed, never what is resting — so stops are read from a local file you
maintain. Where a stop is missing the panel says so rather than inventing one.

Usage:
    python src/portfolio_local.py
"""

import json
import os
import sys
import webbrowser
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import ibkr_flex

# Outside the repository, deliberately. See the security note above.
OUTPUT_DIR = os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local")
STOPS_FILE = os.path.join(OUTPUT_DIR, "stops.json")
PAGE = os.path.join(OUTPUT_DIR, "portfolio.html")

# A position bigger than this is worth being told about rather than having to
# notice. Not a rule, just a prompt.
CONCENTRATION_WARN = 0.25


def log(msg):
    print(msg, flush=True)


def load_stops():
    """{ticker: stop_price}, from Notion where possible.

    The trade log is authoritative — it is where the stop is decided — so the
    local file is only a cache, refreshed on every successful read. That keeps
    the panel working on a plane or with the token unset, and means there is
    never a second place to maintain a stop by hand.
    """
    try:
        import notion_sync
        stops = notion_sync.fetch_stops(log=log)
        if stops:
            with open(STOPS_FILE, "w", encoding="utf-8") as f:
                json.dump(stops, f, indent=2, sort_keys=True)
            return stops
    except Exception as error:
        log(f"  Notion unavailable ({error}); using cached stops")

    if not os.path.exists(STOPS_FILE):
        return {}
    try:
        with open(STOPS_FILE, encoding="utf-8") as f:
            return {k.upper(): float(v) for k, v in json.load(f).items() if v}
    except Exception:
        return {}


def terminal_context(ticker):
    """RS rating and stage from the terminal's own files, if it knows the name."""
    out = {}
    path = os.path.join(config.ticker_dir("US"), f"{ticker}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            ratings = [v for v in (data.get("rs") or []) if v is not None]
            if ratings:
                out["rs"] = ratings[-1]
        except Exception:
            pass

    classification = os.path.join(config.data_dir("US"), "classification.json")
    if os.path.exists(classification):
        try:
            with open(classification, encoding="utf-8") as f:
                tags = json.load(f).get(ticker)
            if tags:
                out["sector"], out["industry"] = tags[0], tags[1]
        except Exception:
            pass

    trajectory = os.path.join(config.docs_dir("US"), "tmle", f"{ticker}.json")
    if os.path.exists(trajectory):
        try:
            with open(trajectory, encoding="utf-8") as f:
                data = json.load(f)
            stages = [s for s in (data.get("stage_label") or []) if s]
            if stages:
                out["stage"] = stages[-1]
        except Exception:
            pass
    return out


def build(data, stops):
    """Positions with everything derived, plus account-level totals."""
    nav_rows = data["nav"]
    nav = nav_rows[-1]["total"] if nav_rows else None
    prior = nav_rows[-2]["total"] if len(nav_rows) > 1 else None

    rows = []
    for position in data["positions"]:
        fx = position.get("fx_to_base") or 1.0
        value_base = (position.get("value") or 0) * fx
        cost_base = (position.get("cost_money") or 0) * fx
        row = dict(position)
        row["value_base"] = value_base
        row["cost_base"] = cost_base
        row["pct_nav"] = (value_base / nav) if nav else None
        row["unrealized_base"] = (position.get("unrealized") or 0) * fx
        row["unrealized_pct"] = (
            (position["mark"] / position["cost_price"] - 1)
            if position.get("mark") and position.get("cost_price") else None)

        stop = stops.get((position.get("symbol") or "").upper())
        row["stop"] = stop
        if stop and position.get("mark") and position.get("quantity"):
            # Risk is measured from the CURRENT price, not the entry: what a
            # stop actually costs you today is what matters for sizing, and on
            # a winner that is often nothing at all.
            per_share = position["mark"] - stop
            row["risk_base"] = max(per_share, 0) * position["quantity"] * fx
            row["risk_pct_nav"] = (row["risk_base"] / nav) if nav else None
            row["stop_distance"] = per_share / position["mark"]
        else:
            row["risk_base"] = None
            row["risk_pct_nav"] = None
            row["stop_distance"] = None

        row.update(terminal_context(position.get("symbol") or ""))
        rows.append(row)

    rows.sort(key=lambda r: -(r["value_base"] or 0))

    invested = sum(r["value_base"] or 0 for r in rows)
    heat = sum(r["risk_base"] for r in rows if r["risk_base"] is not None)
    missing_stops = [r["symbol"] for r in rows if r["stop"] is None]

    return {
        "as_of": data["as_of"],
        "nav": nav,
        "prior_nav": prior,
        "invested": invested,
        "invested_pct": (invested / nav) if nav else None,
        "cash": (nav - invested) if nav is not None else None,
        "heat": heat,
        "heat_pct": (heat / nav) if nav else None,
        "missing_stops": missing_stops,
        "largest_pct": max((r["pct_nav"] or 0) for r in rows) if rows else 0,
        "positions": rows,
    }


def money(value):
    return "—" if value is None else f"{value:,.2f}"


def pct(value, places=1):
    return "—" if value is None else f"{value * 100:.{places}f}%"


def render(view):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    change = None
    if view["nav"] is not None and view["prior_nav"]:
        change = view["nav"] / view["prior_nav"] - 1

    def cls(value):
        return "up" if (value or 0) > 0 else ("down" if (value or 0) < 0 else "")

    position_rows = []
    for r in view["positions"]:
        stop_cell = (f"{r['stop']:,.2f}" if r["stop"] is not None
                     else '<span class="warn">not set</span>')
        risk_cell = (f"{money(r['risk_base'])} <span class=\"dim\">"
                     f"({pct(r['risk_pct_nav'])})</span>"
                     if r["risk_base"] is not None
                     else '<span class="warn">—</span>')
        position_rows.append(f"""
      <tr>
        <td><b>{r['symbol']}</b><div class="dim">{r.get('industry') or r.get('description') or ''}</div></td>
        <td class="num">{r['quantity']:,.0f}</td>
        <td class="num">{money(r['mark'])}</td>
        <td class="num">{money(r['cost_price'])}</td>
        <td class="num">{money(r['value_base'])}</td>
        <td class="num">{pct(r['pct_nav'])}</td>
        <td class="num {cls(r['unrealized_base'])}">{money(r['unrealized_base'])}
            <div class="dim">{pct(r['unrealized_pct'])}</div></td>
        <td class="num">{stop_cell}</td>
        <td class="num">{risk_cell}</td>
        <td class="num">{r.get('rs') if r.get('rs') is not None else '—'}</td>
        <td>{r.get('stage') or '—'}</td>
      </tr>""")

    warnings = []
    if view["missing_stops"]:
        warnings.append(
            f"No stop recorded for {', '.join(view['missing_stops'])} — "
            f"portfolio heat below excludes {'it' if len(view['missing_stops']) == 1 else 'them'}, "
            f"so the real figure is higher.")
    if view["largest_pct"] > CONCENTRATION_WARN:
        warnings.append(
            f"Largest position is {pct(view['largest_pct'])} of NAV.")

    warning_html = ("".join(f'<div class="warn-line">{w}</div>' for w in warnings)
                    if warnings else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio — local only</title>
<style>
  :root {{
    --bg:#f5f6f8; --panel:#fff; --text:#1a1d24; --dim:#6b7280; --line:#e2e5ea;
    --up:#16a34a; --down:#dc2626; --warn:#ca8a04;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#10131a; --panel:#171b24; --text:#e7e9ee; --dim:#9096a3;
             --line:#262b36; --up:#2ecc71; --down:#f0554b; --warn:#facc15; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font-size:14px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  main {{ padding:24px; max-width:1500px; margin:0 auto; }}
  h1 {{ font-size:19px; margin:0 0 2px; }}
  .sub {{ color:var(--dim); font-size:12px; margin-bottom:20px; }}
  .private {{ display:inline-block; font-size:11px; padding:2px 8px; border-radius:999px;
    background:color-mix(in srgb, var(--warn) 18%, transparent); color:var(--warn);
    margin-left:8px; text-transform:uppercase; letter-spacing:.04em; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:14px; margin-bottom:20px; }}
  .stat {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; }}
  .stat-label {{ font-size:11px; color:var(--dim); text-transform:uppercase;
    letter-spacing:.04em; }}
  .stat-value {{ font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; }}
  .stat-sub {{ font-size:11px; color:var(--dim); }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line);
    vertical-align:top; }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em;
    color:var(--dim); font-weight:600; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  tr:last-child td {{ border-bottom:none; }}
  .dim {{ color:var(--dim); font-size:11px; }}
  .up {{ color:var(--up); }} .down {{ color:var(--down); }}
  .warn {{ color:var(--warn); }}
  .warn-line {{ background:color-mix(in srgb, var(--warn) 12%, transparent);
    border-left:3px solid var(--warn); padding:8px 12px; margin-bottom:8px;
    font-size:13px; border-radius:0 6px 6px 0; }}
  footer {{ margin-top:22px; color:var(--dim); font-size:11px; }}
</style></head><body><main>

<h1>Portfolio<span class="private">local only</span></h1>
<div class="sub">IBKR {view['as_of'] or '—'} · generated {generated} · base currency ·
  never published, never committed</div>

<div class="stats">
  <div class="stat"><div class="stat-label">NAV</div>
    <div class="stat-value">{money(view['nav'])}</div>
    <div class="stat-sub {cls(change)}">{pct(change, 2) if change is not None else '&nbsp;'} vs prior</div></div>
  <div class="stat"><div class="stat-label">Invested</div>
    <div class="stat-value">{pct(view['invested_pct'])}</div>
    <div class="stat-sub">{money(view['invested'])}</div></div>
  <div class="stat"><div class="stat-label">Cash</div>
    <div class="stat-value">{money(view['cash'])}</div>
    <div class="stat-sub">{pct(1 - (view['invested_pct'] or 0))} of NAV</div></div>
  <div class="stat"><div class="stat-label">Portfolio heat</div>
    <div class="stat-value">{pct(view['heat_pct'])}</div>
    <div class="stat-sub">{money(view['heat'])} at risk to stops</div></div>
  <div class="stat"><div class="stat-label">Positions</div>
    <div class="stat-value">{len(view['positions'])}</div>
    <div class="stat-sub">largest {pct(view['largest_pct'])}</div></div>
</div>

{warning_html}

<table>
  <thead><tr>
    <th>Position</th><th class="num">Qty</th><th class="num">Mark</th>
    <th class="num">Cost</th><th class="num">Value</th><th class="num">% NAV</th>
    <th class="num">Unrealized</th><th class="num">Stop</th>
    <th class="num">Risk</th><th class="num">RS</th><th>Stage</th>
  </tr></thead>
  <tbody>{''.join(position_rows) or '<tr><td colspan="11">No open positions.</td></tr>'}</tbody>
</table>

<footer>
  Risk is measured from the current mark to your stop, not from entry — what the
  stop would actually cost you today. Stops come from <b>Initial Stop $</b> in
  the USA Trading Log, cached to <code>stops.json</code> in this folder; IBKR
  cannot supply them, because Flex reports what executed and never what is
  resting.
</footer>

</main></body></html>"""


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    log("fetching from IBKR Flex...")
    xml_text = ibkr_flex.fetch_statement(log=log)
    data = ibkr_flex.parse(xml_text)

    # Keep the raw statement locally so a bad parse can be diagnosed without
    # another round trip, and so history accumulates outside the repo.
    stamp = data["as_of"] or datetime.now().strftime("%Y%m%d")
    raw_dir = os.path.join(OUTPUT_DIR, "statements")
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, f"{stamp}.xml"), "w", encoding="utf-8") as f:
        f.write(xml_text)

    stops = load_stops()
    if not stops:
        log("  no stops available — record 'Initial Stop $' in the USA "
            "Trading Log, or write them into " + STOPS_FILE)

    view = build(data, stops)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(render(view))

    if "--notion" in sys.argv:
        log("pushing to Notion...")
        import notion_sync
        notion_sync.push(view, data, log=log)

    log(f"\n  NAV {money(view['nav'])} · invested {pct(view['invested_pct'])} · "
        f"{len(view['positions'])} positions · heat {pct(view['heat_pct'])}")
    log(f"  written to {PAGE}")
    webbrowser.open(f"file:///{PAGE.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
