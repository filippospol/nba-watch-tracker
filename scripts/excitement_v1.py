#!/usr/bin/env python3
"""Compute a spoiler-safe "Excitement Index" for finished NBA games from ESPN's
win-probability feed, and write it to ``ratings.json`` for the app to read.

The index (0-100) answers one question for a user deciding how to spend their night:
"is this game worth watching?" It is a watchability score, NOT an enhanced win-
probability model. It is built from three independent reasons to tune in, each 0..1:

  * COMPETITIVENESS — was it a real contest? Win-probability closeness (how often it
    was a coin-flip, and for how long), a tight final margin, and lead/tie trading.
    Win-prob flow lives here, as one input among three — not the whole story.
  * DRAMA — a memorable moment: a clutch coin-flip finish, overtime, or the winner
    climbing out of a deep hole ("comeback").
  * STARS — standout individual play: a big top-scoring night, a triple-double, and
    several 30-point scorers (stars going off on both sides).

The three are combined with a noisy-OR — ``1 - (1-comp)*(1-drama)*(1-stars)`` — so the
STRONGEST reason to watch carries the game instead of being averaged away. A game that
is elite on any one pillar (an OT thriller, a buzzer-beater, a 50-point night) lands
high; only a game weak on all three lands low. Scoring/pace is deliberately excluded.

Nothing about the result is written in a way the app shows to users: only the index
number, a tier label (Skip / Worth it / Great / Must-watch) and neutral tags. No scores,
no winner, no margin.

Endpoint (public, undocumented, can change):
  summary: .../basketball/nba/summary?event=<id>   -> winprobability[], boxscore, ...

Run from the repo root:
  python scripts/excitement.py                 # rate recently finished games
  python scripts/excitement.py --from 20261020 --to 20261101
  python scripts/excitement.py --event 401909088 --debug   # one game, show the maths
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from espn import SCOREBOARD_URL, fetch_json, name_to_tricode, canonical_name

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ratings.json"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

# ---- tiers -----------------------------------------------------------------
# Thresholds on the 0-100 index. Tuned so a typical night has a spread of tiers;
# easy to adjust once there's a season of real games to look at.
# Thresholds on the 0-100 watchability score. Each boundary is pegged to the pillar
# strength it implies under the noisy-OR: must ~= one elite pillar (0.85+), great ~= one
# strong pillar (0.68) or two solid ones, good ~= one decent reason (0.48). Provisional
# for the new scale — recalibrate against a real rebuild's distribution.
TIER_MUST  = 85     # "Must-watch"
TIER_GREAT = 68     # "Great"
TIER_GOOD  = 48     # "Worth it"   (below this -> "Skip")


# ======================================================================
#  The index — a pure function so it can be unit-tested without ESPN.
# ======================================================================
def excitement(wp, *, ot_periods=0, top_scorer=0, triple_double=False,
               thirty_plus=0, lead_changes=0, times_tied=0, final_margin=None,
               debug=False):
    """Return (index 0-100, {components}) from a home-win-probability series.

    wp : list[float] in [0,1], one per play, in time order (home team's win
         probability). away prob is 1-wp. A game with <8 points is treated as
         having no usable data and returns 0.

    The score answers "is this worth watching?" via three independent pillars —
    competitiveness, drama, stars — each 0..1, combined with a noisy-OR so the
    strongest reason to watch carries the game rather than being averaged away.
    """
    wp = [min(1.0, max(0.0, float(x))) for x in wp if x is not None]
    if len(wp) < 8:
        return 0, {"reason": "no win-probability data"}

    deltas = [abs(wp[i] - wp[i - 1]) for i in range(1, len(wp))]
    n = len(deltas)

    # 1) SWING — total movement in win probability (the core GEI).
    #    Scaled for game length so an overtime game isn't "exciting" just for
    #    having more plays: divide by plays, then re-scale to a per-game feel.
    swing_sum = sum(deltas)
    swing_per100 = swing_sum / n * 100.0          # length-independent
    # late plays carry more weight: a swing in the 4th/OT matters more than in Q1
    weighted = sum(d * (1.0 + 1.6 * (i / n)) for i, d in enumerate(deltas))
    swing = 0.6 * swing_sum + 0.4 * (weighted / 1.8)

    # 2) TENSION — how often the game was a coin-flip, weighted toward the end.
    tens = 0.0
    tw = 0.0
    for i, p in enumerate(wp):
        w = 1.0 + 2.0 * (i / len(wp))             # later = heavier
        tens += w * (1.0 - 2.0 * abs(p - 0.5))    # 1 at 50/50, 0 at 100/0
        tw += w
    tension = max(0.0, tens / tw)

    # 3) CLOSE_TIME — how long the game stayed genuinely competitive. Fraction of
    #    plays where the win probability was inside a 35-65% band, weighted toward
    #    the later stages. This is the "was it close for a long time" signal.
    ct = 0.0
    ctw = 0.0
    for i, p in enumerate(wp):
        w = 1.0 + 1.0 * (i / len(wp))
        ct += w * (1.0 if abs(p - 0.5) <= 0.15 else 0.0)
        ctw += w
    close_time = ct / ctw

    # 4) FINISH — was it still in doubt down the stretch? Closest the game got to
    #    50/50 across the final ~8% of plays (clutch time).
    tail = wp[max(0, int(len(wp) * 0.92)):]
    finish = max(0.0, 1.0 - 2.0 * min(abs(p - 0.5) for p in tail))

    # 5) COMEBACK — how big a hole the eventual winner climbed out of.
    winner_home = wp[-1] >= 0.5
    win_series = wp if winner_home else [1 - p for p in wp]
    comeback = max(0.0, 0.5 - min(win_series)) * 2.0   # 0..1 (down to a near-loss)

    # 6) ACTION — raw back-and-forth, independent of the WP curve: how often the
    #    lead actually changed hands and how often the game was tied. A game that
    #    traded buckets all night rates here even if one side was mildly favored.
    action = min(1.0,
                 0.65 * _norm(lead_changes, 2, 20) +
                 0.35 * _norm(times_tied, 2, 16))

    # ======================================================================
    #  "Worth watching?" — three independent reasons to tune in, each 0..1,
    #  combined so the STRONGEST reason carries the game (noisy-OR), not
    #  averaged. A game elite on any one pillar lands high; only a game weak on
    #  all three lands low. This is a watchability score, not a drama/win-prob
    #  index: win-probability closeness is just one input to one pillar.
    # ======================================================================

    # A) COMPETITIVENESS — was it a real contest? Win-prob closeness (how often
    #    it was a coin-flip, and for how long), a tight final margin, and
    #    lead/tie trading. Margin is spoiler-safe: it reveals neither winner
    #    nor score.
    wp_close = 0.5 * tension + 0.5 * close_time
    if final_margin is not None:
        margin_close = 1.0 - _norm(final_margin, 2, 22)   # 2 pts -> ~1, 22+ -> 0
        competitiveness = 0.45 * wp_close + 0.30 * margin_close + 0.25 * action
    else:
        margin_close = None
        competitiveness = 0.62 * wp_close + 0.38 * action

    # B) DRAMA — a memorable moment: a clutch coin-flip finish, overtime (tied
    #    at the buzzer by definition), or the winner climbing out of a deep hole.
    ot_signal = min(1.0, 0.75 + 0.12 * (ot_periods - 1)) if ot_periods >= 1 else 0.0
    drama = 0.45 * finish + 0.40 * ot_signal + 0.15 * comeback

    # C) STARS — standout individual play: a big top-scoring night, a triple-
    #    double, and several 30-point scorers (stars going off on both sides).
    stars = min(1.0,
                0.55 * _norm(top_scorer, 25, 55) +
                0.25 * (1.0 if triple_double else 0.0) +
                0.20 * _norm(thirty_plus, 1, 4))

    comp_p = max(0.0, min(1.0, competitiveness))
    dram_p = max(0.0, min(1.0, drama))
    star_p = max(0.0, min(1.0, stars))

    # BEST REASON carries the game: noisy-OR of the three pillars. The top pillar
    # sets the floor; each weaker pillar adds on top with diminishing returns.
    worth = 1.0 - (1.0 - comp_p) * (1.0 - dram_p) * (1.0 - star_p)
    index = int(round(max(0.0, min(100.0, 100.0 * worth))))

    comp = {
        "competitiveness": round(comp_p, 3),
        "drama": round(dram_p, 3),
        "stars": round(star_p, 3),
        "wp_close": round(wp_close, 3),
        "margin_close": (round(margin_close, 3) if margin_close is not None else None),
        "action": round(action, 3),
        "finish": round(finish, 3),
        "ot_signal": round(ot_signal, 3),
        "comeback": round(comeback, 3),
        "tension": round(tension, 3),
        "close_time": round(close_time, 3),
        "swing": round(swing, 3),
        "ot": ot_periods,
        "lead_changes": lead_changes,
        "times_tied": times_tied,
        "final_margin": final_margin,
        "top_scorer": top_scorer,
        "thirty_plus": thirty_plus,
        "triple_double": triple_double,
    }
    if debug:
        for k, v in comp.items():
            print(f"    {k:16} {v}")
    return index, comp


def _norm(x, lo, hi):
    """Map x in [lo,hi] to [0,1] with a soft knee; clamps outside."""
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


def tier(index):
    if index >= TIER_MUST:
        return "must"
    if index >= TIER_GREAT:
        return "great"
    if index >= TIER_GOOD:
        return "good"
    return "skip"


# ======================================================================
#  Pull the pieces out of an ESPN game summary (spoiler-safe fields only).
# ======================================================================
def _win_prob_series(summary):
    arr = summary.get("winprobability") or []
    out = []
    for e in arr:
        v = e.get("homeWinPercentage")
        if v is not None:
            out.append(float(v))
    return out


def _ot_periods(summary):
    # status.period > 4 means overtime; each extra period beyond 4 is one OT.
    hdr = summary.get("header") or {}
    comps = (hdr.get("competitions") or [{}])
    st = (comps[0].get("status") or {})
    per = st.get("period")
    if isinstance(per, int) and per > 4:
        return per - 4
    # fallback: count distinct periods in plays, if present
    return 0


def _team_stat(summary, names, labels):
    """Best integer value of a team-level box score stat, by name or label."""
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
    """Return (top_points, had_triple_double, thirty_plus_scorers).

    Spoiler-safe: individual lines only, never a team total or the winner.
    """
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
    """Absolute final point margin, or None if unavailable.

    Margin only — it reveals neither the winner nor the score, so it stays
    spoiler-safe. Used solely to feed the close-final bonus, never written out.
    """
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


def _identity_from_event(ev):
    """(game_id, ot_periods) from a scoreboard event, or (None, 0).

    The scoreboard event is the reliable source of team sides, the game's UTC
    date (which the app's gameId() is built from) and the period count (for OT).
    """
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
    """Rate one finished game. `ev` is a scoreboard event dict.

    Returns (game_id, value) or None. Only the win-probability shape and
    spoiler-safe extras go into the value — never scores, margin or winner.
    """
    gid, ot = _identity_from_event(ev)
    if not gid:
        return None
    event_id = ev.get("id")
    summary = fetch_json(f"{SUMMARY_URL}?event={event_id}")
    wp = _win_prob_series(summary)
    if len(wp) < 8:
        return None  # win-probability not published (yet)
    if not ot:
        ot = _ot_periods(summary)   # belt-and-braces if the event lacked a period

    top, td, thirty = _star(summary)
    lc = _lead_changes(summary)
    tt = _times_tied(summary)
    margin = _final_margin(summary)
    if debug:
        print(f"  {gid}  (event {event_id})  plays={len(wp)}")
    index, c = excitement(wp, ot_periods=ot, top_scorer=top, triple_double=td,
                          thirty_plus=thirty, lead_changes=lc, times_tied=tt,
                          final_margin=margin, debug=debug)

    # The app shows only the Hype score (meter + "/100"), so ratings.json stays
    # lean and carries no spoiler-y tags — no "comeback", no result hints.
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
    """Find a scoreboard event dict for a single id (scans recent dates)."""
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
        # default: the last 4 days (covers late runs and corrections)
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
            # A finished game's rating never changes, so skip ones already rated —
            # this keeps the live watcher's repeated polls cheap (no summary re-fetch).
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
