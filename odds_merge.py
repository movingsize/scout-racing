#!/usr/bin/env python3
"""
odds_merge.py — pure bridge from scout_odds.json (Betfair Exchange) into the MLB
model feed. No Betfair/Playwright imports; just reads a JSON file and reshapes.

This is the DURABLE replacement for the ESPN closing-odds path: mlb_fetch.py
calls apply_odds() in build_feed so every rebuild carries correctly-keyed live
and closing odds natively, and mlb_odds_watcher.py calls it on a loop to keep
them fresh as the Betfair watcher freezes closes. Both live_odds and closing_odds
are sourced from Betfair (native AUD decimal), keyed to teams+date upstream, so
the ESPN carry-forward bug (a prior slate's line on tonight's game) can't occur.

Only moneyline is mapped to away/home (the trustworthy market). total_runs /
run_line carry the raw Betfair live block for reference but are not line-resolved
yet — consumers should price moneyline only. A missing/in-play price is null,
never faked.
"""

import datetime
import json
import os

# Betfair full team name -> the abbrev Scout uses in gids (mlb-<away>-<home>).
# Single source of truth (betfair_odds_fetch imports this).
MLB_TEAM_ABBR = {
    "Arizona Diamondbacks": "ari", "Atlanta Braves": "atl", "Baltimore Orioles": "bal",
    "Boston Red Sox": "bos", "Chicago Cubs": "chc", "Chicago White Sox": "cws",
    "Cincinnati Reds": "cin", "Cleveland Guardians": "cle", "Colorado Rockies": "col",
    "Detroit Tigers": "det", "Houston Astros": "hou", "Kansas City Royals": "kc",
    "Los Angeles Angels": "laa", "Los Angeles Dodgers": "lad", "Miami Marlins": "mia",
    "Milwaukee Brewers": "mil", "Minnesota Twins": "min", "New York Mets": "nym",
    "New York Yankees": "nyy", "Oakland Athletics": "ath", "Athletics": "ath",
    "Philadelphia Phillies": "phi", "Pittsburgh Pirates": "pit", "San Diego Padres": "sd",
    "San Francisco Giants": "sf", "Seattle Mariners": "sea", "St. Louis Cardinals": "stl",
    "Tampa Bay Rays": "tb", "Texas Rangers": "tex", "Toronto Blue Jays": "tor",
    "Washington Nationals": "wsh",
}

LIVE_ODDS_SOURCE = "betfair_exchange_aud"


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def moneyline_by_side(gid, prices):
    """Map {full team name: price} onto away/home using the gid (mlb-<away>-<home>)."""
    parts = gid.split("-")
    away, home = (parts[1], parts[2]) if len(parts) >= 3 else (None, None)
    out = {"away": None, "home": None}
    for name, price in (prices or {}).items():
        ab = MLB_TEAM_ABBR.get(name)
        if ab == away:
            out["away"] = price
        elif ab == home:
            out["home"] = price
    return out


def _live_block(gid, ev):
    """Betfair live pre-off odds for a game, or None if none (in-play/no offers)."""
    markets = (ev or {}).get("markets") or {}
    ml = markets.get("moneyline") or {}
    live = ml.get("live_odds")
    if not live or live.get("inplay") or not live.get("prices"):
        return None
    return {
        "source": LIVE_ODDS_SOURCE,
        "captured_at": live.get("captured_at"),
        "market_status": live.get("market_status"),
        "inplay": live.get("inplay"),
        "moneyline": moneyline_by_side(gid, live.get("prices")),
        "total_runs": (markets.get("total_runs") or {}).get("live_odds"),
        "handicap": (markets.get("handicap") or {}).get("live_odds"),
    }


def _closing_block(gid, ev):
    """Betfair frozen close (CLV anchor) for a game, or None if not yet frozen.
    Moneyline only; total/run_line left null until line-extraction lands, rather
    than emitting the current messy line-less values."""
    ml = (((ev or {}).get("markets") or {}).get("moneyline") or {})
    close = ml.get("closing_odds")
    if not close or not close.get("prices"):
        return None
    return {
        "source": LIVE_ODDS_SOURCE,
        "captured_at": close.get("captured_at"),
        "frozen_at": close.get("frozen_at"),
        "moneyline": moneyline_by_side(gid, close.get("prices")),
        "total": None,
        "run_line": None,
    }


def load_odds_events(odds_path):
    """{gid: event} from scout_odds.json's MLB block, or {} if unavailable.
    A missing/broken odds file must never crash or wipe the model feed."""
    if not os.path.exists(odds_path):
        return {}
    try:
        with open(odds_path, encoding="utf-8") as f:
            return (json.load(f).get("mlb") or {}).get("events") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def apply_odds(feed, odds_path):
    """Set game['live_odds'] and game['closing_odds'] on every game in `feed`
    from scout_odds.json. Returns (live_count, close_count).

    If the odds file is unavailable, leaves the feed untouched (does NOT null
    existing odds) — a transient missing file can't erase a capture."""
    events = load_odds_events(odds_path)
    if not events:
        return (0, 0)
    live_n = close_n = 0
    for gid, game in feed.get("games", {}).items():
        ev = events.get(gid)
        live = _live_block(gid, ev)
        close = _closing_block(gid, ev)
        game["live_odds"] = live
        game["closing_odds"] = close
        live_n += bool(live)
        close_n += bool(close)
    feed["live_odds_source"] = f"{LIVE_ODDS_SOURCE} (scout_odds.json)"
    feed["live_odds_updated_at"] = _iso_now()
    return (live_n, close_n)


if __name__ == "__main__":
    # Standalone self-test against local files (no network).
    root = os.path.dirname(os.path.abspath(__file__))
    mlb_path = os.path.join(root, "mlb", "out", "scout_mlb.json")
    odds_path = os.path.join(root, "scout_odds.json")
    with open(mlb_path, encoding="utf-8") as f:
        feed = json.load(f)
    live_n, close_n = apply_odds(feed, odds_path)
    print(f"apply_odds: {live_n} live / {close_n} closing across {len(feed.get('games', {}))} games")
    for gid in ("mlb-bos-cws", "mlb-mil-stl", "mlb-nyy-tb"):
        g = feed.get("games", {}).get(gid, {})
        print(f"  {gid}: live={g.get('live_odds') and g['live_odds']['moneyline']} "
              f"close={g.get('closing_odds') and g['closing_odds']['moneyline']}")
