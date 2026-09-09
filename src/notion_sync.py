"""
notion_sync.py — move portfolio data between IBKR, this terminal, and Notion.

WHY NOTION IS THE DESTINATION AND NOT A WEB HOST
---------------------------------------------------------------------------
Positions and account equity must never sit on a public host. Notion is a
private workspace Neil already pays for and already reads on every device, so
pushing the numbers there gives phone and tablet access without publishing
anything. The alternative — serving the local HTML page over the internet —
would put the same data somewhere addressable by a URL, which is the thing we
are avoiding.

WHAT MOVES IN EACH DIRECTION
---------------------------------------------------------------------------
  Notion -> here   stops. The USA Trading Log carries "Initial Stop $" per
                   trade. That is the only record of intended risk: IBKR Flex
                   reports what executed, never what is resting, so a stop
                   order sitting at the broker is invisible to every API it
                   offers. Notion is therefore authoritative, not a copy.

  here -> Notion   the position snapshot and one NAV row per day. Positions
                   MIRROR the broker, so rows for names no longer held are
                   archived rather than left to rot. NAV ACCUMULATES, one row
                   per report date, so an equity and exposure history builds
                   up next to the trade log it should be read against.

The token is an internal Notion integration, kept in .env alongside the FMP
and IBKR credentials. .env is gitignored. Nothing here writes into data/ or
docs/, the two trees the pipeline commits and publishes.

Usage:
    python src/notion_sync.py        # stops in, positions and NAV out
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

API = "https://api.notion.com/v1"

# Pinned deliberately. Notion's newer versions reshape databases into
# "data sources", which would change every request here for no gain.
NOTION_VERSION = "2022-06-28"

TRADES_DB = "39d4788c-7a99-8048-a632-eb09eaa6e3a4"      # USA Trading Log
POSITIONS_DB = "476e0f4e-5033-4f9d-8144-ebb77c8b2f8f"   # IBKR Positions
NAV_DB = "fb99ad0c-c723-45d5-b92b-a35770f512c2"         # IBKR NAV History

STOP_FIELD = "Initial Stop $"

# Every trading-log database the Trade Journal panel reads, one account per
# entry. Deliberately a list rather than one hardcoded ID -- (NG-IBKR) is the
# only one confirmed shared with the integration so far; adding the two
# Angel One logs later is appending a row here, nothing else changes.
TRADE_DATABASES = [
    {"label": "IBKR (NG)", "id": TRADES_DB},
]

# Read (and, for the four below, written back) per trade. Formula fields are
# deliberately excluded -- Cost Value, PnL, PnL%, Hold Days, Win, Outcome,
# and Initial Stop % all compute themselves in Notion from these, so there
# is nothing to sync for them, only to read for display.
TRADE_TEXT_FIELD = "Entry Thesis"
TRADE_SELECT_FIELDS = ["Entry Setup", "Exit Setup", "Buy Quality", "Sell Quality"]
TRADE_FORMULA_FIELDS = ["Cost Value", "PnL", "PnL%", "Hold Days", "Win",
                        "Outcome", "Initial Stop %"]


class NotionError(RuntimeError):
    pass


def token():
    """Integration token from the environment, falling back to .env."""
    value = os.environ.get("NOTION_TOKEN")
    if value:
        return value

    path = os.path.join(config.ROOT_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, raw = line.partition("=")
                if key.strip() == "NOTION_TOKEN":
                    value = raw.strip().strip('"').strip("'")
                    if value:
                        return value
    raise NotionError(
        "NOTION_TOKEN is not set — add it to .env "
        "(create the integration at notion.so/my-integrations)")


def _call(method, path, body=None):
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token()}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        # The two failures worth naming, because both have a specific fix and
        # the raw API message ("Could not find database") does not say so.
        if error.code == 404:
            raise NotionError(
                f"{path} not found — the database exists but has not been "
                f"shared with the integration. Open it in Notion, ... menu -> "
                f"Connections -> add your integration.") from None
        if error.code == 401:
            raise NotionError("NOTION_TOKEN was rejected") from None
        raise NotionError(f"{method} {path} failed [{error.code}]: "
                          f"{detail[:300]}") from None


def query(database_id, body=None):
    """Every row of a database, following pagination."""
    rows, cursor = [], None
    while True:
        payload = dict(body or {})
        payload["page_size"] = 100
        if cursor:
            payload["start_cursor"] = cursor
        page = _call("POST", f"/databases/{database_id}/query", payload)
        rows.extend(page.get("results", []))
        if not page.get("has_more"):
            return rows
        cursor = page.get("next_cursor")


# --- reading Notion property values ----------------------------------------

def _text_of(prop):
    if not prop:
        return None
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip() or None


def _number_of(prop):
    return prop.get("number") if prop else None


def _select_of(prop):
    if not prop:
        return None
    value = prop.get("select")
    return value.get("name") if value else None


def _date_of(prop):
    if not prop:
        return None
    value = prop.get("date")
    return value.get("start") if value else None


def _formula_of(prop):
    """Formula properties nest the actual value one level deeper, under
    whichever type the formula happens to resolve to -- Cost Value resolves
    to a number, Outcome to a string, etc. Read whichever is present."""
    if not prop:
        return None
    value = prop.get("formula") or {}
    for kind in ("number", "string", "boolean", "date"):
        if kind in value and value[kind] is not None:
            return value[kind].get("start") if kind == "date" else value[kind]
    return None


# --- writing Notion property values ----------------------------------------

def title(value):
    return {"title": [{"text": {"content": str(value)[:2000]}}]}


def text(value):
    if value is None:
        return {"rich_text": []}
    return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}


def number(value):
    return {"number": None if value is None else round(float(value), 6)}


def date(value):
    return {"date": {"start": value} if value else None}


def select(value):
    return {"select": {"name": str(value)} if value else None}


# --- stops: Notion -> here -------------------------------------------------

def fetch_stops(log=print):
    """{ticker: stop} for every trade with no close date.

    A trade without a stop recorded is omitted rather than defaulted, so the
    portfolio panel can say "not set" instead of quietly inventing risk.
    """
    rows = query(TRADES_DB, {
        "filter": {"property": "Date Closed", "date": {"is_empty": True}}})

    stops, missing = {}, []
    for row in rows:
        properties = row.get("properties", {})
        ticker = _text_of(properties.get("Ticker"))
        if not ticker:
            continue
        stop = _number_of(properties.get(STOP_FIELD))
        if stop:
            stops[ticker.upper()] = float(stop)
        else:
            missing.append(ticker.upper())

    log(f"  stops from Notion: {len(stops)} of {len(rows)} open trades")
    if missing:
        log(f"  no {STOP_FIELD} recorded for {', '.join(sorted(missing))}")
    return stops


# --- Trade Journal: full read/write, both directions -----------------------

def fetch_database_schema(database_id):
    """{property_name: [option names]} for every select/multi_select property
    -- read live off the database itself, never hand-typed, so a preset you
    add in Notion (or here) shows up the moment either side reloads."""
    db = _call("GET", f"/databases/{database_id}")
    options = {}
    for name, spec in db.get("properties", {}).items():
        ptype = spec.get("type")
        if ptype in ("select", "multi_select"):
            options[name] = [o["name"] for o in spec.get(ptype, {}).get("options", [])]
    return options


def _trade_from_page(page, account_label):
    p = page.get("properties", {})
    return {
        "pageId": page["id"],
        "notionUrl": page.get("url"),
        "account": account_label,
        "ticker": _text_of(p.get("Ticker")),
        "dateOpened": _date_of(p.get("Date Opened")),
        "dateClosed": _date_of(p.get("Date Closed")),
        "entryPrice": _number_of(p.get("Entry Price")),
        "exitPrice": _number_of(p.get("Exit Price")),
        "shares": _number_of(p.get("Shares")),
        "initialStop": _number_of(p.get(STOP_FIELD)),
        "entrySetup": _select_of(p.get("Entry Setup")),
        "exitSetup": _select_of(p.get("Exit Setup")),
        "buyQuality": _select_of(p.get("Buy Quality")),
        "sellQuality": _select_of(p.get("Sell Quality")),
        "entryThesis": _text_of(p.get(TRADE_TEXT_FIELD)),
        "costValue": _formula_of(p.get("Cost Value")),
        "initialStopPct": _formula_of(p.get("Initial Stop %")),
        "pnl": _formula_of(p.get("PnL")),
        "pnlPct": _formula_of(p.get("PnL%")),
        "holdDays": _formula_of(p.get("Hold Days")),
        "outcome": _formula_of(p.get("Outcome")),
    }


def fetch_trades(log=print):
    """Every trade, every configured account, open and closed -- no filter.
    This is the one-time-per-load read that seeds the whole Trade Journal
    panel; unlike fetch_stops() it does not narrow to open positions, because
    closed trades are exactly what the completeness filters and any future
    backtest need to see."""
    all_trades = []
    for entry in TRADE_DATABASES:
        try:
            pages = query(entry["id"])
        except NotionError as error:
            log(f"  {entry['label']}: {error}")
            continue
        for page in pages:
            all_trades.append(_trade_from_page(page, entry["label"]))
        log(f"  {entry['label']}: {len(pages)} trades")
    return all_trades


def update_trade(page_id, fields, log=print):
    """Write one or more editable properties back onto a single trade page.
    `fields` is {property_name: raw_value} -- select/text distinguished by
    which property name it targets, since that's already fixed per field."""
    properties = {}
    for name, value in fields.items():
        if name in TRADE_SELECT_FIELDS:
            properties[name] = select(value)
        elif name == TRADE_TEXT_FIELD:
            properties[name] = text(value)
        else:
            raise NotionError(f"{name!r} is not an editable trade field")
    _call("PATCH", f"/pages/{page_id}", {"properties": properties})
    log(f"  updated {', '.join(fields)} on {page_id}")


