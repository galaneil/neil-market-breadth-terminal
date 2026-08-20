"""
broker_api.py — one portfolio shape, whatever the broker underneath.

WHY AN ABSTRACTION AND NOT TWO PAGES
---------------------------------------------------------------------------
IBKR and Angel One disagree about almost everything at the wire level: one
returns end-of-day XML reports, the other live JSON; one reports position
value in the instrument's own currency with a separate FX rate, the other in
rupees throughout; one calls it markPrice, the other ltp. None of that is
interesting once you are looking at a portfolio, so it is resolved here, once,
and every consumer — the local page, the Notion sync — sees the same fields.

The shape:

    {
      broker, currency, as_of, live,
      account:   {nav, cash, invested, invested_pct, heat, heat_pct,
                  positions, largest_pct, missing_stops},
      positions: [{symbol, quantity, mark, cost_price, value, cost,
                   unrealized, unrealized_pct, pct_nav, stop, risk,
                   risk_pct_nav, stop_distance, currency}],
      nav_history: [[iso_date, nav], ...],
    }

`live` says whether the marks are real-time or a stale end-of-day snapshot,
because a P&L number means different things in those two cases and the page
should not present them identically.

WHAT IS DELIBERATELY NOT HERE
---------------------------------------------------------------------------
No order placement, for any broker. Angel One's credential can trade and
IBKR's gateway session can trade, so the absence has to be a property of the
code rather than of the connection.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A position bigger than this is worth being told about.
CONCENTRATION_WARN = 0.25


def _iso(stamp):
    """20260806 -> 2026-08-06. Passes through anything already dashed."""
    text = str(stamp or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def usd_rate_from_positions(raw):
    """Base-currency -> USD multiplier, taken from IBKR's own FX rate.

    Preferred over an external quote because it is the exact rate IBKR used to
    convert this statement, so the USD view reconciles with the CAD view
    instead of differing by whatever the market moved since.
    """
    for p in raw:
        if (p.get("currency") or "").upper() == "USD" and p.get("fx_to_base"):
            return 1.0 / p["fx_to_base"]
    return None


def usd_rate_from_fmp(base_code):
    """Fallback for a base currency with no USD position to derive from.

    Returns None rather than guessing — a wrong FX rate silently misstates
    every figure on the page, which is worse than showing only the base view.
    """
    try:
        import main as pipeline
        from fmp_client import FMPClient
        key = os.environ.get("FMP_API_KEY") or pipeline.api_key_from_env_file()
        if not key:
            return None
        quote = FMPClient(key).quote_one(f"USD{base_code}")
        price = (quote or {}).get("price")
        return (1.0 / price) if price else None
    except Exception:
        return None


def _account(positions, nav, cash=None):
    """The account-level figures every broker view needs, derived once."""
    invested = sum(p["value"] or 0 for p in positions)
    risks = [p["risk"] for p in positions if p["risk"] is not None]
    heat = sum(risks)

    # Open P&L across the book, and it as a share of what was actually paid —
    # not of NAV, which would shrink the number by however much cash is idle.
    unrealized = sum(p["unrealized"] or 0 for p in positions)
    cost = sum(p["cost"] or 0 for p in positions)

    return {
        "unrealized": unrealized,
        "unrealized_pct": (unrealized / cost) if cost else None,
        "cost": cost,
        "nav": nav,
        "cash": cash if cash is not None else ((nav - invested) if nav else None),
        "invested": invested,
        "invested_pct": (invested / nav) if nav else None,
        "heat": heat,
        "heat_pct": (heat / nav) if nav else None,
        "positions": len(positions),
        "largest_pct": max((p["pct_nav"] or 0 for p in positions), default=0),
        "missing_stops": [p["symbol"] for p in positions if p["stop"] is None],
        "concentrated": max((p["pct_nav"] or 0 for p in positions), default=0)
                        > CONCENTRATION_WARN,
    }


def _position(symbol, quantity, mark, cost_price, value, cost, unrealized,
              currency, stop, nav):
    """One holding with everything derived from the same three inputs.

    Risk is measured from the CURRENT mark to the stop, not from entry: what
    the stop would actually cost from here is what matters for sizing the next
    trade. On a position already in profit that is often nothing.
    """
    row = {
        "symbol": symbol,
        "quantity": quantity,
        "mark": mark,
        "cost_price": cost_price,
        "value": value,
        "cost": cost,
        "unrealized": unrealized,
        "unrealized_pct": ((mark / cost_price - 1)
                           if mark and cost_price else None),
        "pct_nav": (value / nav) if (nav and value is not None) else None,
        "currency": currency,
        "stop": stop,
        "risk": None,
        "risk_pct_nav": None,
        "stop_distance": None,
    }
    if stop and mark and quantity:
        per_share = mark - stop
        row["risk"] = max(per_share, 0) * quantity
        row["risk_pct_nav"] = (row["risk"] / nav) if nav else None
        row["stop_distance"] = per_share / mark
    return row


# ── IBKR ───────────────────────────────────────────────────────────────────

def _nav_history_path(broker):
    import config
    return os.path.join(os.path.dirname(config.ROOT_DIR), "Portfolio Local",
                        f"nav_daily_{broker}.json")


def _remember_nav_history(broker, history):
    """Keep the broker's own daily NAV closes across runs.

    The live gateway has no NAV history endpoint — it only knows about now.
    Flex does, so whenever Flex runs its series is kept here and the live view
    reuses it. Otherwise switching to live would blank the equity curve, which
    would look like data loss rather than a different data source.
    """
    if not history:
        return
    path = _nav_history_path(broker)
    merged = dict(_recall_nav_history(broker))
    merged.update({d: v for d, v in history})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=1, sort_keys=True)
    except Exception:
        pass


def _recall_nav_history(broker):
    path = _nav_history_path(broker)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return sorted(json.load(f).items())
    except Exception:
        return []


def ibkr_live(account_id=None, stops=None, log=print):
    """Portfolio from the Client Portal gateway: live marks, real stops.

    Prices come from a per-contract snapshot rather than the position record,
    because the position's mktPrice tracks the regular session only and this
    has to be right outside it too. Each row carries where its price came
    from, so the page can be honest about a stale quote instead of presenting
    it as a live tick.
    """
    import ibkr_cp

    ibkr_cp.ensure_session()
    accts = ibkr_cp.accounts()
    if not account_id:
        account_id = accts[0]["accountId"] if accts else None
    if not account_id:
        raise RuntimeError("gateway returned no accounts")

    book = ibkr_cp.ledger(account_id) or {}
    base = book.get("BASE") or {}
    nav = base.get("netliquidationvalue")
    cash = base.get("cashbalance")
    currency = "CAD"
    for row in accts:
        if row.get("accountId") == account_id:
            currency = row.get("currency") or currency

    raw = ibkr_cp.positions(account_id)
    quotes = ibkr_cp.snapshot([r.get("conid") for r in raw], log=log)

    resting = dict(stops or {})
    resting.update(ibkr_cp.resting_stops(account_id, log=log))
    resting = {k.upper(): v for k, v in resting.items() if v}

    # One FX rate per currency, taken from the ledger IBKR itself computed.
    fx = {}
    for code, row in book.items():
        if isinstance(row, dict) and row.get("exchangerate"):
            fx[code] = row["exchangerate"]

    rows, stale = [], []
    for r in raw:
        symbol = (r.get("contractDesc") or "").upper()
        quote = quotes.get(str(r.get("conid")), {})
        mark = quote.get("price") or _num(r.get("mktPrice"))
        if quote.get("source") in (None, "none"):
            stale.append(symbol)

        quantity = _num(r.get("position"))
        avg = _num(r.get("avgPrice")) or _num(r.get("avgCost"))
        rate = fx.get(r.get("currency"), 1.0)
        value = (quantity * mark * rate) if (quantity and mark) else None

        row = _position(
            symbol=symbol, quantity=quantity, mark=mark, cost_price=avg,
            value=value,
            cost=(quantity * avg * rate) if (quantity and avg) else None,
            unrealized=_num(r.get("unrealizedPnl")),
            currency=r.get("currency"), stop=resting.get(symbol), nav=nav)
        # Risk is computed in the instrument's currency by _position; convert
        # so it sits in the same base as NAV, or heat is understated by the FX.
        for key in ("risk",):
            if row.get(key) is not None:
                row[key] *= rate
        row["risk_pct_nav"] = (row["risk"] / nav) if (row["risk"] and nav) else None
        row["price_source"] = quote.get("source", "position")
        rows.append(row)

    rows.sort(key=lambda r: -(r["value"] or 0))
    if stale:
        log(f"  no live quote for {', '.join(stale)} — using the position mark")

    from datetime import datetime
    return {
        "broker": "ibkr",
        "label": "IBKR",
        "account_id": account_id,
        "currency": currency,
        "usd_rate": (1.0 / fx["USD"]) if fx.get("USD") else None,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "live": True,
        "account": _account(rows, nav, cash=cash),
        "positions": rows,
        "nav_history": _recall_nav_history("ibkr"),
    }


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ibkr(stops=None, log=print):
    """Portfolio from the Flex reports service.

    End-of-day by construction: Flex reports what settled, so `live` is False
    and stops must be supplied, since a resting order is invisible to it. The
    Client Portal gateway is the path to live marks and real stops; it slots in
    here as a second ibkr_* function returning the same shape.
    """
    import ibkr_flex

    data = ibkr_flex.parse(ibkr_flex.fetch_statement(log=log))
    stops = {k.upper(): v for k, v in (stops or {}).items()}

    nav_rows = data["nav"]
    nav = nav_rows[-1]["total"] if nav_rows else None

    positions = []
    for p in data["positions"]:
        fx = p.get("fx_to_base") or 1.0
        symbol = (p.get("symbol") or "").upper()
        positions.append(_position(
            symbol=symbol,
            quantity=p.get("quantity"),
            mark=p.get("mark"),
            cost_price=p.get("cost_price"),
            # Values converted to BASE currency; the marks stay in the
            # instrument's own, which is what the position is quoted in.
            value=(p.get("value") or 0) * fx,
            cost=(p.get("cost_money") or 0) * fx,
            unrealized=(p.get("unrealized") or 0) * fx,
            currency=p.get("currency"),
            stop=stops.get(symbol),
            nav=nav,
        ))
    positions.sort(key=lambda r: -(r["value"] or 0))

    history = [[_iso(r["date"]), r["total"]] for r in nav_rows
               if r.get("date") and r.get("total") is not None]
    # Flex is the only source of daily NAV closes, so keep them for the live
    # view, which knows nothing before this moment.
    _remember_nav_history("ibkr", history)

    return {
        "broker": "ibkr",
        "label": "IBKR",
        "currency": "CAD",
        # Multiplier for amounts already expressed in the base currency.
        # Per-share figures (mark, cost, stop) are NOT converted — a share
        # price and a stop are quoted in the market you trade them in, and
        # restating them in another currency makes them unrecognisable.
        "usd_rate": usd_rate_from_positions(data["positions"]),
        "as_of": _iso(data["as_of"]),
        "live": False,
        "account_id": (data["positions"][0].get("account")
                       if data["positions"] else None),
        "account": _account(positions, nav),
        "positions": positions,
        "nav_history": [[_iso(r["date"]), r["total"]] for r in nav_rows
                        if r.get("date") and r.get("total") is not None],
    }


# ── Angel One ──────────────────────────────────────────────────────────────

def angelone(session, stops=None, log=print):
    """Portfolio from SmartAPI. `session` is (token, api_key) from a login.

    Marks are live LTPs, so `live` is True. NAV history is not something
    SmartAPI offers — there is no equity-curve endpoint — so the curve is
    built from what we record ourselves, day by day, and starts empty.
    """
    import angelone as angel

    token, api_key = session
    book = angel.portfolio(token, api_key)

    # Stops resting at the broker beat anything recorded by hand: they are
    # what would actually execute. The manual file only fills the gaps.
    resting = angel.stops(token, api_key, log=log)
    stops = {k.upper(): v for k, v in (stops or {}).items()}
    stops.update(resting)

    cash = book["funds"]["available_cash"] or 0
    holdings_value = book["totals"]["current"] or 0
    nav = holdings_value + cash

    positions = []
    for p in book["positions"]:
        symbol = (p.get("symbol") or "").upper()
        positions.append(_position(
            symbol=symbol,
            quantity=p.get("quantity"),
            mark=p.get("mark"),
            cost_price=p.get("cost_price"),
            value=p.get("value"),
            cost=p.get("cost_money"),
            unrealized=p.get("unrealized"),
            currency="INR",
            stop=stops.get(symbol),
            nav=nav,
        ))
    positions.sort(key=lambda r: -(r["value"] or 0))

    from datetime import date
    return {
        "broker": "angelone",
        "label": "Angel One",
        "currency": "INR",
        # No USD position exists to derive a rate from, so this one has to
        # come from outside. None simply hides the USD toggle.
        "usd_rate": usd_rate_from_fmp("INR"),
        "as_of": date.today().isoformat(),
        "live": True,
        "account": _account(positions, nav, cash=cash),
        "positions": positions,
        "nav_history": [],
    }
