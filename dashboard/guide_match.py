"""
Guide attribution: match scraped reviews to the guide who ran the tour.

Booking data comes from TourDash (``data/bookings.csv``, produced by
``scrapers/tourdash_scraper.py``); each confirmed booking carries the guide who
checked the guests in plus the tour name and date. Reviews come from the public
platforms and use *their own* tour names and a review date that is only roughly
the visit date. So matching is fuzzy on two axes:

- **Tour name** — normalised (lower-cased, punctuation stripped, whitespace
  collapsed) then compared with :func:`difflib.SequenceMatcher` ratio. A review
  is attributed only if the best candidate clears ``MATCH_THRESHOLD``.
- **Date** — a review is matched against bookings within ``DATE_WINDOW`` days of
  the review date, to absorb the slack between a tour and its review.

This module is deliberately free of Streamlit/pandas-display concerns and side
effects so it can be unit-tested directly. ``attach_guides`` takes the reviews
and bookings DataFrames and returns a copy of the reviews with a ``guide``
column (``None`` where no confident match is found).
"""

import re
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

import pandas as pd

MATCH_THRESHOLD = 0.6   # min SequenceMatcher ratio on normalised tour names
DATE_WINDOW = 1         # ± days around the review date to search bookings

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_tour(name) -> str:
    """Lower-case, strip punctuation, collapse whitespace for fuzzy comparison."""
    return " ".join(_NON_ALNUM.sub(" ", str(name).lower()).split())


def build_guide_index(bookings: pd.DataFrame):
    """Build a ``date -> [(tour_norm, guide), ...]`` lookup from bookings.

    Expects columns ``tour_name``, ``tour_date`` (datetime64), ``guide``. When a
    (tour, date) slot has several guides (multiple departures), the guide who
    checked in the most bookings that day wins — a single, stable attribution.
    """
    by_date = defaultdict(list)
    if bookings is None or bookings.empty:
        return by_date

    work = bookings.copy()
    work["_tour_norm"] = work["tour_name"].map(normalize_tour)
    work["_date"] = pd.to_datetime(work["tour_date"], errors="coerce").dt.date
    work = work.dropna(subset=["_date"])
    work = work[work["guide"].astype(str).str.strip() != ""]

    # Count bookings per (date, tour_norm, guide), then keep the top guide per slot.
    counts = work.groupby(["_date", "_tour_norm", "guide"]).size()
    best = {}  # (date, tour_norm) -> (guide, count)
    for (d, tour_norm, guide), cnt in counts.items():
        key = (d, tour_norm)
        if key not in best or cnt > best[key][1]:
            best[key] = (guide, cnt)

    for (d, tour_norm), (guide, _cnt) in best.items():
        by_date[d].append((tour_norm, guide))
    return by_date


def match_one(tour_norm: str, review_date, by_date,
              threshold: float = MATCH_THRESHOLD, window: int = DATE_WINDOW):
    """Return the best-matching guide for one review, or ``None``.

    ``review_date`` is a ``datetime.date``; ``by_date`` is the index from
    :func:`build_guide_index`.
    """
    if review_date is None or pd.isna(review_date):
        return None

    best_guide, best_ratio = None, 0.0
    for delta in range(-window, window + 1):
        for cand_norm, guide in by_date.get(review_date + timedelta(days=delta), ()):
            ratio = SequenceMatcher(None, tour_norm, cand_norm).ratio()
            if ratio > best_ratio:
                best_ratio, best_guide = ratio, guide
    return best_guide if best_ratio >= threshold else None


def attach_guides(reviews: pd.DataFrame, bookings: pd.DataFrame,
                  date_col: str = "review_date",
                  threshold: float = MATCH_THRESHOLD,
                  window: int = DATE_WINDOW) -> pd.DataFrame:
    """Return a copy of ``reviews`` with a ``guide`` column (``None`` if unmatched).

    ``date_col`` is the reviews column holding the (datetime) review date used
    for the ± window match.
    """
    out = reviews.copy()
    out["guide"] = None
    if bookings is None or bookings.empty or out.empty:
        return out

    by_date = build_guide_index(bookings)
    if not by_date:
        return out

    rev_norm = out["tour_name"].map(normalize_tour)
    rev_dates = pd.to_datetime(out[date_col], errors="coerce").dt.date

    cache = {}  # (tour_norm, date) -> guide, since reviews repeat tour/date a lot
    guides = []
    for tour_norm, rdate in zip(rev_norm, rev_dates):
        key = (tour_norm, rdate)
        if key not in cache:
            cache[key] = match_one(tour_norm, rdate, by_date, threshold, window)
        guides.append(cache[key])

    out["guide"] = guides
    return out
