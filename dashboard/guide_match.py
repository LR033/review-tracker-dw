"""
Guide attribution: match scraped reviews to the guide who ran the tour.

Booking data comes from TourDash (``data/bookings.csv``, produced by
``scrapers/tourdash_scraper.py``); each confirmed booking carries the guide who
checked the guests in, the tour, and the date. Reviews come from the public
platforms.

The two sides name tours completely differently: TourDash uses short **codes**
(``LB``, ``MM``, ``TM``, ``HG``, ``HIP``, ``ND``) while the review platforms use
full marketing titles ("Le Marais Free Tour: Where Parisians Go"). Fuzzy string
matching can't bridge that, so we map **both** sides to a shared canonical key
(the code) and then match exactly on **key + date (± a small day window)**:

1. Booking ``tour_name`` is already the code → canonical key directly.
2. Review ``tour_name`` → canonical key via keyword matching on the title.
3. A review matches a booking when their canonical keys are equal and the
   review date is within ``DATE_WINDOW`` days of the tour date (exact date
   preferred). When a (key, date) slot had several guides, the one who checked
   in the most guests that day wins.

This module is deliberately free of Streamlit so it can be unit-tested directly.
``attach_guides`` takes the reviews and bookings DataFrames and returns a copy
of the reviews with a ``guide`` column (``None`` where no match is found).
"""

import re
from datetime import timedelta
from typing import Optional

import pandas as pd

DATE_WINDOW = 1  # ± days around the review date to search bookings

# Canonical tour keys are the TourDash codes. Bookings already store the code as
# their tour_name; reviews are mapped onto these via keywords below.
BOOKING_CODES = {"LB", "MM", "TM", "HG", "HIP", "ND"}

# Human-readable names for the codes (for reference / labels).
CODE_TO_NAME = {
    "LB": "Left Bank",
    "MM": "Montmartre",
    "TM": "Le Marais",
    "HG": "Places Parisians Love",
    "HIP": "Paris Icons Express",
    "ND": "Notre-Dame (inactive)",
}

# Keyword → canonical key for review titles. Matched against a normalised title
# (lower-cased, punctuation→space), first hit wins. Keywords are written in that
# normalised form (e.g. "notre dame", not "Notre-Dame"). Reviews never map to ND
# (the inactive Notre-Dame code) — Notre-Dame/Louvre titles are the current
# "Paris Icons Express" (HIP) tour.
REVIEW_KEYWORDS = (
    ("marais", "TM"),
    ("montmartre", "MM"),
    ("left bank", "LB"),
    ("parisians love", "HG"),
    ("hidden gems", "HG"),
    ("icons", "HIP"),
    ("notre dame", "HIP"),
    ("louvre", "HIP"),
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_tour(name) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    return " ".join(_NON_ALNUM.sub(" ", str(name).lower()).split())


def booking_key(tour_name) -> Optional[str]:
    """Canonical key for a TourDash booking tour_name (the code itself)."""
    code = str(tour_name).strip().upper()
    return code if code in BOOKING_CODES else None


def review_key(tour_name) -> Optional[str]:
    """Canonical key for a review's marketing title, via keyword match."""
    norm = normalize_tour(tour_name)
    for keyword, key in REVIEW_KEYWORDS:
        if keyword in norm:
            return key
    return None


def build_guide_index(bookings: pd.DataFrame) -> dict:
    """Build a ``(canonical_key, date) -> guide`` lookup from bookings.

    Expects columns ``tour_name`` (code), ``tour_date`` (datetime/parseable),
    ``guide``. When a slot has several guides (multiple departures), the guide
    who checked in the most guests that day wins — a single, stable attribution.
    """
    index: dict = {}
    if bookings is None or bookings.empty:
        return index

    work = bookings.copy()
    work["_key"] = work["tour_name"].map(booking_key)
    work["_date"] = pd.to_datetime(work["tour_date"], errors="coerce").dt.date
    work = work.dropna(subset=["_date"])
    work = work[work["_key"].notna()]
    work = work[work["guide"].astype(str).str.strip() != ""]

    # Count bookings per (date, key, guide); keep the top guide per (key, date).
    counts = work.groupby(["_date", "_key", "guide"]).size()
    best: dict = {}  # (key, date) -> (guide, count)
    for (d, key, guide), cnt in counts.items():
        slot = (key, d)
        if slot not in best or cnt > best[slot][1]:
            best[slot] = (guide, cnt)
    return {slot: guide for slot, (guide, _cnt) in best.items()}


def _deltas(window: int):
    """Day offsets to try, exact date first then outward: 0, -1, +1, -2, +2…"""
    yield 0
    for off in range(1, window + 1):
        yield -off
        yield off


def match_one(key: Optional[str], review_date, index: dict,
              window: int = DATE_WINDOW) -> Optional[str]:
    """Return the guide for one review's (key, date), or ``None``.

    Tries the exact tour date first, then expands outward within ``window``.
    """
    if key is None or review_date is None or pd.isna(review_date):
        return None
    for delta in _deltas(window):
        guide = index.get((key, review_date + timedelta(days=delta)))
        if guide is not None:
            return guide
    return None


def attach_guides(reviews: pd.DataFrame, bookings: pd.DataFrame,
                  date_col: str = "review_date",
                  window: int = DATE_WINDOW) -> pd.DataFrame:
    """Return a copy of ``reviews`` with a ``guide`` column (``None`` if unmatched).

    ``date_col`` is the reviews column holding the (datetime) review date used
    for the ± window match.
    """
    out = reviews.copy()
    out["guide"] = None
    if bookings is None or bookings.empty or out.empty:
        return out

    index = build_guide_index(bookings)
    if not index:
        return out

    rev_keys = out["tour_name"].map(review_key)
    rev_dates = pd.to_datetime(out[date_col], errors="coerce").dt.date

    cache: dict = {}  # (key, date) -> guide, since reviews repeat tour/date a lot
    guides = []
    for key, rdate in zip(rev_keys, rev_dates):
        slot = (key, rdate)
        if slot not in cache:
            cache[slot] = match_one(key, rdate, index, window)
        guides.append(cache[slot])

    out["guide"] = guides
    return out
