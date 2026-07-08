#!/usr/bin/env python3
"""
data_adequacy.py — the "DATA BAD" guardrail (single source of truth).

Purpose (per the Scout brief §0 guardrail): the model must emit a genuine,
data-derived probability for every binary outcome — OR an explicit no-call.
Inadequate data must NEVER be silently backfilled with the market price or a
fabricated number. This module is the choke point that enforces that rule.

Design principle: FAIL CLOSED.
  A record is a callable prediction ONLY if it affirmatively proves it has the
  inputs to be one. Anything else — missing data_status, a status/data
  disagreement (the classic "confirmed but the field is null" bug), an
  exception during assessment, or an unknown shape — resolves to NO_CALL.
  The default is "DATA BAD", not "OK".

Two tiers of inadequacy are distinguished, because they mean different things
downstream:
  * NO_CALL  ("DATA BAD")  — no genuine model read is possible. The model has
                             nothing to say. predProb/model-prob fields are
                             NULLED so nothing downstream can resurrect a
                             number from de-vig. This is the guardrail.
  * OK, confidence="thin"  — enough to model, but the input is sparse/partial.
                             This is NOT a no-call; it's a callable prediction
                             that the blend must widen toward market
                             (the *BlendThin weights). Kept as a signal, not
                             suppressed.
  * OK, confidence="ok"    — full-strength inputs.

Every emitted record gets a `call` block:
    "call": {
        "call":       "OK" | "NO_CALL",
        "label":      "OK" | "DATA BAD",
        "confidence": "ok" | "thin" | None,   # None when NO_CALL
        "reason":     "<human string>",
        "guard":      "data_adequacy vX"
    }

Portal contract: if call.call == "NO_CALL", render a no-call ("DATA BAD") card
and DO NOT compute a modelProb — never fall back to de-vig for it.

Run `python data_adequacy.py` to self-test against the committed JSON feeds.
"""

VERSION = "1.0"
GUARD_TAG = f"data_adequacy v{VERSION}"

CALL_OK = "OK"
CALL_NONE = "NO_CALL"
LABEL_OK = "OK"
LABEL_BAD = "DATA BAD"
CONF_OK = "ok"
CONF_THIN = "thin"


class Verdict:
    __slots__ = ("call", "confidence", "reason")

    def __init__(self, call, confidence, reason):
        self.call = call
        self.confidence = confidence
        self.reason = reason

    @property
    def ok(self):
        return self.call == CALL_OK

    def block(self):
        return {
            "call": self.call,
            "label": LABEL_OK if self.ok else LABEL_BAD,
            "confidence": self.confidence if self.ok else None,
            "reason": self.reason,
            "guard": GUARD_TAG,
        }


def _ok(reason, confidence=CONF_OK):
    return Verdict(CALL_OK, confidence, reason)


def _bad(reason):
    return Verdict(CALL_NONE, None, "data bad: " + reason)


# ─────────────────────────── small shared helpers ───────────────────────────

def _career_starts(career):
    """Parse a 'starts:wins-seconds-thirds' record ('10:1-0-1') -> starts int.
    None/malformed -> 0 (fail-closed: no proven starts)."""
    if not career or not isinstance(career, str):
        return 0
    head = career.split(":", 1)[0].strip()
    return int(head) if head.isdigit() else 0


def _has_real_finish(runner):
    """True if the runner has at least one genuine race run on record — from
    recent_runs, last5_parsed, the raw last5 string, or a non-zero career.

    This is the ACTUAL model input for racing, read number-anchored from the
    data itself rather than from the data_status label or a single derived
    field. That matters: the recurring 'unraced/career:null despite real last5'
    bug (e.g. Cobblestone Way x72455x173) is exactly a case where the label and
    a derived field lie but the raw last5 tells the truth. The guardrail trusts
    the raw form, so real form overrides a stale label instead of being
    suppressed by it. Trials/jump-outs are excluded — a trial is not race form,
    so a genuine first-starter with only trials still reads as no form."""
    for run in runner.get("recent_runs") or []:
        pos = run.get("pos")
        if isinstance(pos, int) and pos > 0 and not run.get("trial"):
            return True
    for p in runner.get("last5_parsed") or []:
        if not p.get("spell") and not p.get("nonFinish"):
            return True
    # Raw last5 fallback for feeds that predate parse_last5: any digit is a real
    # run ('x'/'-' are spells, letters are non-finishes like fell). This is what
    # rescues the 100+ pre-fix runners with a real last5 but no last5_parsed.
    last5 = runner.get("last5")
    if isinstance(last5, str) and any(ch.isdigit() for ch in last5):
        return True
    return _career_starts(runner.get("career")) > 0


