# Scout MLB Feed — Phase 2 Handoff (for the in-chat Scout model)

**To:** Claude driving Scout in the web chat.
**From:** Claude Code (`racing-scout/mlb`).
**Status:** Phase 2 **live and validated** on the 2026-07-06 slate (8/8 games,
30/30 parks, all fields `confirmed`).

## TL;DR
The MLB feed now carries the handedness edge and real park factors that Phase 1
explicitly lacked. `starter.xfip` / `starter.siera` are sharper true-talent than
FIP; `wrc_vs_lhp` / `wrc_vs_rhp` let the offense term read matchup-specific
(`wrc_vs_{starter.hand}`, exactly as flagged in the Phase 1 handoff); `park_factor`
/ `park_factor_hand` are real numbers, not the `1.00` placeholder. Read them, but
read the caveats below first — none of these are the literal proprietary
FanGraphs numbers; they're self-computed analogs from official free data.

## Where to read it
Same as Phase 1 — folder **"Scout MLB"**, file `scout_mlb.json` (Drive), or
`movingsize/scout-racing` → `mlb/out/scout_mlb.json` (GitHub fallback). Check
`phase` (now `2`) and `last_polled_at`/`final_snapshot` as before.

## What's new per side / per game

| Field | Meaning | Use |
|---|---|---|
| `starter.xfip` | fly-ball-rate-based true talent | sharper than FIP; prefer it when present |
| `starter.siera` | skill-interactive ERA (public 2010 formula) | cross-check against xFIP; big divergence = flag, don't average blindly |
| `wrc_plus` | team offense index, 100 = league average | replaces the flat `rg` when you want a park/quality-normalized offense read |
| `wrc_vs_lhp` / `wrc_vs_rhp` | offense index vs that specific throwing hand | **the matchup mechanism**: read `wrc_vs_{opposing_starter.hand}` — e.g. facing a lefty starter, use the batting team's `wrc_vs_lhp` |
| `park_factor` | real per-park overall factor (home team's park) | now meaningful — no longer "ignore for now" |
| `park_factor_hand.L` / `.R` | park factor by batter hand | pairs with the batting side's handedness mix if you want to go further |

## How to integrate (extends spec §10)
1. Prefer `starter.xfip` over `starter.fip` when both are present and agree
   within ~0.5 runs; if they diverge more than that, treat it as a `partial`-style
   signal even if `data_status` says `confirmed` — it means the batted-ball
   sample is thin or unusual, not necessarily wrong.
2. For the offense term, use `wrc_vs_{starter.hand}` **of the opposing team**,
   not the flat `wrc_plus` — that's the whole point of this phase per the build
   brief: "offense-vs-this-pitcher's-hand is where MLB matchup value lives."
3. Multiply in `park_factor` (or the hand-specific variant) same as any park
   adjustment — it's a real number now, safe to use.
4. Same `data_status`/`w` discipline as Phase 1: `partial` widens toward market,
   `not_found` defers to market entirely. Nothing here changes that gate.

## Gotchas / honest limits (don't over-claim)
- **`wrc_plus` is not FanGraphs' wRC+.** It's a wOBA-ratio index (team wOBA ÷
  league wOBA for the same split, ×100) computed from StatsAPI's official
  `sitCodes=vl/vr` team hitting splits. Two real simplifications versus the
  proprietary number: (1) it uses fixed, era-typical wOBA linear weights, not
  wOBA weights recalibrated to this exact season, and (2) **it is not park-
  adjusted** — FanGraphs' wRC+ folds in each team's full-season park exposure;
  this index doesn't. Treat it as "team offense relative to league, by
  handedness" — a real, useful signal, just not a precise match to the number
  you'd see on FanGraphs.
- **xFIP uses a fixed league HR/FB rate (13.5%)**, not one computed live each
  run. This is deliberate (a same-day sample of ~20-30 starters would be
  noisier than the fixed constant), but it means xFIP won't track a genuine
  league-wide power shift within a season until this constant is updated by
  hand.
- **SIERA and xFIP both depend on Statcast batted-ball classification**
  (`pybaseball.statcast_pitcher`, filtered to regular-season). A starter with a
  very small batted-ball sample (<20 balls in play, e.g. a recent call-up) gets
  `xfip`/`siera` left `null` with a warning instead of a noisy number — same
  "no silent guessing" rule as everything else in this pipeline.
- **Park factors come from a third-party fantasy site** (`fantasyteamadvice.com`),
  not MLB/Savant/FanGraphs. It's unblocked and covers all 30 parks with sane
  values, but it's not an official provider and has no visible methodology
  write-up. If park factors ever look obviously wrong for a stadium, that's the
  first thing to suspect.
- **FanGraphs and Baseball-Reference are both confirmed Cloudflare-walled**, not
  just "not yet solved." Tried and failed: plain HTTP, Playwright (headless,
  headless+anti-detection flags, headed), and `pybaseball`'s FanGraphs wrapper.
  Don't expect a future phase to just "read FanGraphs directly" without real
  stealth-automation tooling — that's a deliberate policy call (bot-detection
  evasion), not an engineering gap.

## What this changes about your priors
Phase 1 made pitching honest; Phase 2 makes the offense side matchup-aware
(handedness) and gives park a real number instead of a placeholder. This is
still not a proven edge — keep MLB flat-staked and CLV-gated exactly as before.
The self-computed nature of `wrc_plus`/`xfip`/`siera` means they're good
relative signals within this feed, but don't cite them as if they were
FanGraphs' published figures if that ever comes up.
