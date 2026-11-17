# Live dashboard — setup (5 minutes)

A GitHub Actions workflow that publishes a status page to GitHub Pages on every
merge to `main`. Shows build/test/coverage now; renders scenario run output
automatically once FALCON-066/084 produce it.

## Where the files go (extract at the repo root)
- `.github/workflows/live-dashboard.yml`  — the CI + deploy workflow
- `scripts/build_dashboard.py`             — assembles `site/` from test + run outputs
- `dashboard/index.html`                   — the page shell (reads `data/summary.json`)

## One-time GitHub setup
1. Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. Private repo? GitHub Pages needs a **Pro / Team / Education** plan. Your team's
   GitHub Education plan covers it. (Public repos: free, nothing to do.)
3. Merge these files to `main` (or run the workflow manually from the **Actions**
   tab via "Run workflow"). The run prints the published URL, and it also shows
   under Settings → Pages.

## Adjust to your repo (two spots in the workflow)
- **Install step** — change to match your setup, e.g. `make install`, or
  `pip install -e ".[dev]"`, or `pip install -r requirements.txt`.
- **Test step** — change `pytest ...` to your command (e.g. `make test`). Keep the
  `|| true` so a red test still publishes the page (it'll show red, not block).

## Later (when the scenario harness exists, ~FALCON-066 / 084)
Uncomment the "OPTIONAL" step in the workflow and point it at your harness so it
writes a `run.jsonl` (one JSON exchange per line) at the repo root, e.g.:

    python -m falcon.harness --depth 3 --scenario scenarios/demo.yaml --log-out run.jsonl || true

`build_dashboard.py` already picks up `run.jsonl` automatically and the page shows
the exchange count. To grow it into the full tree/commands view, extend
`dashboard/index.html` to read `data/run.jsonl` and draw the hierarchy — the data
plumbing is already there.

## Notes
- The page is a **visualization/status** surface only — the agents still run
  offline inside the CI runner, so this stays consistent with FALCON's
  offline/LAN-only design.
- This is a **Nice-to-have** deliverable; keep it from pulling focus off the
  Must/Should tickets.
