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


def _dates_to_poll() -> list[str]:
    """The current NBA night (ET), plus the day before to catch any late final."""
    now = datetime.now(EASTERN)
    return [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]


def main() -> int:
    try:
        rows = json.loads(OUT.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        print("schedule.json missing/invalid — run fetch_schedule.py first", file=sys.stderr)
        print("PENDING=0")
        return 1

    index = {(r[0], r[1], r[2]): r for r in rows if isinstance(r, list) and len(r) >= 3}
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
                pending += 1                     # scheduled or in progress tonight
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
    print(f"PENDING={pending if fetched else 'unknown'}")   # 0 => tonight's slate is complete
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