# ────────────────────────────── RACING ──────────────────────────────────────

# data_status tiers as produced by betfair_racing_fetch.reconcile_data_status
_RACE_BAD_STATUS = {"not_found", "unraced"}          # no form to model
_RACE_THIN_STATUS = {"reconstructed"}                # career rebuilt from last5
_RACE_OK_STATUS = {"confirmed"}


def assess_racing_runner(runner):
    """A runner is callable only if it has real form to price. A scratched /
    non-active runner is a no-call regardless of form (it won't start)."""
    status = (runner.get("status") or "").upper()
    if status and status not in ("ACTIVE",):
        return _bad(f"runner not active (status={status})")

    ds = runner.get("data_status")
    has_form = _has_real_finish(runner)

    # Number-anchored, fail-closed: the decision is driven by whether real race
    # form is actually present, NOT by the data_status label. This deliberately
    # lets genuine last5 form override a stale 'unraced'/'not_found'/null-career
    # label — the recurring underrating bug — instead of trusting the label to
    # suppress an in-form horse.
    if not has_form:
        # No real form anywhere. This is a genuine no-call; the label just
        # colours the reason (unraced first-starter vs a form-source miss).
        if ds in _RACE_BAD_STATUS:
            return _bad(f"data_status={ds} and no race form on record")
        if ds is None:
            return _bad("no data_status and no race form on record")
        return _bad(f"data_status={ds} but no race form present")

    # Real form present -> callable. Confidence reflects how complete the
    # supporting metadata is (reconstructed / null career / thin sample -> thin).
    starts = _career_starts(runner.get("career"))
    if ds in _RACE_THIN_STATUS:
        return _ok(f"career reconstructed from last5 ({runner.get('career')})", CONF_THIN)
    if ds in _RACE_OK_STATUS and starts >= 3:
        return _ok(f"confirmed form, {starts} career starts")
    if ds is None and starts == 0:
        return _ok("real last5 form present but career metadata missing "
                   "(pre-fix feed or lookup miss)", CONF_THIN)
    return _ok(f"form present ({starts} career starts, ds={ds})", CONF_THIN)


def enforce_racing(race):
    """Stamp every runner in a race report with a call verdict. Racing model
    probabilities are computed portal-side, so there is no feed-side prob field
    to null; the call block is the contract the portal must honor."""
    counts = {CALL_OK: 0, CALL_NONE: 0}
    for runner in race.get("runners") or []:
        v = _safe(assess_racing_runner, runner)
        runner["call"] = v.block()
        counts[v.call] += 1
    race["adequacy_summary"] = _summary(race.get("runners") or [])
    return counts


# ─────────────────────────────── MLB ────────────────────────────────────────

def _mlb_side_empty(block):
    """A team block with no starter AND no offense/bullpen context is empty —
    nothing to anchor a prediction on that side."""
    starter = block.get("starter") or {}
    starter_dead = starter.get("data_status") == "not_found" or starter.get("fip") is None
    no_context = (block.get("rg") is None
                  and block.get("bullpen_ra9") is None
                  and block.get("wrc_plus") is None)
    return starter_dead and no_context


def assess_mlb_game(game):
    """A game is callable if both sides have something to anchor on. Both
    starters TBD, or an entirely empty side, is a no-call. One side thin ->
    callable but thin (blend widens)."""
    away = game.get("away") or {}
    home = game.get("home") or {}
    if not away or not home:
        return _bad("missing a team block")

    away_starter = (away.get("starter") or {}).get("data_status")
    home_starter = (home.get("starter") or {}).get("data_status")

    if _mlb_side_empty(away) or _mlb_side_empty(home):
        return _bad("a team side has neither a probable pitcher nor offense/bullpen context")

    if away_starter == "not_found" and home_starter == "not_found":
        return _bad("both probable pitchers TBD (no pitching to anchor either side)")

    away_ds = away.get("data_status")
    home_ds = home.get("data_status")
    if away_ds == "not_found" or home_ds == "not_found":
        return _bad(f"a team block not_found (away={away_ds}, home={home_ds})")

    if "partial" in (away_ds, home_ds) or "not_found" in (away_starter, home_starter):
        return _ok(f"partial inputs (away={away_ds}, home={home_ds})", CONF_THIN)
    return _ok("both sides confirmed")


