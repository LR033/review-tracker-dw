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
        "booked": 8,
        "attended": 6,
        "status": "confirmed",
    }
    base.update(over)
    return base


def test_transform_keeps_confirmed_guided():
    row = td.transform(_booking())
    assert row is not None
    assert row["booking_id"] == "B1"
    assert row["tour_name"] == "Le Marais Free Tour"
    assert row["tour_date"] == "2026-06-10"          # ISO datetime truncated to date
    assert row["guide"] == "Marie Dubois"
    assert row["platform"] == "freetour"
    assert row["booked_adults"] == 8
    assert row["attended_adults"] == 6
    assert row["status"] == "confirmed"


def test_transform_drops_unconfirmed():
    assert td.transform(_booking(status="cancelled")) is None
    assert td.transform(_booking(status="pending")) is None


def test_transform_drops_missing_guide():
    assert td.transform(_booking(checked_in_by=None)) is None
    assert td.transform(_booking(checked_in_by="")) is None
    assert td.transform(_booking(checked_in_by="   ")) is None


def test_transform_handles_missing_nested_fields():
    # No tour object at all → empty name/date, but still kept if confirmed+guided.
    row = td.transform({"id": "B2", "checked_in_by": "Paul", "status": "confirmed"})
    assert row is not None
    assert row["tour_name"] == ""
    assert row["tour_date"] == ""
    assert row["guide"] == "Paul"


def test_extract_list_tolerates_key_names():
    assert td._extract_list({"bookings": [1, 2]}) == [1, 2]
    assert td._extract_list({"data": [3]}) == [3]
    assert td._extract_list({"results": [4]}) == [4]
    assert td._extract_list({"nothing": 1}) == []


# ---------------------------------------------------------------------------
# guide_match
# ---------------------------------------------------------------------------

def _bookings_df():
    return pd.DataFrame([
        {"tour_name": "Le Marais Free Tour", "tour_date": "2026-06-10", "guide": "Marie"},
        {"tour_name": "Le Marais Free Tour", "tour_date": "2026-06-10", "guide": "Marie"},
        {"tour_name": "Le Marais Free Tour", "tour_date": "2026-06-10", "guide": "Jacques"},
        {"tour_name": "Montmartre Walk", "tour_date": "2026-06-12", "guide": "Sophie"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))


def test_normalize_tour():
    assert gm.normalize_tour("Le Marais — Free Tour!") == "le marais free tour"
    assert gm.normalize_tour("  Montmartre   Walk  ") == "montmartre walk"


def test_build_guide_index_picks_dominant_guide():
    by_date = gm.build_guide_index(_bookings_df())
    from datetime import date
    slot = by_date[date(2026, 6, 10)]
    # Marie has 2 check-ins that day vs Jacques' 1 → Marie wins the slot.
    marais = [g for (tn, g) in slot if tn == "le marais free tour"]
    assert marais == ["Marie"]


def test_attach_guides_matches_within_window():
    reviews = pd.DataFrame([
        # Same tour name, review one day after the tour → matches (±1 day).
        {"tour_name": "Le Marais Free Tour", "review_date": "2026-06-11"},
        # Different tour, exact date → matches Sophie.
        {"tour_name": "Montmartre Walk", "review_date": "2026-06-12"},
        # Right tour, but 5 days off → outside the ±1 window → no match.
        {"tour_name": "Le Marais Free Tour", "review_date": "2026-06-20"},
        # Unrelated tour name → no match.
        {"tour_name": "Seine River Cruise", "review_date": "2026-06-10"},
    ]).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))

    out = gm.attach_guides(reviews, _bookings_df(), date_col="review_date")
    guides = list(out["guide"])
    assert guides[0] == "Marie"
    assert guides[1] == "Sophie"
    assert guides[2] is None
    assert guides[3] is None


def test_attach_guides_fuzzy_name():
    # Platform uses a slightly different name; should still match the booking.
    reviews = pd.DataFrame([
        {"tour_name": "Le Marais Free Walking Tour", "review_date": "2026-06-10"},
    ]).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))
    out = gm.attach_guides(reviews, _bookings_df(), date_col="review_date")
    assert out["guide"].iloc[0] == "Marie"


def test_attach_guides_empty_bookings():
    reviews = pd.DataFrame([
        {"tour_name": "Le Marais Free Tour", "review_date": "2026-06-11"},
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
