# review-tracker-dw

Review aggregation dashboard for **Discover Walks** (Paris tours). Collects
reviews from 6 platforms into a single CSV and visualizes them in Streamlit.

## Platform status

| Platform     | Scraper                          | Status          |
| ------------ | -------------------------------- | --------------- |
| Freetour.com | `scrapers/freetour_scraper.py`   | ✅ Implemented  |
| GuruWalk     | `scrapers/guruwalk_scraper.py`   | ✅ Implemented  |
| TripAdvisor  | `scrapers/tripadvisor_scraper.py`| 🔲 Stub         |
| GetYourGuide | `scrapers/getyourguide_scraper.py`| 🔲 Stub        |
| Viator       | `scrapers/viator_scraper.py`     | 🔲 Stub         |
| Google       | `scrapers/google_scraper.py`     | 🔲 Stub         |

Dashboard (`dashboard/app.py`) is also a stub.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Usage

Run a scraper standalone (from the repo root):

```bash
python scrapers/freetour_scraper.py
```

Run the dashboard (once implemented):

```bash
streamlit run dashboard/app.py
```

## Data

All reviews land in `data/reviews.csv`:

```
platform, tour_name, rating, reviewer_name, review_text, review_date, url, scraped_at
```

Deduplication: a review is identified by
`(platform, tour_name, reviewer_name, review_date)` — re-running a scraper
only appends reviews not already on file (`base_scraper.save_review`).

The file ships with a few sample rows so the dashboard has something to
render before the first scrape.

## Automation

`.github/workflows/scrape.yml` runs the implemented scrapers daily at
10:00 Paris time and commits new reviews. Add scrapers to the workflow's
"Run scrapers" step as they're implemented.

## Constraints

- No paid APIs — everything is scraped with Playwright (async).
- Scrapers pace themselves (`polite_delay`) and retry with backoff (`retry`)
  to stay polite and survive transient failures.
