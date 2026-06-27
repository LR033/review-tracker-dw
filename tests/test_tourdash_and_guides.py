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
    # Bookings use TourDash codes as tour_name (TM = Le Marais, MM = Montmartre,
    # LB = Left Bank), now with a contact_name (the lead customer).
    # TM 2026-06-10 has TWO guides → ambiguous for the date fallback.
    # MM 2026-06-12 has ONE guide; LB 2026-06-15 has one guide across two rows.
    return pd.DataFrame([
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Marie",
         "contact_name": "Loretta Smith"},
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Jacques",
         "contact_name": "John Doe"},
        {"tour_name": "MM", "tour_date": "2026-06-12", "guide": "Sophie",
         "contact_name": "Emma Brown"},
        {"tour_name": "LB", "tour_date": "2026-06-15", "guide": "Pierre",
         "contact_name": "Carlos Ruiz"},
        {"tour_name": "LB", "tour_date": "2026-06-15", "guide": "Pierre",
         "contact_name": "Maria Ruiz"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))


def _reviews(rows):
    return pd.DataFrame(rows).assign(review_date=lambda d: pd.to_datetime(d["review_date"]))


def test_normalize_tour():
    assert gm.normalize_tour("Le Marais — Free Tour!") == "le marais free tour"
    assert gm.normalize_tour("  Montmartre   Walk  ") == "montmartre walk"


def test_normalize_name_folds_accents():
    assert gm.normalize_name("Léo Armingaud") == "leo armingaud"
    assert gm.normalize_name("  Anaëlle  Planckaert! ") == "anaelle planckaert"


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


def test_name_match_is_primary_and_disambiguates():
    # TM on 2026-06-10 has two guides; the reviewer name picks the right one,
    # where a date-only match could not.
    reviews = _reviews([
        {"tour_name": "Le Marais Free Tour: Where Parisians Go",
         "reviewer_name": "Loretta Smith", "review_date": "2026-06-10"},
        {"tour_name": "Le Marais Free Tour: Where Parisians Go",
         "reviewer_name": "John Doe", "review_date": "2026-06-11"},
    ])
    out = gm.attach_guides(reviews, _bookings_df())
    assert list(out["guide"]) == ["Marie", "Jacques"]
    assert list(out["match_method"]) == ["name", "name"]


def test_name_match_folds_accents():
    bookings = pd.DataFrame([
        {"tour_name": "MM", "tour_date": "2026-06-12", "guide": "Sophie",
         "contact_name": "Anaëlle Planckaert"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))
    reviews = _reviews([
        {"tour_name": "Montmartre Paris Free Tour", "reviewer_name": "Anaelle Planckaert",
         "review_date": "2026-06-12"},
    ])
    out = gm.attach_guides(reviews, bookings)
    assert out["guide"].iloc[0] == "Sophie"
    assert out["match_method"].iloc[0] == "name"


def test_name_threshold_is_0_100_scale():
    # rapidfuzz token_set_ratio returns 0–100, so the threshold is 75 (not 0.75).
    assert gm.NAME_THRESHOLD == 75


def test_name_match_handles_abbreviated_last_name():
    # token_set_ratio matches an abbreviated last name ("Sarah M." ↔ "Sarah
    # Mitchell") where difflib's full-string ratio would not — the reason for
    # the rapidfuzz switch. TM that day has two guides, so a "name" result
    # (not None/date_unambiguous) proves the abbreviated name matched.
    bookings = pd.DataFrame([
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Marie",
         "contact_name": "Sarah Mitchell"},
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "Jacques",
         "contact_name": "Other Person"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))
    reviews = _reviews([
        {"tour_name": "Le Marais Free Tour", "reviewer_name": "Sarah M.",
         "review_date": "2026-06-10"},
    ])
    out = gm.attach_guides(reviews, bookings)
    assert out["guide"].iloc[0] == "Marie"
    assert out["match_method"].iloc[0] == "name"


def test_name_match_ties_broken_by_date():
    # Same customer name on two TM dates with different guides; the review date
    # is closest to 2026-06-20, so guide B should win.
    bookings = pd.DataFrame([
        {"tour_name": "TM", "tour_date": "2026-06-10", "guide": "GuideA",
         "contact_name": "Repeat Customer"},
        {"tour_name": "TM", "tour_date": "2026-06-20", "guide": "GuideB",
         "contact_name": "Repeat Customer"},
    ]).assign(tour_date=lambda d: pd.to_datetime(d["tour_date"]))
    reviews = _reviews([
        {"tour_name": "Le Marais Free Tour", "reviewer_name": "Repeat Customer",
         "review_date": "2026-06-19"},
    ])
    out = gm.attach_guides(reviews, bookings)
    assert out["guide"].iloc[0] == "GuideB"
    assert out["match_method"].iloc[0] == "name"


