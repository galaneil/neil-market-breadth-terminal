"""
cache.py — generic rolling-window JSON cache: {series_id: {date: value}}.

This is NOT the chart history (that's the accumulating JSONL in data/*.jsonl,
which grows forever). This is an internal calculation buffer — e.g. the last
~280 trading days of closing price per S&P1500 ticker, or the last ~25 days
of sector performance per sector — trimmed on every run so it stays a small,
bounded size instead of growing without limit. It exists because computing
5d/20d moves and 52-week highs/lows needs a rolling lookback window, and
GitHub Actions runners keep no state between runs — anything needed next time
has to be committed to the repo.
"""

import json
import os
import pandas as pd


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save(cache, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cache, f, separators=(",", ":"))


def set_value(cache, series_id, date_str, value):
    if value is None:
        return
    cache.setdefault(series_id, {})[date_str] = value


def trim(cache, keep_days):
    for series_id, series in cache.items():
        if len(series) > keep_days:
            kept_dates = sorted(series.keys())[-keep_days:]
            cache[series_id] = {d: series[d] for d in kept_dates}
    return cache


def to_series(cache, series_id):
    """Returns a pandas Series of values indexed by date (ascending), or empty Series."""
    series = cache.get(series_id, {})
    if not series:
        return pd.Series(dtype=float)
    s = pd.Series(series)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()
