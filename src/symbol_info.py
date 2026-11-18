"""One-stock header info for detail views: TradingView exchange symbol, company
name, logo, sector/industry, market cap. Cached in memory after the first lookup."""
import json
import os

import config
import movers

_CACHE = {}


_RESOLVED = {}


def resolve(country, ticker):
    """Sector, industry, logo and name for a listed ticker the nightly universe leaves out (ADRs such as ARM, ASML,
    TSM). None when TradingView has no such stock on the market's exchanges."""
    ticker = "".join(ch for ch in (ticker or "").upper().split(":")[-1] if ch.isalnum() or ch in ".-_")[:16]
    if not ticker:
        return None
    key = (country, ticker)
    if key in _RESOLVED:
        return _RESOLVED[key]
    cfg = config.COUNTRIES[country]
    out = None
    try:
        from tradingview_screener import Query, col
        _, df = (Query().set_markets(cfg["tv_market"])
                 .select("name", "description", "sector", "industry", "logoid", "exchange", "type")
                 .where(col("name") == ticker, col("exchange").isin(cfg["exchanges"])).limit(3)
                 .get_scanner_data())
        if len(df):
            row = df.iloc[0]
            out = {"ticker": ticker, "name": row.get("description"), "sector": row.get("sector"), "industry": row.get("industry"),
                   "logoid": row.get("logoid"), "exchange": row.get("exchange"), "tv": row.get("ticker"), "kind": row.get("type")}
    except Exception:
        return None                          # a network hiccup is not cached, so the next search tries again
    _RESOLVED[key] = out
    return out


def info(country, ticker):
    ticker = "".join(ch for ch in (ticker or "").upper() if ch.isalnum() or ch in ".-_")[:16]
    key = (country, ticker)
    if key in _CACHE:
        return _CACHE[key]
    cfg = config.COUNTRIES[country]
    out = {"ticker": ticker, "tv": movers.tv_symbols(country).get(ticker, ticker),
           "name": None, "logo": None, "sector": None, "industry": None, "mcap": None}
    try:
        with open(os.path.join(config.data_dir(country), "classification.json"), encoding="utf-8") as f:
            row = json.load(f).get(ticker) or []
        out["sector"] = row[0] if len(row) > 0 else None
        out["industry"] = row[1] if len(row) > 1 else None
        out["logo"] = row[2] if len(row) > 2 else None
        out["mcap"] = row[3] if len(row) > 3 else None
    except (OSError, ValueError):
        pass
    try:
        from tradingview_screener import Query, col
        _, df = (Query().set_markets(cfg["tv_market"]).select("name", "description")
                 .where(col("name") == ticker, col("exchange").isin(cfg["exchanges"])).limit(1)
                 .get_scanner_data())
        if len(df):
            out["name"] = df.iloc[0]["description"]
    except Exception:
        pass
    _CACHE[key] = out
    return out
