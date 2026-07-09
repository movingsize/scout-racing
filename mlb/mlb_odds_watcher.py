#!/usr/bin/env python3
"""
Scout MLB — Odds Watcher (Betfair propagation)
=================================================
Runs under the existing ScoutMLBOddsWatcher scheduled task. Its job changed:
instead of capturing ESPN/DraftKings closing odds (retired — that path keyed on
teams alone and carried a prior slate's line into a new same-teams game), it now
PROPAGATES the correctly-keyed Betfair odds from scout_odds.json into
scout_mlb.json, the model feed.

scout_odds.json is kept fresh (live_odds refreshed pre-off, closing_odds frozen
write-once at the in-play flip) by betfair_odds_fetch.py + betfair_odds_watcher.py.
This watcher just mirrors those into every game's live_odds / closing_odds via
odds_merge, so the model always reads current Betfair prices even between
mlb_fetch rebuilds. Only the odds fields are touched; the feed is re-read fresh
before each write; and a write/Drive-push/GitHub-commit happens only when the
odds actually changed (not on every timestamp tick). Exits when every game has a
frozen close, or the runtime cap elapses.
"""

import datetime
import json
import os
import subprocess
import sys
import time

import mlb_fetch as mf

MLB_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(MLB_ROOT)
sys.path.insert(0, REPO_ROOT)
import drive_push
import odds_merge

OUT_FILE = mf.OUT_FILE
ODDS_FILE = os.path.join(REPO_ROOT, "scout_odds.json")

MAX_RUNTIME_SECONDS = 14 * 3600   # a staggered slate spans ~8-10h first-to-last pitch
POLL_INTERVAL = 45                # propagate at least this often


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def git(args, check=True):
    return subprocess.run(["git"] + args, cwd=REPO_ROOT, capture_output=True, text=True, check=check)


def git_commit_push(paths, message):
    subprocess.run(["git", "add"] + paths, cwd=REPO_ROOT, check=True)
    diff = git(["diff", "--cached", "--name-only"])
    if not diff.stdout.strip():
        return
    subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
    for attempt in range(3):
        push = git(["push", "origin", "main"], check=False)
        if push.returncode == 0:
            return
        print(f"  ! push failed (attempt {attempt + 1}): {push.stderr.strip()}")
        git(["pull", "--rebase", "origin", "main"], check=False)
    print("  ! git push failed after retries -- data is committed locally, will retry next write")


def load_feed():
    with open(OUT_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_feed(feed):
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)


def odds_signature(feed):
    """Odds-only view, so we commit on real price changes, not timestamp ticks."""
    return {gid: (g.get("live_odds"), g.get("closing_odds"))
            for gid, g in feed.get("games", {}).items()}


def all_closed(feed):
    games = feed.get("games", {})
    return bool(games) and all(g.get("closing_odds") for g in games.values())


def propagate():
    """Re-read scout_mlb.json fresh, merge current Betfair odds from
    scout_odds.json, and write/push only if the odds actually changed.
    Returns (changed, feed)."""
    feed = load_feed()
    before = json.dumps(odds_signature(feed), sort_keys=True)
    live_n, close_n = odds_merge.apply_odds(feed, ODDS_FILE)
    after = json.dumps(odds_signature(feed), sort_keys=True)
    if before == after:
        return False, feed

    feed["final_snapshot"] = all_closed(feed)
    feed["last_polled_at"] = mf.iso_now()
    save_feed(feed)
    ok = drive_push.push_json("scout_mlb.json", feed)
    rel = os.path.relpath(OUT_FILE, REPO_ROOT).replace("\\", "/")
    git_commit_push([rel], f"Betfair odds: {live_n} live / {close_n} closing ({mf.iso_now()})")
    print(f"  propagated Betfair odds: {live_n} live / {close_n} closing "
          f"(Drive {'ok' if ok else 'skipped/failed'})")
    return True, feed


def main():
    if not os.path.exists(OUT_FILE):
        print("no scout_mlb.json found -- run mlb_fetch.py first")
        return
    if not os.path.exists(ODDS_FILE):
        print("no scout_odds.json found -- run betfair_odds_fetch.py first (nothing to propagate)")
        return

    deadline = now_utc() + datetime.timedelta(seconds=MAX_RUNTIME_SECONDS)
    print(f"propagating Betfair odds into scout_mlb.json until close-complete "
          f"or {deadline.isoformat()}")

    while now_utc() < deadline:
        _, feed = propagate()
        if all_closed(feed):
            print("all games have a Betfair close -- done")
            return
        remaining = (deadline - now_utc()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL, remaining))

    print("odds watcher done (runtime cap reached)")


if __name__ == "__main__":
    main()
