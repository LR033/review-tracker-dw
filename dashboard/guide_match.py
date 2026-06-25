"""
Guide attribution: match scraped reviews to the guide who ran the tour.

Booking data comes from TourDash (``data/bookings.csv``, produced by
``scrapers/tourdash_scraper.py``); each confirmed booking carries the guide who
checked the guests in, the lead customer (``contact_name``), the tour, and the
date. Reviews come from the public platforms.

Matching is two-tier, preferring the precise signal:

1. **Name match (primary).** Normalise the review's ``reviewer_name`` and the
   booking's ``contact_name`` (accent-folded, lower-cased, punctuation
   stripped) and fuzzy-compare them with rapidfuzz ``token_set_ratio`` (0–100,
   token-aware so "Sarah M." still matches "Sarah Mitchell"). To avoid
   cross-tour collisions of common names, candidates are restricted to bookings
   on the *same canonical tour* (see the keyword map below); ties on the match
   score are broken by date proximity. A match at or above ``NAME_THRESHOLD``
   attributes that booking's guide (``match_method="name"``).

2. **Date fallback (secondary).** Only used when the name match fails. Look at
   bookings for the review's tour within ``DATE_WINDOW`` days of the review
   date. Attribute the guide **only if exactly one** guide ran that tour in that
   window (``match_method="date_unambiguous"``). If two or more guides did, the
   attribution is ambiguous and we record ``None`` rather than guess — this is
   the case the old date-only matching got wrong.

Tours are named differently on each side: TourDash uses short codes
(``LB``/``MM``/``TM``/``HG``/``HIP``/``ND``); reviews use full marketing titles
mapped onto those codes by keyword. The code is the canonical key.

This module is deliberately free of Streamlit so it can be unit-tested directly.
``attach_guides`` returns a copy of the reviews with ``guide`` and
``match_method`` columns (both ``None`` where no confident match is found).
"""

import re
import unicodedata
from collections import defaultdict
from datetime import timedelta
from typing import Optional

import pandas as pd
from rapidfuzz import fuzz

DATE_WINDOW = 1       # ± days around the review date for the date fallback
# Min rapidfuzz token_set_ratio (0–100) for a reviewer↔contact name match.
# token_set_ratio is token-aware, so abbreviated names ("Sarah M." vs
# "Sarah Mitchell") and reordered names still score well.
NAME_THRESHOLD = 75

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


def normalize_name(name) -> str:
    """Accent-fold, lower-case, strip punctuation, collapse whitespace.

    Accent folding ("Léo" → "leo", "Anaëlle" → "anaelle") lets platform
    reviewer names match booking contact names that differ only by diacritics.
    """
    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_NON_ALNUM.sub(" ", ascii_only.lower()).split())


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


def build_booking_index(bookings: pd.DataFrame):
    """Build the lookup structures used for matching.

    Returns ``(name_index, date_guides)`` where:

    - ``name_index``: ``key -> {contact_norm: [(date, guide), ...]}`` for the
      primary name match (grouped by normalised contact name to avoid redundant
      fuzzy comparisons).
    - ``date_guides``: ``(key, date) -> {guide, ...}`` for the date fallback's
      unambiguity check.
    """
    name_index: dict = defaultdict(lambda: defaultdict(list))
    date_guides: dict = defaultdict(set)
    if bookings is None or bookings.empty:
        return name_index, date_guides

    work = bookings.copy()
    work["_key"] = work["tour_name"].map(booking_key)
    work["_date"] = pd.to_datetime(work["tour_date"], errors="coerce").dt.date
    work = work.dropna(subset=["_date"])
    work = work[work["_key"].notna()]
    work = work[work["guide"].astype(str).str.strip() != ""]

    contacts = (work["contact_name"] if "contact_name" in work.columns
                else pd.Series([""] * len(work), index=work.index))

    for key, d, guide, contact in zip(work["_key"], work["_date"], work["guide"], contacts):
        date_guides[(key, d)].add(guide)
        cn = normalize_name(contact)
        if cn:
            name_index[key][cn].append((d, guide))
    return name_index, date_guides


def _name_match(key, reviewer_name, review_date, name_index, threshold):
    """Best name-matched guide for one review, or ``None``.

    Restricted to bookings on the same tour ``key``; ties broken by date
    proximity to ``review_date``.
    """
    if key is None or not reviewer_name:
        return None
    rn = normalize_name(reviewer_name)
    if not rn:
        return None

    cn_map = name_index.get(key)
    if not cn_map:
        return None

    best_ratio, best_contacts = 0.0, []
    for cn in cn_map:
        ratio = fuzz.token_set_ratio(rn, cn)
        if ratio > best_ratio:
            best_ratio, best_contacts = ratio, [cn]
        elif ratio == best_ratio:
            best_contacts.append(cn)

    if best_ratio < threshold:
        return None

    # Candidate (date, guide) entries among the best-scoring contact names.
    entries = [e for cn in best_contacts for e in cn_map[cn]]
    if review_date is not None and not pd.isna(review_date):
        entries.sort(key=lambda e: abs((e[0] - review_date).days))
    return entries[0][1]


def _date_fallback(key, review_date, date_guides, window):
    """Guide for one review via the unambiguous date fallback, or ``None``.

    Attributes only when exactly one guide ran the tour within ``window`` days.
    """
    if key is None or review_date is None or pd.isna(review_date):
        return None
    guides = set()
    for delta in range(-window, window + 1):
        guides |= date_guides.get((key, review_date + timedelta(days=delta)), set())
    return next(iter(guides)) if len(guides) == 1 else None


def match_review(key, reviewer_name, review_date, name_index, date_guides,
                 window: int = DATE_WINDOW, threshold: float = NAME_THRESHOLD):
    """Return ``(guide, match_method)`` for one review.

    ``match_method`` is ``"name"``, ``"date_unambiguous"``, or ``None``.
    """
    guide = _name_match(key, reviewer_name, review_date, name_index, threshold)
    if guide is not None:
        return guide, "name"

    guide = _date_fallback(key, review_date, date_guides, window)
    if guide is not None:
        return guide, "date_unambiguous"

    return None, None


def attach_guides(reviews: pd.DataFrame, bookings: pd.DataFrame,
                  date_col: str = "review_date",
                  window: int = DATE_WINDOW,
                  name_threshold: float = NAME_THRESHOLD) -> pd.DataFrame:
    """Return a copy of ``reviews`` with ``guide`` and ``match_method`` columns.

    Both are ``None`` where no confident match is found. ``date_col`` is the
    reviews column holding the (datetime) review date.
    """
    out = reviews.copy()
    out["guide"] = None
    out["match_method"] = None
    if bookings is None or bookings.empty or out.empty:
        return out

    name_index, date_guides = build_booking_index(bookings)
    if not name_index and not date_guides:
        return out

    rev_keys = out["tour_name"].map(review_key)
    rev_dates = pd.to_datetime(out[date_col], errors="coerce").dt.date
    rev_names = (out["reviewer_name"] if "reviewer_name" in out.columns
                 else pd.Series([""] * len(out), index=out.index))

    cache: dict = {}  # (key, date, name) -> (guide, method)
    guides, methods = [], []
    for key, rdate, rname in zip(rev_keys, rev_dates, rev_names):
        slot = (key, rdate, str(rname))
        if slot not in cache:
            cache[slot] = match_review(
                key, rname, rdate, name_index, date_guides, window, name_threshold
            )
        guide, method = cache[slot]
        guides.append(guide)
        methods.append(method)

    out["guide"] = guides
    out["match_method"] = methods
    return out
