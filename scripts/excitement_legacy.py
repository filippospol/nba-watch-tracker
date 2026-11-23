#!/usr/bin/env python3
"""Frozen copy of the *previous* Excitement Index scoring (pre-rebalance).

This is kept only so the old and new scores can be compared side by side; the
live scoring lives in ``excitement.py``. The difference is the weighting:

  * OLD (this file): the base is 100% win-probability flow
        swing 0.42 + tension 0.18 + close_time 0.18 + finish 0.16 + comeback 0.06
    and lead changes / top scorer / triple-double are small flat *bonuses*.

  * NEW (excitement.py): win-probability flow is ~75% of the base; "action"
    (lead changes + times tied) is 15% and "stars" (top scorer + triple-double
    + 30-point scorers) is 10%, as proper weighted components.

Only the pure scoring function is duplicated here. For data extraction and
batch running, use ``excitement.py`` (its extractors feed both scorers — see
``compare_scores.py``).
"""
from __future__ import annotations

# tiers — unchanged from the original
TIER_MUST = 70      # "Must-watch"
TIER_GOOD = 45      # "Worth it"  (below this -> "Skip")


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


def excitement(wp, *, ot_periods=0, top_scorer=0, triple_double=False,
               lead_changes=0, debug=False, **_ignored):
    """OLD scoring. Return (index 0-100, {components}) from a home-win-prob series.

    Extra keyword arguments (e.g. ``thirty_plus``, ``times_tied``) are accepted
    and ignored, so the same call used for the new scorer works here unchanged.
    """
    wp = [min(1.0, max(0.0, float(x))) for x in wp if x is not None]
    if len(wp) < 8:
        return 0, {"reason": "no win-probability data"}

    deltas = [abs(wp[i] - wp[i - 1]) for i in range(1, len(wp))]
    n = len(deltas)

    # 1) SWING — total movement in win probability (the core GEI).
    swing_sum = sum(deltas)
    swing_per100 = swing_sum / n * 100.0
    weighted = sum(d * (1.0 + 1.6 * (i / n)) for i, d in enumerate(deltas))
    swing = 0.6 * swing_sum + 0.4 * (weighted / 1.8)

    # 2) TENSION — how often the game was a coin-flip, weighted toward the end.
    tens = 0.0
    tw = 0.0
    for i, p in enumerate(wp):
        w = 1.0 + 2.0 * (i / len(wp))
        tens += w * (1.0 - 2.0 * abs(p - 0.5))
        tw += w
    tension = max(0.0, tens / tw)

    # 3) CLOSE_TIME — fraction of plays inside a 35-65% band, weighted late.
    ct = 0.0
    ctw = 0.0
    for i, p in enumerate(wp):
        w = 1.0 + 1.0 * (i / len(wp))
        ct += w * (1.0 if abs(p - 0.5) <= 0.15 else 0.0)
        ctw += w
    close_time = ct / ctw

    # 4) FINISH — closest the game got to 50/50 across the final ~8% of plays.
    tail = wp[max(0, int(len(wp) * 0.92)):]
    finish = max(0.0, 1.0 - 2.0 * min(abs(p - 0.5) for p in tail))

    # 5) COMEBACK — how big a hole the eventual winner climbed out of.
    winner_home = wp[-1] >= 0.5
    win_series = wp if winner_home else [1 - p for p in wp]
    comeback = max(0.0, 0.5 - min(win_series)) * 2.0

    # ---- combine: the GAME ITSELF (win-prob flow) is 100% of the base -------
    base = (
        100.0 * _norm(swing, 1.3, 9.5) * 0.42 +   # total drama (the core GEI)
        100.0 * tension * 0.18 +                  # how often it was a coin-flip
        100.0 * close_time * 0.18 +               # close for a long stretch
        100.0 * finish * 0.16 +                   # clutch-time doubt
        100.0 * comeback * 0.06
    )

    # ---- bonuses: game-shape extras first; player box score barely moves it
    bonus = 0.0
    if comeback >= 0.85:
        bonus += 6
    elif comeback >= 0.70:
        bonus += 3
    if ot_periods >= 1:
        bonus += 6 + 3 * (ot_periods - 1)
    if lead_changes >= 15:
        bonus += 3
    elif lead_changes >= 10:
        bonus += 1
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
