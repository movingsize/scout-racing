#!/usr/bin/env python3
"""
Scout MLB — Odds Watcher (T-5min closing-odds capture)
=========================================================
Lightweight companion to mlb_fetch.py, mirroring race_watcher.py's role for
the racing pipeline. Does none of mlb_fetch.py's slow work (no StatsAPI /
Statcast / park-factor fetches) -- just precisely-timed ESPN odds snapshots.

For every game in mlb/out/scout_mlb.json that doesn't have closing_odds yet,
sleeps until ~5 min before its start_time_utc, takes one odds snapshot (ESPN
scoreboard API, DraftKings -- see mlb_fetch.fetch_espn_odds), and writes
closing_odds back into that game's block. Only that field is touched --
everything mlb_fetch.py wrote is left alone, and the file is re-read fresh
before each write in case a concurrent mlb_fetch.py run has rebuilt it in the
meantime. Pushes to Drive and commits+pushes to GitHub after each capture,
same as race_watcher.py.

Invoked once per day by its own Scheduled Task (ScoutMLBOddsWatcher), started
before the earliest game and left running for the day's slate; exits once
every loaded game has been captured, or the safety-cap runtime elapses.
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

OUT_FILE = mf.OUT_FILE
REPO_URL = "https://github.com/movingsize/scout-racing"

MAX_RUNTIME_SECONDS = 14 * 3600  # safety cap for the whole process (a staggered
                                  # MLB slate can span ~8-10 hours first-to-last pitch)
POLL_SLEEP_CAP = 30              # wake at least this often so the deadline check bites


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_utc(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


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


def sleep_until(target, deadline):
    """Blocks until `target`, waking periodically to check `deadline`.
    Returns False (without having reached `target`) if the deadline hits first."""
    while True:
        now = now_utc()
        if now >= target:
            return True
        if now >= deadline:
            return False
        # Bound the sleep by however long remains until EITHER boundary, not
        # just POLL_SLEEP_CAP -- otherwise a target far past a near deadline
        # (or vice versa) can sleep straight through the nearer one before the
        # next check.
        nap = min((target - now).total_seconds(), (deadline - now).total_seconds(), POLL_SLEEP_CAP)
        time.sleep(max(nap, 0))


def capture_and_write(gid, date):
    """Re-reads the feed fresh (in case mlb_fetch.py rewrote it while we were
    sleeping), fetches current ESPN odds, and writes closing_odds for one game
    if its window is still open and it hasn't been captured already."""
    feed = load_feed()
    game = feed["games"].get(gid)
    if game is None:
        print(f"  ! {gid} no longer in the feed, skipping")
        return
    if game.get("closing_odds"):
        print(f"  {gid} already captured (by a concurrent run), skipping")
        return

    espn_odds = mf.fetch_espn_odds(date)
    odds = espn_odds.get(gid)
    if not odds:
        print(f"  ! no ESPN odds found for {gid} at capture time")
        return

    game["closing_odds"] = odds
    feed["last_polled_at"] = mf.iso_now()
    feed["final_snapshot"] = all(g.get("closing_odds") for g in feed["games"].values())
    save_feed(feed)

    ok = drive_push.push_json("scout_mlb.json", feed)
    print(f"  Drive push: {'ok' if ok else 'skipped/failed'}")

    rel_path = os.path.relpath(OUT_FILE, REPO_ROOT).replace("\\", "/")
    git_commit_push([rel_path], f"Closing odds: {gid} ({mf.iso_now()})")

    print(f"captured closing odds for {gid}: "
          f"ML {odds['moneyline']} / total {odds['total']['line']} / RL {odds['run_line']['line']}")


def main():
    if not os.path.exists(OUT_FILE):
        print("no scout_mlb.json found -- run mlb_fetch.py first")
        return

    feed = load_feed()
    date = feed["slate_date"]
    games = feed.get("games", {})
    pending = sorted(
        (gid for gid, g in games.items() if not g.get("closing_odds")),
        key=lambda gid: games[gid]["start_time_utc"],
    )
    if not pending:
        print("all games already have closing_odds -- nothing to do")
        return

    deadline = now_utc() + datetime.timedelta(seconds=MAX_RUNTIME_SECONDS)
    print(f"watching {len(pending)} game(s) for closing odds, "
          f"process deadline {deadline.isoformat()}")

    for gid in pending:
        start = parse_utc(games[gid]["start_time_utc"])
        capture_at = start - datetime.timedelta(seconds=mf.CLOSING_LEAD_SECONDS)
        give_up_at = start + datetime.timedelta(seconds=mf.CLOSING_GRACE_SECONDS)
        target = max(capture_at, now_utc())

        if target > give_up_at:
            print(f"  ! missed the capture window entirely for {gid}, skipping")
            continue

        reached = sleep_until(target, min(deadline, give_up_at))
        if not reached:
            print(f"  ! process deadline hit before {gid}'s capture window arrived, stopping")
            break

        capture_and_write(gid, date)

    print("odds watcher done")


if __name__ == "__main__":
    main()