def test_date_fallback_unambiguous():
    # No name match (reviewer unknown), but MM on 2026-06-12 has a single guide.
    reviews = _reviews([
        {"tour_name": "Montmartre Paris Free Tour", "reviewer_name": "Nobody Here",
         "review_date": "2026-06-12"},
        # LB on 2026-06-15 has one distinct guide across two bookings → Pierre.
        {"tour_name": "Paris Left Bank: Writers", "reviewer_name": "Unknown Person",
         "review_date": "2026-06-15"},
    ])
    out = gm.attach_guides(reviews, _bookings_df())
    assert list(out["guide"]) == ["Sophie", "Pierre"]
    assert list(out["match_method"]) == ["date_unambiguous", "date_unambiguous"]


def test_date_fallback_ambiguous_returns_none():
    # No name match and TM on 2026-06-10 has TWO guides → ambiguous → None.
    reviews = _reviews([
        {"tour_name": "Le Marais Free Tour", "reviewer_name": "Totally Unknown",
         "review_date": "2026-06-10"},
    ])
    out = gm.attach_guides(reviews, _bookings_df())
    assert out["guide"].iloc[0] is None
    assert out["match_method"].iloc[0] is None


def test_no_key_no_match():
    # A title with no canonical keyword can't match on name or date.
    reviews = _reviews([
        {"tour_name": "Seine River Cruise", "reviewer_name": "Loretta Smith",
         "review_date": "2026-06-10"},
    ])
    out = gm.attach_guides(reviews, _bookings_df())
    assert out["guide"].iloc[0] is None
    assert out["match_method"].iloc[0] is None


def test_attach_guides_adds_both_columns_and_valid_methods():
    out = gm.attach_guides(
        _reviews([{"tour_name": "Le Marais Free Tour",
                   "reviewer_name": "Loretta Smith", "review_date": "2026-06-10"}]),
        _bookings_df(),
    )
    assert "guide" in out.columns and "match_method" in out.columns
    assert set(out["match_method"].dropna()) <= {"name", "date_unambiguous"}


def test_attach_guides_empty_bookings():
    reviews = _reviews([
        {"tour_name": "Le Marais Free Tour", "reviewer_name": "Loretta Smith",
         "review_date": "2026-06-11"},
    ])
    out = gm.attach_guides(reviews, pd.DataFrame())
    assert "guide" in out.columns and "match_method" in out.columns
    assert out["guide"].iloc[0] is None
    assert out["match_method"].iloc[0] is None


# ---------------------------------------------------------------------------
# guide_match.apply_overrides (manual reassignment)
# ---------------------------------------------------------------------------

def _matched(rows):
    df = pd.DataFrame(rows)
    df["review_date"] = pd.to_datetime(df["review_date"])
    for col in ("guide", "match_method"):
        if col not in df.columns:
            df[col] = None
    return df


def test_apply_overrides_sets_guide_manual():
    reviews = _matched([
        {"platform": "getyourguide", "tour_name": "Le Marais Free Tour",
         "reviewer_name": "Jane Doe", "review_date": "2026-06-10",
         "guide": "Marie", "match_method": "name"},
        {"platform": "freetour", "tour_name": "Montmartre Tour",
         "reviewer_name": "Bob", "review_date": "2026-06-11",
         "guide": None, "match_method": None},
    ])
    overrides = pd.DataFrame([
        {"platform": "getyourguide", "tour_name": "Le Marais Free Tour",
         "reviewer_name": "Jane Doe", "review_date": "2026-06-10", "guide": "Sophie"},
    ])
    out = gm.apply_overrides(reviews, overrides)
    # Override beats the automatic match on the first row only.
    assert out["guide"].iloc[0] == "Sophie"
    assert out["match_method"].iloc[0] == "manual"
    assert out["guide"].iloc[1] is None


def test_apply_overrides_blank_clears_attribution():
    reviews = _matched([
        {"platform": "gyg", "tour_name": "X", "reviewer_name": "A",
         "review_date": "2026-06-10", "guide": "Marie", "match_method": "name"},
    ])
    overrides = pd.DataFrame([
        {"platform": "gyg", "tour_name": "X", "reviewer_name": "A",
         "review_date": "2026-06-10", "guide": ""},
    ])
    out = gm.apply_overrides(reviews, overrides)
    assert out["guide"].iloc[0] is None
    assert out["match_method"].iloc[0] is None


def test_apply_overrides_empty_is_noop():
    reviews = _matched([
        {"platform": "gyg", "tour_name": "X", "reviewer_name": "A",
         "review_date": "2026-06-10", "guide": "Marie", "match_method": "name"},
    ])
    out = gm.apply_overrides(reviews, pd.DataFrame(columns=[
        "platform", "tour_name", "reviewer_name", "review_date", "guide"]))
    assert out["guide"].iloc[0] == "Marie"
    assert out["match_method"].iloc[0] == "name"


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
