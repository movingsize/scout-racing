# Scout MLB Feed — Phase 2 Build Brief (for Claude Code)

**Owner:** Claude Code, `racing-scout/mlb`. Continues `mlb_fetch.py`.
**Prereq:** Phase 1 shipped (StatsAPI + Savant → FIP/xERA/R-G/bullpen). Read
`README.md` and `PHASE1_HANDOFF.md` first.

## Goal
Add the three inputs Phase 1 deliberately deferred, all of which need data that
today only FanGraphs (or a browser) exposes:
1. **xFIP / SIERA** — sharper true-talent than raw FIP.
2. **wRC+ overall + vs LHP / vs RHP** — the handedness edge (offense-vs-this-
   pitcher's-hand is where MLB matchup value lives).
3. **Real park factors** (overall + by hand) — replace the `1.00` placeholder.

## The blocker to solve first: FanGraphs is Cloudflare-walled
Verified 2026-07-06: `https://www.fangraphs.com/api/leaders/...` returns **HTTP
403 "Just a moment…"** (Cloudflare JS challenge) to any scripted request,
regardless of UA. This is the whole reason Phase 1 routed around it. Phase 2's
first task is a decision, not code. Candidate approaches, in rough preference:

| Option | Notes | Risk |
|---|---|---|
| **Playwright render** | `playwright` is **already a dependency** of the racing fetcher (`betfair_racing_fetch.py`). Render the FanGraphs leaderboard page / hit its API with a real browser context, scrape the table or intercept the JSON. Same pattern that beat bot-detection for racing sites. | Cloudflare may still challenge headless; may need stealth flags / persistent context. **Try this first.** |
| **`pybaseball`** | Wraps FanGraphs + Savant. May itself be Cloudflare-blocked now; check before committing. | Adds a dep; same underlying wall. |
| **Savant-only for park + xwOBA splits** | Savant *does* expose park factors and platoon xwOBA splits key-lessly — may cover park + some handedness without FanGraphs at all. | Doesn't give wRC+/xFIP/SIERA; partial win. |

**Do the endpoint-validation dance first** (as with the racing scraper): confirm
what actually returns before building the parser.

## Known key-less win: Savant park factors
Baseball Savant's park-factors leaderboard is CSV and was **not** Cloudflare-
blocked in Phase 1 testing. Validate:
`baseballsavant.mlb.com/leaderboard/statcast-park-factors` (and the by-handedness
variant). This likely delivers `park_factor` + `park_factor_hand` **without**
touching FanGraphs — knock this out even if the FanGraphs question is unresolved.

## Schema additions (extend, don't break Phase 1)
```jsonc
"starter": { ..., "xfip": 4.85, "siera": 4.90 },          // + raw components kept
"away": {
  ..., "wrc_plus": 102, "wrc_vs_rhp": 98, "wrc_vs_lhp": 110
},
"park_factor": 1.03,
"park_factor_hand": { "L": 1.05, "R": 1.01 },
"park_factor_status": "confirmed"                          // was "placeholder"
```
- Keep all Phase 1 fields; add these alongside. Old readers keep working.
- Extend `data_status`/`warnings` to cover the new fields (e.g. `partial` if
  wRC+ present but splits missing — spec §6). **No silent nulls.**
- Range QA (spec §9): xFIP/SIERA 1.5–7.0, wRC+ 50–160, park 0.85–1.20.

## Model-side (note for the in-chat Scout model, not this pipeline)
Once the feed carries handedness: opponent offense term should read
`wrc_vs_{starter.hand}` — that's the mechanism that turns "team offense" into
"offense vs this pitcher's hand". Flag this in the next handoff doc.

## Acceptance criteria
- `scout_mlb.json` gains xFIP/SIERA, wRC+ (+ splits), and real park factors, each
  with correct `data_status` when a source falls back.
- Every slate game still appears (no silent drops).
- Cross-source sanity: FanGraphs vs Savant true-talent within ~1.0 run, else warn.
- Runs unattended (scheduled), i.e. the Cloudflare solution must not need a human.
  If only an attended/manual FanGraphs pull is achievable, ship park factors +
  Savant splits automatically and document FanGraphs as manual — don't fake it.

## Open decisions to resolve (spec §12, carried forward)
- FanGraphs approach (Playwright vs pybaseball vs skip) — **decide via validation.**
- wRC+ split granularity — clean export vs the splits tool.
- Recent-form window — 30d vs weighted season/recent (also relevant to Phase 3).

## Not in Phase 2 (later)
Last-30d weighting, bullpen availability, run distribution for totals/run-line
(Phase 3); closing-odds capture for CLV (Phase 4 — the scaling unlock).