def enforce_mlb(feed):
    """Stamp every game with a call verdict."""
    counts = {CALL_OK: 0, CALL_NONE: 0}
    for game in (feed.get("games") or {}).values():
        v = _safe(assess_mlb_game, game)
        game["call"] = v.block()
        counts[v.call] += 1
    feed["adequacy_summary"] = _summary_from_calls(counts)
    return counts


# ────────────────────────────── TENNIS ──────────────────────────────────────

# Model-probability fields that must be nulled when a match is a no-call, so no
# downstream blend can turn "data bad" into a number.
_TENNIS_PROB_FIELDS = ("p_elo_match_a", "pA_serve", "pB_serve")


def assess_tennis_match(match):
    """A match is callable only if it carries at least one genuine structural
    signal (surface-Elo match prob and/or the serve-Markov point probs).
    A match with neither would only be priceable off de-vig market — which §0
    forbids — so it is a no-call, not a silent de-vig fallback."""
    ds = match.get("data_status")
    if ds == "not_found":
        return _bad("a player is not on the Elo leaderboard (no independent read)")

    p_elo = match.get("p_elo_match_a")
    p_serve = match.get("pA_serve")

    if p_elo is None and p_serve is None:
        return _bad("no structural signal (neither surface-Elo nor serve-Markov); "
                    "would fall back to de-vig, which is forbidden")

    if p_serve is None:
        # Elo-only: a real, independent read but no score distribution.
        return _ok("surface-Elo only (no serve-Markov distribution)", CONF_THIN)

    if ds == "partial":
        return _ok("serve-Markov present but a side is partial", CONF_THIN)
    return _ok("surface-Elo + serve-Markov distribution")


def enforce_tennis(feed):
    """Stamp every match with a call verdict and NULL the model-prob fields on
    any no-call, so nothing downstream can resurrect a number for it."""
    counts = {CALL_OK: 0, CALL_NONE: 0}
    for match in (feed.get("matches") or {}).values():
        v = _safe(assess_tennis_match, match)
        match["call"] = v.block()
        if not v.ok:
            for f in _TENNIS_PROB_FIELDS:
                if f in match:
                    match[f] = None
        counts[v.call] += 1
    feed["adequacy_summary"] = _summary_from_calls(counts)
    return counts


# ─────────────────────────── infra: fail-closed ─────────────────────────────

def _safe(fn, record):
    """Run an assessor fail-closed: any exception -> NO_CALL. A guardrail that
    can crash is not a guardrail."""
    try:
        v = fn(record)
        if not isinstance(v, Verdict) or v.call not in (CALL_OK, CALL_NONE):
            return _bad("adequacy check returned an unknown verdict (fail-closed)")
        return v
    except Exception as e:  # noqa: BLE001 — intentional catch-all, fail closed
        return _bad(f"adequacy check raised {type(e).__name__}: {e} (fail-closed)")


def _summary(runners):
    calls = {CALL_OK: 0, CALL_NONE: 0}
    for r in runners:
        c = (r.get("call") or {}).get("call")
        if c in calls:
            calls[c] += 1
    return _summary_from_calls(calls)


def _summary_from_calls(counts):
    return {
        "guard": GUARD_TAG,
        "ok": counts.get(CALL_OK, 0),
        "no_call": counts.get(CALL_NONE, 0),
    }


# ────────────────────────────── self-test ───────────────────────────────────

