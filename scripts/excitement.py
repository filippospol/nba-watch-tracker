name: Update Excitement Ratings

# Backstop only. The live watcher already rates games as they finish; this runs
# once each morning to pick up any rating whose win-probability was published
# late, and to fill gaps if the watcher was delayed.
#
# It can also do a full re-score on demand: run it from the Actions tab
# (workflow_dispatch) with a from_date and rebuild=true to wipe ratings.json and
# re-rate the whole season from scratch — e.g. after a scoring-methodology change.
on:
  schedule:
    - cron: "50 9 * * *"         # 09:50 Greek — just after the schedule backstop
      timezone: "Europe/Athens"
  workflow_dispatch:
    inputs:
      from_date:
        description: "Backfill start date YYYYMMDD (blank = normal last-4-days run)"
        required: false
        default: ""
      rebuild:
        description: "Delete ratings.json first, so every game is re-scored"
        type: boolean
        required: false
        default: false

permissions:
  contents: write

concurrency:
  group: data-updates
  cancel-in-progress: false

jobs:
  excitement:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Rate games
        env:
          FROM_DATE: ${{ inputs.from_date }}
          REBUILD: ${{ inputs.rebuild }}
        run: |
          # On schedule runs both inputs are empty/false -> identical to the old
          # no-arg behaviour, so the nightly backstop is unchanged.
          if [ "$REBUILD" = "true" ]; then
            echo "Rebuild requested: removing ratings.json so all games re-score."
            rm -f ratings.json
          fi
          if [ -n "$FROM_DATE" ]; then
            echo "Backfilling from $FROM_DATE ..."
            python scripts/excitement.py --from "$FROM_DATE"
          else
            python scripts/excitement.py
          fi

      - name: Commit if changed
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add ratings.json
          if git diff --cached --quiet; then
            echo "No ratings changes."
          else
            git commit -m "Update ratings.json"
            git pull --rebase --autostash
            git push
          fi
