"""
relabel_environment.py — restate stored environment history under the current
trend bands, one off.

WHY
---------------------------------------------------------------------------
The daily pipeline appends; it never revisits a day it has already written. So
changing how the verdict is decided only affects tomorrow, and the six years
already stored keep the old call. That history is what the replay view and the
trend-strength chart read, so leaving it alone would mean the same session
reading "choppy" in replay and "bullish" live.

WHAT CHANGES
---------------------------------------------------------------------------
Only the two labels that the rule decides:

  trend.label   recomputed from the factor counts already stored on the row,
                using the current TREND_BULL/BEAR fractions.
  overall       now follows trend alone, rather than requiring trend,
                participation and internals to agree.

Nothing is recomputed from prices. factors_favourable, participation counts
and internals averages are observations of a closed session and are copied
through untouched — this restates the verdict, it does not revise the tape.

Each file is written to a sibling .relabel and swapped in only after the row
count matches, so a failure leaves the original intact.

Usage:
    python src/relabel_environment.py            # report only
    python src/relabel_environment.py --write
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from metrics import environment


def restate(row):
    """The row as it would be written today, verdict-wise."""
    out = dict(row)
    trend = row.get("trend")
    if trend and trend.get("factors_total"):
        trend = dict(trend)
        trend["label"] = environment._label(
            trend["factors_favourable"],
            *config.trend_thresholds(trend["factors_total"]))
        out["trend"] = trend
    out["overall"] = environment._overall(out.get("trend"))
    return out


def process(path, write):
    rows, before, after = [], {}, {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            before[row.get("overall")] = before.get(row.get("overall"), 0) + 1
            new = restate(row)
            after[new["overall"]] = after.get(new["overall"], 0) + 1
            rows.append(new)

    temp = path + ".relabel"
    with open(temp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    check = sum(1 for line in open(temp, encoding="utf-8") if line.strip())
    ok = check == len(rows)
    if write and ok:
        os.replace(temp, path)
    else:
        os.remove(temp)
    return before, after, len(rows), ok


def main():
    write = "--write" in sys.argv

    for country in config.COUNTRIES:
        path = os.path.join(config.data_dir(country), "environment.jsonl")
        if not os.path.exists(path):
            continue
        before, after, n, ok = process(path, write)

        def show(counts):
            order = ("bullish", "choppy", "bearish", "unknown")
            return "  ".join(f"{k} {counts.get(k, 0)}"
                             for k in order if counts.get(k))

        print(f"  {country}  {n} sessions{'' if ok else '  ROW MISMATCH - SKIPPED'}")
        print(f"    before: {show(before)}")
        print(f"    after:  {show(after)}")

    if not write:
        print("\n  dry run — nothing written. Re-run with --write to apply.")


if __name__ == "__main__":
    main()
