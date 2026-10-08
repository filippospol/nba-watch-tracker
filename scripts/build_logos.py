#!/usr/bin/env python3
"""Generate ``logos.json`` from ESPN's public team feed, and optionally save the
logo images into the site so the app can serve them itself.

Each ESPN team is matched to the app's tricode by full name (so
"Golden State Warriors" -> "GSW" and non-NBA teams are skipped).

Modes:
  * default         -> write logos.json as { "ATL": <ESPN CDN URL>, ... }
  * ``--embed``     -> write logos.json with embedded data URIs instead of URLs
  * ``--download DIR`` -> ALSO download every logo to DIR/<TRI>.png
                       (e.g. docs/logos/ATL.png). The app loads logos from there,
                       which keeps them same-origin so the PNG export can draw them.

Logos are static — run on demand or monthly, not daily.

Run from the repo root:

    python scripts/build_logos.py                        # ESPN URLs only
    python scripts/build_logos.py --download docs/logos  # URLs + image files for the site
    python scripts/build_logos.py --embed                # embedded PNG data URIs
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from espn import TEAMS_URL, fetch, fetch_json, name_to_tricode

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logos.json"


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


def _arg_value(flag: str) -> str | None:
    argv = sys.argv[1:]
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return None


def _download_pngs(urls: dict[str, str]) -> tuple[dict[str, bytes], list[str]]:
    """Fetch every logo PNG; return ({tri: bytes}, [failed tricodes])."""
    pngs: dict[str, bytes] = {}
    failed: list[str] = []
    for tri, href in sorted(urls.items()):
        try:
            png = fetch(href)
            if not png.startswith(b"\x89PNG"):
                raise ValueError("not a PNG")
            pngs[tri] = png
            print(f"  {tri}: ok ({len(png) // 1024} KB)")
        except Exception as exc:  # noqa: BLE001
            failed.append(tri)
            print(f"  {tri}: FAILED ({exc})", file=sys.stderr)
    return pngs, failed


def main() -> int:
    embed = "--embed" in sys.argv
    allow_partial = "--allow-partial" in sys.argv
    download_dir = _arg_value("--download")
    if "--download" in sys.argv and not download_dir:
        print("ERROR: --download needs a folder, e.g. --download docs/logos", file=sys.stderr)
        return 2

    urls = team_logo_urls(fetch_json(TEAMS_URL))
    if len(urls) < 30:
        print(f"WARNING: matched only {len(urls)}/30 NBA teams.", file=sys.stderr)
    if not urls:
        print("ERROR: no teams matched — nothing written.", file=sys.stderr)
        return 1

    pngs: dict[str, bytes] = {}
    failed: list[str] = []
    if embed or download_dir:
        pngs, failed = _download_pngs(urls)
        if not pngs:
            print("ERROR: no logos downloaded — nothing written.", file=sys.stderr)
            return 1
        if failed and not allow_partial:
            print(
                f"ERROR: {len(failed)} logo(s) failed: {', '.join(failed)}. "
                "Leaving files untouched (use --allow-partial to override).",
                file=sys.stderr,
            )
            return 1

    # --download: write the image files into the site folder
    if download_dir:
        target = (ROOT / download_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        written = 0
        for tri, png in pngs.items():
            path = target / f"{tri}.png"
            if not path.exists() or path.read_bytes() != png:
                path.write_bytes(png)
                written += 1
        print(f"Saved {len(pngs)} logos to {target} ({written} new or changed)")

    # logos.json: URLs by default, data URIs with --embed
    if embed:
        logos = {tri: "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                 for tri, png in pngs.items()}
        OUT.write_text(_dump(logos), encoding="utf-8")
        print(f"Wrote {len(logos)} embedded logos to {OUT}")
    else:
        OUT.write_text(_dump(urls), encoding="utf-8")
        print(f"Wrote {len(urls)} logo URLs to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
