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


def build_sp1500():
    """Returns a deduped DataFrame[ticker, name, sector, index] covering the S&P 1500."""
    frames = [_fetch_constituents(name, url) for name, url in WIKI_URLS.items()]
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset="ticker", keep="first").reset_index(drop=True)
    return combined


if __name__ == "__main__":
    uni = build_sp1500()
    print(f"Built S&P 1500 universe: {len(uni)} unique tickers")
    print(uni["index"].value_counts())
    print(uni.head())
