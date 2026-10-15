"""Offline unit tests for dashboard/gsheet_store.py.

These never touch Google: a fake in-memory worksheet is injected in place of
``_worksheet`` so the upsert / delete / load + key-matching logic can be
exercised without credentials or network. Also checks that the backend is
disabled (``enabled() is False``) when no service-account secret is present, so
the app falls back to CSV in dev/CI/tests.

Run directly:
    .venv/bin/python tests/test_gsheet_store.py
or under pytest:
    pytest tests/test_gsheet_store.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))

import gsheet_store as gs  # noqa: E402


class FakeWorksheet:
    """Minimal stand-in for a gspread worksheet backed by a list of rows
    (row 0 is the header), implementing only what gsheet_store calls."""

    def __init__(self, header):
        self.rows = [list(header)]
        self.value_input_options = []  # records the option used on each write

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def get_all_records(self, expected_headers=None):
        header = self.rows[0]
        out = []
        for r in self.rows[1:]:
            padded = list(r) + [""] * (len(header) - len(r))
            out.append(dict(zip(header, padded)))
        return out

    def update(self, cell, values, value_input_option=None):
        # cell like "A7" -> 1-based row 7 -> index 6
        row_idx = int(cell[1:]) - 1
        self.rows[row_idx] = list(values[0])
        self.value_input_options.append(value_input_option)

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))
        self.value_input_options.append(value_input_option)

    def delete_rows(self, i):  # 1-based
        del self.rows[i - 1]


def _install_fakes(monkeypatch_target=gs):
    """Replace _worksheet with per-tab fakes; return the tab->FakeWorksheet map."""
    sheets = {
        gs.NOTES_TAB: FakeWorksheet(gs.HEADERS[gs.NOTES_TAB]),
        gs.RESPONSES_TAB: FakeWorksheet(gs.HEADERS[gs.RESPONSES_TAB]),
    }
    monkeypatch_target._worksheet = lambda tab: sheets[tab]
    return sheets


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_norm_and_key_matching():
    assert gs._norm("  GetYourGuide ") == "getyourguide"
    key = ("getyourguide", "Paris: Marais", "Jo", "2026-10-07")
    # Case-insensitive on platform/tour/reviewer; date compared stripped.
    assert gs._key_matches(["GetYourGuide", "paris: marais", "JO", "2026-10-07"], key)
    assert not gs._key_matches(["freetour", "paris: marais", "jo", "2026-10-07"], key)
    assert not gs._key_matches(["getyourguide", "paris: marais", "jo", "2026-10-08"], key)
    assert not gs._key_matches(["too", "short"], key)  # guards short rows


def test_enabled_false_without_service_account():
    # No gcp_service_account secret (and gspread not installed) -> disabled.
    assert gs.enabled() is False


# ---------------------------------------------------------------------------
# Upsert / load / delete round-trips (notes)
# ---------------------------------------------------------------------------

def test_note_upsert_insert_then_update_then_delete():
    sheets = _install_fakes()
    key = ("getyourguide", "Paris: Marais Guided Tour", "Jo", "2026-10-07")

    gs.upsert_row(gs.NOTES_TAB, key, {"note": "first", "updated_at": "t1"})
    rows = gs.load_rows(gs.NOTES_TAB)
    assert len(rows) == 1
    assert rows[0]["note"] == "first"
    assert rows[0]["platform"] == "getyourguide"

    # Same identity (different casing) -> update in place, not a duplicate row.
    key2 = ("GetYourGuide", "paris: marais guided tour", "jo", "2026-10-07")
    gs.upsert_row(gs.NOTES_TAB, key2, {"note": "second", "updated_at": "t2"})
    rows = gs.load_rows(gs.NOTES_TAB)
    assert len(rows) == 1, "upsert created a duplicate instead of updating"
    assert rows[0]["note"] == "second"

    # A different review -> separate row.
    other = ("freetour", "Le Marais", "Sue", "2026-10-06")
    gs.upsert_row(gs.NOTES_TAB, other, {"note": "n", "updated_at": "t3"})
    assert len(gs.load_rows(gs.NOTES_TAB)) == 2

    # Delete the first; the other remains.
    gs.delete_row(gs.NOTES_TAB, key)
    rows = gs.load_rows(gs.NOTES_TAB)
    assert len(rows) == 1
    assert rows[0]["reviewer_name"] == "Sue"

    # Deleting a missing key is a no-op.
    gs.delete_row(gs.NOTES_TAB, key)
    assert len(gs.load_rows(gs.NOTES_TAB)) == 1


def test_writes_use_raw_so_notes_are_never_evaluated_as_formulas():
    sheets = _install_fakes()
    ws = sheets[gs.NOTES_TAB]
    key = ("getyourguide", "Paris: Marais", "Jo", "2026-10-07")

    # A note that looks like a spreadsheet formula must be stored literally.
    formula_like = "=HYPERLINK(\"http://evil\",\"x\")"
    gs.upsert_row(gs.NOTES_TAB, key, {"note": formula_like, "updated_at": "t1"})  # insert path
    gs.upsert_row(gs.NOTES_TAB, key, {"note": formula_like, "updated_at": "t2"})  # update path

    # Both the append (insert) and the update went through as RAW.
    assert ws.value_input_options == ["RAW", "RAW"], ws.value_input_options
    assert gs.load_rows(gs.NOTES_TAB)[0]["note"] == formula_like


def test_response_upsert_and_delete():
    _install_fakes()
    key = ("guruwalk", "Montmartre", "Alexandra", "2026-10-01")
    gs.upsert_row(gs.RESPONSES_TAB, key, {"responded_at": "2026-10-10T00:00:00+00:00"})
    rows = gs.load_rows(gs.RESPONSES_TAB)
    assert len(rows) == 1 and rows[0]["responded_at"].startswith("2026-10-10")
    gs.delete_row(gs.RESPONSES_TAB, key)
    assert gs.load_rows(gs.RESPONSES_TAB) == []


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
