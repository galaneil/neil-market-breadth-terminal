"""
broker_sync.py — log trades into Notion straight from what the broker did,
so nothing about a trade's objective facts is ever typed by hand. The only
things left for a human are the chart and the entry thesis — the two fields
no API can answer.

TWO BROKERS, TWO DIFFERENT LEVELS OF TRUTH RIGHT NOW
-----------------------------------------------------------------------------
IBKR (NG-IBKR)     Read via ibkr_flex.py's Flex report. Flex needs no session
                   and no login, so this runs unattended, daily, forever. But
                   the currently-configured Flex Query has no Trades section
                   — only OpenPosition, so a NEW position (with its real
                   entry price and open date) can be logged, but a CLOSE
                   cannot: a position vanishing between two reports says
                   nothing about what it sold for or exactly when. Rather
                   than guess, closes are only ever reported as
                   "possibly closed" for a human to confirm and enter. Fixing
                   this for good means adding "Trades" to the Flex Query on
                   IBKR's own site — no code change reaches that.

Angel One (ShG-AO, See TRADE_DATABASES.
SuG-AO)            Read via angelone.tradebook() — real fills, so both opens
                   AND closes get real prices and dates. The catch is
                   sessions: SmartAPI needs a PIN+TOTP login roughly once a
                   day, so this only runs when a session already exists
                   (i.e. right after someone logs into Live Portfolio), not
                   on an unattended clock — see angelone.py's own docstring
                   for why that trade-off is deliberate, not an oversight.

MATCHING RULE, BOTH BROKERS
-----------------------------------------------------------------------------
A broker position/fill is matched to Notion by ticker within one account's
log, against rows with an empty Date Closed. Nothing here ever touches a row
that already has a Date Closed, and nothing here ever writes Entry Setup,
Exit Setup, Buy/Sell Quality, Entry Thesis, or a chart — those stay entirely
the human's.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notion_sync


def _open_rows(database_id):
    """{TICKER: notion page} for every row with no Date Closed."""
    pages = notion_sync.query(database_id, {
        "filter": {"property": "Date Closed", "date": {"is_empty": True}}})
    out = {}
    for page in pages:
        ticker = notion_sync._text_of(page.get("properties", {}).get("Ticker"))
        if ticker:
            out[ticker.upper()] = page
    return out


def reconcile_ibkr(log=print):
    """New IBKR positions become new Notion rows automatically ONLY when the
    Flex report actually carries a real open date for them. As currently
    configured it does not (openDateTime comes back blank on every position,
    verified against a live statement) -- so a new position instead comes
    back in `needs_date`, pre-filled with everything the report DOES know
    (ticker, entry price, shares), for a one-field confirm rather than either
    a fabricated date or full manual entry. Positions that vanished are
    reported, never guessed at."""
    import ibkr_flex

    account = next(a for a in notion_sync.TRADE_DATABASES if a["label"] == "NG-IBKR")
    result = {"account": "NG-IBKR", "created": [], "needs_date": [],
              "possibly_closed": [], "error": None}
    try:
        statement = ibkr_flex.parse(ibkr_flex.fetch_statement(log=log))
    except Exception as error:
        log(f"  IBKR reconcile skipped: {error}")
        result["error"] = str(error)
        return result

    open_notion = _open_rows(account["id"])
    seen = set()
    for pos in statement.get("positions", []):
        ticker = (pos.get("symbol") or "").upper()
        qty = pos.get("quantity")
        if not ticker or not qty:
            continue
        seen.add(ticker)
        if ticker in open_notion:
            continue
        opened = (pos.get("opened") or "")[:10]
        if not opened or pos.get("cost_price") is None:
            log(f"  IBKR: {ticker} has no open date from Flex, needs a manual date")
            result["needs_date"].append({
                "ticker": ticker, "entryPrice": pos.get("cost_price"), "shares": qty})
            continue
        created = notion_sync.create_trade(
            account, ticker, opened, pos["cost_price"], qty, log=log)
        result["created"].append({"ticker": ticker, **created})

    result["possibly_closed"] = [
        {"ticker": t, "pageId": row["id"], "notionUrl": row.get("url")}
        for t, row in open_notion.items() if t not in seen]
    return result


def _angelone_legs(fills):
    """Fills, oldest first, folded into round-trip legs per symbol: a leg
    starts the moment a flat position takes on size and ends the moment it
    returns to flat. A pyramid add or a partial trim stays inside the same
    leg — its entry/exit price is the quantity-weighted average of the buys
    and sells that make it up, same as Cost Value already averages entries
    in Notion's own formulas."""
    fills = [f for f in fills if f.get("symbol") and f.get("quantity") and f.get("price")]
    fills.sort(key=lambda f: f.get("datetime") or "")

    legs = {}   # symbol -> list of legs; a leg is {qty, buys:[(qty,price,dt)], sells:[...]}
    running = {}
    for f in fills:
        sym = f["symbol"]
        side = 1 if f["side"] == "BUY" else -1
        qty = f["quantity"] * side
        pos = running.get(sym, 0)
        leglist = legs.setdefault(sym, [])

        if pos == 0:
            leglist.append({"buys": [], "sells": []})
        leg = leglist[-1]
        (leg["buys"] if side > 0 else leg["sells"]).append(
            (f["quantity"], f["price"], f.get("datetime")))
        running[sym] = pos + qty

    out = {}
    for sym, leglist in legs.items():
        parsed = []
        for leg in leglist:
            buy_qty = sum(q for q, _, _ in leg["buys"])
            sell_qty = sum(q for q, _, _ in leg["sells"])
            if buy_qty <= 0:
                continue  # a leg that starts short — not this model, skip
            entry_price = sum(q * p for q, p, _ in leg["buys"]) / buy_qty
            opened = min((d for _, _, d in leg["buys"] if d), default=None)
            closed = exit_price = None
            if sell_qty >= buy_qty and leg["sells"]:
                exit_price = sum(q * p for q, p, _ in leg["sells"]) / sell_qty
                closed = max((d for _, _, d in leg["sells"] if d), default=None)
            parsed.append({
                "shares": buy_qty, "entryPrice": entry_price, "opened": opened,
                "closed": closed, "exitPrice": exit_price,
                "open": closed is None,
            })
        out[sym] = parsed
    return out


