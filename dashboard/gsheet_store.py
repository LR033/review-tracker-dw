"""Private Google Sheet persistence for the dashboard's internal notes and
response-tracking records.

Why this exists
---------------
Streamlit Community Cloud runs the app in an ephemeral container: anything the
app writes to the local filesystem (``data/notes.csv`` / ``data/responses.csv``)
is lost on every reboot, redeploy, or resource-sleep, and nothing commits it
back to GitHub. The repo is also **public**, so internal notes must never be
stored in it. This module persists notes/responses to a *private* Google Sheet
instead, which survives restarts and is not world-readable.

Design
------
- **Opt-in / graceful fallback.** When the required secrets are absent (local
  dev, CI, the AppTest suite) ``enabled()`` returns ``False`` and the app keeps
  using its CSV code path unchanged. The Sheet is used only on the deployed app
  where the secrets exist.
- One spreadsheet, two tabs: ``notes`` and ``responses``. Each row is keyed on
  the same ``(platform, tour_name, reviewer_name, review_date)`` tuple the rest
  of the app uses (compared case-insensitively, stripped). Writes are upserts;
  clearing a note / un-marking a response deletes the row.
- Concurrency is last-write-wins (acceptable for this low-volume, single-team
  tool).

Setup (one time)
----------------
1. Create a Google Cloud project; enable the **Google Sheets API** and
   **Google Drive API**.
2. Create a **service account**; download its JSON key.
3. Create a Google Sheet (any name) and **share it** with the service account's
   ``client_email`` as an Editor.
4. In ``.streamlit/secrets.toml`` (and in the Streamlit Cloud app's Secrets):

       [gcp_service_account]
       type = "service_account"
       project_id = "..."
       private_key_id = "..."
       private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
       client_email = "name@project.iam.gserviceaccount.com"
       client_id = "..."
       token_uri = "https://oauth2.googleapis.com/token"
       # (paste the remaining fields from the JSON key verbatim)

       [gsheet]
       spreadsheet_id = "<the id from the sheet URL .../d/<ID>/edit>"

5. ``pip install gspread google-auth`` (already in requirements.txt).

The tabs and their header rows are created automatically on first write.
"""

from __future__ import annotations

import streamlit as st

NOTES_TAB = "notes"
RESPONSES_TAB = "responses"

HEADERS = {
    NOTES_TAB: ["platform", "tour_name", "reviewer_name", "review_date",
                "note", "updated_at"],
    RESPONSES_TAB: ["platform", "tour_name", "reviewer_name", "review_date",
                    "responded_at"],
}

_KEY_COLS = 4  # first four columns form the identity key


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _spreadsheet_id():
    try:
        return st.secrets["gsheet"]["spreadsheet_id"]
    except Exception:
        return None


def enabled() -> bool:
    """True only when a service account + spreadsheet id are configured and
    gspread is importable. Any failure (no secrets.toml, missing keys, package
    not installed) disables the Sheet backend rather than raising."""
    try:
        sa = st.secrets.get("gcp_service_account", None) if hasattr(st.secrets, "get") \
            else st.secrets["gcp_service_account"]
    except Exception:
        return False
    if not sa or not _spreadsheet_id():
        return False
    try:
        import gspread  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Connection (cached for the session)
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def _spreadsheet():
    import gspread
    sa = dict(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(sa)
    return gc.open_by_key(_spreadsheet_id())


def _worksheet(tab: str):
    """Return the worksheet for ``tab``, creating it (with its header row) if it
    doesn't exist yet."""
    import gspread
    ss = _spreadsheet()
    try:
        return ss.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab, rows=100, cols=len(HEADERS[tab]))
        ws.append_row(HEADERS[tab], value_input_option="RAW")
        return ws


# ---------------------------------------------------------------------------
# Key matching
# ---------------------------------------------------------------------------

def _norm(value) -> str:
    return str(value).strip().lower()


def _key_matches(row_values, key_fields) -> bool:
    """row_values: a sheet row (list). key_fields: (platform, tour, reviewer,
    date). review_date is compared stripped but case-sensitively, like the rest
    of the app; the other three are case-folded."""
    if len(row_values) < _KEY_COLS:
        return False
    return (
        _norm(row_values[0]) == _norm(key_fields[0])
        and _norm(row_values[1]) == _norm(key_fields[1])
        and _norm(row_values[2]) == _norm(key_fields[2])
        and str(row_values[3]).strip() == str(key_fields[3]).strip()
    )


# ---------------------------------------------------------------------------
# Reads / writes
# ---------------------------------------------------------------------------

def load_rows(tab: str) -> list[dict]:
    """All data rows of a tab as dicts keyed by the header names."""
    ws = _worksheet(tab)
    return ws.get_all_records(expected_headers=HEADERS[tab])


def upsert_row(tab: str, key_fields, extra: dict) -> None:
    """Insert or replace the row identified by ``key_fields``.

    ``extra`` supplies the non-key columns (e.g. {"note":..,"updated_at":..} or
    {"responded_at":..}). The full row is written in HEADERS[tab] order.
    """
    ws = _worksheet(tab)
    record = {
        "platform": key_fields[0], "tour_name": key_fields[1],
        "reviewer_name": key_fields[2], "review_date": key_fields[3],
        **extra,
    }
    new_values = [record.get(col, "") for col in HEADERS[tab]]

    values = ws.get_all_values()  # includes header row at index 0
    for i, row in enumerate(values[1:], start=2):  # sheet rows are 1-based
        if _key_matches(row, key_fields):
            ws.update(f"A{i}", [new_values], value_input_option="RAW")
            return
    ws.append_row(new_values, value_input_option="RAW")


def delete_row(tab: str, key_fields) -> None:
    """Remove the row identified by ``key_fields`` (no-op if absent)."""
    ws = _worksheet(tab)
    values = ws.get_all_values()
    for i, row in enumerate(values[1:], start=2):
        if _key_matches(row, key_fields):
            ws.delete_rows(i)
            return
