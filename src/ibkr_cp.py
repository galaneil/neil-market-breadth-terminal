"""
ibkr_cp.py — live portfolio from IBKR's Client Portal Gateway.

WHY THIS EXISTS ALONGSIDE ibkr_flex.py
---------------------------------------------------------------------------
Flex is a REPORTS service: end-of-day, settled, and blind to anything resting
or currently trading. It cannot answer "what is this worth right now", "what
are my stops", or "what happened after the close". This module can, because
the gateway proxies an authenticated session to the live trading system.

The cost is that the gateway needs a browser login roughly daily and cannot
run unattended, which is why Flex stays as the overnight fallback rather than
being deleted.

THE SESSION IS THE WHOLE PROBLEM
---------------------------------------------------------------------------
Everything awkward about this API is session state.

  * The session LAPSES after a few minutes of inactivity. Not a clean error
    either — endpoints start returning 401 one at a time while others still
    work. /portfolio/{id}/positions kept answering while /ledger returned 401,
    which reads like a permissions problem and is not one. /tickle is what
    keeps it alive, so this module tickles before every read.

  * /portfolio/{id}/* needs /portfolio/accounts called first in the session.
    Skipping that preflight gives 401 rather than a useful message.

  * /iserver/account/orders is EVENTUALLY CONSISTENT. The first call after a
    fresh session returns an empty list; the data arrives on a later call. So
    it is polled rather than read once. Do NOT pass force=true to hurry it —
    that CLEARS the cache and returns empty, which is the opposite of what the
    name suggests and cost me a confusing few minutes.

OUTSIDE REGULAR HOURS
---------------------------------------------------------------------------
The position record's mktPrice follows the regular session. For pre-market and
after-hours the live quote has to be asked for separately, per contract, via
/iserver/marketdata/snapshot. That endpoint needs priming too: the first call
returns fields that are not yet populated, so it is called twice.

What comes back depends on the account's market data subscriptions. Rather
than assume, each position reports where its price came from, and the page can
say so instead of implying a live tick it never received.

Read-only: no order placement, and none should ever be added.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = "https://localhost:5000/v1/api"

# The gateway signs its own certificate — there is no public hostname to issue
# a real one for. Verification is therefore disabled for THIS host only, and
# the connection never leaves the machine.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

ORDER_POLL_ATTEMPTS = 6
ORDER_POLL_SECONDS = 2

# Snapshot field ids. IBKR identifies market data by number, not name.
FIELD_LAST = "31"          # last traded price, including extended hours
FIELD_BID = "84"
FIELD_ASK = "86"
FIELD_MARK = "7635"        # IBKR's own mark, used when there is no last
FIELD_PRIOR_CLOSE = "7741"
SNAPSHOT_FIELDS = ",".join([FIELD_LAST, FIELD_BID, FIELD_ASK, FIELD_MARK,
                            FIELD_PRIOR_CLOSE])


class GatewayError(RuntimeError):
    pass


class NotAuthenticated(GatewayError):
    pass


def _request(path, method="GET", body=None, timeout=30):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "User-Agent": "neil-portfolio/1.0"},
    )
    try:
        with urllib.request.urlopen(request, context=_CTX, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise NotAuthenticated(f"{path} returned 401") from None
        raise GatewayError(f"{path} failed [{error.code}]") from None
    except Exception as error:                     # connection refused, etc.
        raise GatewayError(f"gateway unreachable: {error}") from None

    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise GatewayError(f"{path} returned non-JSON") from None


def tickle():
    """Keep the session alive. Cheap, and the fix for the phantom 401s."""
    return _request("/tickle", method="POST")


def auth_status():
    """{authenticated, connected, competing} — or None if unreachable."""
    try:
        return _request("/iserver/auth/status", method="POST")
    except GatewayError:
        return None


def ensure_session():
    """Tickle, and reauthenticate if the brokerage session has dropped.

    A gateway can be 'connected' but not 'authenticated' after an idle spell;
    /iserver/reauthenticate revives it without another browser login, which is
    the difference between this working all day and needing a password every
    time you glance at the page.
    """
    tickle()
    status = auth_status() or {}
    if status.get("authenticated"):
        return status
    if status.get("connected"):
        try:
            _request("/iserver/reauthenticate", method="POST")
            time.sleep(2)
            status = auth_status() or {}
        except GatewayError:
            pass
    if not status.get("authenticated"):
        raise NotAuthenticated(
            "gateway is running but not logged in — open https://localhost:5000")
    return status


def accounts():
    """Preflight. /portfolio/{id}/* returns 401 until this has been called."""
    ensure_session()
    _request("/iserver/accounts")
    return _request("/portfolio/accounts") or []


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ledger(account_id):
    """Cash and net liquidation per currency, plus the base-currency totals."""
    tickle()
    return _request(f"/portfolio/{account_id}/ledger") or {}


def positions(account_id):
    tickle()
    return _request(f"/portfolio/{account_id}/positions/0") or []


def resting_stops(account_id=None, log=print):
    """{ticker: stop} from live orders — intended risk, not executed trades.

    Polled because the endpoint is eventually consistent: a fresh session
    returns an empty list first and fills in on a later call.
    """
    tickle()
    _request("/iserver/accounts")

    seen = {}
    for attempt in range(ORDER_POLL_ATTEMPTS):
        try:
            payload = _request("/iserver/account/orders") or {}
        except GatewayError:
            time.sleep(ORDER_POLL_SECONDS)
            continue
        for order in (payload.get("orders") or []):
            if account_id and order.get("acct") not in (account_id, None):
                continue
            if (order.get("side") or "").upper() != "SELL":
                continue
            # auxPrice is the trigger on a stop; price is the limit on a limit.
            stop = _number(order.get("auxPrice"))
            ticker = (order.get("ticker") or "").upper()
            status = (order.get("status") or "").lower()
            if ticker and stop and status in ("presubmitted", "submitted",
                                              "pendingsubmit"):
                seen[ticker] = max(seen.get(ticker, 0), stop)
        if seen:
            break
        time.sleep(ORDER_POLL_SECONDS)

    log(f"  stops resting at broker: {len(seen)}")
    return seen


def snapshot(conids, log=print):
    """{conid: {price, source, bid, ask, prior_close}} including outside RTH.

    Called twice on purpose: the first request subscribes and returns fields
    that have not populated yet, the second returns them.
    """
    if not conids:
        return {}
    ids = ",".join(str(c) for c in conids)
    path = f"/iserver/marketdata/snapshot?conids={ids}&fields={SNAPSHOT_FIELDS}"

    rows = []
    for attempt in range(3):
        tickle()
        try:
            rows = _request(path) or []
        except GatewayError as error:
            log(f"  snapshot unavailable ({error})")
            return {}
        if any(r.get(FIELD_LAST) or r.get(FIELD_MARK) for r in rows):
            break
        time.sleep(1.5)

    out = {}
    for row in rows:
        conid = row.get("conid")
        if conid is None:
            continue
        # A leading C or H marks a close or halted price rather than a trade.
        raw_last = str(row.get(FIELD_LAST) or "").lstrip("CH")
        last = _number(raw_last)
        mark = _number(row.get(FIELD_MARK))
        price, source = (last, "last") if last else (mark, "mark")
        out[str(conid)] = {
            "price": price,
            "source": source if price else "none",
            "bid": _number(row.get(FIELD_BID)),
            "ask": _number(row.get(FIELD_ASK)),
            "prior_close": _number(row.get(FIELD_PRIOR_CLOSE)),
        }
    return out


def available():
    """True if the gateway is up AND logged in — cheap enough to call often."""
    try:
        ensure_session()
        return True
    except GatewayError:
        return False


if __name__ == "__main__":
    def log(msg):
        print(msg, flush=True)

    log("checking gateway...")
    status = ensure_session()
    log(f"  authenticated={status.get('authenticated')} "
        f"competing={status.get('competing')}\n")

    accts = accounts()
    for a in accts:
        log(f"  account {a.get('accountId')}  {a.get('currency')}  "
            f"{a.get('acctCustType') or a.get('type')}")
    account_id = accts[0]["accountId"] if accts else None

    log("\nledger:")
    for ccy, row in (ledger(account_id) or {}).items():
        if isinstance(row, dict) and row.get("netliquidationvalue") is not None:
            log(f"  {ccy:8} cash={row.get('cashbalance')} "
                f"netliq={row.get('netliquidationvalue')} "
                f"stock={row.get('stockmarketvalue')}")

    rows = positions(account_id)
    log(f"\n{len(rows)} positions")
    quotes = snapshot([r.get("conid") for r in rows], log=log)
    stops = resting_stops(account_id, log=log)

    log("")
    for r in rows:
        q = quotes.get(str(r.get("conid")), {})
        ticker = (r.get("contractDesc") or "").upper()
        log(f"  {ticker:8} qty={r.get('position'):<6} "
            f"rth_mark={r.get('mktPrice')!s:<14} "
            f"live={q.get('price')!s:<10} ({q.get('source')}) "
            f"stop={stops.get(ticker, '-')}")