# --- positions and NAV: here -> Notion -------------------------------------

def _index_by_title(database_id):
    """{title: page_id} for the rows already there, so we patch not duplicate."""
    index = {}
    for row in query(database_id):
        for prop in row.get("properties", {}).values():
            if prop.get("type") == "title":
                key = _text_of(prop)
                if key:
                    index[key.upper()] = row["id"]
                break
    return index


def _write(database_id, key, properties, existing):
    page_id = existing.get(key.upper())
    if page_id:
        _call("PATCH", f"/pages/{page_id}", {"properties": properties})
        return False
    _call("POST", "/pages", {"parent": {"database_id": database_id},
                             "properties": properties})
    return True


def push_positions(view, log=print):
    """Mirror the current holdings, archiving rows for names no longer held."""
    existing = _index_by_title(POSITIONS_DB)
    as_of = view["as_of"]
    held = set()

    for row in view["positions"]:
        ticker = (row.get("symbol") or "").upper()
        if not ticker:
            continue
        held.add(ticker)
        _write(POSITIONS_DB, ticker, {
            "Ticker": title(ticker),
            "As Of": date(as_of),
            "Account": text(row.get("account")),
            "Conid": text(row.get("conid")),
            "Exchange": text(row.get("exchange")),
            "Currency": select(row.get("currency")),
            "Quantity": number(row.get("quantity")),
            "Mark Price": number(row.get("mark")),
            "Cost Price": number(row.get("cost_price")),
            "Value (base)": number(row.get("value_base")),
            "% of NAV": number(row.get("pct_nav")),
            "Unrealized P/L": number(row.get("unrealized_base")),
            "Unrealized %": number(row.get("unrealized_pct")),
            "Stop": number(row.get("stop")),
            "Risk (base)": number(row.get("risk_base")),
            "Risk % NAV": number(row.get("risk_pct_nav")),
            "Stop Distance": number(row.get("stop_distance")),
            "RS": number(row.get("rs")),
            "Stage": text(row.get("stage")),
            "Industry": text(row.get("industry")),
        }, existing)

    # This table is a mirror of the broker, so a row for something that is no
    # longer held is wrong rather than historical. Archiving puts it in
    # Notion's trash, where it can be restored; the trade log keeps the record.
    closed = [t for t in existing if t not in held]
    for ticker in closed:
        _call("PATCH", f"/pages/{existing[ticker]}", {"archived": True})

    log(f"  positions: {len(held)} written"
        + (f", {len(closed)} archived ({', '.join(sorted(closed))})"
           if closed else ""))


