"""
ibkr_flex.py — read the portfolio from IBKR's Flex Web Service.

WHY FLEX AND NOT THE TWS API
---------------------------------------------------------------------------
The TWS/Gateway API needs the desktop application running and logged in. Neil
uses IBKR on the web, so that path is a non-starter. The Client Portal Gateway
is the other option and needs a local Java process plus a browser re-auth about
once a day — too fragile for something that should just work each morning.

Flex is a report API: one token, one saved query, plain HTTPS. Nothing to run,
nothing to log into. The cost is that it serves END-OF-DAY statement data
rather than live positions, which is the right granularity for position sizing
and portfolio risk reviewed daily.

THE TWO-STEP HANDSHAKE
---------------------------------------------------------------------------
Flex does not return the report from one call. You ask for it, IBKR generates
it in the background and hands back a reference code, and you collect it with a
second call. A collection attempt made too early returns error 1019
("statement generation in progress"), which is not a failure — it means try
again shortly. That retry is handled here so the caller sees either a report or
a real error.

Read-only by construction: the Flex service exposes reports and nothing else.
There is no order path in this module and there should never be one.
"""

import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
VERSION = 3

# IBKR's code for "the report is still being generated". Everything else that
# comes back with an ErrorCode is a genuine problem.
IN_PROGRESS_CODE = "1019"
MAX_ATTEMPTS = 12
SECONDS_BETWEEN_ATTEMPTS = 5


class FlexError(RuntimeError):
    pass


def _get(url, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        # Flex rejects requests without a user agent.
        headers={"User-Agent": "neil-market-breadth-terminal/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def _error_of(root):
    """(code, message) if the response is an error envelope, else None."""
    if root.tag == "FlexStatementResponse" or root.find("ErrorCode") is not None:
        code = root.findtext("ErrorCode")
        if code:
            return code, (root.findtext("ErrorMessage") or "").strip()
    return None


def credentials():
    """Token and query id from the environment, falling back to .env."""
    token = os.environ.get("IBKR_FLEX_TOKEN")
    query = os.environ.get("IBKR_FLEX_QUERY_ID")
    if token and query:
        return token, query

    import config
    path = os.path.join(config.ROOT_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key == "IBKR_FLEX_TOKEN" and not token:
                    token = value
                elif key == "IBKR_FLEX_QUERY_ID" and not query:
                    query = value
    if not token or not query:
        raise FlexError("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID are not set")
    return token, query


def fetch_statement(token=None, query_id=None, log=print):
    """The report XML as a string, waiting for generation if necessary."""
    if not token or not query_id:
        token, query_id = credentials()

    root = ET.fromstring(_get(f"{BASE}/SendRequest",
                              {"t": token, "q": query_id, "v": VERSION}))
    error = _error_of(root)
    if error and root.findtext("Status") != "Success":
        raise FlexError(f"request rejected [{error[0]}]: {error[1]}")

    reference = root.findtext("ReferenceCode")
    collect_url = root.findtext("Url") or f"{BASE}/GetStatement"
    if not reference:
        raise FlexError("no reference code returned")
    log(f"  requested; reference {reference}, collecting...")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        body = _get(collect_url, {"t": token, "q": reference, "v": VERSION})
        try:
            parsed = ET.fromstring(body)
        except ET.ParseError:
            return body            # not XML we recognise; hand it back as-is

        error = _error_of(parsed)
        if not error:
            return body
        code, message = error
        if code != IN_PROGRESS_CODE:
            raise FlexError(f"collection failed [{code}]: {message}")
        log(f"  still generating (attempt {attempt}/{MAX_ATTEMPTS})")
        time.sleep(SECONDS_BETWEEN_ATTEMPTS)

    raise FlexError("statement was still generating after "
                    f"{MAX_ATTEMPTS * SECONDS_BETWEEN_ATTEMPTS}s")


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(xml_text):
    """{positions, cash, nav, as_of} from the statement XML."""
    root = ET.fromstring(xml_text)

    positions = []
    for node in root.iter("OpenPosition"):
        get = node.get
        positions.append({
            "account": get("accountId"),
            "symbol": get("symbol"),
            "description": get("description"),
            "conid": get("conid"),
            "exchange": get("listingExchange"),
            "currency": get("currency"),
            "fx_to_base": _number(get("fxRateToBase")),
            "asset_class": get("assetCategory"),
            "quantity": _number(get("position")),
            "mark": _number(get("markPrice")),
            "value": _number(get("positionValue")),
            "cost_price": _number(get("costBasisPrice")),
            "cost_money": _number(get("costBasisMoney")),
            "pct_of_nav": _number(get("percentOfNAV")),
            "unrealized": _number(get("fifoPnlUnrealized")),
            "side": get("side"),
            "opened": get("openDateTime"),
            "report_date": get("reportDate"),
        })

    cash = []
    for node in root.iter("CashReportCurrency"):
        get = node.get
        cash.append({
            "account": get("accountId"),
            "currency": get("currency"),
            "level": get("levelOfDetail"),
            "ending": _number(get("endingCash")),
            "ending_settled": _number(get("endingSettledCash")),
            "starting": _number(get("startingCash")),
            "deposits": _number(get("deposits")),
            "withdrawals": _number(get("withdrawals")),
            "dividends": _number(get("dividends")),
        })

    nav = []
    for node in root.iter("EquitySummaryByReportDateInBase"):
        get = node.get
        nav.append({
            "account": get("accountId"),
            "date": get("reportDate"),
            "cash": _number(get("cash")),
            "stock": _number(get("stock")),
            "total": _number(get("total")),
        })
    nav.sort(key=lambda r: r["date"] or "")

    return {
        "positions": positions,
        "cash": cash,
        "nav": nav,
        "as_of": nav[-1]["date"] if nav else (
            positions[0]["report_date"] if positions else None),
    }


if __name__ == "__main__":
    def log(msg):
        print(msg, flush=True)

    log("requesting statement...")
    xml_text = fetch_statement(log=log)
    log(f"  received {len(xml_text):,} bytes")

    data = parse(xml_text)
    log(f"\nas of {data['as_of']}")
    log(f"{len(data['positions'])} positions, {len(data['cash'])} cash rows, "
        f"{len(data['nav'])} NAV rows")

    for row in data["nav"][-2:]:
        log(f"  NAV {row['date']}  total {row['total']:,.2f}  "
            f"(cash {row['cash']:,.2f} + stock {row['stock']:,.2f})")

    for position in sorted(data["positions"],
                           key=lambda p: -(p.get("value") or 0))[:15]:
        log(f"  {position['symbol']:8} {position['quantity']:>10,.0f} @ "
            f"{position['mark']:>9,.2f}  value {position['value']:>12,.2f}  "
            f"{position['pct_of_nav'] or 0:>5.1f}% of NAV")
