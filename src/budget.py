"""
budget.py — fail loudly when a published page gets too heavy.

WHY
---------------------------------------------------------------------------
Twice now the terminal has become unusable in a Notion embed and the way we
found out was Neil saying it felt slow. Both times the number that mattered
was sitting on disk, measurable, and nobody was looking at it: index.html had
reached 41MB by inlining six years of history a day at a time.

Nothing about that failure was sudden. It grew by about 18KB every weekday for
six months. A single check would have caught it in week one, which is the
whole argument for this file.

WHAT IT CHECKS AND WHY THESE NUMBERS
---------------------------------------------------------------------------
The budgets are set against what the browser must process BEFORE it can show
anything, because that is what "laggy" actually means:

  shell   the HTML itself. Should be a skeleton — data belongs in fetched
          files. If this creeps up, something started inlining again, which
          is precisely the regression that caused the 41MB page.

  paint   shell + every tail the loader waits on. Fixed-size by design (252
          sessions each), so growth here means a new panel was added without
          a tail, or TAIL_SESSIONS was raised.

  page    any single published file. Catches a panel that carries a large
          non-series payload, which externalize_series does not touch.

Deliberately does NOT fail the build. This runs at the end of the daily
refresh, and a size regression is not a reason to throw away a good day of
market data — the report goes to the Action log, where a bad number is
visible without being destructive.
"""

import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

KB = 1024
MB = 1024 * 1024

# Generous against today's numbers (shell 8KB, paint 559KB gzipped) so this
# reports real regressions rather than normal drift.
SHELL_LIMIT = 250 * KB
PAINT_LIMIT = 2 * MB
PAGE_LIMIT = 3 * MB


def _gzipped(path):
    """What the browser actually downloads — Pages serves these compressed."""
    with open(path, "rb") as f:
        return len(gzip.compress(f.read(), 6))


def measure(country):
    docs = config.docs_dir(country)
    series = os.path.join(docs, "series")

    pages = {}
    for name in os.listdir(docs):
        path = os.path.join(docs, name)
        if name.endswith(".html") and os.path.isfile(path):
            pages[name] = os.path.getsize(path)

    tails = 0
    if os.path.isdir(series):
        for name in os.listdir(series):
            if name.endswith(".tail.json"):
                tails += _gzipped(os.path.join(series, name))

    shell = pages.get("index.html", 0)
    return {
        "shell": shell,
        "paint": shell + tails,
        "tails": tails,
        "pages": pages,
        "heaviest": max(pages.items(), key=lambda kv: kv[1]) if pages else None,
    }


def check(country, log=print):
    """Report the sizes that decide whether an embed feels instant or broken."""
    m = measure(country)
    problems = []

    log(f"  [{country}] shell {m['shell']/KB:.0f}KB · "
        f"tails {m['tails']/KB:.0f}KB gzipped · "
        f"first paint {m['paint']/KB:.0f}KB")

    if m["shell"] > SHELL_LIMIT:
        problems.append(
            f"index.html is {m['shell']/MB:.1f}MB (budget "
            f"{SHELL_LIMIT/KB:.0f}KB) — something is being inlined again "
            f"instead of written to series/")
    if m["paint"] > PAINT_LIMIT:
        problems.append(
            f"first paint is {m['paint']/MB:.1f}MB (budget {PAINT_LIMIT/MB:.0f}MB) "
            f"— a panel is probably loading full history instead of a tail")

    for name, size in sorted(m["pages"].items(), key=lambda kv: -kv[1]):
        if size > PAGE_LIMIT:
            problems.append(f"{name} is {size/MB:.1f}MB "
                            f"(budget {PAGE_LIMIT/MB:.0f}MB)")

    for problem in problems:
        log(f"  !! SIZE BUDGET: {problem}")
    if not problems:
        heaviest, size = m["heaviest"]
        log(f"  [{country}] within budget · heaviest page {heaviest} "
            f"{size/KB:.0f}KB")
    return problems


def check_all(log=print):
    log("checking page size budgets...")
    problems = []
    for country in config.COUNTRIES:
        problems.extend(check(country, log=log))
    return problems


if __name__ == "__main__":
    found = check_all()
    print(f"\n{len(found)} budget problem(s)")
