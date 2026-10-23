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

EVERY ROUND TRIP IS MATCHED TO ITS OWN ROW
-----------------------------------------------------------------------------
A report can carry several full round trips for one ticker (DELL: bought
Sep 24, sold Oct 14, bought again Oct 15, sold Oct 16). Each round trip ("leg")
is matched to the Notion row with the same open date and entry price, never to
"the one open row for this ticker". An earlier version looked only at the
latest leg and applied it to whichever row was open, so when two legs landed in
one sync the second leg's buy became a "pyramid add" of the first position and
its exit closed that position at the wrong date and price (a -1.3% trade
showed as -14.4%).

The rules now are:
  * A leg that matches a row: an open row gets its adds (leg still open) or its
    close (leg closed). A row that already has a Date Closed is never touched.
  * A leg with no row is a NEW position and gets its own row (a pyramid add is
    only ever a buy inside a leg that was already open). Legs older than the
    newest row already logged for that ticker are history and are not
    resurrected; with no rows at all, only the latest leg is logged.
  * A position that predates the report window matches no leg by date; the
    old single-leg fallback applies only when exactly one leg exists.

_RECONCILE_LOCK exists because the exact same corruption can happen from a
single run, too: a manual "Sync from brokers" click racing the automatic
24h background sync, both reading Notion's open rows before either has
written, both trying to resolve the same leg.
"""

import os
import sys
import threading
from datetime import date as _date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notion_sync

_RECONCILE_LOCK = threading.Lock()


def _open_rows(database_id):
    """{TICKER: notion page} for every row with no Date Closed."""
    pages = notion_sync.query(database_id, {
        "filter": {"property": "Date Closed", "date": {"is_empty": True}}})
    out = {}
    for page in pages:
        props = page.get("properties", {})
        # A pyramid add shares its core's ticker; letting it into this map
        # would let it shadow the core row it hangs off.
        if notion_sync._relation_ids(props.get(notion_sync.PARENT_FIELD)):
            continue
        ticker = notion_sync._text_of(props.get("Ticker"))
        if ticker:
            out[ticker.upper()] = page
    return out


def _add_events(leg):
    """A leg's buys folded to one event per day: [{date, shares, price}],
    oldest first. Two fills of one order split across prices are one add."""
    by_day = {}
    for qty, price, dt in leg.get("buys", []):
        day = (dt or "")[:10]
        if not day:
            continue
        e = by_day.setdefault(day, [0, 0.0])
        e[0] += qty
        e[1] += qty * price
    return [{"date": d, "shares": q, "price": v / q} for d, (q, v) in sorted(by_day.items())]


def _sync_adds(account, symbol, core, leg, result, log):
    """Every buy after the core row's own open date becomes its own row,
    linked to the core (Parent Trade) so it can carry its own chart and
    thesis. Already-logged adds are matched by date, so a rerun is a no-op."""
    core_date = notion_sync._date_of(core.get("properties", {}).get("Date Opened")) or ""
    logged = set()
    for child in notion_sync.child_rows(account["id"], core["id"]):
        d = notion_sync._date_of(child.get("properties", {}).get("Date Opened"))
        if d:
            logged.add(d[:10])
    for ev in _add_events(leg):
        if ev["date"] <= core_date[:10] or ev["date"] in logged:
            continue
        created = notion_sync.create_trade(
            account, symbol, ev["date"], ev["price"], ev["shares"], log=log,
            parent_id=core["id"], entry_setup="Pyramid Add")
        result.setdefault("added", []).append({"ticker": symbol, "date": ev["date"],
                                               "shares": ev["shares"], **created})


def _core_rows(database_id, since):
    """Every core (non-add) row opened on/after `since`, open or closed, as
    {id, ticker, opened, closed, entry, page}."""
    pages = notion_sync.query(database_id, {
        "filter": {"property": "Date Opened", "date": {"on_or_after": since}}})
    out = []
    for page in pages:
        props = page.get("properties", {})
        if notion_sync._relation_ids(props.get(notion_sync.PARENT_FIELD)):
            continue
        out.append({"id": page["id"], "page": page,
                    "ticker": (notion_sync._text_of(props.get("Ticker")) or "").upper(),
                    "opened": (notion_sync._date_of(props.get("Date Opened")) or "")[:10],
                    "closed": (notion_sync._date_of(props.get("Date Closed")) or "")[:10] or None,
                    "entry": notion_sync._number_of(props.get("Entry Price"))})
    return out


def _days_apart(a, b):
    return abs((_date.fromisoformat(a) - _date.fromisoformat(b)).days)


def _row_gap(row, leg):
    """How far a row is from being this leg (None if it cannot be): same open
    date (a day of slack for time zones), and an entry price within 1% of the
    leg's first buy. Smaller is closer."""
    events = _add_events(leg)
    opened = (leg.get("opened") or "")[:10]
    if not row["opened"] or not opened or _days_apart(row["opened"], opened) > 1:
        return None
    if events and row["entry"]:
        gap = abs(row["entry"] - events[0]["price"]) / events[0]["price"]
        return gap if gap <= 0.01 else None
    return 0.0


