#!/usr/bin/env python3
"""Compute a spoiler-safe "Excitement Index" for finished NBA games and write it to
``ratings.json`` for the app to read.

The index (0-100) answers one question for a user deciding how to spend their night:
"is this game worth watching?" It is a watchability score. It is built from three
independent reasons to tune in, each 0..1, combined with a noisy-OR so the STRONGEST
reason carries the game instead of being averaged away:

  * COMPETITIVENESS — was it a real contest? Built on an information-gain core (how
    much the outcome was genuinely in doubt and how much each play *revealed*), plus
    coin-flip closeness over real game time, a tight final margin, and lead/tie trading.
  * DRAMA — a memorable moment: a clutch coin-flip finish or a late lead-flipping
    game-winner, overtime, the winner climbing out of a deep hole, or a huge run.
  * STARS — standout individual play: a big top-scoring night, a triple-double, and
    several 30-point scorers (stars going off on both sides).

What changed from ``excitement_v1.py`` (same pillars, same noisy-OR, same output)
is the *internals*, following the game-excitement literature:

  1. INFORMATION GAIN, not win-probability distance. The win-prob core is the summed
     KL-divergence of each play (surprise / information revealed), not the summed
     absolute change. A move between extreme probabilities (1%->10%, a comeback
     flickering alive) counts far more than a mid-range move (41%->50%), and a sudden
     lurch counts more than a slow drip — which is how real drama actually feels.
     (inpredictable 2026 validated this against WikiHoops fan ratings: KL aligns with
     human "was it exciting?" judgments better than summed |dWP|.)
  2. SHANNON ENTROPY for tension — the principled "how uncertain was the outcome"
     curve (1.0 at 50/50, falling off toward the extremes), replacing a linear proxy.
  3. REAL GAME CLOCK, not play index. ESPN's ``plays`` carry period + clock, joinable
     to each win-probability point, so "late" means the actual 4th quarter, not the
     last few rows of a table. Plays don't fall evenly in time (they bunch up late),
     so the old ``i/n`` proxy mis-weighted the stretch that matters most.
  4. PLAYS mining — a late lead-flipping make (a real game-winner) and the biggest
     scoring run of the game, both pulled from ``plays`` and fed to the drama pillar.

Everything stays spoiler-safe: only the index number and a tier label (Skip / Worth it
/ Great / Must-watch) are written. No scores, no winner, no margin.

Endpoint (public, undocumented, can change):
  summary: .../basketball/nba/summary?event=<id>   -> winprobability[], plays[], boxscore, ...

Run from the repo root:
  python scripts/excitement.py                 # rate recently finished games
  python scripts/excitement.py --from 20261020 --to 20261101
  python scripts/excitement.py --event 401909088 --debug   # one game, show the maths
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from espn import SCOREBOARD_URL, fetch_json, name_to_tricode, canonical_name

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ratings.json"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

# ---- tiers -----------------------------------------------------------------
# Thresholds on the 0-100 watchability score. Provisional for the information-gain
# scale — recalibrate against a real rebuild's distribution (see compare_scores.py).
TIER_MUST  = 85     # "Must-watch"
TIER_GREAT = 68     # "Great"
TIER_GOOD  = 48     # "Worth it"   (below this -> "Skip")

# ---- information-gain normalization ----------------------------------------
# The win-prob core is the total KL-divergence ("bits of surprise") over the game.
# INFO_LO/HI map that total onto [0,1]. Provisional knee; tune to the real backtest
# distribution (compare_scores.py prints the percentiles needed to set these).
INFO_LO = 0.6       # a flat, decided game reveals little
INFO_HI = 3.2       # a classic nail-biter reveals a lot

# game-clock constants (seconds)
REG_SECONDS = 720   # 12-minute quarter
OT_SECONDS  = 300   # 5-minute overtime
CLUTCH_SECONDS = 180     # "clutch" = the final 3:00 of regulation/OT
GAME_WINNER_SECONDS = 15 # a lead-flipping make this late = a game-winner

_EPS = 1e-3


# ======================================================================
#  Math helpers
# ======================================================================
def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _norm(x, lo, hi):
    """Map x in [lo,hi] to [0,1] with a soft knee; clamps outside."""
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def _entropy(p):
    """Shannon entropy (bits) of a binary outcome at probability p.

    1.0 at p=0.5 (maximum uncertainty), 0.0 at p in {0,1}. This is the principled
    'how in doubt was the result' curve that replaces the old linear 1-2|p-0.5|.
    """
    p = _clamp(p, _EPS, 1.0 - _EPS)
    return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))


def _kl_step(p, q):
    """KL-divergence of one play: information revealed moving from prior q to posterior p.

    For the binary win/loss outcome, D_KL(P || Q) = p*log2(p/q) + (1-p)*log2((1-p)/(1-q)).
    This is the per-play 'surprise'. Summed over the game it is the information-gain
    excitement core. Verified against inpredictable's worked example: a 14%->49% swing
    returns ~0.501 bits.
    """
    p = _clamp(p, _EPS, 1.0 - _EPS)
    q = _clamp(q, _EPS, 1.0 - _EPS)
    return p * math.log2(p / q) + (1.0 - p) * math.log2((1.0 - p) / (1.0 - q))


# ======================================================================
#  The index — a pure function so it can be unit-tested without ESPN.
# ======================================================================
def excitement(wp, *, wp_t=None, ot_periods=0, top_scorer=0, triple_double=False,
               thirty_plus=0, lead_changes=0, times_tied=0, final_margin=None,
               game_winner=False, biggest_run=0, debug=False, **_ignored):
    """Return (index 0-100, {components}) from a home-win-probability series.

    wp   : list[float] in [0,1], one per play, in time order (home win probability).
    wp_t : optional list[float] in [0,1], same length as wp, the fraction of game time
           elapsed at each play (0 at tip, 1 at the final buzzer), from the real clock.
           If None, play index is used as a fallback (the old behaviour).

    A game with <8 win-probability points is treated as having no usable data -> 0.
    """
    wp = [min(1.0, max(0.0, float(x))) for x in wp if x is not None]
    if len(wp) < 8:
        return 0, {"reason": "no win-probability data"}

    n = len(wp)
    # Elapsed-time fraction per play: real game clock if we have it, else play index.
    if wp_t and len(wp_t) == n:
        t = [_clamp(float(x), 0.0, 1.0) for x in wp_t]
    else:
        t = [i / (n - 1) for i in range(n)]

    # 1) INFO — information gain (summed KL-divergence). The core "how much did this
    #    game reveal / how in doubt was it" signal, replacing summed |dWP|. Each play's
    #    surprise is weighted up a little late in the game (a late reveal is more
    #    memorable), but KL already does most of the leverage weighting on its own.
    info_bits = 0.0
    for i in range(1, n):
        info_bits += _kl_step(wp[i], wp[i - 1]) * (1.0 + 0.8 * t[i])
    info = _norm(info_bits, INFO_LO, INFO_HI)

    # 2) TENSION — mean Shannon entropy over the game, weighted toward the end. High
    #    when the outcome stayed genuinely uncertain; the real-clock weighting means
    #    "the end" is the actual closing minutes, not the last rows of the feed.
    tens = 0.0
    tw = 0.0
    for i in range(n):
        w = 1.0 + 2.0 * t[i]
        tens += w * _entropy(wp[i])
        tw += w
    tension = tens / tw if tw else 0.0

    # 3) CLOSE_TIME — share of game *time* spent genuinely competitive (win prob inside
    #    a 35-65% band), weighted late. Time-weighted, so a game close deep into the
    #    4th scores higher than one close only early.
    ct = 0.0
    ctw = 0.0
    for i in range(n):
        w = 1.0 + 1.0 * t[i]
        ct += w * (1.0 if abs(wp[i] - 0.5) <= 0.15 else 0.0)
        ctw += w
    close_time = ct / ctw if ctw else 0.0

    # 4) FINISH — was it still a coin-flip down the stretch? Closest to 50/50 across the
    #    clutch window (final CLUTCH_SECONDS by real clock; falls back to the last ~8%
    #    of plays when no clock is available).
    if wp_t and len(wp_t) == n:
        clutch_cut = 1.0 - (CLUTCH_SECONDS / _total_seconds(ot_periods))
        tail = [wp[i] for i in range(n) if t[i] >= clutch_cut] or wp[-max(1, n // 12):]
    else:
        tail = wp[max(0, int(n * 0.92)):]
    finish = max(0.0, 1.0 - 2.0 * min(abs(p - 0.5) for p in tail))

    # 5) COMEBACK — how big a hole the eventual winner climbed out of.
    winner_home = wp[-1] >= 0.5
    win_series = wp if winner_home else [1 - p for p in wp]
    comeback = max(0.0, 0.5 - min(win_series)) * 2.0

    # 6) ACTION — raw back-and-forth, independent of the WP curve.
    action = min(1.0,
                 0.65 * _norm(lead_changes, 2, 20) +
                 0.35 * _norm(times_tied, 2, 16))

    # ======================================================================
    #  Three independent reasons to tune in, each 0..1, combined by noisy-OR.
    # ======================================================================

    # A) COMPETITIVENESS — information gain is the backbone, with coin-flip closeness,
    #    a tight final margin (spoiler-safe: no winner, no score), and lead/tie trading.
    wp_close = 0.5 * tension + 0.5 * close_time
    if final_margin is not None:
        margin_close = 1.0 - _norm(final_margin, 2, 22)
        competitiveness = 0.35 * info + 0.30 * wp_close + 0.20 * margin_close + 0.15 * action
    else:
        margin_close = None
        competitiveness = 0.45 * info + 0.35 * wp_close + 0.20 * action

    # B) DRAMA — a clutch coin-flip finish OR a late lead-flipping game-winner, overtime,
    #    a deep comeback, or a huge run (momentum).
    gw_signal = 0.90 if game_winner else 0.0
    ot_signal = min(1.0, 0.75 + 0.12 * (ot_periods - 1)) if ot_periods >= 1 else 0.0
    momentum = _norm(biggest_run, 10, 22)
    clutch = max(finish, gw_signal)
    drama = 0.45 * clutch + 0.33 * ot_signal + 0.12 * comeback + 0.10 * momentum

    # C) STARS — standout individual play (unchanged).
    stars = min(1.0,
                0.55 * _norm(top_scorer, 25, 55) +
                0.25 * (1.0 if triple_double else 0.0) +
                0.20 * _norm(thirty_plus, 1, 4))

    comp_p = _clamp(competitiveness, 0.0, 1.0)
    dram_p = _clamp(drama, 0.0, 1.0)
    star_p = _clamp(stars, 0.0, 1.0)

    # BEST REASON carries the game: noisy-OR of the three pillars.
    worth = 1.0 - (1.0 - comp_p) * (1.0 - dram_p) * (1.0 - star_p)
    index = int(round(_clamp(100.0 * worth, 0.0, 100.0)))

    comp = {
        "competitiveness": round(comp_p, 3),
        "drama": round(dram_p, 3),
        "stars": round(star_p, 3),
        "info": round(info, 3),
        "info_bits": round(info_bits, 3),
        "wp_close": round(wp_close, 3),
        "margin_close": (round(margin_close, 3) if margin_close is not None else None),
        "action": round(action, 3),
        "finish": round(finish, 3),
        "game_winner": game_winner,
        "ot_signal": round(ot_signal, 3),
        "comeback": round(comeback, 3),
        "momentum": round(momentum, 3),
        "tension": round(tension, 3),
        "close_time": round(close_time, 3),
        "ot": ot_periods,
        "lead_changes": lead_changes,
        "times_tied": times_tied,
        "biggest_run": biggest_run,
        "final_margin": final_margin,
        "top_scorer": top_scorer,
        "thirty_plus": thirty_plus,
        "triple_double": triple_double,
        "used_real_clock": bool(wp_t and len(wp_t) == n),
    }
    if debug:
        for k, v in comp.items():
            print(f"    {k:16} {v}")
    return index, comp


def tier(index):
    if index >= TIER_MUST:
        return "must"
    if index >= TIER_GREAT:
        return "great"
    if index >= TIER_GOOD:
        return "good"
    return "skip"


def _total_seconds(ot_periods):
    return 4 * REG_SECONDS + max(0, ot_periods) * OT_SECONDS


# ======================================================================
#  Pull the pieces out of an ESPN game summary (spoiler-safe fields only).
# ======================================================================
def _win_prob_series(summary):
    """(wp, play_ids): home win probability per entry, with its play id for the clock join."""
    arr = summary.get("winprobability") or []
    wp, pids = [], []
    for e in arr:
        v = e.get("homeWinPercentage")
        if v is not None:
            wp.append(float(v))
            pids.append(e.get("playId"))
    return wp, pids


def _parse_clock(disp):
    """'5:30' or '0:04.7' -> seconds remaining in the period, or None."""
    if not disp:
        return None
    try:
        parts = str(disp).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, TypeError):
        return None


def _play_clock_map(summary, ot_periods):
    """{play_id -> elapsed_game_fraction in [0,1]} from plays[] period + clock."""
    total = _total_seconds(ot_periods)
    out = {}
    for pl in summary.get("plays") or []:
        pid = pl.get("id")
        per = ((pl.get("period") or {}).get("number"))
        rem = _parse_clock((pl.get("clock") or {}).get("displayValue"))
        if pid is None or not isinstance(per, int) or rem is None:
            continue
        if per <= 4:
            prior, dur = (per - 1) * REG_SECONDS, REG_SECONDS
        else:
            prior, dur = 4 * REG_SECONDS + (per - 5) * OT_SECONDS, OT_SECONDS
        elapsed = prior + (dur - rem)
        out[pid] = _clamp(elapsed / total, 0.0, 1.0) if total else 0.0
    return out


def _wp_times(wp, pids, summary, ot_periods):
    """Elapsed-time fraction per win-probability point, via the plays clock join.

    Returns a list aligned to wp, or None if the clock can't be recovered (then the
    scorer falls back to play index). Any point whose play id is missing is filled
    forward from the last known time, then back-filled, so the series stays monotone-ish.
    """
    cmap = _play_clock_map(summary, ot_periods)
    if not cmap:
        return None
    t = [cmap.get(pid) for pid in pids]
    if all(x is None for x in t):
        return None
    # forward-fill then back-fill the gaps
    last = 0.0
    for i in range(len(t)):
        if t[i] is None:
            t[i] = last
        else:
            last = t[i]
    nxt = t[-1]
    for i in range(len(t) - 1, -1, -1):
        if t[i] is None:
            t[i] = nxt
        else:
            nxt = t[i]
    return t


def _ot_periods(summary):
    hdr = summary.get("header") or {}
    comps = (hdr.get("competitions") or [{}])
    st = (comps[0].get("status") or {})
    per = st.get("period")
    if isinstance(per, int) and per > 4:
        return per - 4
    return 0


def _team_stat(summary, names, labels):
    box = summary.get("boxscore") or {}
    best = 0
    for team in box.get("teams", []):
        for stat in team.get("statistics", []):
            if stat.get("name") in names or stat.get("label") in labels:
                try:
                    best = max(best, int(float(stat.get("displayValue", 0))))
                except (TypeError, ValueError):
                    pass
    return best


def _lead_changes(summary):
    return _team_stat(summary, {"leadChanges"}, {"Lead Changes"})


def _times_tied(summary):
    return _team_stat(summary, {"timesTied"}, {"Times Tied"})


def _star(summary):
    """(top_points, had_triple_double, thirty_plus_scorers) — individual lines only."""
    top = 0
    td = False
    thirty = 0
    box = summary.get("boxscore") or {}
    for team in box.get("players", []):
        for grp in team.get("statistics", []):
            keys = grp.get("keys") or grp.get("names") or []
            idx = {k: i for i, k in enumerate(keys)}
            for a in grp.get("athletes", []):
                vals = a.get("stats") or []
                def num(key):
                    i = idx.get(key)
                    try:
                        return float(vals[i])
                    except (TypeError, ValueError, IndexError):
                        return 0.0
                pts = num("points")
                top = max(top, int(pts))
                if pts >= 30:
                    thirty += 1
                cats = sum(1 for key in ("points", "rebounds", "assists",
                                         "steals", "blocks") if num(key) >= 10)
                if cats >= 3:
                    td = True
    return top, td, thirty


def _final_margin(summary):
    """Absolute final point margin, or None. Reveals neither winner nor score."""
    hdr = summary.get("header") or {}
    comps = (hdr.get("competitions") or [{}])
    cs = (comps[0].get("competitors") or [])
    scores = []
    for c in cs:
        try:
            scores.append(float(c.get("score")))
        except (TypeError, ValueError):
            pass
    if len(scores) >= 2:
        return abs(scores[0] - scores[1])
    return None


def _running_scores(summary):
    """Ordered (home_score, away_score) after each play, from plays[] — for run and
    game-winner detection. ESPN plays carry running homeScore/awayScore."""
    seq = []
    for pl in summary.get("plays") or []:
        h, a = pl.get("homeScore"), pl.get("awayScore")
        if h is None or a is None:
            continue
        try:
            seq.append((int(h), int(a), pl))
        except (TypeError, ValueError):
            continue
    return seq


def _biggest_run(summary):
    """Largest run of unanswered points by one side, from the plays score sequence."""
    seq = _running_scores(summary)
    best = 0
    run_team = None   # 'h' or 'a'
    run = 0
    ph, pa = 0, 0
    for h, a, _pl in seq:
        dh, da = h - ph, a - pa
        ph, pa = h, a
        if dh > 0 and da == 0:
            run = run + dh if run_team == "h" else dh
            run_team = "h"
        elif da > 0 and dh == 0:
            run = run + da if run_team == "a" else da
            run_team = "a"
        elif dh > 0 and da > 0:
            run_team = None  # shouldn't happen on one play; reset
            run = 0
        best = max(best, run)
    return best


def _game_winner(summary, ot_periods):
    """True if a scoring play in the final GAME_WINNER_SECONDS of Q4/OT flips the lead
    to the team that made it. A real late go-ahead/buzzer make — spoiler-safe (it is
    never written out; it only feeds the drama pillar)."""
    seq = _running_scores(summary)
    ph, pa = 0, 0
    for h, a, pl in seq:
        per = ((pl.get("period") or {}).get("number"))
        rem = _parse_clock((pl.get("clock") or {}).get("displayValue"))
        scoring = pl.get("scoringPlay")
        dh, da = h - ph, a - pa
        before, after = ph - pa, h - a
        ph, pa = h, a
        if not scoring or not isinstance(per, int) or per < 4 or rem is None:
            continue
        if rem > GAME_WINNER_SECONDS:
            continue
        # a make that takes the lead for the scoring side (sign of the margin flips to it)
        if dh > 0 and after > 0 and before <= 0:
            return True
        if da > 0 and after < 0 and before >= 0:
            return True
    return False


def features_from_summary(summary):
    """Every input all three scorers need, extracted once from a summary. Spoiler-safe
    enough for local analysis (it carries final_margin, which never reaches the app)."""
    ot = _ot_periods(summary)
    wp, pids = _win_prob_series(summary)
    wp_t = _wp_times(wp, pids, summary, ot) if wp else None
    top, td, thirty = _star(summary)
    return {
        "wp": wp,
        "wp_t": wp_t,
        "ot_periods": ot,
        "top_scorer": top,
        "triple_double": td,
        "thirty_plus": thirty,
        "lead_changes": _lead_changes(summary),
        "times_tied": _times_tied(summary),
        "final_margin": _final_margin(summary),
        "game_winner": _game_winner(summary, ot),
        "biggest_run": _biggest_run(summary),
    }


def _identity_from_event(ev):
    """(game_id, ot_periods) from a scoreboard event, or (None, 0)."""
    comp = (ev.get("competitions") or [{}])[0]
    cs = comp.get("competitors") or []
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None, 0
    htri = name_to_tricode(canonical_name((home.get("team") or {}).get("displayName", "")))
    atri = name_to_tricode(canonical_name((away.get("team") or {}).get("displayName", "")))
    if not htri or not atri:
        return None, 0
    date_iso = (ev.get("date") or comp.get("date") or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(date_iso).astimezone(timezone.utc)
    except ValueError:
        return None, 0
    gid = f"{dt.strftime('%Y-%m-%d')}_{atri}@{htri}"   # == the app's gameId()
    period = ((comp.get("status") or ev.get("status") or {}).get("period"))
    ot = (period - 4) if isinstance(period, int) and period > 4 else 0
    return gid, ot


def rate_event(ev, *, debug=False):
    """Rate one finished game. Returns (game_id, {"x", "t"}) or None."""
    gid, ot = _identity_from_event(ev)
    if not gid:
        return None
    event_id = ev.get("id")
    summary = fetch_json(f"{SUMMARY_URL}?event={event_id}")
    feats = features_from_summary(summary)
    if len(feats["wp"]) < 8:
        return None  # win-probability not published (yet)
    if not feats["ot_periods"]:
        feats["ot_periods"] = ot

    if debug:
        print(f"  {gid}  (event {event_id})  plays={len(feats['wp'])}  "
              f"real_clock={feats['wp_t'] is not None}")
    index, _c = excitement(debug=debug, **feats)

    # The app shows only the Hype score, so ratings.json stays lean and spoiler-free.
    return gid, {"x": index, "t": tier(index)}


# ======================================================================
#  Batch: walk finished dates and (re)build ratings.json
# ======================================================================
def _arg(flag):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else None


def _finished_events(date_yyyymmdd):
    board = fetch_json(f"{SCOREBOARD_URL}?dates={date_yyyymmdd}")
    out = []
    for ev in board.get("events", []):
        st = (ev.get("status") or {}).get("type") or {}
        if st.get("completed") and ev.get("id"):
            out.append(ev)
    return out


def _event_from_scoreboard(event_id):
    today = datetime.now(timezone.utc)
    for d in range(10):
        day = (today - timedelta(days=d)).strftime("%Y%m%d")
        board = fetch_json(f"{SCOREBOARD_URL}?dates={day}")
        for ev in board.get("events", []):
            if str(ev.get("id")) == str(event_id):
                return ev
    return None


def main():
    debug = "--debug" in sys.argv
    one = _arg("--event")
    if one:
        ev = _event_from_scoreboard(one)
        if not ev:
            print("event not found in recent scoreboards")
            return 1
        r = rate_event(ev, debug=debug)
        print(json.dumps(r, indent=2) if r else "not finished / no data")
        return 0

    date_from = _arg("--from")
    date_to = _arg("--to")
    if not date_from:
        today = datetime.now(timezone.utc)
        dates = [(today - timedelta(days=d)).strftime("%Y%m%d") for d in range(4)]
    else:
        board = fetch_json(SCOREBOARD_URL)
        cal = [str(e)[:10].replace("-", "") for e in
               ((board.get("leagues") or [{}])[0].get("calendar") or [])]
        dates = [d for d in cal if d >= date_from and (not date_to or d <= date_to)]

    ratings = {}
    if OUT.exists():
        try:
            ratings = json.loads(OUT.read_text())
        except json.JSONDecodeError:
            ratings = {}

    added = 0
    for d in sorted(set(dates)):
        for ev in _finished_events(d):
            gid0, _ot = _identity_from_event(ev)
            if gid0 and isinstance(ratings.get(gid0), dict) and "x" in ratings[gid0]:
                continue
            try:
                r = rate_event(ev, debug=debug)
            except Exception as exc:  # noqa: BLE001
                print(f"  event {ev.get('id')}: {exc}", file=sys.stderr)
                continue
            if r:
                gid, val = r
                if ratings.get(gid) != val:
                    ratings[gid] = val
                    added += 1
            time.sleep(0.25)  # be polite

    OUT.write_text(json.dumps(ratings, ensure_ascii=False, separators=(",", ":"),
                              sort_keys=True) + "\n", encoding="utf-8")
    print(f"ratings.json: {len(ratings)} games ({added} new/changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
