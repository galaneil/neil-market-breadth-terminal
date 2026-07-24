"""
metrics/industries.py — thin wrapper around tv_industry.py, shaping its output
into the record format store.py expects for data/industry_ranks.jsonl.

This is also what backs panel 4 (industry rank drill-down): since every daily
run appends one full industry table here, the drill-down is just a client-side
filter over this same accumulating file by industry name — no separate storage.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tv_industry


def compute_industry_ranks():
    """Returns a list of {industry, chg_1d, chg_5d, chg_20d, rank, n_members} dicts,
    ranked by today's cap-weighted change (best first)."""
    stocks = tv_industry.fetch_stock_performance()
    ranked = tv_industry.aggregate_industries(stocks)
    return ranked.to_dict(orient="records")


if __name__ == "__main__":
    import json
    print(json.dumps(compute_industry_ranks()[:10], indent=2))