def _assign(legs, rows):
    """{leg index: row}. Every row goes to at most one leg and every leg gets at
    most one row; the closest pairs are settled first, so two same-day round
    trips (a quick scalp, then a bigger buy) each find their own row."""
    pairs = sorted((g, i, k) for i, leg in enumerate(legs) for k, row in enumerate(rows)
                   if (g := _row_gap(row, leg)) is not None)
    out, used_rows = {}, set()
    for g, i, k in pairs:
        if i in out or k in used_rows:
            continue
        out[i] = rows[k]; used_rows.add(k)
    return out


def plan_leg_actions(legs, rows, open_row=None):
    """Decide what to do for one ticker. `legs` are its round trips, oldest
    first; `rows` its core rows from Notion (any state); `open_row` the single
    open row for the ticker, used only by the pre-window fallback. Returns a
    list of {"do": "adds" | "close" | "create", "leg": leg, "row": row-or-None}.
    Pure function so the rules can be tested without Notion."""
    actions = []
    matched = _assign(legs, rows)
    newest = max((r["opened"] for r in rows if r["opened"]), default=None)
    for i, leg in enumerate(legs):
        row = matched.get(i)
        opened = (leg.get("opened") or "")[:10]
        if row:
            if row["closed"]:
                continue                       # resolved already: never touched
            actions.append({"do": "adds" if leg["open"] else "close", "leg": leg, "row": row})
        elif (newest is None and i == len(legs) - 1) or (newest is not None and opened and opened > newest):
            actions.append({"do": "create", "leg": leg, "row": None})
            newest = opened if newest is None else max(newest, opened)
    if not matched and open_row is not None and len(legs) == 1:
        # Position opened before the report window: the legacy single-leg rule.
        leg = legs[0]
        actions = [{"do": "adds" if leg["open"] else "close", "leg": leg,
                    "row": {"id": open_row["id"], "page": open_row, "closed": None}}]
    return actions


def _already_logged(database_id, symbol, date_iso, price):
    """True when a row for this ticker, opened on this date (a day of slack) at this price (1%), already
    exists in ANY form: core or pyramid add, open or closed. Core rows are only looked at in a date window
    above, so a leg that is really an add on a position opened before the report window used to look like a
    brand-new trade and was created again, next to the add that was already there."""
    if not date_iso or not price:
        return False
    lo, hi = _days_before(date_iso, 1), _days_before(date_iso, -1)
    pages = notion_sync.query(database_id, {"filter": {"and": [
        {"property": "Date Opened", "date": {"on_or_after": lo}},
        {"property": "Date Opened", "date": {"on_or_before": hi}}]}})
    for page in pages:
        props = page.get("properties", {})
        if (notion_sync._text_of(props.get("Ticker")) or "").upper() != symbol.upper():
            continue
        entry = notion_sync._number_of(props.get("Entry Price"))
        if entry and abs(entry - price) / price <= 0.01:
            return True
    return False


