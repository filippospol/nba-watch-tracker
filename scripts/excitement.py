#!/usr/bin/env python3
"""Compute a spoiler-safe "Excitement Index" for finished NBA games from ESPN's
win-probability feed, and write it to ``ratings.json`` for the app to read.

The index (0-100) is built mainly from the Game Excitement Index idea popularised
by inpredictable / Yale: add up how much the win probability swings over the game.
A nail-biter swings constantly; a blowout barely moves. On top of that we fold in a
few extras that fans feel: how close it still was late ("clutch"), how big a hole the
winner climbed out of ("comeback"), overtime, and a star's big scoring night.

Nothing about the result is written in a way the app shows to users: only the index
number, a tier label (Skip / Worth it / Must-watch) and neutral tags. No scores,
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
TIER_MUST = 70      # "Must-watch"
TIER_GOOD = 45      # "Worth it"  (below this -> "Skip")


# ======================================================================
#  The index — a pure function so it can be unit-tested without ESPN.
# ======================================================================
def excitement(wp, *, ot_periods=0, top_scorer=0, triple_double=False,
               lead_changes=0, debug=False):
    """Return (index 0-100, {components}) from a home-win-probability series.

    wp : list[float] in [0,1], one per play, in time order (home team's win
         probability). away prob is 1-wp. A game with <8 points is treated as
         having no usable data and returns 0.
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

    # ---- combine: the GAME ITSELF is ~94% of the score -------------------
    # Weights sum to 1.0. Win-probability flow (swing + tension + close_time +
    # finish) is the whole story; comeback is a small flow extra.
    base = (
        100.0 * _norm(swing, 1.3, 9.5) * 0.42 +   # total drama (the core GEI)
        100.0 * tension * 0.18 +                  # how often it was a coin-flip
        100.0 * close_time * 0.18 +               # close for a long stretch
        100.0 * finish * 0.16 +                   # clutch-time doubt
        100.0 * comeback * 0.06
    )

    # ---- bonuses: game-shape extras first; player box score barely moves it
    bonus = 0.0
    if comeback >= 0.85:          # winner was once all but beaten
        bonus += 6
    elif comeback >= 0.70:
        bonus += 3
    if ot_periods >= 1:
        bonus += 6 + 3 * (ot_periods - 1)         # 2OT, 3OT add more
    if lead_changes >= 15:
        bonus += 3
    elif lead_changes >= 10:
        bonus += 1
    # individual performance is deliberately minor (a point or two at most)
    if top_scorer >= 50:
        bonus += 2
    elif top_scorer >= 40:
        bonus += 1
    if triple_double:
        bonus += 1

    index = int(round(max(0.0, min(100.0, base + bonus))))

    comp = {
        "swing_sum": round(swing_sum, 3),
        "swing_per100": round(swing_per100, 2),
        "swing": round(swing, 3),
        "tension": round(tension, 3),
        "close_time": round(close_time, 3),
        "finish": round(finish, 3),
        "comeback": round(comeback, 3),
        "base": round(base, 1),
        "bonus": round(bonus, 1),
        "ot": ot_periods,
        "lead_changes": lead_changes,
        "top_scorer": top_scorer,
        "triple_double": triple_double,
    }
    if debug:
        for k, v in comp.items():
            print(f"    {k:14} {v}")
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


def _lead_changes(summary):
    box = summary.get("boxscore") or {}
    best = 0
    for team in box.get("teams", []):
        for stat in team.get("statistics", []):
            if stat.get("name") == "leadChanges" or stat.get("label") == "Lead Changes":
                try:
                    best = max(best, int(float(stat.get("displayValue", 0))))
                except (TypeError, ValueError):
                    pass
    return best


def _star(summary):
    """Return (top_points, had_triple_double) without exposing team scores."""
    top = 0
    td = False
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
                cats = sum(1 for key in ("points", "rebounds", "assists",
                                         "steals", "blocks") if num(key) >= 10)
                if cats >= 3:
                    td = True
    return top, td


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

    top, td = _star(summary)
    lc = _lead_changes(summary)
    if debug:
        print(f"  {gid}  (event {event_id})  plays={len(wp)}")
    index, c = excitement(wp, ot_periods=ot, top_scorer=top, triple_double=td,
                          lead_changes=lc, debug=debug)

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
