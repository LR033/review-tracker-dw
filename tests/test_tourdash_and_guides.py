"""
Tests for the TourDash booking pull and the guide-matching logic.

Pure-logic tests only (no network, no Streamlit): they exercise
``tourdash_scraper.transform`` and the ``guide_match`` module.

Run directly (no pytest needed):

    .venv/bin/python tests/test_tourdash_and_guides.py

or under pytest if installed:

    pytest tests/
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))
sys.path.insert(0, str(ROOT / "dashboard"))

import tourdash_scraper as td  # noqa: E402
import guide_match as gm  # noqa: E402


# ---------------------------------------------------------------------------
# tourdash_scraper.transform
# ---------------------------------------------------------------------------

def _booking(**over):
    base = {
        "id": "B1",
        "tour": {"name": "Le Marais Free Tour", "start_time": "2026-06-10T10:00:00+00:00"},
        "checked_in_by": "Marie Dubois",
        "platform": "freetour",
        "booked": {"adults": 8, "children": 1, "infants": 0},
        "attended": {"adults": 6, "children": 1, "infants": 0},
        "status": "new",
    }
    base.update(over)
    return base


def test_transform_keeps_guided_noncancelled():
    row = td.transform(_booking())
    assert row is not None
    assert row["booking_id"] == "B1"
    assert row["tour_name"] == "Le Marais Free Tour"
    assert row["tour_date"] == "2026-06-10"          # ISO datetime truncated to date
    assert row["guide"] == "Marie Dubois"
    assert row["platform"] == "freetour"
    assert row["booked_adults"] == 8                 # pulled from booked.adults
    assert row["attended_adults"] == 6               # pulled from attended.adults
    assert row["status"] == "new"
    # "modified" is also kept (it just means the booking was edited).
    assert td.transform(_booking(status="modified")) is not None


def test_transform_drops_cancelled():
    assert td.transform(_booking(status="cancelled")) is None
    assert td.transform(_booking(status="CANCELLED")) is None


def test_transform_drops_missing_guide():
    assert td.transform(_booking(checked_in_by=None)) is None
    assert td.transform(_booking(checked_in_by="")) is None
    assert td.transform(_booking(checked_in_by="   ")) is None


def test_transform_handles_missing_nested_fields():
    # No tour/booked/attended objects → empty fields, but still kept if guided.
    row = td.transform({"id": "B2", "checked_in_by": "Paul", "status": "new"})
    assert row is not None
    assert row["tour_name"] == ""
    assert row["tour_date"] == ""
    assert row["booked_adults"] == ""
    assert row["attended_adults"] == ""
    assert row["guide"] == "Paul"


def test_month_chunks_cover_range_with_from_before_to():
    chunks = list(td._month_chunks(date(2025, 1, 1), date(2026, 6, 24)))
    assert len(chunks) == 18                          # Jan 2025 .. Jun 2026 inclusive
    assert chunks[0] == ("2025-01-01", "2025-01-31")
    assert chunks[-1] == ("2026-06-01", "2026-06-24")  # current month capped at "to"
    for frm, to in chunks:                            # API requires from < to
        assert frm < to


def test_extract_list_tolerates_key_names():
    assert td._extract_list({"bookings": [1, 2]}) == [1, 2]
    assert td._extract_list({"data": [3]}) == [3]
    assert td._extract_list({"results": [4]}) == [4]
    assert td._extract_list({"nothing": 1}) == []


# ---------------------------------------------------------------------------
# guide_match
# ---------------------------------------------------------------------------

def _bookings_df():
    # Bookings use TourDash codes as tour_name (TM = Le Marais, MM = Montmartre).
    return pd.DataFrame([
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Marie"},
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Marie"},
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Jacques"},
        {"tour_name": "MM", "tour_date": "2026-06-12", "guide": "Sophie"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))


def test_normalize_tour():
    assert gm.normalize_tour("Le Marais — Free Tour!") == "le marais free tour"
    assert gm.normalize_tour("  Montmartre   Walk  ") == "montmartre walk"


def test_booking_key():
    assert gm.booking_key("TM") == "TM"
    assert gm.booking_key(" lb ") == "LB"            # trimmed + upper-cased
    assert gm.booking_key("HIP") == "HIP"
    assert gm.booking_key("ZZ") is None              # unknown code
    assert gm.booking_key("") is None


def test_review_key_real_titles():
    cases = {
        "Le Marais Free Tour: Where Parisians Go": "TM",
        "Paris: Marais without crowds. Guided Tour.": "TM",
        "Montmartre Paris Free Tour: Moulin Rouge to Sacre Coeur": "MM",
        "Places Parisians Love: Classic Treasures, Hidden Gems & Locals' Picks": "HG",
        "Paris Icons Express Free Tour: Notre-Dame to Louvre": "HIP",
        "Paris Left Bank: Writers, Revolution & Black Coffee": "LB",
        "Seine River Cruise by Night": None,         # no keyword → no key
    }
    for title, expected in cases.items():
        assert gm.review_key(title) == expected, title


def test_build_guide_index_picks_dominant_guide():
    from datetime import date
    index = gm.build_guide_index(_bookings_df())
    # Marie has 2 check-ins that day vs Jacques' 1 → Marie wins the TM slot.
    assert index[("TM", date(2026, 6, 10))] == "Marie"
    assert index[("MM", date(2026, 6, 12))] == "Sophie"


def test_attach_guides_matches_within_window():
    reviews = pd.DataFrame([
        # Marais title, review one day after the tour → matches TM (±1 day).
        {"tour_name": "Le Marais Free Tour: Where Parisians Go", "review_date": "2026-06-11"},
        # Montmartre title, exact date → matches Sophie (MM).
        {"tour_name": "Montmartre Paris Free Tour: Moulin Rouge to Sacre Coeur",
         "review_date": "2026-06-12"},
        # Right tour, but 5 days off → outside the ±1 window → no match.
        {"tour_name": "Paris: Marais without crowds", "review_date": "2026-06-20"},
        # No keyword in the title → no key → no match.
        {"tour_name": "Seine River Cruise", "review_date": "2026-06-10"},
    ]).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))

    out = gm.attach_guides(reviews, _bookings_df(), date_col="review_date")
    guides = list(out["guide"])
    assert guides[0] == "Marie"
    assert guides[1] == "Sophie"
    assert guides[2] is None
    assert guides[3] is None


def test_attach_guides_prefers_exact_date():
    # Two TM departures with different guides on adjacent days; the review is on
    # the 10th, so the exact-date guide (Marie) must win over the ±1 neighbour.
    bookings = pd.DataFrame([
        {"tour_name": "TM", "tour_date": "2026-06-09", "guide": "Jacques"},
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Marie"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))
    reviews = pd.DataFrame([
        {"tour_name": "Le Marais Free Tour: Where Parisians Go", "review_date": "2026-06-10"},
    ]).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))
    out = gm.attach_guides(reviews, bookings, date_col="review_date")
    assert out["guide"].iloc[0] == "Marie"


def test_attach_guides_empty_bookings():
    reviews = pd.DataFrame([
        {"tour_name": "Le Marais Free Tour: Where Parisians Go", "review_date": "2026-06-11"},
    ]).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))
    out = gm.attach_guides(reviews, pd.DataFrame(), date_col="review_date")
    assert "guide" in out.columns
    assert out["guide"].iloc[0] is None


# ---------------------------------------------------------------------------
# Minimal runner (so it works without pytest)
# ---------------------------------------------------------------------------

def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