def reconcile_angelone(token, api_key, account_label, log=print):
    """New Angel One fills become new Notion rows or close existing ones,
    both with real prices — the trade book has full execution history, not
    just a snapshot. Requires an active session; call this right after a
    successful login rather than on an unattended clock."""
    from angelone import tradebook

    account = next((a for a in notion_sync.TRADE_DATABASES if a["label"] == account_label), None)
    result = {"account": account_label, "created": [], "closed": [], "error": None}
    if not account:
        result["error"] = f"no TRADE_DATABASES entry for {account_label!r}"
        return result

    try:
        fills = tradebook(token, api_key, log=log)
    except Exception as error:
        log(f"  {account_label} reconcile skipped: {error}")
        result["error"] = str(error)
        return result

    legs_by_symbol = _angelone_legs(fills)
    open_notion = _open_rows(account["id"])

    for symbol, legs in legs_by_symbol.items():
        existing = open_notion.get(symbol)
        for leg in legs:
            opened = (leg["opened"] or "")[:10]
            if leg["open"]:
                if existing or not opened:
                    continue  # already logged, or too little data to log
                created = notion_sync.create_trade(
                    account, symbol, opened, leg["entryPrice"], leg["shares"], log=log)
                result["created"].append({"ticker": symbol, **created})
            elif existing:
                closed = (leg["closed"] or "")[:10]
                if not closed or leg["exitPrice"] is None:
                    continue
                notion_sync.close_trade(existing["id"], closed, leg["exitPrice"], log=log)
                result["closed"].append({
                    "ticker": symbol, "pageId": existing["id"],
                    "notionUrl": existing.get("url"), "exitPrice": leg["exitPrice"],
                    "dateClosed": closed})
    return result
