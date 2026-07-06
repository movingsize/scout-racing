# Scout MLB Feed — Phase 3 Handoff (for the in-chat Scout model)

**To:** Claude driving Scout in the web chat.
**From:** Claude Code (`racing-scout/mlb`).
**Status:** Phase 3 Priority 1 (closing-odds capture) **live**. Priority 2
(run-distribution) is your own build per the spec's ownership table — the
pipeline's job there was just making sure `closing_odds.total`/`.run_line` are
present, which they are. Priority 3 (recent-form/bullpen) is **not built**,
per the spec's own gating (don't build until CLV shows the core model has a
pulse).

## TL;DR
Every game now gets a `closing_odds` block once its own capture window
arrives (~5 min before first pitch through ~20 min after) — moneyline, total,
and run-line, all decimal, from ESPN's DraftKings feed. This is the keystone
piece: read it at settlement, write `closePrice` onto the RESULTS entry, and
the Diagnostics CLV panel is live. **Nothing about `mlbBlend` should move
until CLV says so** — that hasn't changed.

## Where to read it
Same as before — folder "Scout MLB", file `scout_mlb.json` (Drive), or
`movingsize/scout-racing` → `mlb/out/scout_mlb.json` (GitHub fallback). Check
`phase` (now `3`).

## The one thing that changed: `final_snapshot`'s meaning

In Phase 1/2, `final_snapshot` was a flag a human/script set on the pre-lock
run. **In Phase 3 it's computed**: `true` only once *every* game in `games`
has a `closing_odds` block. Because a slate's games are staggered across
several hours, this stays `false` for most of the day even as individual
games' closes land one by one. **Don't gate per-game CLV reads on the
top-level `final_snapshot` flag — check `game.closing_odds` presence
per-game.** The flag is only useful as a "has the whole day's slate finished
closing" signal, not a per-bet one.

## `closing_odds` schema (per game)

```jsonc
"closing_odds": {
  "captured_at_utc": "2026-07-06T18:05:12Z",
  "source": "espn/DraftKings",
  "moneyline": { "away": 1.57, "home": 2.44 },
  "total":     { "line": 8.0, "over": 1.88, "under": 1.95 },
  "run_line":  { "line": 1.5, "away": 1.98, "home": 1.85 }
}
```
Absent entirely until captured — never a fake/placeholder value. A
sub-field can be `null` if that specific market was suspended at capture time
(ESPN returns `"OFF"` for a temporarily-pulled line, e.g. around a pitching
change) — `moneyline` is what gates whether the block gets written at all;
`total`/`run_line` can have `null` line/price even when `moneyline` is solid.

## How to integrate (Priority 1)
1. At settlement, match the bet's game + market + side against that game's
   `closing_odds`, and write `closePrice` onto the RESULTS entry exactly as
   already planned.
2. The Diagnostics CLV panel computes avg CLV / beat-close rate as before.
3. **Raise `mlbBlend` above 0.5 only on positive, consistent CLV over ~25-30
   bets** — this rule is unchanged, and this phase's entire purpose was to
   make that measurement possible, not to preempt it.

## How to integrate (Priority 2 — your build)
The pipeline now carries `closing_odds.total` and `closing_odds.run_line`
alongside `moneyline` in the same capture, so your run-distribution model has
the lines it needs (current/live odds, not just the close, would need a
different read — this feed only captures the close, once, per game; if you
need pre-close totals/RL for other purposes, that's a separate ask). Model
building, blending, and CLV-gating for Totals/RL is entirely your side per the
ownership table — nothing here changes.

## Honest limits (don't over-claim)
- **Single book (DraftKings via ESPN), not a sharp close.** The spec said
  "consistency > book identity" — fine for CLV, but don't treat this as a
  Pinnacle-grade line.
- **T-5min timing is unvalidated against many real slates yet.** If CLV
  numbers look systematically skewed in one direction, check whether the
  watcher is actually firing near first pitch (it runs as a Scheduled Task,
  `ScoutMLBOddsWatcher`) before doubting the model itself.
- **Doubleheader game-2 matching (`-g2`) is an assumption**, not validated
  against a real one yet — flag it if a doubleheader's `closing_odds` ever
  looks attached to the wrong game.
- Same "no silent guessing" discipline as everything else in this feed: a
  missing `closing_odds` block means not-yet-captured or a real capture
  failure (check the watcher's run log), never a stand-in number.

## What this changes about your priors
This is the keystone the last two phases were building toward: the model is
no longer stuck at a frozen, unvalidated `w=0.5`. Once ~25-30 bets have real
`closePrice` data, the CLV panel gives an honest answer about whether MLB has
moved past "market-level" — and only then does raising `mlbBlend`, or trusting
xFIP/SIERA/wRC+ more than the market, become licensed. Until that reads
positive and consistent, treat every Phase 1-3 input exactly as skeptically as
before.
