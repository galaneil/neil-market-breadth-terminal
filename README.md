# Market Breadth Terminal

A daily-refreshing market breadth dashboard for US equities (NASDAQ, S&P 500, Russell 2000), built from
Financial Modeling Prep (index/stock data) and TradingView (industry rank data via `tradingview-screener`).

Runs entirely in GitHub Actions on a daily schedule after US market close. No local execution required.
Published to GitHub Pages from `docs/index.html`.

## Structure

- `src/` — Python pipeline: fetch data, compute metrics, accumulate history, render the dashboard.
- `data/` — accumulating JSON Lines history for every panel (one row per trading day).
- `data/_cache/` — internal rolling price-window cache (not charted directly, just a calc buffer).
- `docs/` — the published static site (GitHub Pages source).

## Local run

```bash
pip install -r requirements.txt
FMP_API_KEY=your_key_here python src/main.py
```

## Scope

US only today. Structured so a second country (India: Nifty 50/Midcap/Smallcap) can be added later via
`src/config.py`'s `COUNTRIES` dict, without changing storage format or rendering logic.
