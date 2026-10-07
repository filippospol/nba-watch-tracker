#!/usr/bin/env python3
"""Generate ``logos.json`` from ESPN's public team feed.

Each ESPN team is matched to the app's tricode by full name (so
"Golden State Warriors" -> "GSW" and non-NBA teams are skipped), and the file
is written as ``{ "ATL": <value>, ... }``.

Value format:
  * default      -> the ESPN CDN URL ("https://a.espncdn.com/i/teamlogos/...")
  * ``--embed``  -> a self-contained data URI ("data:image/png;base64,...")

URL mode needs only one request (the team list); it's the light, default
choice. NOTE: the current app blob-wraps each value as image/svg+xml, so using
this file needs a one-line change in loadLogos() to use the value as an <img>
src directly. Logos are static — run on demand, not daily.

Run from the repo root:

    python scripts/build_logos.py            # ESPN URLs (default)
    python scripts/build_logos.py --embed     # embedded PNG data URIs instead
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from espn import TEAMS_URL, fetch, fetch_json, name_to_tricode

OUT = Path(__file__).resolve().parents[1] / "logos.json"


def _iter_teams(data: dict):
    sports = data.get("sports") or [{}]
    leagues = sports[0].get("leagues") or [{}]
    for wrap in leagues[0].get("teams", []):
        yield wrap.get("team") or {}


def _default_logo_href(team: dict) -> str | None:
    logos = team.get("logos") or []
    if not logos:
        return None
    for lg in logos:  # prefer the plain "default" (not the dark) variant
        rel = lg.get("rel") or []
        if "default" in rel and "dark" not in rel:
            return lg.get("href")
    return logos[0].get("href")


def team_logo_urls(data: dict) -> dict[str, str]:
    """Return ``{tricode: logo_url}`` for the 30 NBA teams."""
    out: dict[str, str] = {}
    for team in _iter_teams(data):
        tri = name_to_tricode(team.get("displayName", ""))
        href = _default_logo_href(team)
        if tri and href:
            out[tri] = href
    return out


def _dump(logos: dict[str, str]) -> str:
    inner = ",\n".join(
        f"  {json.dumps(tri)}: {json.dumps(val, ensure_ascii=False)}"
        for tri, val in sorted(logos.items())
    )
    return "{\n" + inner + "\n}\n"


def main() -> int:
    embed = "--embed" in sys.argv
    allow_partial = "--allow-partial" in sys.argv

    urls = team_logo_urls(fetch_json(TEAMS_URL))
    if len(urls) < 30:
        print(f"WARNING: matched only {len(urls)}/30 NBA teams.", file=sys.stderr)
    if not urls:
        print("ERROR: no teams matched — nothing written.", file=sys.stderr)
        return 1

    # Default: store the URLs as-is (one request, nothing to download).
    if not embed:
        OUT.write_text(_dump(urls), encoding="utf-8")
        print(f"Wrote {len(urls)} logo URLs to {OUT}")
        return 0

    # --embed: download each PNG and inline it as a data URI.
    logos: dict[str, str] = {}
    failed: list[str] = []
    for tri, href in sorted(urls.items()):
        try:
            png = fetch(href)
            if not png.startswith(b"\x89PNG"):
                raise ValueError("not a PNG")
            logos[tri] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            print(f"  {tri}: ok ({len(png) // 1024} KB)")
        except Exception as exc:  # noqa: BLE001
            failed.append(tri)
            print(f"  {tri}: FAILED ({exc})", file=sys.stderr)

    if not logos:
        print("ERROR: no logos produced — nothing written.", file=sys.stderr)
        return 1
    if failed and not allow_partial:
        print(
            f"ERROR: {len(failed)} logo(s) failed: {', '.join(failed)}. "
            "Leaving logos.json untouched (use --allow-partial to override).",
            file=sys.stderr,
        )
        return 1

    OUT.write_text(_dump(logos), encoding="utf-8")
    print(f"Wrote {len(logos)} embedded logos to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