def _apply_actions(account, symbol, actions, result, log):
    for act in actions:
        leg, row = act["leg"], act["row"]
        closed = (leg["closed"] or "")[:10]
        if act["do"] == "adds":
            _sync_adds(account, symbol, row["page"], leg, result, log)
        elif act["do"] == "close":
            if not closed or leg["exitPrice"] is None:
                continue
            notion_sync.close_trade(row["id"], closed, leg["exitPrice"], log=log)
            _close_children(account, row["page"], closed, leg["exitPrice"], log)
            result["closed"].append({"ticker": symbol, "pageId": row["id"],
                                     "notionUrl": row["page"].get("url"),
                                     "exitPrice": leg["exitPrice"], "dateClosed": closed})
        elif act["do"] == "create":
            events = _add_events(leg)
            first = events[0] if events else {"date": (leg["opened"] or "")[:10],
                                              "shares": leg["shares"], "price": leg["entryPrice"]}
            if not first["date"]:
                continue
            if _already_logged(account["id"], symbol, first["date"], first["price"]):
                log(f"  {symbol}: {first['date']} @ {first['price']} is already logged, not creating it again")
                continue
            created = notion_sync.create_trade(account, symbol, first["date"], first["price"],
                                               first["shares"], log=log)
            result["created"].append({"ticker": symbol, **created})
            core = {"id": created["pageId"], "url": created.get("notionUrl"),
                    "properties": {"Date Opened": {"type": "date", "date": {"start": first["date"]}}}}
            _sync_adds(account, symbol, core, leg, result, log)
            if closed and leg["exitPrice"] is not None:
                notion_sync.close_trade(created["pageId"], closed, leg["exitPrice"], log=log)
                _close_children(account, core, closed, leg["exitPrice"], log)
                result["closed"].append({"ticker": symbol, "pageId": created["pageId"],
                                         "notionUrl": created.get("notionUrl"),
                                         "exitPrice": leg["exitPrice"], "dateClosed": closed})


def _close_children(account, core, closed, exit_price, log):
    for child in notion_sync.child_rows(account["id"], core["id"]):
        if not notion_sync._date_of(child.get("properties", {}).get("Date Closed")):
            notion_sync.close_trade(child["id"], closed, exit_price, log=log)


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

    try:
        import stop_capture
        stop_capture.from_statement(statement, log=log)
    except Exception as error:
        log(f"  stop capture from Flex orders skipped: {error}")

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

        # IBKR's daily report stops at the last business day. With the gateway logged in, executions from the days after
        # it are added, so a trade you just made is caught the moment you press sync. Only days AFTER the report's last
        # day are taken from the gateway: the same day can be split into different fills in the two sources.
        result["live"] = {"used": False, "added": 0, "reportThrough": None, "reason": None}
        try:
            import ibkr_cp
            as_of = statement.get("as_of")
            through = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}" if as_of and len(as_of) == 8 else (as_of or "")[:10]
            result["live"]["reportThrough"] = through
            extra = [e for e in ibkr_cp.recent_executions(days=5) if e["datetime"][:10] > through]
            fills.extend({k: e[k] for k in ("symbol", "quantity", "price", "side", "datetime")} for e in extra)
            result["live"].update(used=True, added=len(extra))
            log(f"  IBKR live gateway: {len(extra)} executions after {through}")
        except Exception as error:
            result["live"]["reason"] = str(error)[:120]
            log(f"  IBKR live gateway not used: {error}")

        handled = set()  # tickers whose open/close state came from a real fill
        all_legs = _legs_from_fills(fills)
        stamps = [(l["opened"] or "")[:10] for ls in all_legs.values() for l in ls if l.get("opened")]
        rows = _core_rows(account["id"], _days_before(min(stamps), 3)) if stamps else []
        for symbol, legs in all_legs.items():
            if not legs:
                continue
            actions = plan_leg_actions(legs, [r for r in rows if r["ticker"] == symbol],
                                       open_notion.get(symbol))
            if legs[-1]["open"] or any(a["do"] in ("close", "create") for a in actions):
                handled.add(symbol)
            _apply_actions(account, symbol, actions, result, log)

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


def _days_before(iso, n):
    from datetime import timedelta
    return (_date.fromisoformat(iso) - timedelta(days=n)).isoformat()


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

        if side < 0 and pos <= 0:
            # A sell with nothing to sell means its buy fell outside the
            # report's window (Flex only looks back ~30 days). Counting it
            # drove the running position negative, so the next buys netted
            # it back to flat and looked like a closed round trip -- which
            # then closed a still-open row with a stale exit price.
            continue
        if pos == 0:
            leglist.append({"buys": [], "sells": []})
        leg = leglist[-1]
        (leg["buys"] if side > 0 else leg["sells"]).append(
            (f["quantity"], f["price"], f.get("datetime")))
        # A sell larger than what the report window shows us bought means the rest came from shares held before the
        # window. The position is flat after it, never negative: a negative balance swallowed the NEXT buy into the old
        # leg (TWLO: sold 17 against 4 seen, then a fresh buy of 10 looked like part of the closed trade, and its sale
        # was skipped as "nothing to sell").
        running[sym] = max(0, pos + qty)

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
                "shares": buy_qty, "sellShares": sell_qty, "entryPrice": entry_price, "opened": opened,
                "closed": closed, "exitPrice": exit_price,
                "open": closed is None, "buys": leg["buys"],
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
        stamps = [(l["opened"] or "")[:10] for ls in legs_by_symbol.values() for l in ls if l.get("opened")]
        rows = _core_rows(account["id"], _days_before(min(stamps), 3)) if stamps else []
        for symbol, legs in legs_by_symbol.items():
            if legs:
                _apply_actions(account, symbol, plan_leg_actions(
                    legs, [r for r in rows if r["ticker"] == symbol], open_notion.get(symbol)),
                    result, log)
        return result


