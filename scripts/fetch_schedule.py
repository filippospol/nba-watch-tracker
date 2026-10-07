#!/usr/bin/env python3
"""Generate ``schedule.json`` from ESPN's public scoreboard feed.

Output matches the app's embedded ``SEED``: a JSON array of
``["YYYY-MM-DD HH:MM", away, home]`` where the timestamp is US Eastern wall
time (ESPN reports UTC, so it is converted) and team names are the app's
canonical "City Name" form.

Strategy: one scoreboard call returns the season's ``calendar`` of game dates;
each date is then fetched and its games flattened. By default only games
between two of the 30 NBA teams are kept (``--include-nonnba`` keeps all).

Run from the repo root:

    python scripts/fetch_schedule.py                 # whole season
    python scripts/fetch_schedule.py --from 20251020 --to 20251101
    python scripts/fetch_schedule.py --include-nonnba
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from espn import SCOREBOARD_URL, canonical_name, fetch_json, name_to_tricode

OUT = Path(__file__).resolve().parents[1] / "schedule.json"
EASTERN = ZoneInfo("America/New_York")


def _parse_args(argv: list[str]) -> tuple[str | None, str | None, bool]:
    include_nonnba = "--include-nonnba" in argv
    date_from = date_to = None
    for i, a in enumerate(argv):
        if a == "--from" and i + 1 < len(argv):
            date_from = argv[i + 1]
        elif a == "--to" and i + 1 < len(argv):
            date_to = argv[i + 1]
    return date_from, date_to, include_nonnba


def calendar_dates(board: dict) -> list[str]:
    """Return the season's game dates as 'YYYYMMDD' strings, from a scoreboard."""
    leagues = board.get("leagues") or [{}]
    cal = leagues[0].get("calendar") or []
    out = []
    for entry in cal:
        # entries look like "2025-10-21T07:00Z"; the date prefix is the ET game day
        d = str(entry)[:10]
        if len(d) == 10:
            out.append(d.replace("-", ""))
    return out


def _eastern(iso_utc: str) -> str | None:
    """'2026-01-21T00:00Z' (UTC) -> '2026-01-20 19:00' (US Eastern)."""
    s = iso_utc.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M")


def rows_from_board(board: dict, *, include_nonnba: bool = False) -> list[list[str]]:
    """Flatten one scoreboard day into ``[when, away, home]`` rows."""
    rows: list[list[str]] = []
    for ev in board.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        competitors = comps[0].get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_name = canonical_name((home.get("team") or {}).get("displayName", ""))
        away_name = canonical_name((away.get("team") or {}).get("displayName", ""))
        if not include_nonnba and not (
            name_to_tricode(home_name) and name_to_tricode(away_name)
        ):
            continue
        when = _eastern(ev.get("date") or comps[0].get("date") or "")
        if not when:
            continue
        row = [when, away_name, home_name]
        score = _final_score(comps[0], ev, home, away)
        if score is not None:
            row += list(score)  # [away_score, home_score]
        rows.append(row)
    return rows


def _final_score(comp: dict, ev: dict, home: dict, away: dict):
    """Return (away_score, home_score) as ints if the game is final, else None."""
    status = (comp.get("status") or ev.get("status") or {}).get("type") or {}
    if not status.get("completed"):
        return None
    try:
        return int(away.get("score")), int(home.get("score"))
    except (TypeError, ValueError):
        return None


def _dump(rows: list) -> str:
    inner = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in rows)
    return "[\n" + inner + "\n]\n"


def main() -> int:
    date_from, date_to, include_nonnba = _parse_args(sys.argv[1:])

    board = fetch_json(SCOREBOARD_URL)  # default call carries the calendar
    dates = calendar_dates(board)
    if date_from:
        dates = [d for d in dates if d >= date_from]
    if date_to:
        dates = [d for d in dates if d <= date_to]
    if not dates:
        print("ERROR: no game dates found in the scoreboard calendar.", file=sys.stderr)
        return 1

    seen: set[tuple[str, str, str]] = set()
    rows: list[list[str]] = []
    for i, d in enumerate(dates):
        day = fetch_json(f"{SCOREBOARD_URL}?dates={d}")
        for row in rows_from_board(day, include_nonnba=include_nonnba):
            key = (row[0], row[1], row[2])
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(dates)} dates, {len(rows)} games so far")
        time.sleep(0.2)  # be polite to ESPN

    if not rows:
        print("ERROR: parsed 0 games. Leaving schedule.json untouched.", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    OUT.write_text(_dump(rows), encoding="utf-8")
    print(f"Wrote {len(rows)} games to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
