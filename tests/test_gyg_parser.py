"""Offline unit tests for scrapers/getyourguide_scraper.parse_reviews().

GYG migrated its reviews widget to a generic SDUI atom tree (2026-11-09), which
silently broke the old ``type:"review"`` parser. These tests pin the new parser
against a fixture captured from the live blocks endpoint
(``tests/fixtures/gyg_reviews_blocks.json``) with real reviewer first names
stripped to placeholders ("Traveler A/B") and one card synthesised down to 3
filled stars to exercise partial-rating counting.

Run directly:
    .venv/bin/python tests/test_gyg_parser.py
or under pytest:
    pytest tests/test_gyg_parser.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scrapers"))

import getyourguide_scraper as gyg  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "gyg_reviews_blocks.json").read_text())


def _parsed():
    return gyg.parse_reviews(FIXTURE)


def test_parses_every_card():
    assert len(_parsed()) == 4


def test_rating_counts_filled_stars_only():
    out = _parsed()
    # 129471020 is a genuine 4-star review: 4 filled + 1 'surface-secondary'.
    assert out["129471020"]["rating"] == 4
    # 999000001 is the synthesised 3-star card (two cards stay at full 5).
    assert out["999000001"]["rating"] == 3
    assert out["129257336"]["rating"] == 5
    assert out["129196476"]["rating"] == 5


def test_author_name_and_country_format():
    out = _parsed()
    # Named reviewer keeps the "<name> – <country>" shape the dedup key relies on.
    assert out["129257336"]["name"] == "Traveler A – Australia"
    # Anonymous reviewers use GYG's en-US label (matches existing CSV rows).
    assert out["129471020"]["name"] == "GetYourGuide traveler – Netherlands"


def test_dates_parse_to_iso_day():
    out = _parsed()
    assert out["129471020"]["date"] == "2026-11-08"
    assert out["129257336"]["date"] == "2026-10-30"
    assert out["129196476"]["date"] == "2026-10-28"


def test_body_text_extracted_and_rating_only_is_blank():
    out = _parsed()
    # Full body survives even though GYG visually collapses long reviews.
    assert out["129257336"]["text"].startswith("A fantastic tour with our guide Adrian")
    assert "Adrian" in out["129196476"]["text"]  # non-English body still captured
    # Rating-only reviews carry no text (common on GYG).
    assert out["129471020"]["text"] == ""


def test_parse_review_date_helper():
    assert gyg._parse_review_date("November 8, 2026 - Verified booking") == "2026-11-08"
    assert gyg._parse_review_date("October 30, 2026 - Verified booking") == "2026-10-30"
    assert gyg._parse_review_date("not a date") == ""


def test_output_shape_matches_save_contract():
    # Keys the scraper later maps into the reviews.csv row.
    for r in _parsed().values():
        assert set(r) == {"rating", "name", "date", "text"}


# ---------------------------------------------------------------------------
# Minimal runner
# ---------------------------------------------------------------------------

def _run_all():
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
    sys.exit(1 if _run_all() else 0)
