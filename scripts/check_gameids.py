#!/usr/bin/env python3
"""Cross-check schedule.json (ESPN) against gameids.json (NBA.com).

schedule.json and gameids.json come from two different feeds and are joined in
the browser purely by the app's game id ``YYYY-MM-DD_AWAY@HOME`` (UTC day). This
script rebuilds that key from schedule.json exactly the way docs/index.html does
(``etWallToUTCms`` + ``gameId``) and reports how many scheduled games have a
matching entry in gameids.json — i.e. how many matchups will actually get a link.

Run from the repo root, after populating gameids.json:

    python scripts/fetch_gameids.py
    python scripts/check_gameids.py            # coverage + any misses
    python scripts/check_gameids.py --show 5   # also print 5 sample keys
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from espn import NAME2TRI, canonical_name

ROOT = Path(__file__).resolve().parents[1]

# Mirror the app's logic (docs/index.html etWallToUTCms) EXACTLY so the key we build
# here is byte-for-byte the one the browser builds from the same schedule row.
# US Eastern: EDT (UTC-4) from the 2nd Sunday of March 02:00 to the 1st Sunday of
# November 02:00, else EST (UTC-5) — computed per year.
ABBR = dict(NAME2TRI)


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    # weekday(): Mon=0..Sun=6 -> days to first Sunday
    first_sun = 1 + ((6 - first.weekday()) % 7)
    return datetime(year, month, first_sun + (n - 1) * 7, 2, 0, tzinfo=timezone.utc)


def _et_offset(naive: datetime) -> int:
    y = naive.year
    return 4 if (_nth_sunday(y, 3, 2) <= naive < _nth_sunday(y, 11, 1)) else 5


def et_wall_to_utc_ms(wall: str) -> int:
    d, t = wall.split(" ")
    y, mo, da = (int(x) for x in d.split("-"))
    h, mi = (int(x) for x in t.split(":"))
    naive = datetime(y, mo, da, h, mi, tzinfo=timezone.utc)
    off = _et_offset(naive)
    return int((datetime(y, mo, da, tzinfo=timezone.utc) + timedelta(hours=h + off, minutes=mi)).timestamp() * 1000)


def app_game_id(wall: str, away: str, home: str) -> str | None:
    a, h = canonical_name(away), canonical_name(home)
    if a not in ABBR or h not in ABBR:
        return None  # not an NBA matchup; the app never links these
    ms = et_wall_to_utc_ms(wall)
    day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{day}_{ABBR[a]}@{ABBR[h]}"


def main(argv: list[str]) -> int:
    show = 0
    if "--show" in argv:
        i = argv.index("--show")
        show = int(argv[i + 1]) if i + 1 < len(argv) else 10

    sched = json.loads((ROOT / "schedule.json").read_text(encoding="utf-8"))
    ids = json.loads((ROOT / "gameids.json").read_text(encoding="utf-8"))

    keys = []
    for row in sched:
        gid = app_game_id(row[0], row[1], row[2])
        if gid:
            keys.append((gid, row))

    matched = [(k, r) for k, r in keys if k in ids]
    missing = [(k, r) for k, r in keys if k not in ids]

    total = len(keys)
    print(f"NBA matchups in schedule.json : {total}")
    print(f"with a game id in gameids.json: {len(matched)}  ({(len(matched)/total*100 if total else 0):.1f}%)")
    print(f"without an id (no link yet)   : {len(missing)}")

    if show:
        print(f"\nSample app keys built from schedule.json (ESPN side):")
        for k, r in keys[:show]:
            tag = "linked" if k in ids else "no id"
            print(f"  {k}   {r[1]} @ {r[2]}   [{tag}]")

    if missing and len(missing) <= 40:
        print(f"\nScheduled games still missing an id:")
        for k, r in missing:
            print(f"  {k}   {r[1]} @ {r[2]}")
    elif missing:
        print(f"\n{len(missing)} games missing an id (showing 40):")
        for k, r in missing[:40]:
            print(f"  {k}   {r[1]} @ {r[2]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
