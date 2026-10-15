"""One-time migration: copy local data/notes.csv and data/responses.csv into the
private Google Sheet (see gsheet_store).

Use this once, after configuring the Google Sheet secrets, to seed the sheet
with any notes/responses that were written to the local CSVs before the Sheet
backend existed.

Idempotent: each row is written with ``gsheet_store.upsert_row``, which matches
on the (platform, tour_name, reviewer_name, review_date) key and updates in
place — so running it twice updates existing rows rather than duplicating them.

Run from the repo root (so .streamlit/secrets.toml is found) with the project
venv:

    .venv/bin/python dashboard/migrate_to_sheet.py            # migrate both
    .venv/bin/python dashboard/migrate_to_sheet.py --dry-run  # show what would move
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import gsheet_store`
import gsheet_store  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTES_CSV = DATA_DIR / "notes.csv"
RESPONSES_CSV = DATA_DIR / "responses.csv"

KEY_COLS = ["platform", "tour_name", "reviewer_name", "review_date"]


def _rows(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    return pd.read_csv(csv_path, dtype=str).fillna("").to_dict("records")


def _key_fields(r: dict):
    return (r.get("platform", ""), r.get("tour_name", ""),
            r.get("reviewer_name", ""), r.get("review_date", ""))


def migrate(dry_run: bool = False) -> int:
    if not gsheet_store.enabled():
        print("ERROR: the Google Sheet backend is not configured.\n"
              "  - add [gcp_service_account] and [gsheet].spreadsheet_id to "
              ".streamlit/secrets.toml\n"
              "  - pip install gspread google-auth\n"
              "  - run this from the repo root so secrets.toml is found.",
              file=sys.stderr)
        return 1

    notes = _rows(NOTES_CSV)
    responses = _rows(RESPONSES_CSV)
    print(f"notes.csv rows:     {len(notes)}")
    print(f"responses.csv rows: {len(responses)}")
    if dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    n_notes = 0
    for r in notes:
        note = (r.get("note") or "").strip()
        if not note:
            continue  # empty note = no record
        gsheet_store.upsert_row(gsheet_store.NOTES_TAB, _key_fields(r), {
            "note": note,
            "updated_at": r.get("updated_at", ""),
        })
        n_notes += 1

    n_resp = 0
    for r in responses:
        gsheet_store.upsert_row(gsheet_store.RESPONSES_TAB, _key_fields(r), {
            "responded_at": r.get("responded_at", ""),
        })
        n_resp += 1

    print(f"\nMigrated {n_notes} notes and {n_resp} responses into the Sheet "
          f"(idempotent — re-running updates in place).")
    return 0


if __name__ == "__main__":
    sys.exit(migrate(dry_run="--dry-run" in sys.argv[1:]))
