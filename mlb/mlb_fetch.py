#!/usr/bin/env python3
"""
Scout — MLB Stats Feed Pipeline (Phase 1 MVP)
==============================================
Produces scout_mlb.json for the Scout MLB model (mlbWinProb / MLB_INPUTS in
scout-portal.jsx). Replaces the model's hand-eyeballed ERA + runs/game inputs
with true-talent metrics pulled from official, script-friendly APIs.

WHAT THIS BUILDS (Phase 1 — see mlb/README.md for phases 2-4)
-------------------------------------------------------------
Per game on the slate:
  - starter true-talent:  FIP (computed from raw counting stats) + Statcast
                          xERA / xwOBA as the quality cross-check
  - team offense:         runs/game
  - bullpen:              relief RA9
  - park factor:          neutral placeholder (real factors are Phase 2)
Each starter / team block carries data_status (confirmed|partial|not_found)
and a warnings[] list — the racing-pipeline "never emit a silent null" lesson.

DATA SOURCES (validated 2026-07-06; deviates from the original spec — see below)
--------------------------------------------------------------------------------
1. MLB StatsAPI (statsapi.mlb.com, free, official, no key)
     - schedule, probable pitchers, MLBAM ids, start times, gameNumber
     - per-pitcher season counting stats  -> FIP is COMPUTED here
     - team hitting (runs/game) and reliever split (bullpen RA9)
2. Baseball Savant (baseballsavant.mlb.com, CSV leaderboard, no key)
     - starter xERA / xwOBA (Statcast true-talent), joined on player_id = MLBAM

SPEC DEVIATION: the spec named FanGraphs as the primary source for FIP/xFIP/SIERA.
FanGraphs now sits behind a Cloudflare JS challenge (HTTP 403 to any script), so
it is unusable from an unattended pipeline. Phase 1 therefore:
  - COMPUTES FIP from StatsAPI raw stats (HR/BB/HBP/K/IP + league constant), and
  - uses Savant xERA/xwOBA for Statcast-based quality.
xFIP / SIERA and wRC+ handedness splits (FanGraphs-only) are deferred to Phase 2,
where the Cloudflare problem gets solved (browser-render or an alternate host).

OUTPUT
------
mlb/out/scout_mlb.json  (strict unchanging filename, overwritten in place)
Optionally mirrored to a "Scout MLB" Google Drive folder via the parent repo's
drive_push.py (reuses racing-scout/token.json). Run with --drive to enable.

USAGE
-----
    python mlb/mlb_fetch.py                 # today's US slate, local file only
    python mlb/mlb_fetch.py --date 2026-07-06
    python mlb/mlb_fetch.py --final         # mark final_snapshot=true (pre-lock run)
    python mlb/mlb_fetch.py --drive         # also push to Google Drive
"""

import argparse
import csv
import datetime
import io
import json
import os
import sys

try:
    import requests
except ImportError:
    print("ERROR: missing 'requests'. Run: pip install requests")
    sys.exit(1)

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    EASTERN = None

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
OUT_FILE = os.path.join(OUT_DIR, "scout_mlb.json")

STATSAPI = "https://statsapi.mlb.com/api/v1"
SAVANT_XSTATS = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type=pitcher&year={year}&position=&team=&min=1&csv=true"
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ScoutMLB/1.0"
HTTP_TIMEOUT = 30

# StatsAPI abbreviation -> Scout abbreviation. Scout lowercases the standard
# 3-letter code; the only historical override is Arizona (AZ -> ari). If Scout
# turns out to use a different code for any team, add it here — this map is the
# game-id contract (see README §Game IDs).
ABBR_OVERRIDE = {"AZ": "ari"}

# Sanity ranges (QA §9). Out-of-range -> warning + data_status downgrade.
RANGE = {
    "fip": (1.5, 7.0),
    "xera": (1.5, 7.5),
    "bullpen_ra9": (2.5, 6.5),
    "rg": (2.5, 7.0),
}

