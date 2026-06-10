"""
Shared scraping utilities for the Discover Walks review tracker.

Every platform scraper imports from this module:

- polite_delay()          -- random sleep between requests (anti-bot pacing)
- retry()                 -- run an async operation with exponential backoff
- load_existing_reviews() -- data/reviews.csv as a DataFrame
- existing_keys()         -- set of dedup keys for fast membership checks
- is_duplicate()          -- check one review against the key set
- save_review()           -- append a review to reviews.csv if it's new

A review is identified by (platform, tour_name, reviewer_name, review_date),
lowercased and stripped. Review text is deliberately excluded from the key:
platforms often truncate or reflow the same review between visits.
"""

import asyncio
import csv
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REVIEWS_FILE = DATA_DIR / "reviews.csv"

FIELDNAMES = [
    "platform",
    "tour_name",
    "rating",
    "reviewer_name",
    "review_text",
    "review_date",
    "url",
    "scraped_at",
]

MIN_DELAY_S = 1.5
MAX_DELAY_S = 4.5


# ---------------------------------------------------------------------------
# Anti-bot pacing + retries
# ---------------------------------------------------------------------------

async def polite_delay(min_s: float = MIN_DELAY_S, max_s: float = MAX_DELAY_S) -> None:
    """Sleep a random interval so request timing doesn't look scripted."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def retry(coro_factory, attempts: int = 3, base_delay: float = 2.0):
    """Run an async operation, retrying with exponential backoff + jitter.

    ``coro_factory`` is a zero-arg callable returning a *fresh* coroutine each
    time, e.g. ``await retry(lambda: page.goto(url))``. Raises the last
    exception if every attempt fails.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                print(f"    Attempt {attempt}/{attempts} failed ({exc}); retrying in {delay:.1f}s")
                await asyncio.sleep(delay)
    raise last_exc


# ---------------------------------------------------------------------------
# CSV + deduplication
# ---------------------------------------------------------------------------

def load_existing_reviews() -> pd.DataFrame:
    """Return reviews.csv as a string-typed DataFrame (empty if missing)."""
    if not REVIEWS_FILE.exists():
        return pd.DataFrame(columns=FIELDNAMES)
    return pd.read_csv(REVIEWS_FILE, dtype=str).fillna("")


def _key(review) -> tuple:
    """Dedup key for a review dict or DataFrame row."""
    return (
        str(review["platform"]).strip().lower(),
        str(review["tour_name"]).strip().lower(),
        str(review["reviewer_name"]).strip().lower(),
        str(review["review_date"]).strip(),
    )


def existing_keys(df: pd.DataFrame = None) -> set:
    """Set of dedup keys for every review already on disk."""
    if df is None:
        df = load_existing_reviews()
    return {_key(row) for _, row in df.iterrows()}


def is_duplicate(review: dict, keys: set) -> bool:
    return _key(review) in keys


def save_review(review: dict, keys: set = None) -> bool:
    """Append one review to reviews.csv if it isn't already there.

    Returns True if the row was written, False if it was a duplicate.

    When saving in a loop, build the key set once with ``existing_keys()``
    and pass it in -- it is updated in place, so the same reviewer appearing
    twice in one scrape is also deduplicated.
    """
    if keys is None:
        keys = existing_keys()
    if is_duplicate(review, keys):
        return False

    row = dict(review)
    row.setdefault(
        "scraped_at",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = REVIEWS_FILE.exists()
    with open(REVIEWS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

    keys.add(_key(row))
    return True