def _selftest():
    import glob
    import json
    import os

    root = os.path.dirname(os.path.abspath(__file__))
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
        print(("  PASS " if cond else "  FAIL ") + msg)

    # Synthetic unit cases -----------------------------------------------------
    print("Synthetic unit cases:")
    check(not assess_racing_runner({"data_status": "unraced", "recent_runs": []}).ok,
          "racing: unraced -> NO_CALL")
    check(not assess_racing_runner({"data_status": "confirmed", "career": None,
                                    "recent_runs": [], "last5_parsed": []}).ok,
          "racing: 'confirmed' but no form -> NO_CALL (status/data disagreement)")
    check(assess_racing_runner({"status": "ACTIVE", "data_status": "confirmed",
                                "career": "10:1-0-1",
                                "recent_runs": [{"pos": 3}]}).ok,
          "racing: confirmed with real form -> OK")
    check(not assess_racing_runner({"status": "REMOVED", "career": "10:1-0-1",
                                    "recent_runs": [{"pos": 3}]}).ok,
          "racing: scratched runner -> NO_CALL")
    check(assess_racing_runner({"data_status": "reconstructed",
                                "last5_parsed": [{"pos": 1, "spell": False,
                                                  "nonFinish": False}]}).confidence == CONF_THIN,
          "racing: reconstructed -> OK/thin")
    # The recurring underrating bug: real last5, but stale label + null career.
    check(assess_racing_runner({"status": "ACTIVE", "data_status": "unraced",
                                "career": None, "recent_runs": [],
                                "last5": "x72455x173"}).ok,
          "racing: real last5 overrides stale 'unraced'/null-career label -> OK")

    check(not assess_tennis_match({"data_status": "not_found"}).ok,
          "tennis: player not on Elo board -> NO_CALL")
    check(not assess_tennis_match({"data_status": "confirmed", "p_elo_match_a": None,
                                   "pA_serve": None}).ok,
          "tennis: no structural signal -> NO_CALL (no de-vig fallback)")
    check(assess_tennis_match({"data_status": "partial", "p_elo_match_a": 0.6,
                               "pA_serve": None}).confidence == CONF_THIN,
          "tennis: Elo-only -> OK/thin")
    check(assess_tennis_match({"data_status": "confirmed", "p_elo_match_a": 0.6,
                               "pA_serve": 0.64}).confidence == CONF_OK,
          "tennis: Elo + serve-Markov -> OK/ok")

    check(not assess_mlb_game({"away": {"data_status": "confirmed",
                                        "starter": {"data_status": "not_found", "fip": None},
                                        "rg": None, "bullpen_ra9": None, "wrc_plus": None},
                               "home": {"data_status": "confirmed",
                                        "starter": {"data_status": "confirmed", "fip": 3.9}}}).ok,
          "mlb: empty away side -> NO_CALL")
    check(assess_mlb_game({"away": {"data_status": "confirmed",
                                    "starter": {"data_status": "confirmed", "fip": 3.5}},
                           "home": {"data_status": "partial",
                                    "starter": {"data_status": "confirmed", "fip": 4.1}}}).confidence == CONF_THIN,
          "mlb: one side partial -> OK/thin")

    # Against committed real feeds --------------------------------------------
    print("\nCommitted racing feeds:")
    tot = {CALL_OK: 0, CALL_NONE: 0}
    for f in sorted(glob.glob(os.path.join(root, "races", "*.json"))):
        race = json.load(open(f, encoding="utf-8"))
        c = enforce_racing(race)
        tot[CALL_OK] += c[CALL_OK]
        tot[CALL_NONE] += c[CALL_NONE]
    print(f"  {tot[CALL_OK]} OK / {tot[CALL_NONE]} NO_CALL across all runners")
    check(tot[CALL_OK] > 0 and tot[CALL_NONE] > 0,
          "racing: real feed produced a mix of OK and NO_CALL (guardrail is discriminating)")

    mlb_path = os.path.join(root, "mlb", "out", "scout_mlb.json")
    if os.path.exists(mlb_path):
        print("\nCommitted MLB feed:")
        feed = json.load(open(mlb_path, encoding="utf-8"))
        c = enforce_mlb(feed)
        print(f"  {c[CALL_OK]} OK / {c[CALL_NONE]} NO_CALL across {len(feed.get('games', {}))} games")
        check(c[CALL_OK] + c[CALL_NONE] == len(feed.get("games", {})),
              "mlb: every game got a verdict")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
    return 0 if not failures else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