# Fallback FIP constant if league totals can't be computed. Real constant is
# derived from league totals each run (see compute_fip_constant).
FIP_FALLBACK_CONST = 3.15


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
def get_json(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_text(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


# ─── small utilities ──────────────────────────────────────────────────────────
def scout_abbr(statsapi_abbr):
    return ABBR_OVERRIDE.get(statsapi_abbr, statsapi_abbr.lower())


def ip_to_float(ip):
    """StatsAPI innings pitched use .1/.2 = 1/3, 2/3 of an inning. '471.1' -> 471.333."""
    if ip in (None, ""):
        return 0.0
    s = str(ip)
    if "." in s:
        whole, frac = s.split(".", 1)
        thirds = {"0": 0.0, "1": 1 / 3, "2": 2 / 3}.get(frac[0], 0.0)
        return int(whole) + thirds
    return float(s)


def rnd(x, n=2):
    return None if x is None else round(x, n)


def in_range(key, val):
    lo, hi = RANGE[key]
    return val is not None and lo <= val <= hi


# ─── league FIP constant ──────────────────────────────────────────────────────
def compute_fip_constant(season):
    """cFIP so that league FIP == league ERA. Derived from summed team pitching."""
    try:
        data = get_json(
            f"{STATSAPI}/teams/stats?season={season}&sportIds=1"
            f"&group=pitching&stats=season"
        )
        tHR = tBB = tHBP = tK = tER = 0.0
        tIP = 0.0
        for grp in data.get("stats", []):
            for sp in grp.get("splits", []):
                s = sp["stat"]
                tHR += float(s.get("homeRuns", 0) or 0)
                tBB += float(s.get("baseOnBalls", 0) or 0)
                tHBP += float(s.get("hitByPitch", 0) or 0)
                tK += float(s.get("strikeOuts", 0) or 0)
                tER += float(s.get("earnedRuns", 0) or 0)
                tIP += ip_to_float(s.get("inningsPitched", 0))
        if tIP <= 0:
            return FIP_FALLBACK_CONST
        lg_era = tER * 9 / tIP
        lg_fip_raw = (13 * tHR + 3 * (tBB + tHBP) - 2 * tK) / tIP
        return round(lg_era - lg_fip_raw, 3)
    except Exception as e:
        print(f"  ! league FIP constant fallback ({e}); using {FIP_FALLBACK_CONST}")
        return FIP_FALLBACK_CONST


def compute_fip(stat, const):
    """FIP from a pitcher season stat block. Returns (fip, ip)."""
    ip = ip_to_float(stat.get("inningsPitched", 0))
    if ip <= 0:
        return None, 0.0
    hr = float(stat.get("homeRuns", 0) or 0)
    bb = float(stat.get("baseOnBalls", 0) or 0)
    hbp = float(stat.get("hitByPitch", 0) or 0)
    k = float(stat.get("strikeOuts", 0) or 0)
    fip = (13 * hr + 3 * (bb + hbp) - 2 * k) / ip + const
    return round(fip, 2), ip


# ─── Savant Statcast expected stats (xERA / xwOBA), keyed by MLBAM id ──────────
def load_savant_xstats(season):
    out = {}
    try:
        text = get_text(SAVANT_XSTATS.format(year=season))
        # Savant prepends a UTF-8 BOM; left in place it stops csv from seeing the
        # first header field as quoted, so the embedded comma in
        # "last_name, first_name" shifts every column by one. Strip it first.
        text = text.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            pid = (row.get("player_id") or "").strip()
            if not pid:
                continue
            def num(key):
                v = (row.get(key) or "").strip().strip('"')
                try:
                    return float(v)
                except ValueError:
                    return None
            out[pid] = {"xera": num("xera"), "est_woba": num("est_woba")}
    except Exception as e:
        print(f"  ! Savant xstats unavailable ({e}); starters will lack xERA")
    return out


# ─── StatsAPI fetches ─────────────────────────────────────────────────────────
def fetch_schedule(date):
    url = (
        f"{STATSAPI}/schedule?sportId=1&date={date}"
        f"&hydrate=probablePitcher,team,linescore"
    )
    data = get_json(url)
    dates = data.get("dates", [])
    return dates[0].get("games", []) if dates else []


def fetch_all_team_rg(season):
    """{team_id: runs_per_game} for every team, one call."""
    out = {}
    data = get_json(
        f"{STATSAPI}/teams/stats?season={season}&sportIds=1"
        f"&group=hitting&stats=season"
    )
    for grp in data.get("stats", []):
        for sp in grp.get("splits", []):
            tid = sp.get("team", {}).get("id")
            s = sp["stat"]
            g = s.get("gamesPlayed") or 0
            r = s.get("runs") or 0
            if tid and g:
                out[tid] = round(r / g, 2)
    return out


_bullpen_cache = {}


def fetch_bullpen_ra9(team_id, season):
    """Relief RA9 = relief runs * 9 / relief IP, via the sp/rp statSplit."""
    if team_id in _bullpen_cache:
        return _bullpen_cache[team_id]
    val = None
    try:
        data = get_json(
            f"{STATSAPI}/teams/{team_id}/stats?season={season}"
            f"&group=pitching&stats=statSplits&sitCodes=rp"
        )
        for grp in data.get("stats", []):
            for sp in grp.get("splits", []):
                s = sp["stat"]
                ip = ip_to_float(s.get("inningsPitched", 0))
                runs = float(s.get("runs", 0) or 0)
                if ip > 0:
                    val = round(runs * 9 / ip, 2)
    except Exception as e:
        print(f"  ! bullpen RA9 unavailable for team {team_id} ({e})")
    _bullpen_cache[team_id] = val
    return val


_pitcher_cache = {}


def fetch_pitcher(mlbam_id, season, fip_const, savant):
    """Returns a starter dict (or a not_found stub) for one probable pitcher."""
    if mlbam_id in _pitcher_cache:
        return _pitcher_cache[mlbam_id]

    warnings = []
    try:
        data = get_json(
            f"{STATSAPI}/people/{mlbam_id}"
            f"?hydrate=stats(group=pitching,type=season,season={season})"
        )
        person = data["people"][0]
    except Exception as e:
        stub = {
            "name": None, "mlbam_id": mlbam_id, "hand": None,
            "fip": None, "era": None, "ip": None, "xera": None, "xwoba": None,
            "data_status": "not_found",
            "warnings": [f"pitcher lookup failed: {e}"],
        }
        _pitcher_cache[mlbam_id] = stub
        return stub

    name = person.get("fullName")
    hand = person.get("pitchHand", {}).get("code")

    stats = person.get("stats", [])
    splits = stats[0].get("splits", []) if stats else []
    status = "confirmed"
    fip = era = ip = None
    if splits:
        s = splits[0]["stat"]
        era = float(s["era"]) if s.get("era") not in (None, "", "-.--") else None
        fip, ip = compute_fip(s, fip_const)
        if ip < 30:
            warnings.append(f"low sample: {ip:.1f} IP")
            status = "partial"
        if fip is not None and not in_range("fip", fip):
            warnings.append(f"FIP {fip} out of sane range")
            status = "partial"
    else:
        warnings.append("no season pitching stats (season debut / no MLB innings)")
        status = "partial"

    sav = savant.get(str(mlbam_id), {})
    xera = sav.get("xera")
    xwoba = sav.get("est_woba")
    if xera is None:
        warnings.append("no Savant xERA (below batted-ball minimum)")

    starter = {
        "name": name, "mlbam_id": mlbam_id, "hand": hand,
        "fip": fip, "era": era, "ip": rnd(ip, 1),
        "xera": xera, "xwoba": xwoba,
        "data_status": status,
        "warnings": warnings,
    }
    _pitcher_cache[mlbam_id] = starter
    return starter


def build_team_block(team_side, season, rg_map, savant, fip_const):
    """team_side is StatsAPI teams.away/home."""
    team = team_side["team"]
    tid = team["id"]
    abbr = scout_abbr(team["abbreviation"])
    warnings = []

    rg = rg_map.get(tid)
    if not in_range("rg", rg):
        warnings.append(f"runs/game missing or out of range ({rg})")

    bullpen = fetch_bullpen_ra9(tid, season)
    if not in_range("bullpen_ra9", bullpen):
        warnings.append(f"bullpen RA9 missing or out of range ({bullpen})")

    prob = team_side.get("probablePitcher")
    if prob and prob.get("id"):
        starter = fetch_pitcher(prob["id"], season, fip_const, savant)
    else:
        starter = {
            "name": None, "mlbam_id": None, "hand": None,
            "fip": None, "era": None, "ip": None, "xera": None, "xwoba": None,
            "data_status": "not_found",
            "warnings": ["probable pitcher TBD"],
        }

    # team-level status: not_found starter or missing both context fields is a
    # real gap; otherwise confirmed/partial follows the weakest present field.
    if starter["data_status"] == "not_found":
        status = "partial"  # game still has offense/bullpen context; model widens
    elif rg is None and bullpen is None:
        status = "not_found"
    elif warnings or starter["data_status"] == "partial":
        status = "partial"
    else:
        status = "confirmed"

    return {
        "team": abbr,
        "starter": starter,
        "bullpen_ra9": bullpen,
        "rg": rg,
        "data_status": status,
        "warnings": warnings,
    }


def game_id(away_abbr, home_abbr, game_number):
    gid = f"mlb-{away_abbr}-{home_abbr}"
    if game_number and game_number > 1:
        gid += f"-g{game_number}"
    return gid


def build_feed(date, final_snapshot):
    season = int(date[:4])
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    print(f"Building MLB feed for slate {date} (season {season})...")
    fip_const = compute_fip_constant(season)
    print(f"  league FIP constant: {fip_const}")
    savant = load_savant_xstats(season)
    print(f"  Savant xstats: {len(savant)} pitchers")
    rg_map = fetch_all_team_rg(season)
    print(f"  team runs/game: {len(rg_map)} teams")

    schedule = fetch_schedule(date)
    print(f"  scheduled games: {len(schedule)}")

    games = {}
    for g in schedule:
        away_side = g["teams"]["away"]
        home_side = g["teams"]["home"]
        away_abbr = scout_abbr(away_side["team"]["abbreviation"])
        home_abbr = scout_abbr(home_side["team"]["abbreviation"])
        gid = game_id(away_abbr, home_abbr, g.get("gameNumber"))

        away_block = build_team_block(away_side, season, rg_map, savant, fip_const)
        home_block = build_team_block(home_side, season, rg_map, savant, fip_const)

        games[gid] = {
            "game_pk": g.get("gamePk"),
            "game_number": g.get("gameNumber"),
            "start_time_utc": g.get("gameDate"),
            "park_factor": 1.00,  # neutral placeholder — real factors are Phase 2
            "park_factor_status": "placeholder",
            "away": away_block,
            "home": home_block,
        }
        print(f"    {gid}: "
              f"{away_block['starter']['name'] or 'TBD'} ({away_block['starter']['fip']}) @ "
              f"{home_block['starter']['name'] or 'TBD'} ({home_block['starter']['fip']})")

    feed = {
        "slate_date": date,
        "generated_at_utc": now,
        "last_polled_at": now,
        "final_snapshot": final_snapshot,
        "source_versions": {
            "statsapi": "v1",
            "savant_xstats": str(season),
            "fip_constant": fip_const,
        },
        "phase": 1,
        "games": games,
    }
    return feed


def us_slate_date():
    if EASTERN is not None:
        return datetime.datetime.now(EASTERN).date().isoformat()
    return datetime.date.today().isoformat()


def main():
    ap = argparse.ArgumentParser(description="Scout MLB stats feed (Phase 1)")
    ap.add_argument("--date", default=us_slate_date(), help="slate date YYYY-MM-DD (US calendar)")
    ap.add_argument("--final", action="store_true", help="mark final_snapshot=true")
    ap.add_argument("--drive", action="store_true", help="also push to Google Drive 'Scout MLB' folder")
    args = ap.parse_args()

    feed = build_feed(args.date, final_snapshot=args.final)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2)
    print(f"\nWrote {OUT_FILE}  ({len(feed['games'])} games)")

    if args.drive:
        push_to_drive(feed)


def push_to_drive(feed):
    """Reuse the parent repo's drive_push, pointed at a 'Scout MLB' folder."""
    try:
        sys.path.insert(0, os.path.dirname(ROOT))
        import drive_push
        drive_push.DRIVE_FOLDER_NAME = "Scout MLB"
        drive_push._folder_id = None  # reset the parent's cached racing folder id
        ok = drive_push.push_json("scout_mlb.json", feed)
        print("Drive push:", "ok" if ok else "skipped/failed")
    except Exception as e:
        print(f"Drive push error: {e}")


if __name__ == "__main__":
    main()