def push_nav(view, data, log=print):
    """One row per report date. Re-running the same day updates it in place."""
    existing = _index_by_title(NAV_DB)

    # Cash-report totals for the base currency, if the statement carries them.
    deposits = withdrawals = settled = None
    for row in data.get("cash", []):
        if (row.get("level") or "").lower() in ("baseCurrency".lower(), "base"):
            deposits, withdrawals = row.get("deposits"), row.get("withdrawals")
            settled = row.get("ending_settled")
            break

    written = 0
    for row in data.get("nav", []):
        stamp = row.get("date")
        if not stamp:
            continue
        latest = stamp == view["as_of"]
        properties = {
            "Date": title(stamp),
            "Account": text(row.get("account")),
            "NAV": number(row.get("total")),
            "Cash": number(row.get("cash")),
            "Stock": number(row.get("stock")),
        }
        # Position-derived figures only describe the snapshot we actually hold
        # positions for. Stamping them on every historical NAV row would be a
        # fabrication — those days had different holdings.
        if latest:
            properties.update({
                "Invested %": number(view["invested_pct"]),
                "Open Positions": number(len(view["positions"])),
                "Largest Position %": number(view["largest_pct"]),
                "Portfolio Heat %": number(view["heat_pct"]),
                "Heat (base)": number(view["heat"]),
                "Stops Missing": number(len(view["missing_stops"])),
                "Settled Cash": number(settled),
                "Deposits": number(deposits),
                "Withdrawals": number(withdrawals),
            })
        _write(NAV_DB, stamp, properties, existing)
        written += 1

    log(f"  NAV history: {written} dated rows")


def push(view, data, log=print):
    push_positions(view, log=log)
    push_nav(view, data, log=log)


if __name__ == "__main__":
    import portfolio_local

    def log(msg):
        print(msg, flush=True)

    log("reading stops from Notion...")
    stops = fetch_stops(log=log)

    log("fetching from IBKR Flex...")
    import ibkr_flex
    data = ibkr_flex.parse(ibkr_flex.fetch_statement(log=log))
    view = portfolio_local.build(data, stops)

    log("pushing to Notion...")
    push(view, data, log=log)
    log(f"\n  NAV {view['nav']:,.2f} · invested "
        f"{(view['invested_pct'] or 0) * 100:.1f}% · heat "
        f"{(view['heat_pct'] or 0) * 100:.1f}%")
