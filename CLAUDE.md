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
- `data/responses.csv` — written by the dashboard when a review is marked
  "responded". Schema: platform, tour_name, reviewer_name, review_date,
  responded_at. Keyed on the same `(platform, tour_name, reviewer_name,
  review_date)` tuple the scrapers dedup on. Path is overridable via the
  `DW_RESPONSES_CSV` env var (used by the dashboard tests).
- `dashboard/app.py` — Streamlit dashboard (3 tabs; see Status).
- `.github/workflows/scrape.yml` — daily cron; only runs implemented
  scrapers; add new ones to its "Run scrapers" step.

## Status (2026-06-10)

- ✅ `freetour_scraper.py` implemented and verified against the live site.
  Selectors documented in its docstring. Ratings are /10 on the site,
  stored as /5. Only the ~10 most recent reviews per tour are exposed
  without pagination (Marais alone has 334 total — historical backlog
  needs a pagination step if ever wanted).
- ✅ `guruwalk_scraper.py` implemented and verified. Targets **Charing
  Cross Tours** (the company's GuruWalk brand). Review carousel exposes
  only ~5 most recent reviews per tour; dates are month-granularity
  (stored as YYYY-MM); rating from star-SVG path shapes. Selectors
  documented in its docstring.
- ✅ `getyourguide_scraper.py` implemented and verified. Enumerates the 8
  Discover Walks activities from the supplier page (`/discover-walks-s2584/`),
  then drives GYG's own `activity-details-page/blocks` endpoint via an in-page
  `fetch` to paginate the full review history (newest-first). Server caps
  pagination at offset 300, so the two big tours (Marais/Montmartre) yield the
  newest ~300; smaller tours come back complete. **Requires headed real
  Chrome** (Cloudflare 403s every headless variant) — so it canNOT run in CI;
  run it from a desktop. Endpoint shape + caveats in its docstring.
  - Tour names are **English**: the scraper runs with locale `en-US` (locale +
    Accept-Language + the blocks payload's `*Language` fields), since the
    dashboard is English-facing. The reviews themselves stay in each reviewer's
    original language. Changing the locale changes the titles — and therefore
    the dedup keys — so keep it pinned at `en-US`.
  - **Header capture is once-only.** `on_request` grabs the forwardable header
    set from the *first* blocks POST the page fires and then stops. Refreshing
    it on later POSTs (incl. the scraper's own pagination fetches) picks up a
    thinner header set that makes the reviews endpoint ignore the offset and
    return only the ~10 highlighted reviews — which silently truncates deep
    pagination (10/tour instead of ~300).
- ⛔ `tripadvisor_scraper.py` — recon on 2026-06-10 hit a hard block
  (403 + empty body, headless AND headed, stealth args insufficient;
  DataDome tier). See the stub docstring for options (owner export from
  TA Management Center is the recommended path).
- ✅ `dashboard/app.py` implemented. Streamlit, dark theme + Plotly
  (matches `~/freetour-tracker/dashboard.py` style; theme in
  `.streamlit/config.toml`). Three tabs:
  - **Reviews** — quick period buttons (7d/30d/90d/1y/All), sort selector
    (newest / lowest / highest), and the feed. Each card can be marked
    "responded" (persisted to `data/responses.csv`); responded reviews get a
    green badge, unresponded 1–2★ reviews a red "needs reply" badge. Keeps
    the per-review "Draft reply with Claude" button.
  - **Analytics** — period-over-period KPI cards (this period vs the previous
    equal window), a volume + avg-rating chart with weekly/monthly/yearly
    toggle, per-platform and rating-distribution charts, and an "Analyze with
    Claude" section (general + per-tour) that *streams* a themes/complaints/
    praised-guides/trends summary and caches it in session_state.
  - **Health** — auto-generated alerts panel + per-tour health table (last
    30 days: count, avg, trend vs prior 30d, low-review count, response rate,
    and a 🟢 ≥4.8 / 🟡 4.5–4.7 / 🔴 <4.5 status).
  Filters: platform + tour are global; the star-rating slider scopes the
  Reviews feed only. Both Claude features use `claude-sonnet-4-6` with the key
  from `st.secrets["ANTHROPIC_API_KEY"]`, and degrade gracefully (disabled
  with a hint) when it's absent. Run: `streamlit run dashboard/app.py`.
- 🔲 Remaining scrapers are docstring stubs. Each stub's docstring records
  platform-specific gotchas (bot protection, lazy loading, consent walls).

## Conventions

- Python, async Playwright, pandas. No paid APIs for *review collection*
  (hard constraint) — scrapers must never depend on a paid aggregator. The
  dashboard's optional Claude reply-drafting is the one deliberate paid-API
  exception (user-requested, key-gated, off by default).
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
