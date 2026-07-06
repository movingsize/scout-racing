# Scout — MLB Stats Feed (Phase 3)

Data pipeline that produces `scout_mlb.json` for the Scout MLB model
(`mlbWinProb` / `MLB_INPUTS` in `scout-portal.jsx`). Sibling to the racing
fetcher; lives here as a subfolder to reuse the parent repo's Drive auth
(`../token.json`, `../client_secret.json`) and `drive_push.py`.

Built to the handoff spec `scout-mlb-feed-spec.md`. **Phases 1-3 are all
complete and live-validated** (Phase 1/2 against the 2026-07-06 slate; Phase 3
against the 2026-07-05 slate, 15 games).

## What Phase 3 adds

| Field | Source | Notes |
|---|---|---|
| `closing_odds` (per game) | ESPN scoreboard API (DraftKings), key-less | moneyline, total, run-line, captured near each game's own first pitch |

Priority 2 of the Phase 3 spec (run-distribution for Totals/Run-Line) is
explicitly **Scout's own work** (in-chat), not this pipeline's — the pipeline's
job there is just making sure `closing_odds.total`/`.run_line` are present,
which they are. Priority 3 (recent-form weighting, bullpen availability) is
explicitly gated behind CLV proving the core model out and **not built yet** —
see the spec's own sequencing.

See `PHASE3_HANDOFF.md` for the in-chat model integration notes.

### Honest limitations of closing-odds capture

- **Single book (DraftKings via ESPN), not a sharp-book close.** The Phase 3
  spec explicitly says "consistency > book identity" — this is fine for CLV
  purposes, but don't read `closing_odds` as a Pinnacle-grade sharp line.
- **A run-line or total can be temporarily suspended** (ESPN returns `"OFF"`
  for the line/odds, e.g. around a probable-pitcher change) — this correctly
  parses to `null`, not a fabricated number. A `null` sub-field doesn't mean
  the whole `closing_odds` block failed; `moneyline` is the field gating
  whether `closing_odds` gets written at all.
- **Doubleheader join key is an assumption, not confirmed.** A second game
  between the same two teams on the same date gets `-g2` appended in the same
  chronological order ESPN lists them — this hasn't been validated against a
  real doubleheader (none fell on the validated slates).
- **The captured price is whatever ESPN shows at the moment of the poll**,
  which is only the true "closing" line if the watcher's timing is right. The
  T-5min target is unvalidated against many real slates yet — if CLV numbers
  look systematically off, check whether the watcher is actually firing near
  first pitch (Scheduled Task history) before doubting the model.

## What Phase 2 adds

| Field | Source | Notes |
|---|---|---|
| Starter **xFIP** | **computed** from StatsAPI + Statcast batted-ball type | fly-ball rate × fixed league HR/FB constant, same shape as FIP |
| Starter **SIERA** | **computed**, public Swartz/BP 2010 formula | needs GB/FB/PU split from Statcast, not derivable from box-score fields alone |
| Team **wRC+**, **wRC+ vs LHP/RHP** | **computed** from StatsAPI `sitCodes=vl/vr` hitting splits | wOBA-ratio index vs league average for the same split — see caveats below |
| **Park factor**, **park factor by hand** | fantasyteamadvice.com (third-party) | real numbers, replaces the Phase 1 `1.00` placeholder |

