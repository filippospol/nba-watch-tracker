#!/usr/bin/env Rscript
# Generate gameids.json for NBA Watch Tracker, from your laptop.
#
# Maps the app's own game id  "YYYY-MM-DD_AWAY@HOME"  (UTC calendar day)  ->  NBA.com's
# 10-digit game id, so each matchup can link to https://www.nba.com/game/<away>-vs-<home>-<id>.
# Reads the league's own schedule feed on cdn.nba.com (which your laptop can reach even if
# the GitHub runner can't), so the NBA Cup / non-chronological numbering is always correct.
#
# Usage (from the repo root, so it writes next to schedule.json):
#   Rscript scripts/fetch_gameids.R
# then:  git add gameids.json && git commit -m "Update gameids.json" && git push
#
# Needs the httr and jsonlite packages (installed automatically if missing).

suppressWarnings(suppressMessages({
  for (p in c("httr", "jsonlite")) {
    if (!requireNamespace(p, quietly = TRUE)) {
      install.packages(p, repos = "https://cloud.r-project.org")
    }
  }
  library(httr); library(jsonlite)
}))

# The 30 NBA tricodes, exactly as the app uses them. Anything else (All-Star sides,
# international preseason opponents) is skipped so only real matchups get a link.
NBA_TRICODES <- c("ATL","BOS","BKN","CHA","CHI","CLE","DAL","DEN","DET","GSW",
                  "HOU","IND","LAC","LAL","MEM","MIA","MIL","MIN","NOP","NYK",
                  "OKC","ORL","PHI","PHX","POR","SAC","SAS","TOR","UTA","WAS")

# NBA's static season schedule. The "_1" file is the CDN-cached copy that refreshes
# within minutes of a schedule change; the plain file is the fallback.
SCHEDULE_URLS <- c(
  "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
  "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
)

# Browser-like headers — NBA's CDN (Akamai) 403s requests that don't look like a
# real nba.com tab.
NBA_HEADERS <- add_headers(
  `User-Agent`      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  Accept            = "*/*",
  `Accept-Language` = "en-US,en;q=0.9",
  Referer           = "https://www.nba.com/",
  Origin            = "https://www.nba.com"
)

fetch_feed <- function() {
  for (url in SCHEDULE_URLS) {
    for (attempt in 1:4) {
      resp <- tryCatch(GET(url, NBA_HEADERS, timeout(60)), error = function(e) NULL)
      if (!is.null(resp) && status_code(resp) == 200) {
        txt <- content(resp, as = "text", encoding = "UTF-8")
        return(fromJSON(txt, simplifyVector = FALSE))
      }
      Sys.sleep(2 * attempt)
    }
  }
  stop("Could not fetch the NBA schedule feed from cdn.nba.com (network / 403).")
}

# Allow an offline test:  Rscript scripts/fetch_gameids.R path/to/local_feed.json
args <- commandArgs(trailingOnly = TRUE)
feed <- if (length(args) >= 1) fromJSON(readLines(args[1], warn = FALSE), simplifyVector = FALSE) else fetch_feed()

game_dates <- feed$leagueSchedule$gameDates
keys <- character(0); ids <- character(0)

for (d in game_dates) {
  for (g in d$games) {
    gid <- as.character(g$gameId)
    if (length(gid) == 0 || gid == "" || grepl("^0+$", gid)) next          # skip blanks / placeholder "0000000000"
    away <- g$awayTeam$teamTricode
    home <- g$homeTeam$teamTricode
    if (is.null(away) || is.null(home)) next
    if (!(away %in% NBA_TRICODES) || !(home %in% NBA_TRICODES)) next        # NBA matchups only
    utc <- as.character(g$gameDateTimeUTC)                                  # e.g. "2026-10-03T23:00:00Z"
    if (length(utc) == 0 || nchar(utc) < 10) next
    day <- substr(utc, 1, 10)                                              # the UTC calendar day = the app's key date
    keys <- c(keys, paste0(day, "_", away, "@", home))
    ids  <- c(ids, gid)
  }
}

# Sort by key (stable git diff) and drop any duplicate keys, keeping the first.
o <- order(keys); keys <- keys[o]; ids <- ids[o]
keep <- !duplicated(keys); keys <- keys[keep]; ids <- ids[keep]

if (length(keys) == 0) stop("Parsed 0 game ids; leaving gameids.json untouched.")

# Same on-disk format as the Python version: one "key": "id" per line.
body <- paste0('  "', keys, '": "', ids, '"', collapse = ",\n")
cat(paste0("{\n", body, "\n}\n"), file = "gameids.json")
cat(sprintf("Wrote %d game ids to gameids.json\n", length(keys)))
