# Scout MLB Feed — Phase 1 Handoff (for the in-chat Scout model)

**To:** Claude driving Scout in the web chat.
**From:** Claude Code (`racing-scout/mlb`).
**Status:** Phase 1 **live and validated** on the 2026-07-06 slate. Both delivery
paths up.

## TL;DR
The MLB model no longer needs hand-eyeballed ERA + runs/game. A real feed now
exists with **true-talent pitching (FIP), Statcast xERA/xwOBA, team R/G, and
bullpen RA9** for every game on the slate. Read it, map it into `MLB_INPUTS`, and
let `mlbWinProb()` drive `modelProb` at a **cautious blend** — do **not** trust
it fully until CLV validates.

## Where to read it
- **Primary (Drive connector):** folder **"Scout MLB"**, file **`scout_mlb.json`**
  (`search_files` → `read_file_content`). This is the same path you already use
  for racing.
- **Fallback (GitHub):** `movingsize/scout-racing` → `mlb/out/scout_mlb.json`.
- Check `last_polled_at` and `final_snapshot` for freshness, same discipline as
  racing. A morning file is `final_snapshot: false`; the pre-lock refresh is
  `true`.

## What's in each game
Keys are `mlb-{away}-{home}` (Scout's existing convention; `-g2` for
doubleheaders; `AZ→ari`). Per side:

| Field | Meaning | Use |
|---|---|---|
| `starter.fip` | true-talent run prevention (computed) | **primary pitching input** — replaces ERA |
| `starter.xera`, `starter.xwoba` | Statcast expected | quality cross-check / blend |
| `starter.era`, `ip`, `hand` | reference / sample size / handedness | context |
| `bullpen_ra9` | relief runs·9/IP | bullpen input |
| `rg` | team runs/game | offense context |
| `park_factor` | **1.00 placeholder** | **ignore for now** — real factors are Phase 2 |

## How to integrate (spec §10, unchanged)
1. Map `scout_mlb.json` → `MLB_INPUTS`. `starter.fip` is the metric the model's
   pitching term should read (it already accepted a single `fip`-shaped field).
2. Drive `modelProb` via `w · mlbWinProb() + (1 − w) · marketDeVig`.
   - **Start `w = 0.5`.** It is unproven on clean data (MLB was +2.9pt, z=0.22).
   - **Raise `w` only when CLV confirms** — positive, consistent CLV over ~25–30
     bets. *Not* win/loss ROI. The Diagnostics CLV panel is the gate.
3. **Honour `data_status`:**
   - `confirmed` → normal blend.
   - `partial` → widen toward market (lower effective `w`).
   - `not_found` (e.g. TBD probable) → **defer to market entirely**, exactly like
     an unscored racing runner. Never invent a number for these.

## Gotchas / honest limits (don't over-claim)
- **FIP is computed here, not FanGraphs.** FanGraphs is Cloudflare-blocked to
  scripts. It's a solid true-talent metric, but it's `(13·HR+3·(BB+HBP)−2·K)/IP`
  + a league constant — no xFIP/SIERA yet.
- **No handedness, no wRC+, no park factors yet.** So the model is still
  matchup-blind on offense-vs-hand and park. Treat its offense/park terms as
  coarse. All three land in Phase 2.
- **`park_factor` is a hardcoded 1.00 placeholder** — carries `park_factor_status:
  "placeholder"`. Do not read edge into it.
- Accented names serialize as `á` (standard JSON) — they parse to "Sánchez"
  correctly; nothing to fix.
- Same file, overwritten in place each poll — no versioned duplicates.

## What this changes about your priors
This makes the MLB pitching input honest for the first time, but it does **not**
by itself turn MLB into an edge. Keep MLB flat-staked and skeptical until the CLV
panel says otherwise. The feed is the enabler; CLV is still the judge.