See `PHASE2_HANDOFF.md` for the in-chat model integration notes (in particular:
none of the Phase 2 metrics above are the literal proprietary FanGraphs number —
they're self-computed analogs. Read the caveats before trusting them at face value.)

## Deviation from the spec: FanGraphs AND Savant's park-factors leaderboard are both unusable from a script

The spec named FanGraphs as the primary source for FIP/xFIP/SIERA/wRC+, and
Phase 1 assumed Savant's own park-factors leaderboard was a key-less CSV win.
**Both verified false as of 2026-07-06:**

- FanGraphs sits behind a **Cloudflare JS challenge — HTTP 403 to any scripted
  request**. Tried and failed: plain `requests`, Playwright headless, Playwright
  headless with anti-detection flags (`--disable-blink-features=AutomationControlled`
  + spoofed `navigator.webdriver`), Playwright headed, and `pybaseball`'s
  FanGraphs wrapper (same underlying HTTP call, same 403). Baseball-Reference is
  Cloudflare-walled the same way — not a usable fallback either.
- Savant's `/leaderboard/statcast-park-factors` page no longer renders any data
  via script: `csv=true` returns the full HTML page (not CSV), and the page's
  own JS bundle expects a `data` array that stays empty even after full page
  load, an explicit "Update" click, and a wait — tested against both the
  current season and a completed one. No replacement XHR/CSV endpoint could be
  found. This is a stale assumption from Phase 1, not a scraping shortfall —
  Savant appears to have redesigned this specific leaderboard into a client
  widget that doesn't actually deliver data to non-interactive clients.

Phase 2 routes around both:
- **xFIP / SIERA** are computed locally from StatsAPI counting stats (K, BB,
  HBP, IP, battersFaced) plus Statcast per-pitcher batted-ball type (GB/FB/LD/PU,
  via `pybaseball.statcast_pitcher`, filtered to `game_type == "R"`).
- **wRC+** (overall + vs LHP/RHP) is computed from StatsAPI's official
  `sitCodes=vl`/`vr` team hitting splits — the same key-less mechanism already
  used for the Phase 1 bullpen split — turned into a wOBA-ratio index against a
  live league baseline (also from StatsAPI, summed across all 30 teams).
- **Park factors** are scraped from `fantasyteamadvice.com/mlb/park-factors` — a
  third-party fantasy-baseball site, not an official provider, but unblocked
  (200 OK, no Cloudflare wall), `robots.txt`-allowed, and covering all 30 parks
  with sane (0.92–1.15) values split by batter hand.

### Honest limitations of the self-computed metrics

- **wRC+** here is a wOBA-ratio index (`100 × team_wOBA_split / league_wOBA_split`),
  not FanGraphs' actual wRC+ number: no park adjustment is folded in (that would
  need a full schedule-weighted build, not just a stadium factor), and the wOBA
  linear weights are fixed, era-typical constants (`WOBA_WEIGHTS`), not
  season-recalibrated the way FanGraphs republishes them every year.
- **xFIP** uses a fixed league-average HR/FB rate (`XFIP_LG_HRFB = 0.135`)
  rather than one computed live each run — deliberate: a same-day slate sample
  of ~20-30 starters would be a noisier estimate than this well-documented
  constant, not a more rigorous one.
- **Park factors** come from a third-party site with no visible methodology
  write-up. If `fantasyteamadvice.com` changes format or goes down, this field
  reverts to the placeholder — revisit then, or if the Savant leaderboard ever
  starts working again.

## Output schema (`out/scout_mlb.json`)

Strict unchanging filename, overwritten in place. Freshness fields match the
racing convention (`last_polled_at`, `final_snapshot`).

```jsonc
{
  "slate_date": "2026-07-06",
  "generated_at_utc": "2026-07-06T...Z",
  "last_polled_at": "2026-07-06T...Z",
  "final_snapshot": false,   // true once EVERY game today has closing_odds (see below)
  "source_versions": {
    "statsapi": "v1", "savant_xstats": "2026", "fip_constant": 3.103,
    "park_factors_source": "fantasyteamadvice.com", "xfip_lg_hrfb": 0.135,
    "closing_odds_source": "espn/DraftKings"
  },
  "phase": 3,
  "games": {
    "mlb-phi-kc": {
      "game_pk": 824089, "game_number": 1,
      "start_time_utc": "2026-07-06T18:10:00Z",
      "park_factor": 1.00, "park_factor_hand": { "L": 1.01, "R": 1.00 },
      "park_factor_status": "confirmed",
      "closing_odds": {                          // absent until captured -- see Run cadence
        "captured_at_utc": "2026-07-06T18:05:12Z",
        "source": "espn/DraftKings",
        "moneyline": { "away": 1.57, "home": 2.44 },
        "total": { "line": 8.0, "over": 1.88, "under": 1.95 },
        "run_line": { "line": 1.5, "away": 1.98, "home": 1.85 }
      },
      "away": {
        "team": "phi",
        "starter": {
          "name": "Cristopher Sánchez", "mlbam_id": 650911, "hand": "L",
          "fip": 2.31, "era": 2.00, "ip": 117.0, "xera": 2.91, "xwoba": 0.267,
          "xfip": 2.24, "siera": 2.26,
          "data_status": "confirmed", "warnings": []
        },
        "bullpen_ra9": 4.57, "rg": 4.47,
        "wrc_plus": 98, "wrc_vs_lhp": 95, "wrc_vs_rhp": 99,
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
python mlb/mlb_fetch.py --final         # also run one closing-odds capture pass now,
                                         # for any game currently in its T-5min..T+20min window
python mlb/mlb_fetch.py --drive         # also push to Google Drive "Scout MLB" folder
python mlb/mlb_odds_watcher.py          # continuous per-game closing-odds capture (see below)
```

`--final`'s meaning changed in Phase 3: it no longer just sets a flag, it
actively fetches ESPN odds and captures `closing_odds` for any game whose
window has arrived. `final_snapshot` in the output is now **computed**, not
settable directly — it's `true` only once every game in `games` has a
`closing_odds` block.

