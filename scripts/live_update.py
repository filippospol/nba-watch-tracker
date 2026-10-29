#!/usr/bin/env python3
"""Fast, incremental score update for the live watcher.

Unlike ``fetch_schedule.py`` (which rebuilds the whole season), this only looks
at the current NBA night, merges any new FINAL scores into the existing
``schedule.json``, and prints how many games are still unfinished — ``PENDING=n``
— so the watcher knows when to stop polling for the night.

Run from the repo root:
    python scripts/live_update.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from espn import SCOREBOARD_URL, fetch_json, canonical_name, name_to_tricode
from fetch_schedule import _eastern, _dump, _final_score

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "schedule.json"
EASTERN = ZoneInfo("America/New_York")

# A not-yet-started game only counts as "pending" (keeps the watcher polling)
# once it is due to tip off within this many minutes. This stops a morning run
# from treating tonight's whole unplayed slate as pending and looping all day.
IMMINENT_MINUTES = 90


def _dates_to_poll() -> list[str]:
    """The current NBA night (ET), plus the day before to catch any late final."""
    now = datetime.now(EASTERN)
    return [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]


def _parse_when(when: str) -> datetime | None:
    """'2026-10-28 19:30' (ET wall time) -> aware ET datetime, or None."""
    try:
        return datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
    except (ValueError, TypeError):
        return None


def main() -> int:
    try:
        rows = json.loads(OUT.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        print("schedule.json missing/invalid — run fetch_schedule.py first", file=sys.stderr)
        print("PENDING=0")
        return 1

    index = {(r[0], r[1], r[2]): r for r in rows if isinstance(r, list) and len(r) >= 3}
    now = datetime.now(EASTERN)
    pending = 0
    changed = 0
    fetched = False
    for d in _dates_to_poll():
        try:
            board = fetch_json(f"{SCOREBOARD_URL}?dates={d}")
            fetched = True
        except Exception as exc:  # noqa: BLE001
            print(f"  {d}: {exc}", file=sys.stderr)
            continue
        for ev in board.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            hn = canonical_name((home.get("team") or {}).get("displayName", ""))
            an = canonical_name((away.get("team") or {}).get("displayName", ""))
            if not (name_to_tricode(hn) and name_to_tricode(an)):
                continue
            when = _eastern(ev.get("date") or comp.get("date") or "")
            if not when:
                continue
            status = (comp.get("status") or ev.get("status") or {}).get("type") or {}
            if not status.get("completed"):
                # Keep polling only for games that are live now or about to tip —
                # NOT the rest of a not-yet-played slate. Otherwise a run that
                # GitHub starts late in the morning would see tonight's scheduled
                # games as "pending" and loop uselessly until the time cap.
                state = str(status.get("state") or "").lower()
                if state == "in":
                    pending += 1                 # in progress
                else:
                    tip = _parse_when(when)      # scheduled ("pre") or unknown
                    if tip is not None and tip <= now + timedelta(minutes=IMMINENT_MINUTES):
                        pending += 1             # due now or tipping off very soon
                continue
            score = _final_score(comp, ev, home, away)
            if score is None:
                continue
            a, h = score
            key = (when, an, hn)
            row = index.get(key)
            if row is None:                      # a game not in the schedule yet
                row = [when, an, hn, a, h]
                rows.append(row)
                index[key] = row
                changed += 1
            elif len(row) < 5 or row[3] != a or row[4] != h:
                row[:] = [when, an, hn, a, h]    # fill in / correct the final score
                changed += 1

    if changed:
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        OUT.write_text(_dump(rows), encoding="utf-8")

    print(f"CHANGED={changed}")
    # "unknown" when ESPN was unreachable, so the watcher keeps polling rather
    # than stopping early on a transient network blip.
    # 0 => nothing is live or about to tip, so the watcher can stop for now.
    print(f"PENDING={pending if fetched else 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
