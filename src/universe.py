"""
universe.py — builds the US S&P 1500 (500 + 400 + 600) ticker universe.

Source: Wikipedia's maintained S&P 500/400/600 constituent tables.

iShares' official holdings CSVs (IVV/IJH/IJR) were tried first, since that's the
"official" source, but their .ajax CSV endpoint is gated behind an investor-type
acknowledgment cookie a plain HTTP request can't satisfy — it just returns the
product page's HTML instead of the CSV. Wikipedia's tables are community-maintained
but reliably structured and don't require any auth/cookie dance.
"""

import io
import json
import os

import requests
import pandas as pd

WIKI_URLS = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-breadth-terminal/1.0)"}

REQUIRED_COLUMNS = {"Symbol", "Security", "GICS Sector"}


def _fetch_constituents(index_name, url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = tables[0]

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Wikipedia table for {index_name} is missing expected columns {missing} "
            f"(got {list(df.columns)}) — the page layout may have changed."
        )

    df = df.rename(columns={"Symbol": "ticker", "Security": "name", "GICS Sector": "sector"})
    df["ticker"] = df["ticker"].str.strip().str.replace(".", "-", regex=False)
    df["index"] = index_name
    return df[["ticker", "name", "sector", "index"]]


def _cache_path():
    import config
    return os.path.join(config.cache_dir("US"), "sp1500.json")


def build_sp1500():
    """Returns a deduped DataFrame[ticker, name, sector, index] covering the S&P 1500.

    Falls back to the last good copy when Wikipedia is unreachable. Index
    membership changes a few times a year, so a day-old list is a rounding
    error — whereas losing the whole run is not. A transient SSL error at
    Wikipedia killed a backfill mid-flight and would take the nightly job with
    it just as easily.
    """
    try:
        frames = [_fetch_constituents(name, url) for name, url in WIKI_URLS.items()]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(subset="ticker", keep="first").reset_index(drop=True)
    except Exception as exc:
        path = _cache_path()
        if not os.path.exists(path):
            raise
        with open(path, encoding="utf-8") as f:
            combined = pd.DataFrame(json.load(f))
        print(f"universe: Wikipedia unavailable ({type(exc).__name__}); "
              f"using the cached list of {len(combined)} tickers", flush=True)
        return combined

    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(combined.to_dict("records"), f)
    return combined


if __name__ == "__main__":
    uni = build_sp1500()
    print(f"Built S&P 1500 universe: {len(uni)} unique tickers")
    print(uni["index"].value_counts())
    print(uni.head())