Requires `requests`, `pybaseball`, and `beautifulsoup4`:
`pip install requests pybaseball beautifulsoup4`. `pybaseball` and
`beautifulsoup4` are soft dependencies — if either is missing, xFIP/SIERA or
park factors (respectively) degrade to null/placeholder with a warning rather
than crashing the run; Phase 1's fields are unaffected either way. On Windows,
prefix runs with `PYTHONIOENCODING=utf-8` if the console mangles accented
pitcher names in the progress print (the JSON file itself is always UTF-8).

### Delivery
- **Local:** `out/scout_mlb.json` (primary artifact).
- **Drive (`--drive`):** reuses `../drive_push.py` pointed at a **"Scout MLB"**
  folder, using the parent repo's `token.json`. Additive/best-effort — a Drive
  failure never blocks the local write. *Not run automatically*; the first
  `--drive` run creates the Drive folder.

### Run cadence: two scheduled tasks, mirroring the racing pipeline exactly

MLB's staggered start times (a single US slate date's games can span ~8-10
hours, unlike racing's one-lock-per-meeting) meant a single daily "pre-lock"
run couldn't capture each game's own close. Phase 3 splits this into two
processes, the same split racing already uses (`betfair_racing_fetch.py` +
`race_watcher.py`):

1. **`mlb_fetch.py`** (Scheduled Task **`ScoutMLBFetchPush`**, daily 11pm
   AEST via `mlb_fetch_and_push.ps1`) — rebuilds Phase 1/2 fields fresh each
   run (probables, FIP/xFIP/SIERA, wRC+, park factors), pushes to Drive +
   GitHub. Any `closing_odds` already captured by the watcher is carried
   forward, never overwritten by this rebuild (see `load_existing_games` /
   the merge step in `main()`).
2. **`mlb_odds_watcher.py`** (Scheduled Task **`ScoutMLBOddsWatcher`**, daily
   1am AEST via `mlb_watch_odds.ps1`) — reads the file `mlb_fetch.py` already
   wrote, and for each game without `closing_odds`, sleeps until ~5 min
   before its own `start_time_utc`, captures one ESPN odds snapshot, writes
   just that field back, and pushes to Drive + GitHub. Exits once every
   loaded game has been captured (or a 14-hour safety cap elapses). **Must
   start after `ScoutMLBFetchPush` has run** (it needs that day's game list)
   — the 11pm/1am split gives a 2-hour buffer; adjust if the fetch task ever
   runs later.

Both trigger times were picked from one real slate's spread (games ran
~2:30am–11:30am AEST) — reasonable starting points, not tuned across many
days yet. Adjust via `Set-ScheduledTask` if a slate's actual times drift
outside the watcher's coverage.

A manual `--final` run (see Usage) does a one-off version of step 2's capture
logic, for testing or an ad-hoc pre-lock check without waiting for the
watcher.

## Open decisions (Phase 3 spec §6) — resolved / outstanding

1. **Closing-odds source** — ESPN's public scoreboard API, not The Odds API:
   no signup/key needed, confirmed live moneyline/total/run-line for every
   game on a real slate, matches this pipeline's existing preference for
   key-less official-ish sources. ✅
2. **Exact T-minus for the close** — target T-5min, but the actual capture
   window is T-5min through T+20min (`CLOSING_LEAD_SECONDS`/
   `CLOSING_GRACE_SECONDS`), generous enough to absorb scheduler jitter or a
   late book update. ✅
3. **Push/pull for the close** — separate watcher process (`mlb_odds_watcher.py`
   + its own Scheduled Task), exactly mirroring `race_watcher.py`'s role for
   racing, not a coarser cron poll. ✅
4. **NB dispersion / run-distribution modelling** — explicitly Scout's own
   work per the spec's ownership table; this pipeline only had to ensure
   `closing_odds.total`/`.run_line` are present, which they are. ✅
5. **Recent-form window** — Priority 3 of the Phase 3 spec, explicitly gated
   behind CLV proving the core model out. **Not built** — see the spec's own
   sequencing. ⏳

## Roadmap

- **Phase 3 remainder (gated)** — recent-form weighting (`xfip_l30`/`siera_l30`/
  `form_delta`) and bullpen availability, only once CLV shows the model has a
  pulse (spec's own instruction, not a pipeline limitation).
- **Phase 4** — whatever Scout's CLV panel surfaces as the next priority once
  enough closing-odds data has accumulated.

## Scout-side integration (unchanged from spec §10)

Map `scout_mlb.json` into `MLB_INPUTS` (`starter.fip` already holds the best
metric; `xera` is available as a blend input). Let `mlbWinProb()` drive
`modelProb` via `w·model + (1−w)·marketDeVig`, starting `w=0.5` and only raising
it once **CLV** validates — not win/loss ROI. `not_found`/`partial` games force
`w` low.
