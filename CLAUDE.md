# review-tracker-dw — project context

Review aggregation for **Discover Walks**, a Paris tour company. Scrapes
reviews from 6 platforms into `data/reviews.csv`, visualized via Streamlit.

## Architecture

- `scrapers/base_scraper.py` — shared utilities. ALL scrapers must use:
  - `polite_delay()` between page loads (anti-bot pacing)
  - `retry(lambda: ...)` around navigation (exponential backoff)
  - `save_review(review, keys)` for appends — handles dedup + `scraped_at`
  - Dedup key: `(platform, tour_name, reviewer_name, review_date)`,
    lowercased/stripped. Review text is NOT part of the key (platforms
    truncate/reflow text between visits).
- `scrapers/<platform>_scraper.py` — one standalone async-Playwright script
  per platform, runnable as `python scrapers/<name>.py` from the repo root.
  Each inserts its own dir into `sys.path` so `import base_scraper` works.
- `data/reviews.csv` — append-only. Schema: platform, tour_name, rating,
  reviewer_name, review_text, review_date, url, scraped_at.
- `dashboard/app.py` — Streamlit (stub).
- `.github/workflows/scrape.yml` — daily cron; only runs implemented
  scrapers; add new ones to its "Run scrapers" step.

## Status (2026-06-10)

- ✅ `freetour_scraper.py` implemented. Listing-page selectors are proven
  (taken from the rankings tracker, see below). **Review-block selectors on
  tour pages are unverified candidates** — the script tries several and
  prints which matched; expect to refine them after the first real run.
- 🔲 All other scrapers + dashboard are docstring stubs. Each stub's
  docstring records platform-specific gotchas (bot protection, lazy
  loading, consent walls).

## Conventions

- Python, async Playwright, pandas. No paid APIs (hard constraint).
- Headless Chromium with Paris geolocation + fr-FR locale (matches what the
  rankings tracker does; keeps results consistent with what Paris users see).
- Print progress to stdout (these run in cron/CI; logs are the only trace).

## Related project

`~/freetour-tracker` — daily *rankings* tracker for the same company (plus
GuruWalk rankings for Charing Cross). Useful references:
- `tracker.py` — proven freetour.com listing selectors
  (`.city-tour.js-city-tour`, `.city-tour__title`, `.city-tour__provider-name`),
  GDPR dismissal (`#gdpr button`), "Show more" loop
  (`.filters-button__container .filters-button`).
- `guruwalk_tracker.py` — proven guruwalk.com search selectors
  (`[class*='group/card']`, `.line-clamp-2` title, `.line-clamp-1` provider).
- Its `git_push()` pattern: always `git pull --rebase` before pushing
  (data files get pushed from multiple machines; learned the hard way).

## Discover Walks Paris tours (freetour.com)

- Le Marais Free Tour: Where Parisians Go
- Paris Icons Express Free Tour: Notre-Dame to Louvre
- Montmartre Paris Free Tour: Moulin Rouge to Sacre Coeur
- Paris Left Bank: Writers, Revolution & Black Coffee
- Places Parisians Love: Classic Treasures, Hidden Gems & Locals' Picks
