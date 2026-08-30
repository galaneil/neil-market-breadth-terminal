"""
angelone.py — read an Angel One portfolio through SmartAPI.

READ-ONLY BY POLICY, NOT BY CONSTRUCTION
---------------------------------------------------------------------------
This is the important difference from ibkr_flex.py and the reason the
credential handling here is deliberately awkward.

IBKR's Flex Web Service is a REPORTS service. It has no order path at all, so
a leaked Flex token exposes information and nothing else. Angel One has no
equivalent: holdings live behind the Trading API, the same app type and the
same session that places orders. Of its four app types — Trading, Market Feed,
Historical Data, Publisher — only Trading returns holdings, and Trading can
trade.

So this module contains no order functions, and none should ever be added.
But that is a promise about this file, not a property of the credential. The
credential itself can trade the account, which for someone else's account is
a bar worth taking seriously.

WHY NOTHING SECRET IS STORED
---------------------------------------------------------------------------
SmartAPI sessions last about a day, so unattended refresh would mean keeping
the PIN and the TOTP SEED on disk. A file holding the seed is not a second
factor — it is the first factor written down next to the second. Anything
that could read .env could then trade the account.

The trade is a few seconds of typing against that, and it costs little here
because the point is live position data while you are at the desk anyway:

    .env       ANGELONE_API_KEY, ANGELONE_CLIENT_CODE   (useless alone)
    typed      PIN and the 6-digit TOTP, per run, never written anywhere

To make it unattended later, add pyotp and read a seed from .env — one
function changes. Do that knowing what it gives up.

Usage:
    python src/angelone.py                 # prompts for PIN and TOTP
    python src/angelone.py 123456          # TOTP as an argument
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

ROOT = "https://apiconnect.angelone.in"

LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
HOLDINGS = "/rest/secure/angelbroking/portfolio/v1/getAllHolding"
RMS = "/rest/secure/angelbroking/user/v1/getRMS"
POSITIONS = "/rest/secure/angelbroking/order/v1/getPosition"
ORDER_BOOK = "/rest/secure/angelbroking/order/v1/getOrderBook"
GTT_LIST = "/rest/secure/angelbroking/gtt/v1/ruleList"

# A resting stop can be either a plain stop-loss order sitting in the order
# book, or a GTT rule, and Angel One keeps those in completely separate
# places. Reading only one of them would silently miss half your risk.
OPEN_ORDER_STATUSES = ("open", "trigger pending", "open pending",
                       "modify pending", "validation pending")
GTT_ACTIVE_STATUSES = ("NEW", "ACTIVE", "SENTTOEXCHANGE", "FORALL")

# Order types that mean "get me out at this price".
STOP_ORDER_TYPES = ("STOPLOSS_LIMIT", "STOPLOSS_MARKET")

# SmartAPI rejects requests missing these. The IP and MAC values are recorded
# by Angel One for audit; they are not used to authorise anything, and the
# documented examples use placeholders exactly like this.
CLIENT_HEADERS = {
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "X-ClientLocalIP": "127.0.0.1",
    "X-ClientPublicIP": "127.0.0.1",
    "X-MACAddress": "00:00:00:00:00:00",
}


class AngelOneError(RuntimeError):
    pass


def settings(env_prefix="ANGELONE"):
    """API key and client code from .env — neither is a secret on its own.

    `env_prefix` is what makes a second Angel One account possible without a
    second copy of this file: the default account reads ANGELONE_API_KEY /
    ANGELONE_CLIENT_CODE exactly as before, and any other account (a second
    family member's own SmartAPI app, registered under their own login) reads
    <PREFIX>_API_KEY / <PREFIX>_CLIENT_CODE instead — e.g. env_prefix="ANGELONE2".
    Each account needs its OWN app registered at smartapi.angelone.in under
    that person's own Angel One login; the API key is not shareable across
    accounts.
    """
    key_name = f"{env_prefix}_API_KEY"
    client_name = f"{env_prefix}_CLIENT_CODE"
    values = {}
    path = os.path.join(config.ROOT_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, raw = line.partition("=")
                key = key.strip()
                if key in (key_name, client_name):
                    values[key] = raw.strip().strip('"').strip("'")

    api_key = os.environ.get(key_name) or values.get(key_name)
    client = os.environ.get(client_name) or values.get(client_name)
    if not api_key or not client:
        raise AngelOneError(
            f"{key_name} and {client_name} are not set — add them "
            "to .env (create the app at smartapi.angelone.in)")
    return api_key, client


def _call(path, api_key, token=None, body=None, method="GET"):
    headers = dict(CLIENT_HEADERS)
    headers.update({
        "Content-type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": api_key,
    })
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        ROOT + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise AngelOneError(f"{path} failed [{error.code}]: {detail}") from None

    if not payload.get("status", True):
        raise AngelOneError(f"{path}: {payload.get('message')} "
                            f"({payload.get('errorcode')})")
    return payload.get("data")


def login(pin, totp, api_key=None, client_code=None):
    """A session token. The PIN and TOTP are used here and then discarded."""
    if not api_key or not client_code:
        api_key, client_code = settings()
    data = _call(LOGIN, api_key, method="POST", body={
        "clientcode": client_code, "password": pin, "totp": totp})
    if not data or not data.get("jwtToken"):
        raise AngelOneError("login returned no token")
    return data["jwtToken"], api_key


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(symbol):
    """RRKABEL-EQ -> RRKABEL. The suffix is the series, not the name."""
    return (symbol or "").replace("-EQ", "").replace("-BE", "").upper()


def stops(token, api_key, log=print):
    """{ticker: stop price} read from what is actually resting at the broker.

    This is the thing IBKR's Flex service cannot do and the Client Portal
    gateway can: report intended risk rather than executed trades. Angel One
    exposes it in two unrelated places, so both are read —

      order book   a STOPLOSS_LIMIT / STOPLOSS_MARKET order still open, whose
                   trigger price is the stop.
      GTT rules    a separate service entirely, where a "sell when it falls to
                   X" rule lives. Neil's platform shows these on their own tab,
                   which is a fair hint that a stop can be in either.

    Where both exist for one symbol the HIGHER stop wins: that is the level
    that would actually be hit first on the way down, so it is the real risk.
    """
    found = {}

    try:
        for order in (_call(ORDER_BOOK, api_key, token) or []):
            if (order.get("status") or "").lower() not in OPEN_ORDER_STATUSES:
                continue
            if (order.get("ordertype") or "").upper() not in STOP_ORDER_TYPES:
                continue
            if (order.get("transactiontype") or "").upper() != "SELL":
                continue
            price = _number(order.get("triggerprice")) or _number(order.get("price"))
            symbol = _clean(order.get("tradingsymbol"))
            if symbol and price:
                found[symbol] = max(found.get(symbol, 0), price)
    except AngelOneError as error:
        log(f"  order book unavailable ({error})")

    try:
        rules = _call(GTT_LIST, api_key, token, method="POST",
                      body={"status": list(GTT_ACTIVE_STATUSES),
                            "page": 1, "count": 100}) or []
        for rule in rules:
            if (rule.get("transactiontype") or "").upper() != "SELL":
                continue
            price = _number(rule.get("triggerprice"))
            symbol = _clean(rule.get("tradingsymbol"))
            if symbol and price:
                found[symbol] = max(found.get(symbol, 0), price)
    except AngelOneError as error:
        log(f"  GTT rules unavailable ({error})")

    log(f"  stops resting at broker: {len(found)}")
    return found


def portfolio(token, api_key):
    """{positions, totals, funds} in the same shape ibkr_flex.parse returns.

    getAllHolding carries a totalholding summary alongside the rows, so the
    account-level figures come from Angel One rather than being re-derived
    here — one less place for the two to disagree.
    """
    holdings = _call(HOLDINGS, api_key, token) or {}
    funds = _call(RMS, api_key, token) or {}

    rows = []
    for h in (holdings.get("holdings") or []):
        quantity = _number(h.get("quantity"))
        ltp = _number(h.get("ltp"))
        average = _number(h.get("averageprice"))
        rows.append({
            "symbol": _clean(h.get("tradingsymbol")),
            "exchange": h.get("exchange"),
            "isin": h.get("isin"),
            "quantity": quantity,
            "mark": ltp,
            "cost_price": average,
            "value": (quantity * ltp) if quantity and ltp else None,
            "cost_money": (quantity * average) if quantity and average else None,
            "unrealized": _number(h.get("profitandloss")),
            "unrealized_pct": (_number(h.get("pnlpercentage")) or 0) / 100.0,
            "currency": "INR",
            "fx_to_base": 1.0,
            "product": h.get("product"),
        })

    total = holdings.get("totalholding") or {}
    return {
        "positions": rows,
        "totals": {
            "invested": _number(total.get("totalinvvalue")),
            "current": _number(total.get("totalholdingvalue")),
            "pnl": _number(total.get("totalprofitandloss")),
            "pnl_pct": _number(total.get("totalpnlpercentage")),
        },
        "funds": {
            "net": _number(funds.get("net")),
            "available_cash": _number(funds.get("availablecash")),
            "used_margin": _number(funds.get("utiliseddebits")),
        },
    }


def prompt_credentials(totp=None):
    """Read the two secrets without echoing or storing them."""
    import getpass
    pin = getpass.getpass("Angel One PIN (not stored): ")
    if not totp:
        totp = input("6-digit TOTP from your authenticator: ").strip()
    return pin, totp


if __name__ == "__main__":
    given = sys.argv[1] if len(sys.argv) > 1 else None
    pin, totp = prompt_credentials(given)

    token, api_key = login(pin, totp)
    del pin                      # no reason to keep it in memory either
    print("  authenticated\n")

    book = portfolio(token, api_key)
    totals, funds = book["totals"], book["funds"]

    print(f"  holding value  {totals['current'] or 0:,.2f} INR")
    print(f"  invested       {totals['invested'] or 0:,.2f}")
    print(f"  open P&L       {totals['pnl'] or 0:,.2f} "
          f"({totals['pnl_pct'] or 0:.2f}%)")
    print(f"  cash           {funds['available_cash'] or 0:,.2f}\n")

    for row in sorted(book["positions"], key=lambda r: -(r["value"] or 0)):
        print(f"  {row['symbol']:14} {row['quantity']:>8,.0f} @ "
              f"{row['mark'] or 0:>9,.2f}   value {row['value'] or 0:>12,.2f}   "
              f"P&L {row['unrealized'] or 0:>10,.2f}")
