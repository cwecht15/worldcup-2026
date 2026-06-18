# World Cup Pool 2026 — live + simulated dashboard

A static site that fuses the pool's **live Google-Sheet leaderboard** with **Monte-Carlo
projections** from the `wcpool` engine: each entry's probability of winning the pool, expected
final points, a path-to-victory breakdown, the champion projection, best picks per tier, a team
value/leverage explorer, and the Golden Boot race.

## How it works

```
The Odds API     ─▶ data/teams.csv, data/match_odds.csv   (fetch_odds.py, scheduled)
football-data.org ─▶ data/results.json                    (fetch_results.py, scheduled)
                  build_web_data.py ──▶ web/data/sim.json  (engine + live sheet + scoring)
GitHub Pages serves web/  ──▶  browser
   browser reads:  web/data/sim.json  +  the live Google-Sheet CSV (polled every 60s)
```

**Live results (conditional sim).** When `data/results.json` is present (completed
matches + scorers from football-data.org), the simulation is *conditioned* on it:
played matches are fixed and only the remaining games are simulated. That makes
accrued points, win %, the goals-aware Golden Boot, and the **blocked-from-winning**
flag all reflect where the tournament actually is. With no results file it runs the
pre-tournament projection (footer says which mode is live).

The heavy NumPy simulation can't run in the browser, so a Python builder pre-computes a compact
`sim.json` and the static frontend overlays it on the live sheet (joined by entry name). Live
points stay fresh every 60s for free; the scheduled build keeps the projections current.

## Files

| File | Purpose |
|------|---------|
| `index.html` / `styles.css` | Shell + sleek dark "sportsbook" theme |
| `app.js` | Data layer (load sim.json + live sheet, join, render) |
| `flags.js` | Team → flag image (flagcdn.com) |
| `data/sim.json` | Generated artifact (committed so Pages has data on first load) |

The builder, engine (`wcpool/`), and data (`data/`) live one level up in the repo root.

## Rebuild locally

From the repo root:

```bash
pip install -r requirements.txt
python build_web_data.py                 # build from current odds (no API call)
python build_web_data.py --refresh-odds  # pull fresh DraftKings odds first (needs key)
python build_web_data.py --quick         # fast smoke test
# then preview:
python -m http.server -d web 8901        # open http://127.0.0.1:8901
```

> Serve over HTTP (not `file://`) — the page `fetch()`es `data/sim.json`.

## Deploy (GitHub Pages via Actions)

1. Push this repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions →** add `ODDS_API_KEY` (your The Odds API key)
   and `FOOTBALL_DATA_KEY` (free key from football-data.org, for live results).
4. The workflow `.github/workflows/build-and-deploy.yml` runs on a **game-aligned** cron — seven
   sweeps across 19:00-07:00 UTC (when 2026 WC games actually finish), skipping the dead
   07:00-18:00 UTC window — plus push / manual dispatch. Each run fetches live results, rebuilds
   `sim.json`, and deploys `web/`. **Odds** are refreshed only twice a day (19:00 & 05:00 UTC,
   ~120 requests/month) with a free-tier preflight that skips the refresh if the budget runs low.

Without the secret the site still builds and deploys, using whatever odds are already in
`data/teams.csv`.

## Notes

- Projections **replay the full tournament from current odds** — they do not account for results
  already completed. Live points are real; projected win % is a from-scratch model. This is
  labeled in the footer. (Results-conditional simulation is future work.)
- The live sheet is read from the same published CSV the original site used; entries are matched
  to projections by name. Unmatched names render live-only until the next build.
- `#e=<Entry Name>` deep-links to an entry's path-to-victory.
