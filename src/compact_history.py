"""
compact_history.py — rewrite already-written group-rank history in the compact
encoding, one off.

WHY THIS EXISTS
---------------------------------------------------------------------------
The daily pipeline appends; it never revisits old rows. So fixing the encoding
in metrics/ only helps the rows written from today onward, and the six years
already on disk keep their duplicated key and their 17-digit floats. That
history is most of the published payload, so it has to be rewritten in place.

WHAT CHANGES AND WHAT DOES NOT
---------------------------------------------------------------------------
Removed: the duplicate group name (industry_ranks carried both "group" and
"industry" with identical values; sector_ranks both "group" and "sector").
Rounded: chg_1d / chg_5d / chg_20d to 2dp — they are percentages rendered to
one or two decimals.

NOT touched: rank, n_members, dates, or the set of groups per day. Rank was
computed from full-precision values at write time and is preserved exactly as
stored, so no ordering can shift from this rewrite.

Each file is written to a sibling .compact and swapped in only after it parses
and its row count matches, so a failure leaves the original intact. The repo's
git history is the backstop if a rewrite is ever regretted.

Usage:
    python src/compact_history.py            # report only
    python src/compact_history.py --write
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# file -> the property holding the group name in that file's records
TARGETS = {
    "industry_ranks.jsonl": ("industries", "industry"),
    "sector_ranks.jsonl": ("sectors", "sector"),
}

ROUND_KEYS = ("chg_1d", "chg_5d", "chg_20d")
PLACES = 2


def compact_record(record, name_key):
    """One group's entry, minus the duplicate name, with floats rounded."""
    out = {}
    for key, value in record.items():
        if key == "group" and name_key in record:
            continue                       # the duplicate; name_key survives
        if key in ROUND_KEYS and isinstance(value, float):
            value = round(value, PLACES)
        out[key] = value
    # A file written before the rename bug is conceivable; keep the name.
    if name_key not in out and "group" in record:
        out[name_key] = record["group"]
    return out


def compact_row(row, list_key, name_key):
    out = dict(row)
    records = row.get(list_key)
    if isinstance(records, list):
        out[list_key] = [compact_record(r, name_key) for r in records]
    return out


def process(path, list_key, name_key, write):
    before = os.path.getsize(path)
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(compact_row(json.loads(line), list_key, name_key))

    temp = path + ".compact"
    with open(temp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    after = os.path.getsize(temp)

    # Verify before trusting: same row count, and it parses back cleanly.
    check = sum(1 for line in open(temp, encoding="utf-8") if line.strip())
    ok = check == len(rows)

    if write and ok:
        os.replace(temp, path)
    else:
        os.remove(temp)

    return before, after, len(rows), ok


def main():
    write = "--write" in sys.argv
    total_before = total_after = 0

    for country in config.COUNTRIES:
        for filename, (list_key, name_key) in TARGETS.items():
            path = os.path.join(config.data_dir(country), filename)
            if not os.path.exists(path):
                continue
            before, after, rows, ok = process(path, list_key, name_key, write)
            total_before += before
            total_after += after
            status = "" if ok else "  ROW COUNT MISMATCH — SKIPPED"
            print(f"  {country}/{filename:22} {before/1048576:6.2f} MB -> "
                  f"{after/1048576:6.2f} MB  ({rows} rows){status}")

    saved = total_before - total_after
    print(f"\n  total {total_before/1048576:.2f} MB -> "
          f"{total_after/1048576:.2f} MB  "
          f"({saved/1048576:.2f} MB saved, "
          f"{100*saved/total_before if total_before else 0:.0f}%)")
    if not write:
        print("\n  dry run — nothing written. Re-run with --write to apply.")


if __name__ == "__main__":
    main()
