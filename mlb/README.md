# Scout — MLB Stats Feed (Phase 1)

Data pipeline that produces `scout_mlb.json` for the Scout MLB model
(`mlbWinProb` / `MLB_INPUTS` in `scout-portal.jsx`). Sibling to the racing
fetcher; lives here as a subfolder to reuse the parent repo's Drive auth
(`../token.json`, `../client_secret.json`) and `drive_push.py`.

Built to the handoff spec `scout-mlb-feed-spec.md`. **Phase 1 is complete and
live-validated against the 2026-07-06 slate.**

## What Phase 1 delivers

Replaces the model's hand-eyeballed ERA + runs/game with true-talent inputs:

| Field | Source | Notes |
|---|---|---|
| Starter **FIP** | **computed** from StatsAPI raw stats | `(13·HR + 3·(BB+HBP) − 2·K)/IP + cFIP`; `cFIP` derived from league totals each run |
| Starter **xERA / xwOBA** | Baseball Savant Statcast | joined on `mlbam_id`; the quality cross-check |
| Starter ERA, IP, hand | StatsAPI | ERA kept for reference only |
| Team **runs/game** | StatsAPI team hitting | one call for all 30 teams |
| **Bullpen RA9** | StatsAPI reliever split | `relief runs · 9 / relief IP` (`sitCodes=rp`) |
| Park factor | placeholder `1.00` | real factors are Phase 2 |

## Deviation from the spec: FanGraphs is unusable from a script

The spec named FanGraphs as the primary source for FIP/xFIP/SIERA. FanGraphs now
sits behind a **Cloudflare JS challenge — HTTP 403 to any scripted request**
(verified 2026-07-06). An unattended pipeline can't rely on it.

Phase 1 routes around this with **StatsAPI + Baseball Savant only** — both are
official, key-less, script-friendly JSON/CSV:
- **FIP** is computed locally from StatsAPI counting stats (no FanGraphs needed).
- **xERA/xwOBA** (Statcast) come from Savant and are a *better*, less
  defense-contaminated ERA replacement than the spec assumed.

The FanGraphs-only metrics — **xFIP, SIERA, and wRC+ handedness splits** — are
deferred to Phase 2, which is where the Cloudflare problem gets solved (headless
browser render, or an alternate host / `pybaseball`).

## Output schema (`out/scout_mlb.json`)

Strict unchanging filename, overwritten in place. Freshness fields match the
racing convention (`last_polled_at`, `final_snapshot`).

```jsonc
{
  "slate_date": "2026-07-06",
  "generated_at_utc": "2026-07-06T...Z",
  "last_polled_at": "2026-07-06T...Z",
  "final_snapshot": false,               // true on the pre-lock run (--final)
  "source_versions": { "statsapi": "v1", "savant_xstats": "2026", "fip_constant": 3.101 },
  "phase": 1,
  "games": {
    "mlb-phi-kc": {
      "game_pk": 824089, "game_number": 1,
      "start_time_utc": "2026-07-06T18:10:00Z",
      "park_factor": 1.00, "park_factor_status": "placeholder",
      "away": {
        "team": "phi",
        "starter": {
          "name": "Cristopher Sánchez", "mlbam_id": 650911, "hand": "L",
          "fip": 2.31, "era": 2.00, "ip": 117.0, "xera": 2.91, "xwoba": 0.267,
          "data_status": "confirmed", "warnings": []
        },
        "bullpen_ra9": 4.53, "rg": 4.44,
        "data_status": "confirmed", "warnings": []
      },
      "home": { "...": "..." }
    }
  }
}
```

### Game IDs & team codes
`mlb-{away}-{home}`, lowercase, away first (matches Scout's `GAMES`/`MLB_INPUTS`
keys). Doubleheaders suffix `-g2` (from StatsAPI `gameNumber`). Team codes are
the lowercased StatsAPI abbreviation, with one override — **`AZ → ari`**
(`ABBR_OVERRIDE` in `mlb_fetch.py`). That map is the contract; add overrides
there if Scout uses a different code for any team.

### `data_status` (the racing "no silent nulls" lesson)
Every `starter` and team block carries `confirmed | partial | not_found` plus a
`warnings[]` list:
- **confirmed** — all model-critical fields present from a primary source.
- **partial** — a fallback fired (low IP sample, missing xERA, missing R/G, or a
  `not_found` starter). Usable; Scout should widen the market blend.
- **not_found** — pitcher/team couldn't be resolved. Model should defer to the
  market entirely, exactly like unscored racing runners.

A TBD probable pitcher yields a `not_found` starter (never a fake number) and
drops the team block to `partial`.

## Usage

```bash
python mlb/mlb_fetch.py                 # today's US slate -> out/scout_mlb.json
python mlb/mlb_fetch.py --date 2026-07-06
python mlb/mlb_fetch.py --final         # mark final_snapshot=true (pre-lock run)
python mlb/mlb_fetch.py --drive         # also push to Google Drive "Scout MLB" folder
```

Requires only `requests` (already in the racing venv). On Windows, prefix runs
with `PYTHONIOENCODING=utf-8` if the console mangles accented pitcher names in
the progress print (the JSON file itself is always UTF-8).

### Delivery
- **Local:** `out/scout_mlb.json` (primary artifact).
- **Drive (`--drive`):** reuses `../drive_push.py` pointed at a **"Scout MLB"**
  folder, using the parent repo's `token.json`. Additive/best-effort — a Drive
  failure never blocks the local write. *Not run automatically*; the first
  `--drive` run creates the Drive folder.

### Run cadence (pre-match, no T-3min scenario like racing)
1. Morning run when probables post → `final_snapshot: false`.
2. Pre-lock run ~30–60 min before first pitch (`--final`) → `final_snapshot: true`.

## Open decisions (spec §12) — resolved / outstanding

1. **FanGraphs/Savant endpoints** — Savant CSV works (key-less); FanGraphs is
   Cloudflare-blocked → **routed around** (see deviation above). ✅
2. **wRC+ handedness splits** — FanGraphs-only → **Phase 2** (needs the
   Cloudflare fix). ⏳
3. **Recent-form window** — Phase 1 uses **season-to-date**; last-30d weighting
   is Phase 3. ⏳
4. **Closing line for CLV** — **Phase 4**; not touched yet. ⏳
5. **Repo** — reused `racing-scout` (Drive plumbing already here), per your
   choice to build as a subfolder. ✅

## Roadmap

- **Phase 2** — solve Cloudflare; add xFIP/SIERA, wRC+ vs LHP/RHP, real park
  factors (Savant park-factors leaderboard is key-less and should work).
- **Phase 3** — last-30d weighting, bullpen availability, run distribution for
  totals/run-line.
- **Phase 4** — closing-odds capture → `closePrice` for the CLV panel (the
  metric that licenses scaling).

## Scout-side integration (unchanged from spec §10)

Map `scout_mlb.json` into `MLB_INPUTS` (`starter.fip` already holds the best
metric; `xera` is available as a blend input). Let `mlbWinProb()` drive
`modelProb` via `w·model + (1−w)·marketDeVig`, starting `w=0.5` and only raising
it once **CLV** validates — not win/loss ROI. `not_found`/`partial` games force
`w` low.
