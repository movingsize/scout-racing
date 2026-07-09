#!/usr/bin/env python3
"""
Scout — MLB Stats Feed Pipeline (Phase 3)
==========================================
Produces scout_mlb.json for the Scout MLB model (mlbWinProb / MLB_INPUTS in
scout-portal.jsx). Replaces the model's hand-eyeballed ERA + runs/game inputs
with true-talent metrics pulled from official, script-friendly APIs.

WHAT THIS BUILDS (Phase 3 — see mlb/README.md for what's still gated)
------------------------------------------------------------------------
Per game on the slate:
  - starter true-talent:  FIP + xFIP + SIERA (all computed locally) and
                          Statcast xERA / xwOBA as the quality cross-check
  - team offense:         runs/game, and a wRC+-style offense index overall
                          and vs LHP / vs RHP (self-computed, see below)
  - bullpen:              relief RA9
  - park factor:          real per-park factor overall + by batter hand
  - closing_odds:         moneyline/total/run-line, captured near each game's
                          own first pitch (see mlb_odds_watcher.py -- this
                          file only builds the fresh Phase 1/2 fields and
                          carries forward whatever closing_odds the watcher
                          already captured today)
Each starter / team block carries data_status (confirmed|partial|not_found)
and a warnings[] list — the racing-pipeline "never emit a silent null" lesson.

DATA SOURCES (validated 2026-07-06; deviates from the original spec — see below)
--------------------------------------------------------------------------------
1. MLB StatsAPI (statsapi.mlb.com, free, official, no key)
     - schedule, probable pitchers, MLBAM ids, start times, gameNumber
     - per-pitcher season counting stats  -> FIP/xFIP/SIERA are COMPUTED here
     - team hitting (runs/game, vs LHP/RHP splits) and reliever split (bullpen RA9)
2. Baseball Savant (baseballsavant.mlb.com, CSV leaderboard + Statcast search, no key)
     - starter xERA / xwOBA (Statcast true-talent), joined on player_id = MLBAM
     - per-starter batted-ball type (GB/FB/LD/PU), via pybaseball, for xFIP/SIERA
3. fantasyteamadvice.com (third-party fantasy site, no key)
     - park factors overall + by batter hand (see SPEC DEVIATION)

SPEC DEVIATION: the spec named FanGraphs as the primary source for FIP/xFIP/SIERA/
wRC+, and assumed Savant's own park-factors leaderboard was a key-less CSV win.
Both verified false as of 2026-07-06:
  - FanGraphs sits behind a Cloudflare JS challenge (HTTP 403) to any scripted
    request -- plain HTTP, Playwright (headless, headless+anti-detection flags,
    and headed), and pybaseball's FanGraphs wrapper all hit the identical wall.
    Baseball-Reference is Cloudflare-walled the same way.
  - Savant's park-factors leaderboard page no longer renders any data via
    script: `csv=true` returns the full HTML page, and the page's own JS bundle
    expects a `data` array that stays empty through a full page load, an
    explicit "Update" click, and a wait, in both the current and a completed
    season. No replacement XHR/CSV endpoint could be found.
This phase therefore:
  - COMPUTES FIP/xFIP/SIERA from StatsAPI + Statcast batted-ball data, and
  - COMPUTES a wRC+-style offense index from StatsAPI's official vl/vr hitting
    splits + a fixed wOBA-weights formula (not FanGraphs' proprietary, season-
    recalibrated number -- see WOBA_WEIGHTS / wrc_plus_index comments), and
  - reads park factors from fantasyteamadvice.com, a third-party site (not an
    official provider, but unblocked and robots.txt-allows it).

OUTPUT
------
mlb/out/scout_mlb.json  (strict unchanging filename, overwritten in place)
Optionally mirrored to a "Scout MLB" Google Drive folder via the parent repo's
drive_push.py (reuses racing-scout/token.json). Run with --drive to enable.

USAGE
-----
    python mlb/mlb_fetch.py                 # today's US slate, local file only
    python mlb/mlb_fetch.py --date 2026-07-06
    python mlb/mlb_fetch.py --final         # also run one closing-odds capture pass now
    python mlb/mlb_fetch.py --drive         # also push to Google Drive
    python mlb/mlb_odds_watcher.py          # continuous per-game closing-odds capture
                                            # (the real Phase 3 mechanism; --final is
                                            # for a manual/ad-hoc one-off pass)

Requires: requests, pybaseball, beautifulsoup4 (pip install -r the three).
xFIP/SIERA and park factors degrade to null/placeholder with a warning if
pybaseball / beautifulsoup4 aren't installed -- they never block the Phase 1
fields from writing.
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

# Phase 2 deps. Both optional at import time -- missing either degrades the
# specific fields they power (xFIP/SIERA, park factors) to "unavailable" with a
# warning, rather than crashing the whole Phase 1 feed.
try:
    import pybaseball
    pybaseball.cache.enable()
    HAVE_PYBASEBALL = True
except ImportError:
    HAVE_PYBASEBALL = False

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
OUT_FILE = os.path.join(OUT_DIR, "scout_mlb.json")

sys.path.insert(0, os.path.dirname(ROOT))
import data_adequacy  # DATA BAD guardrail: stamps a call verdict on every game
import odds_merge      # native Betfair live+closing odds (replaces the ESPN path)

ODDS_FILE = os.path.join(os.path.dirname(ROOT), "scout_odds.json")

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
    "xfip": (1.5, 7.0),
    "siera": (1.5, 7.0),
    "wrc_plus": (50, 160),
    "park_factor": (0.85, 1.20),
}

# Fallback FIP constant if league totals can't be computed. Real constant is
# derived from league totals each run (see compute_fip_constant).
FIP_FALLBACK_CONST = 3.15

# ─── Phase 2 sources & constants ───────────────────────────────────────────────
# FanGraphs is Cloudflare-walled (HTTP 403 "Just a moment..." to any scripted
# request, including a headless Playwright browser with anti-detection flags --
# verified 2026-07-06). Baseball-Reference is also Cloudflare-walled the same
# way. pybaseball's FanGraphs wrapper hits the identical 403 (it's just an HTTP
# client over the same walled endpoint). xFIP/SIERA/wRC+ are therefore computed
# here from official free sources instead of read off a FanGraphs table -- the
# same approach Phase 1 already takes for FIP.

# Savant's own park-factors leaderboard (the "known key-less win" Phase 1's
# README assumed) no longer works either: `csv=true` returns the full HTML page
# instead of CSV, and the page's own JS bundle expects a `data` array that stays
# empty even after full page load, an explicit "Update" click, and a wait -- in
# both the current season and a completed one. No replacement XHR/CSV endpoint
# could be found (verified 2026-07-06). fantasyteamadvice.com is a third-party
# fantasy-baseball site, not an official stats provider, but it is unblocked,
# robots.txt-allows scraping, and its table covers all 30 parks with sane
# (0.92-1.15) values split by batter hand. Revisit if Savant's page is ever
# fixed, or if this site changes format / goes down.
FTA_PARK_FACTORS_URL = "https://fantasyteamadvice.com/mlb/park-factors"

# Fixed, era-typical wOBA linear weights (the Tango/"The Book" family of
# constants), not recalibrated per season the way FanGraphs republishes its
# wOBA constants every year. Good enough for a same-season team-vs-league-
# average ratio (which is all the wRC+-style index below needs); not exact
# run-value precision.
WOBA_WEIGHTS = {"bb": 0.69, "hbp": 0.72, "1b": 0.89, "2b": 1.27, "3b": 1.62, "hr": 2.10}

# League-average HR/FB rate for xFIP's "expected home runs" term. Fixed rather
# than computed live each run (a live figure would need a bulk league-wide
# Statcast pull; a same-day slate-only sample of ~20-30 starters would be a
# noisier estimate than this well-documented modern-era constant, not a more
# rigorous one). Revisit if MLB's power environment shifts materially.
XFIP_LG_HRFB = 0.135

# Statcast batted-ball pull window start. Regular season only either way --
# game_type is filtered to "R" after the fetch -- so an early start just costs a
# few wasted empty weeks, not correctness.
STATCAST_SEASON_START = "{season}-03-01"

# ─── Phase 3: closing-odds capture (ESPN scoreboard, DraftKings) ──────────────
# The Odds API would need a signed-up key; ESPN's public scoreboard API needs
# none and was confirmed (2026-07-06) to carry live moneyline/total/run-line
# odds for every game on the slate, keyed by the same team abbreviations
# StatsAPI uses. "Consistency > book identity" per the Phase 3 spec -- one
# book (DraftKings, ESPN's priority-1 provider) beats chasing a sharper line.
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={date}"

# Capture window around each game's first pitch: from LEAD_SECONDS before start
# through GRACE_SECONDS after, so a scheduler wake-up a few minutes late (or a
# manual --final run) still catches it. Shared between the --final one-shot
# pass here and mlb_odds_watcher.py's continuous loop.
CLOSING_LEAD_SECONDS = 300
CLOSING_GRACE_SECONDS = 1200


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


# ─── Phase 2: park factors (fantasyteamadvice.com) ─────────────────────────────
# StatsAPI dropped the city from this team's name during their Oakland ->
# Sacramento relocation limbo ("Athletics"); fantasyteamadvice.com still lists
# them under their pre-move name. Add to this map if another source/rename
# mismatch turns up.
TEAM_NAME_ALIAS = {"Oakland Athletics": "Athletics"}


def fetch_team_name_to_abbr(season):
    """Full team name (e.g. "Pittsburgh Pirates") -> Scout abbr, for joining
    third-party sources that key by name instead of the StatsAPI abbreviation."""
    out = {}
    try:
        data = get_json(f"{STATSAPI}/teams?sportId=1&season={season}")
        for t in data.get("teams", []):
            abbr = scout_abbr(t["abbreviation"])
            out[t["name"]] = abbr
        for alias, canonical in TEAM_NAME_ALIAS.items():
            if canonical in out:
                out[alias] = out[canonical]
    except Exception as e:
        print(f"  ! team name map unavailable ({e})")
    return out


def fetch_park_factors(name_to_abbr):
    """{abbr: {park_factor, park_factor_hand}} from fantasyteamadvice.com (see
    Phase 2 sources comment above for why not Savant/FanGraphs). Uses the "Run"
    columns (overall scoring impact) rather than "Power" (HR-specific)."""
    out = {}
    if not HAVE_BS4:
        print("  ! park factors skipped (missing 'beautifulsoup4')")
        return out
    try:
        html = get_text(FTA_PARK_FACTORS_URL)
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find("tbody").find_all("tr") if table else []
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            link = tds[1].find("a", title=True)
            abbr = name_to_abbr.get(link["title"]) if link else None
            if not abbr:
                continue
            try:
                lhb_run = float(tds[4].get_text(strip=True))
                rhb_run = float(tds[5].get_text(strip=True))
            except ValueError:
                continue
            out[abbr] = {
                "park_factor": round((lhb_run + rhb_run) / 2, 2),
                "park_factor_hand": {"L": lhb_run, "R": rhb_run},
            }
    except Exception as e:
        print(f"  ! park factors unavailable ({e})")
    return out


# ─── Phase 2: wRC+-style handedness offense index (StatsAPI vl/vr splits) ─────
_split_cache = {}


def fetch_hitting_split(team_id, season, sitcode):
    """Team hitting stat block for one vs-hand split. sitCodes vl/vr are the
    same official StatsAPI mechanism as the existing bullpen sitCodes=rp split."""
    key = (team_id, sitcode)
    if key in _split_cache:
        return _split_cache[key]
    stat = None
    try:
        data = get_json(
            f"{STATSAPI}/teams/{team_id}/stats?season={season}"
            f"&group=hitting&stats=statSplits&sitCodes={sitcode}"
        )
        for grp in data.get("stats", []):
            for sp in grp.get("splits", []):
                stat = sp["stat"]
    except Exception as e:
        print(f"  ! hitting split {sitcode} unavailable for team {team_id} ({e})")
    _split_cache[key] = stat
    return stat


def woba_num_den(stat):
    """wOBA numerator/denominator from a StatsAPI hitting stat block (see
    WOBA_WEIGHTS comment for the fixed-weights caveat)."""
    if not stat:
        return None, None
    ab = stat.get("atBats", 0) or 0
    h = stat.get("hits", 0) or 0
    doubles = stat.get("doubles", 0) or 0
    triples = stat.get("triples", 0) or 0
    hr = stat.get("homeRuns", 0) or 0
    singles = h - doubles - triples - hr
    bb = (stat.get("baseOnBalls", 0) or 0) - (stat.get("intentionalWalks", 0) or 0)
    hbp = stat.get("hitByPitch", 0) or 0
    sf = stat.get("sacFlies", 0) or 0
    w = WOBA_WEIGHTS
    num = (w["bb"] * bb + w["hbp"] * hbp + w["1b"] * singles
           + w["2b"] * doubles + w["3b"] * triples + w["hr"] * hr)
    den = ab + bb + sf + hbp
    return num, (den if den > 0 else None)


def fetch_league_split_totals(season, sitcode, team_ids):
    """Sum vl/vr wOBA components across all 30 teams once per run -- the 100
    baseline for the wRC+-style index below."""
    lg_num = lg_den = 0.0
    for tid in team_ids:
        stat = fetch_hitting_split(tid, season, sitcode)
        num, den = woba_num_den(stat)
        if num is not None:
            lg_num += num
            lg_den += den
    return lg_num, lg_den


def wrc_plus_index(team_stat, lg_num, lg_den):
    """Self-computed wOBA-ratio offense index scaled to 100 = league average for
    the same split. This approximates wRC+ (same underlying wOBA idea) but is
    NOT FanGraphs' actual wRC+ number: no park adjustment (that would need a
    full schedule-weighted build, not just a stadium-level factor), and fixed
    rather than season-recalibrated wOBA weights. Good enough for a relative
    vs-hand offense signal, which is the actual matchup-value mechanism the
    brief calls for."""
    num, den = woba_num_den(team_stat)
    if num is None or den is None or not lg_den:
        return None
    lg_woba = lg_num / lg_den
    if lg_woba == 0:
        return None
    return round(100 * (num / den) / lg_woba)


# ─── Phase 2: xFIP / SIERA (Statcast batted-ball type per starter) ─────────────
_battedball_cache = {}


def fetch_pitcher_battedball(mlbam_id, season, through_date):
    """GB/FB/LD/PU counts for a starter's regular-season batted balls allowed,
    via Statcast per-pitch bb_type (pybaseball.statcast_pitcher). Needed for
    xFIP (fly-ball rate) and SIERA (GB-FB-PU term) -- neither is derivable from
    StatsAPI's box-score fields alone. Filters to game_type == "R" since the
    date-range query includes spring training by default."""
    if mlbam_id in _battedball_cache:
        return _battedball_cache[mlbam_id]
    result = None
    if not HAVE_PYBASEBALL:
        _battedball_cache[mlbam_id] = None
        return None
    try:
        start = STATCAST_SEASON_START.format(season=season)
        df = pybaseball.statcast_pitcher(start, through_date, player_id=mlbam_id)
        if df is not None and len(df):
            df = df[df["game_type"] == "R"]
            counts = df["bb_type"].value_counts()
            result = {
                "gb": int(counts.get("ground_ball", 0)),
                "fb": int(counts.get("fly_ball", 0)),
                "ld": int(counts.get("line_drive", 0)),
                "pu": int(counts.get("popup", 0)),
            }
    except Exception as e:
        print(f"  ! Statcast batted-ball data unavailable for pitcher {mlbam_id} ({e})")
    _battedball_cache[mlbam_id] = result
    return result


def compute_xfip(stat, ip, bb_data, const):
    if not bb_data or not ip or ip <= 0:
        return None
    fb = bb_data["fb"]
    k = float(stat.get("strikeOuts", 0) or 0)
    bb = float(stat.get("baseOnBalls", 0) or 0)
    hbp = float(stat.get("hitByPitch", 0) or 0)
    xfip = (13 * (fb * XFIP_LG_HRFB) + 3 * (bb + hbp) - 2 * k) / ip + const
    return round(xfip, 2)


def compute_siera(stat, bb_data):
    """Public SIERA formula (Swartz, Baseball Prospectus 2010)."""
    pa = stat.get("battersFaced")
    if not bb_data or not pa:
        return None
    pa = float(pa)
    k = float(stat.get("strikeOuts", 0) or 0) / pa
    bb = float(stat.get("baseOnBalls", 0) or 0) / pa
    net_gb = (bb_data["gb"] - bb_data["fb"] - bb_data["pu"]) / pa
    sign = -1 if net_gb > 0 else 1
    siera = (
        6.145 - 16.986 * k + 11.434 * bb - 1.858 * net_gb
        + 7.653 * (k ** 2) + sign * 6.664 * (net_gb ** 2)
        + 10.130 * k * net_gb - 5.195 * bb * net_gb
    )
    return round(siera, 2)


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


def fetch_pitcher(mlbam_id, season, fip_const, savant, through_date):
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
            "xfip": None, "siera": None,
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
    xfip = siera = None
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

        bb_data = fetch_pitcher_battedball(mlbam_id, season, through_date)
        balls_in_play = sum(bb_data.values()) if bb_data else 0
        if bb_data and balls_in_play >= 20:
            xfip = compute_xfip(s, ip, bb_data, fip_const)
            siera = compute_siera(s, bb_data)
            if xfip is not None and not in_range("xfip", xfip):
                warnings.append(f"xFIP {xfip} out of sane range")
                status = "partial"
            if siera is not None and not in_range("siera", siera):
                warnings.append(f"SIERA {siera} out of sane range")
                status = "partial"
        else:
            warnings.append(
                "no xFIP/SIERA (Statcast unavailable)" if bb_data is None
                else f"no xFIP/SIERA (low batted-ball sample: {balls_in_play})"
            )
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
        "xfip": xfip, "siera": siera,
        "data_status": status,
        "warnings": warnings,
    }
    _pitcher_cache[mlbam_id] = starter
    return starter


def build_team_block(team_side, season, rg_map, savant, fip_const, lg_woba, through_date):
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

    vl_stat = fetch_hitting_split(tid, season, "vl")
    vr_stat = fetch_hitting_split(tid, season, "vr")
    wrc_vs_lhp = wrc_plus_index(vl_stat, *lg_woba["vl"])
    wrc_vs_rhp = wrc_plus_index(vr_stat, *lg_woba["vr"])
    wrc_plus = None
    if wrc_vs_lhp is None or wrc_vs_rhp is None:
        warnings.append("wRC+ handedness split unavailable")
    else:
        for label, val in (("wrc_vs_lhp", wrc_vs_lhp), ("wrc_vs_rhp", wrc_vs_rhp)):
            if not in_range("wrc_plus", val):
                warnings.append(f"{label} {val} out of sane range")
        pa_l = (vl_stat or {}).get("plateAppearances", 0) or 0
        pa_r = (vr_stat or {}).get("plateAppearances", 0) or 0
        if pa_l + pa_r > 0:
            wrc_plus = round((wrc_vs_lhp * pa_l + wrc_vs_rhp * pa_r) / (pa_l + pa_r))

    prob = team_side.get("probablePitcher")
    if prob and prob.get("id"):
        starter = fetch_pitcher(prob["id"], season, fip_const, savant, through_date)
    else:
        starter = {
            "name": None, "mlbam_id": None, "hand": None,
            "fip": None, "era": None, "ip": None, "xera": None, "xwoba": None,
            "xfip": None, "siera": None,
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
        "wrc_plus": wrc_plus, "wrc_vs_lhp": wrc_vs_lhp, "wrc_vs_rhp": wrc_vs_rhp,
        "data_status": status,
        "warnings": warnings,
    }


def game_id(away_abbr, home_abbr, game_number):
    gid = f"mlb-{away_abbr}-{home_abbr}"
    if game_number and game_number > 1:
        gid += f"-g{game_number}"
    return gid


def iso_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ─── Phase 3: closing-odds capture (ESPN scoreboard, DraftKings) ──────────────
def american_to_decimal(american):
    """American odds string (e.g. "+144", "-175") -> decimal. None if missing
    or a "-0"/"+0" placeholder (ESPN uses these for an unset side)."""
    if american in (None, "", "-0", "+0"):
        return None
    try:
        a = int(american)
    except (TypeError, ValueError):
        return None
    if a > 0:
        return round(1 + a / 100, 2)
    if a < 0:
        return round(1 + 100 / abs(a), 2)
    return None


def _ou_line(raw):
    """"o8" / "u8.5" -> 8.0 / 8.5."""
    if not raw or len(raw) < 2:
        return None
    try:
        return float(raw[1:])
    except ValueError:
        return None


def _rl_line(raw):
    """"+1.5" / "-1.5" -> 1.5 (run-line size, sign doesn't matter here)."""
    if not raw:
        return None
    try:
        return abs(float(raw))
    except ValueError:
        return None


def _build_closing_odds(odds_entry):
    """One ESPN `odds[]` entry -> our closing_odds shape, or None if the book
    hasn't posted a moneyline yet (e.g. very early pre-game)."""
    ml = odds_entry.get("moneyline") or {}
    tot = odds_entry.get("total") or {}
    rl = odds_entry.get("pointSpread") or {}
    ml_away = american_to_decimal(((ml.get("away") or {}).get("close") or {}).get("odds"))
    ml_home = american_to_decimal(((ml.get("home") or {}).get("close") or {}).get("odds"))
    if ml_away is None and ml_home is None:
        return None
    prov = odds_entry.get("provider") or {}
    provider = prov.get("name") or prov.get("displayName") or "unknown"
    return {
        "captured_at_utc": iso_now(),
        "source": f"espn/{provider}",
        "moneyline": {"away": ml_away, "home": ml_home},
        "total": {
            "line": _ou_line(((tot.get("over") or {}).get("close") or {}).get("line")),
            "over": american_to_decimal(((tot.get("over") or {}).get("close") or {}).get("odds")),
            "under": american_to_decimal(((tot.get("under") or {}).get("close") or {}).get("odds")),
        },
        "run_line": {
            "line": _rl_line(((rl.get("away") or {}).get("close") or {}).get("line")),
            "away": american_to_decimal(((rl.get("away") or {}).get("close") or {}).get("odds")),
            "home": american_to_decimal(((rl.get("home") or {}).get("close") or {}).get("odds")),
        },
    }


def fetch_espn_odds(date):
    """{gid: closing_odds dict} for every game on one US slate date, from
    ESPN's public scoreboard API (no key). A second game between the same
    two teams that day (doubleheader) is suffixed -g2, matching game_id()'s
    convention -- ESPN's own doubleheader marking wasn't validated, so this
    is a same-order-as-StatsAPI assumption, not a confirmed join key."""
    out = {}
    try:
        data = get_json(ESPN_SCOREBOARD_URL.format(date=date.replace("-", "")))
        for ev in data.get("events", []):
            comp = ev["competitions"][0]
            competitors = comp.get("competitors", [])
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            if not away or not home:
                continue
            away_abbr = scout_abbr(away["team"]["abbreviation"])
            home_abbr = scout_abbr(home["team"]["abbreviation"])
            gid = f"mlb-{away_abbr}-{home_abbr}"
            if gid in out:
                gid += "-g2"
            odds_list = comp.get("odds") or []
            if not odds_list:
                continue
            entry = _build_closing_odds(odds_list[0])
            if entry:
                out[gid] = entry
    except Exception as e:
        print(f"  ! ESPN odds unavailable ({e})")
    return out


def capture_eligible_closing_odds(games, espn_odds):
    """Write closing_odds into any game whose capture window (T-5min through
    T+20min around first pitch) has arrived and hasn't been captured yet.
    Shared between the --final one-shot pass below and
    mlb_odds_watcher.py's continuous per-game loop. Mutates `games` in place;
    returns the list of gids captured this call."""
    now = datetime.datetime.now(datetime.timezone.utc)
    captured = []
    for gid, g in games.items():
        if g.get("closing_odds"):
            continue
        start = datetime.datetime.fromisoformat(g["start_time_utc"].replace("Z", "+00:00"))
        seconds_to_start = (start - now).total_seconds()
        if -CLOSING_GRACE_SECONDS <= seconds_to_start <= CLOSING_LEAD_SECONDS:
            odds = espn_odds.get(gid)
            if odds:
                g["closing_odds"] = odds
                captured.append(gid)
            else:
                print(f"  ! {gid} is in its closing-odds window but ESPN had no odds for it")
    return captured


def build_feed(date):
    season = int(date[:4])
    now = iso_now()

    print(f"Building MLB feed for slate {date} (season {season})...")
    fip_const = compute_fip_constant(season)
    print(f"  league FIP constant: {fip_const}")
    savant = load_savant_xstats(season)
    print(f"  Savant xstats: {len(savant)} pitchers")
    rg_map = fetch_all_team_rg(season)
    print(f"  team runs/game: {len(rg_map)} teams")

    name_to_abbr = fetch_team_name_to_abbr(season)
    park_factors = fetch_park_factors(name_to_abbr)
    print(f"  park factors: {len(park_factors)}/30 parks (fantasyteamadvice.com)")

    team_ids = list(rg_map.keys())
    lg_woba = {
        "vl": fetch_league_split_totals(season, "vl", team_ids),
        "vr": fetch_league_split_totals(season, "vr", team_ids),
    }
    if lg_woba["vl"][1] and lg_woba["vr"][1]:
        print(f"  league wOBA baseline: vs L {lg_woba['vl'][0]/lg_woba['vl'][1]:.3f} / "
              f"vs R {lg_woba['vr'][0]/lg_woba['vr'][1]:.3f}")
    else:
        print("  ! league wOBA baseline unavailable; wRC+ will be null league-wide")

    schedule = fetch_schedule(date)
    print(f"  scheduled games: {len(schedule)}")

    games = {}
    for g in schedule:
        away_side = g["teams"]["away"]
        home_side = g["teams"]["home"]
        away_abbr = scout_abbr(away_side["team"]["abbreviation"])
        home_abbr = scout_abbr(home_side["team"]["abbreviation"])
        gid = game_id(away_abbr, home_abbr, g.get("gameNumber"))

        away_block = build_team_block(away_side, season, rg_map, savant, fip_const, lg_woba, date)
        home_block = build_team_block(home_side, season, rg_map, savant, fip_const, lg_woba, date)

        pf = park_factors.get(home_abbr)
        games[gid] = {
            "game_pk": g.get("gamePk"),
            "game_number": g.get("gameNumber"),
            "start_time_utc": g.get("gameDate"),
            "park_factor": pf["park_factor"] if pf else 1.00,
            "park_factor_hand": pf["park_factor_hand"] if pf else {"L": 1.00, "R": 1.00},
            "park_factor_status": "confirmed" if pf else "placeholder",
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
        "final_snapshot": False,  # recomputed in main() once closing_odds are merged in
        "source_versions": {
            "statsapi": "v1",
            "savant_xstats": str(season),
            "fip_constant": fip_const,
            "park_factors_source": "fantasyteamadvice.com",
            "xfip_lg_hrfb": XFIP_LG_HRFB,
            "closing_odds_source": "betfair_exchange_aud",
        },
        "phase": 3,
        "games": games,
    }
    # Native odds: inject correctly-keyed Betfair live_odds + closing_odds from
    # scout_odds.json (replaces the ESPN closing path). Missing odds file is a
    # no-op, never wipes anything.
    live_n, close_n = odds_merge.apply_odds(feed, ODDS_FILE)
    print(f"  Betfair odds merged: {live_n} live / {close_n} closing")
    # Guardrail: label every game OK / NO_CALL ("DATA BAD") — a game with no
    # pitching/offense to anchor either side must never be priced off de-vig.
    data_adequacy.enforce_mlb(feed)
    return feed


def us_slate_date():
    if EASTERN is not None:
        return datetime.datetime.now(EASTERN).date().isoformat()
    return datetime.date.today().isoformat()


def load_existing_games(path):
    """Previous run's games, keyed by gid -- used to carry closing_odds
    forward across a rebuild (see main()). Returns {} if no prior file, or on
    any read failure (a corrupt/partial file shouldn't crash a fresh build)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("games", {})
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="Scout MLB stats feed (Phase 3)")
    ap.add_argument("--date", default=us_slate_date(), help="slate date YYYY-MM-DD (US calendar)")
    ap.add_argument("--final", action="store_true",
                     help="DEPRECATED no-op. Closing odds now come from Betfair via "
                          "scout_odds.json (see odds_merge / betfair_odds_watcher); the "
                          "ESPN capture this flag used to trigger has been retired.")
    ap.add_argument("--drive", action="store_true", help="also push to Google Drive 'Scout MLB' folder")
    args = ap.parse_args()

    # Odds (live_odds + closing_odds) are merged natively inside build_feed from
    # scout_odds.json, which is Betfair's write-once durable store. No ESPN fetch
    # and no cross-run carry-forward needed: closing is re-read from that store
    # every build, and a frozen close there is stable.
    feed = build_feed(args.date)

    if args.final:
        print("  (--final is deprecated: closing odds come from Betfair/scout_odds.json now)")

    # final_snapshot means "today's whole slate has closed" -- true only once
    # every game has a captured close, which for a normal staggered slate
    # won't happen until the last game's own T-5min window passes. Check
    # per-game closing_odds presence for game-by-game gating; don't wait on
    # this flag to start reading individual games' closes.
    feed["final_snapshot"] = bool(feed["games"]) and all(
        g.get("closing_odds") for g in feed["games"].values()
    )

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
