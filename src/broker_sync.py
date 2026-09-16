"""
broker_sync.py — log trades into Notion straight from what the broker did,
so nothing about a trade's objective facts is ever typed by hand. The only
things left for a human are the chart and the entry thesis — the two fields
no API can answer.

TWO BROKERS, TWO DIFFERENT TRIGGERS
-----------------------------------------------------------------------------
IBKR (NG-IBKR)     Read via ibkr_flex.py's Flex report — real fills (Trades
                   section, added 2026-09-12), so both opens AND closes get
                   real prices and dates, the same FIFO leg logic as Angel
                   One below. Flex needs no session and no login, so this
                   runs unattended, daily, forever. The one gap: a position
                   opened before the Trades section existed, or older than
                   the report's history window, has no fill in the report at
                   all — those fall back to being logged from the open
                   position alone if it carries a real open date, or into
                   "needs_date" for a one-field human confirm if it doesn't.

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

ONLY THE MOST RECENT ROUND TRIP PER TICKER EVER GETS TOUCHED
-----------------------------------------------------------------------------
A report can carry more than one full round trip for the same ticker (e.g.
MU closed in August, then reopened in September, both inside one 30-day Flex
window). Only the last of those legs is ever live-relevant -- anything
earlier already has its own Notion row from before this system existed, or
from an earlier day's sync. Matching every leg against "the account's one
open row for this ticker" (as an earlier version of this file did) breaks
the moment a ticker has more than one leg in the window: the first, already-
resolved leg would grab the wrong row and close it with stale data, then a
duplicate row would get created for the leg that's actually still open --
and since older legs stay inside the window for weeks, this replayed on
*every single sync*, corrupting the same ticker daily. Only ever looking at
legs[-1] makes a rerun a no-op once a ticker's current state is logged.

_RECONCILE_LOCK exists because the exact same corruption can happen from a
single run, too: a manual "Sync from brokers" click racing the automatic
24h background sync, both reading Notion's open rows before either has
written, both trying to resolve the same leg.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notion_sync

_RECONCILE_LOCK = threading.Lock()


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
    """Real fills (the Flex report's Trades section) drive both new opens and
    closes, via the same FIFO leg logic as Angel One below. A position that
    predates the Trades section, or falls outside the report's history
    window, has no fill to read -- those fall back to the open position
    itself: logged straight from it if a real open date is present, or into
    `needs_date` for a one-field human confirm if not. Positions that
    vanished with no matching close fill are reported as `possibly_closed`,
    never guessed at."""
    import ibkr_flex

    account = next(a for a in notion_sync.TRADE_DATABASES if a["label"] == "NG-IBKR")
    result = {"account": "NG-IBKR", "created": [], "closed": [],
              "needs_date": [], "possibly_closed": [], "error": None}
    try:
        statement = ibkr_flex.parse(ibkr_flex.fetch_statement(log=log))
    except Exception as error:
        log(f"  IBKR reconcile skipped: {error}")
        result["error"] = str(error)
        return result

    with _RECONCILE_LOCK:
        open_notion = _open_rows(account["id"])
        current_positions = {}
        for pos in statement.get("positions", []):
            ticker = (pos.get("symbol") or "").upper()
            if ticker and pos.get("quantity"):
                current_positions[ticker] = pos

        fills = [{
            "symbol": (t.get("symbol") or "").upper(),
            # IBKR's own quantity is already signed (negative on a sell) --
            # unlike Angel One's tradebook, where quantity is always positive
            # and side alone carries direction. _legs_from_fills expects the
            # latter shape (it applies its own sign from `side`), so a raw
            # IBKR quantity here would double-flip the sign on every sell.
            "quantity": abs(t["quantity"]) if t.get("quantity") is not None else None,
            "price": t.get("price"),
            "side": "BUY" if t.get("side") == "BUY" else "SELL",
            "datetime": t.get("datetime"),
        } for t in statement.get("trades", [])]

        handled = set()  # tickers whose open/close state came from a real fill
        for symbol, legs in _legs_from_fills(fills).items():
            if not legs:
                continue
            leg = legs[-1]  # only the current round trip is live-relevant --
                             # see the module docstring for why earlier legs
                             # must never be reprocessed.
            existing = open_notion.get(symbol)
            opened = (leg["opened"] or "")[:10]
            if leg["open"]:
                handled.add(symbol)
                if existing or not opened:
                    continue
                created = notion_sync.create_trade(
                    account, symbol, opened, leg["entryPrice"], leg["shares"], log=log)
                result["created"].append({"ticker": symbol, **created})
            elif existing:
                closed = (leg["closed"] or "")[:10]
                if not closed or leg["exitPrice"] is None:
                    continue
                handled.add(symbol)
                notion_sync.close_trade(existing["id"], closed, leg["exitPrice"], log=log)
                result["closed"].append({
                    "ticker": symbol, "pageId": existing["id"],
                    "notionUrl": existing.get("url"), "exitPrice": leg["exitPrice"],
                    "dateClosed": closed})

        for ticker, pos in current_positions.items():
            if ticker in open_notion or ticker in handled:
                continue
            opened = (pos.get("opened") or "")[:10]
            if opened and pos.get("cost_price") is not None:
                created = notion_sync.create_trade(
                    account, ticker, opened, pos["cost_price"], pos["quantity"], log=log)
                result["created"].append({"ticker": ticker, **created})
            else:
                log(f"  IBKR: {ticker} has no open date from Flex, needs a manual date")
                result["needs_date"].append({
                    "ticker": ticker, "entryPrice": pos.get("cost_price"),
                    "shares": pos["quantity"]})

        result["possibly_closed"] = [
            {"ticker": t, "pageId": row["id"], "notionUrl": row.get("url")}
            for t, row in open_notion.items()
            if t not in current_positions and t not in handled]
        return result


def _legs_from_fills(fills):
    """Fills, oldest first, folded into round-trip legs per symbol: a leg
    starts the moment a flat position takes on size and ends the moment it
    returns to flat. A pyramid add or a partial trim stays inside the same
    leg — its entry/exit price is the quantity-weighted average of the buys
    and sells that make it up, same as Cost Value already averages entries
    in Notion's own formulas. Shared by both brokers -- IBKR's Flex trades
    and Angel One's tradebook fills are normalized to the same shape
    ({symbol, quantity, price, side, datetime}) before reaching this."""
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

    with _RECONCILE_LOCK:
        legs_by_symbol = _legs_from_fills(fills)
        open_notion = _open_rows(account["id"])

        for symbol, legs in legs_by_symbol.items():
            if not legs:
                continue
            leg = legs[-1]  # only the current round trip is live-relevant --
                             # see the module docstring for why earlier legs
                             # must never be reprocessed.
            existing = open_notion.get(symbol)
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
