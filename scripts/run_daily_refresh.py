"""run_daily_refresh.py — the reliable path: run the pipeline on Neil's own
machine, right after the market it covers actually closes, instead of
waiting on GitHub Actions' scheduler.

WHY THIS EXISTS
---------------------------------------------------------------------------
GitHub's own cron scheduler is not a promise of on-time (or even same-day)
execution — it is explicitly documented as best-effort, and on a public repo
with no paid runners it can and does run hours late, or not at all in a given
window. That is what actually happened on 2026-08-31: India's 11:30 UTC slot
fired at 18:04 UTC, and US's 23:00 UTC slot had not fired at all more than
two hours after it should have. No amount of retry logic INSIDE the workflow
fixes a scheduler that never starts the job.

Neil's own machine has none of that uncertainty — it is on, online, and
already running the local Portfolio Hub around the clock. Windows Task
Scheduler firing a script at a fixed local time is not subject to a shared
queue the way GitHub's free-tier cron is. This script is what that scheduled
task runs: pipeline, then the exact same commit-first/pull-rebase/push-retry
sequence the (now-fixed) GitHub Actions workflows use, so the two paths never
fight each other — whichever gets there first wins, the other's rebase just
replays cleanly on top.

GitHub Actions is NOT being removed. It stays as the fallback for the one
day this machine is off — see .github/workflows/daily-us.yml /
daily-in.yml. This script is the primary path; that is the backup.

Usage:
    python scripts/run_daily_refresh.py US
    python scripts/run_daily_refresh.py IN

Meant to be invoked by Windows Task Scheduler shortly after each market's
own close — see setup_scheduled_tasks.ps1 in this same folder for the
one-time setup that registers both tasks.
"""

import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(os.path.dirname(ROOT), "Portfolio Local")


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "daily-refresh.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # a log write failing is not a reason to abort the refresh


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def git(*args):
    return run(["git", *args])


def main():
    if len(sys.argv) < 2 or sys.argv[1].upper() not in ("US", "IN"):
        log("usage: run_daily_refresh.py US|IN")
        sys.exit(2)
    country = sys.argv[1].upper()
    data_subdir = "data/us/" if country == "US" else "data/in/"

    log(f"{country}: starting pipeline")
    started = time.time()
    result = run([sys.executable, "src/main.py", country])
    if result.stdout:
        log(f"{country} pipeline stdout:\n{result.stdout}")
    if result.stderr:
        log(f"{country} pipeline stderr:\n{result.stderr}")
    if result.returncode != 0:
        log(f"{country}: pipeline FAILED after {time.time() - started:.0f}s "
            f"(exit {result.returncode}) — not committing anything")
        sys.exit(1)
    log(f"{country}: pipeline done in {time.time() - started:.0f}s")

    git("config", "user.name", "Neil (local refresh)")
    git("config", "user.email", "neilgala04@gmail.com")
    git("add", data_subdir, "docs/")

    diff = git("diff", "--staged", "--quiet")
    if diff.returncode == 0:
        log(f"{country}: no changes to commit")
        return

    commit = git("commit", "-m", f"{country} daily refresh (local): "
                  f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    if commit.returncode != 0:
        log(f"{country}: git commit failed:\n{commit.stdout}\n{commit.stderr}")
        sys.exit(1)

    # Same commit-first-then-rebase-then-push-with-retry pattern as the
    # GitHub Actions workflows, for the identical reason: the OTHER country's
    # refresh (local or Actions) may push in the same few minutes, and a
    # rebase on top of a real local commit replays cleanly either way.
    for attempt in range(1, 6):
        pull = git("pull", "--rebase", "origin", "main")
        if pull.returncode != 0:
            log(f"{country}: pull --rebase failed on attempt {attempt}:\n{pull.stdout}\n{pull.stderr}")
            time.sleep(attempt * 5)
            continue
        push = git("push")
        if push.returncode == 0:
            log(f"{country}: pushed successfully on attempt {attempt}")
            return
        log(f"{country}: push failed on attempt {attempt}:\n{push.stdout}\n{push.stderr}")
        time.sleep(attempt * 5)

    log(f"{country}: could not push after 5 attempts — giving up. "
        "The pipeline's own output is still committed locally; "
        "next successful run (local or GitHub Actions) will carry it forward.")
    sys.exit(1)


if __name__ == "__main__":
    main()
