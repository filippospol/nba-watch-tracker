#!/usr/bin/env python3
"""Generate ``gameids.json``: the app's game id -> NBA.com's 10-digit game id.

The app builds a game id as ``YYYY-MM-DD_AWAY@HOME`` where the date is the game's
*UTC* calendar day and AWAY/HOME are the three-letter tricodes (see ``gameId`` in
docs/index.html). NBA.com's game page is

    https://www.nba.com/game/<away>-vs-<home>-<gameId>

so the front end only needs that 10-digit ``gameId`` to link each matchup
straight to the game (and, with ``?watchFullGame``, to League Pass).

ESPN — the source for schedule.json — doesn't carry NBA's official game id, so we
read it from the league's own public schedule feed on cdn.nba.com. For every NBA
game (both teams among the 30) we emit

    { "2026-10-03_MIA@TOR": "0012600009", ... }

keyed exactly the way the app keys its games, so the browser lookup is a direct
hit with no date-maths on the client.

Run from the repo root:

    python scripts/fetch_gameids.py

Note: cdn.nba.com must be reachable (it is from GitHub Actions and a normal
machine). ``--source FILE`` reads a local feed copy instead, for offline tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from espn import NAME2TRI, fetch_json

OUT = Path(__file__).resolve().parents[1] / "gameids.json"

# NBA's static season schedule. The ``_1`` file is the CDN-cached copy that
# updates within minutes of a schedule change; the plain file is the fallback.
SCHEDULE_URLS = (
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
    "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
)

# The 30 NBA tricodes, as the app uses them. Anything else (All-Star sides,
# international preseason opponents) is skipped so only real matchups get a link.
NBA_TRICODES = set(NAME2TRI.values())


def _load_feed(source: str | None) -> dict:
    """Return the parsed NBA schedule feed, from ``source`` file or the CDN."""
    if source:
        return json.loads(Path(source).read_text(encoding="utf-8"))
    last: Exception | None = None
    for url in SCHEDULE_URLS:
        try:
            return fetch_json(url)
        except Exception as exc:  # network / parse — try the next URL
            last = exc
    raise RuntimeError(f"Could not fetch the NBA schedule feed: {last}")


def _app_id(game: dict) -> str | None:
    """App game id ``YYYY-MM-DD_AWAY@HOME`` (UTC day), or None if not an NBA game."""
    away = (game.get("awayTeam") or {}).get("teamTricode")
    home = (game.get("homeTeam") or {}).get("teamTricode")
    if away not in NBA_TRICODES or home not in NBA_TRICODES:
        return None
    # gameDateTimeUTC looks like "2026-10-03T23:00:00Z"; its date is the UTC day,
    # which is exactly how the app derives the date half of its game id.
    utc = str(game.get("gameDateTimeUTC") or "")
    day = utc[:10]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None
    return f"{day}_{away}@{home}"


def build_map(feed: dict) -> dict[str, str]:
    """Flatten the feed into ``{app_game_id: nba_game_id}`` for NBA matchups."""
    out: dict[str, str] = {}
    schedule = feed.get("leagueSchedule") or {}
    for date_block in schedule.get("gameDates") or []:
        for game in date_block.get("games") or []:
            gid = str(game.get("gameId") or "").strip()
            if not gid or gid == "0" * len(gid):
                continue
            app_id = _app_id(game)
            if app_id:
                out[app_id] = gid
    return out


def _dump(mapping: dict[str, str]) -> str:
    # One game per line, sorted — a stable, reviewable diff in git.
    inner = ",\n".join(
        f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(mapping.items())
    )
    return "{\n" + inner + "\n}\n"


def main(argv: list[str]) -> int:
    source = None
    if "--source" in argv:
        i = argv.index("--source")
        if i + 1 < len(argv):
            source = argv[i + 1]

    feed = _load_feed(source)
    mapping = build_map(feed)
    if not mapping:
        print("ERROR: parsed 0 game ids. Leaving gameids.json untouched.", file=sys.stderr)
        return 1

    OUT.write_text(_dump(mapping), encoding="utf-8")
    print(f"Wrote {len(mapping)} game ids to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
