"""
Helper functions to scrape data from ESPN.com

* teams  — ``.../basketball/nba/teams``            (team list + logo URLs)
* board  — ``.../basketball/nba/scoreboard?dates=`` (games for a date; also
           returns the season's ``calendar`` of game dates)

* Basic library - endpoints can change without notice so check regularly.

* Author: Filippos Polyzos
"""

# Setup:
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# Proper team abbreviations (from ESPN to NBA.com):
NAME2TRI = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

# Extra: alternate spellings:
ALIAS = {
    "Los Angeles Clippers": "LA Clippers",
    "LA Lakers": "Los Angeles Lakers",
}

# Browser headers:
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Basic functions:
def fetch(url: str, *, retries: int = 3, timeout: int = 30) -> bytes:
    """GET ``url`` with a browser UA and simple backoff; returns raw bytes."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_error}")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))


def canonical_name(name: str) -> str:
    """Normalise an ESPN team name to the app's canonical full name."""
    return ALIAS.get(name, name)


def name_to_tricode(name: str) -> str | None:
    """App tricode for an ESPN team name, or None if it isn't one of the 30."""
    return NAME2TRI.get(canonical_name(name))
