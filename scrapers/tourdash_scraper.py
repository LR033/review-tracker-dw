"""
TourDash booking pull for Discover Walks.

Pulls confirmed bookings (those with a check-in guide) from the TourDash API
into ``data/bookings.csv``. The dashboard uses this file to attribute each
review to the guide who actually ran the tour.

Unlike the review scrapers (async Playwright web-scraping), this is a plain
REST pull: TourDash is the company's *own* booking system, so calling its
official API is not a paid third-party aggregator — the "no paid APIs for
review collection" rule covers *review* collection, which this is not.

API
---
    GET https://tourdash.app/api/v1/bookings
        ?from=YYYY-MM-DD&to=YYYY-MM-DD&page=N&page_size=200
    Authorization: Bearer $TOURDASH_API_KEY

- Rate limit: 20 requests / 60s. We pace at ~1 request / 3.1s, well under it,
  and additionally honour a 429 ``Retry-After`` if one is ever returned.
- Pagination: ``response["pagination"]["total_pages"]``.
- Booking fields used: id, tour.name, tour.start_time, checked_in_by,
  platform, booked, attended, status.

Output
------
``data/bookings.csv`` — rewritten in full each run (we always re-pull the whole
range, so an overwrite keeps attended/status counts current and avoids stale
rows). Written atomically via a temp file. Columns:

    booking_id, tour_name, tour_date, guide, platform,
    booked_adults, attended_adults, status

Only **confirmed** bookings whose **checked_in_by** is set are kept — those are
the bookings we can attribute to a guide.

Run
---
    TOURDASH_API_KEY=... python scrapers/tourdash_scraper.py
"""

import csv
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests

BASE_URL = "https://tourdash.app"
BOOKINGS_ENDPOINT = "/api/v1/bookings"

START_DATE = "2025-01-01"          # pull everything from here to today
PAGE_SIZE = 200

# Rate limit is 20 req / 60s. Pace at ~1 req / 3.1s to stay safely under it.
MIN_REQUEST_INTERVAL_S = 3.1
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BOOKINGS_FILE = DATA_DIR / "bookings.csv"

FIELDNAMES = [
    "booking_id",
    "tour_name",
    "tour_date",
    "guide",
    "platform",
    "booked_adults",
    "attended_adults",
    "status",
]

# Keys a paginated list might live under, in order of preference.
_LIST_KEYS = ("bookings", "data", "results", "items")


def _api_key() -> str:
    key = os.environ.get("TOURDASH_API_KEY", "").strip()
    if not key:
        print("ERROR: TOURDASH_API_KEY is not set in the environment.", file=sys.stderr)
        sys.exit(1)
    return key


def _get(d: dict, path: str, default=None):
    """Nested dict getter: ``_get(booking, "tour.name")``.

    Returns ``default`` for any missing or non-dict step, or a None value.
    """
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def transform(booking: dict) -> Optional[dict]:
    """Map one raw API booking to a CSV row, or ``None`` to skip it.

    Skips bookings that are not confirmed or have no check-in guide
    (``checked_in_by``) — those can't be attributed to a guide.
    """
    guide = _get(booking, "checked_in_by")
    if guide is None or not str(guide).strip():
        return None

    status = str(_get(booking, "status", "")).strip()
    if status.lower() != "confirmed":
        return None

    # tour.start_time is an ISO datetime; keep the calendar date for matching.
    start = str(_get(booking, "tour.start_time", ""))
    tour_date = start[:10]

    return {
        "booking_id": _get(booking, "id", ""),
        "tour_name": _get(booking, "tour.name", ""),
        "tour_date": tour_date,
        "guide": str(guide).strip(),
        "platform": _get(booking, "platform", ""),
        "booked_adults": _get(booking, "booked", ""),
        "attended_adults": _get(booking, "attended", ""),
        "status": status,
    }


def _extract_list(payload: dict) -> list:
    """Pull the bookings array out of a response payload, tolerating key names."""
    for k in _LIST_KEYS:
        val = payload.get(k)
        if isinstance(val, list):
            return val
    return []


def _get_page(session: requests.Session, key: str, from_date: str,
              to_date: str, page: int) -> dict:
    """Fetch one page, retrying transient errors with exponential backoff."""
    params = {"from": from_date, "to": to_date, "page": page, "page_size": PAGE_SIZE}
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                BASE_URL + BOOKINGS_ENDPOINT,
                params=params, headers=headers, timeout=REQUEST_TIMEOUT_S,
            )
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "60") or 60)
                print(f"    429 rate-limited on page {page}; sleeping {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — log + retry any transient failure
            last_exc = exc
            if attempt < MAX_RETRIES:
                delay = 2.0 * (2 ** (attempt - 1))
                print(f"    page {page} attempt {attempt}/{MAX_RETRIES} failed "
                      f"({exc}); retrying in {delay:.0f}s")
                time.sleep(delay)
    raise last_exc


def fetch_all_bookings(key: str, from_date: str, to_date: str) -> list:
    """Page through the whole range, returning kept (confirmed+guided) rows."""
    session = requests.Session()
    rows = []
    page = 1
    total_pages = 1
    last_request = 0.0

    while page <= total_pages:
        # Pace requests to respect the 20-req/60s limit.
        elapsed = time.monotonic() - last_request
        if elapsed < MIN_REQUEST_INTERVAL_S:
            time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
        last_request = time.monotonic()

        payload = _get_page(session, key, from_date, to_date, page)
        total_pages = int(_get(payload, "pagination.total_pages", 1) or 1)

        batch = _extract_list(payload)
        kept = 0
        for booking in batch:
            row = transform(booking)
            if row is not None:
                rows.append(row)
                kept += 1
        print(f"  page {page}/{total_pages}: {len(batch)} bookings, {kept} kept")
        page += 1

    return rows


def write_bookings(rows: list) -> None:
    """Write rows to bookings.csv atomically (temp file + replace)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BOOKINGS_FILE.with_name(BOOKINGS_FILE.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})
    tmp.replace(BOOKINGS_FILE)


def main() -> None:
    key = _api_key()
    to_date = date.today().isoformat()
    print(f"TourDash: pulling bookings {START_DATE} → {to_date}")
    rows = fetch_all_bookings(key, START_DATE, to_date)
    write_bookings(rows)
    print(f"Wrote {len(rows)} confirmed+guided bookings to {BOOKINGS_FILE}")


if __name__ == "__main__":
    main()