def repair_ibkr_legs(apply=False, log=print):
    """Find and (with apply=True) fix NG-IBKR rows that an older sync tangled
    across round trips: a core row closed with a LATER leg's date and price, and
    a later position wrongly linked as a pyramid add of an earlier one.
    Returns the list of changes, one dict each. Nothing else is touched."""
    import ibkr_flex
    account = next(a for a in notion_sync.TRADE_DATABASES if a["label"] == "NG-IBKR")
    statement = ibkr_flex.parse(ibkr_flex.fetch_statement(log=lambda *a: None))
    fills = [{"symbol": (t.get("symbol") or "").upper(), "quantity": abs(t["quantity"]) if t.get("quantity") is not None else None,
              "price": t.get("price"), "side": "BUY" if t.get("side") == "BUY" else "SELL",
              "datetime": t.get("datetime")} for t in statement.get("trades", [])]
    legs_by_symbol = _legs_from_fills(fills)
    trades = [t for t in notion_sync.fetch_trades(log=lambda *a: None) if t["account"] == "NG-IBKR"]
    as_row = lambda t: {"id": t["pageId"], "ticker": t["ticker"], "opened": (t.get("dateOpened") or "")[:10],
                        "closed": (t.get("dateClosed") or "")[:10] or None, "entry": t.get("entryPrice")}
    changes = []
    by_id = {t["pageId"]: t for t in trades}
    for symbol, legs in legs_by_symbol.items():
        cores = [as_row(t) for t in trades if t["ticker"] == symbol and not t.get("parentId")]
        kids = [t for t in trades if t["ticker"] == symbol and t.get("parentId")]
        core_leg = {}
        # Only round trips that net to flat inside the report can be trusted:
        # a leg that sells more than it bought started before the window.
        clean = [l for l in legs if not l["open"] and l["exitPrice"] is not None and abs(l["sellShares"] - l["shares"]) <= 0.01]
        for idx, row in _assign(legs, cores).items():
            leg = legs[idx]
            if leg not in clean:
                continue
            core_leg[row["id"]] = leg
            t = by_id[row["id"]]
            closed = (leg["closed"] or "")[:10]
            if (t.get("dateClosed") or "")[:10] != closed or abs((t.get("exitPrice") or 0) - leg["exitPrice"]) > 0.02:
                changes.append({"ticker": symbol, "pageId": t["pageId"], "opened": t.get("dateOpened"), "kind": "core closed at the wrong date/price",
                                "close": {"from": [t.get("dateClosed"), t.get("exitPrice")], "to": [closed, round(leg["exitPrice"], 4)]}})
        # an "add" opened after its parent's own round trip had already ended is a new position
        for k in kids:
            parent_leg = core_leg.get(k["parentId"])
            if parent_leg is None or not parent_leg["closed"]:
                continue
            if (k.get("dateOpened") or "") >= (parent_leg["closed"] or "")[:10]:
                changes.append({"ticker": symbol, "pageId": k["pageId"], "opened": k.get("dateOpened"), "kind": "new position wrongly linked as a pyramid add",
                                "detach": True, "pyramid_label": k.get("entrySetup") == "Pyramid Add"})
    if apply:
        for c in changes:
            if c.get("detach"):
                notion_sync.detach_from_parent(c["pageId"], clear_pyramid_label=c["pyramid_label"], log=log)
            if c.get("close"):
                notion_sync.close_trade(c["pageId"], c["close"]["to"][0], c["close"]["to"][1], log=log)
    return changes
