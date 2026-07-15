"""
Streamlit AppTest smoke/behaviour checks for dashboard/app.py.

Verifies the app boots and each tab renders without raising, plus the specific
UI changes: the reviews-count selectbox (50/75/100/150), the plain
"Mark as responded" label, the per-review internal-note text area, and the
Guides tab (alerts + reassign popover).

Notes/responses/overrides are redirected to a temp dir via the DW_*_CSV env
vars so the test never touches the real data files.

Run directly (no pytest needed):
    .venv/bin/python tests/test_app_apptest.py
or under pytest:
    pytest tests/test_app_apptest.py
"""

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "dashboard" / "app.py")
sys.path.insert(0, str(ROOT / "dashboard"))  # so `import guide_match` resolves

# Redirect dashboard-written CSVs to a temp dir BEFORE the app module runs.
_TMP = tempfile.mkdtemp(prefix="dw_apptest_")
os.environ["DW_RESPONSES_CSV"] = str(Path(_TMP) / "responses.csv")
os.environ["DW_NOTES_CSV"] = str(Path(_TMP) / "notes.csv")
os.environ["DW_OVERRIDES_CSV"] = str(Path(_TMP) / "guide_overrides.csv")


def _build_tour_ratings_fixture() -> str:
    """Real tour_ratings.csv plus synthetic history so the comparison columns
    have earlier readings to diff against (the live file only has today's run).

    Returns the fixture path. For the first (platform, tour_name) in the real
    file we add a reading ~8 days ago and ~370 days ago, both lower than today's
    rating, so "vs last week" and "vs last year" render an ▲ delta.
    """
    real = ROOT / "data" / "tour_ratings.csv"
    rows = []
    if real.exists():
        with open(real, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    now = datetime.now(timezone.utc)
    if rows:
        first = rows[0]
        cur = float(first["rating"])
        for days, rating in ((8, round(cur - 0.15, 2)), (370, round(cur - 0.25, 2))):
            ts = (now - timedelta(days=days)).isoformat(timespec="seconds")
            rows.append({"platform": first["platform"], "tour_name": first["tour_name"],
                         "rating": f"{rating:g}", "scraped_at": ts})
    path = Path(_TMP) / "tour_ratings.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["platform", "tour_name", "rating", "scraped_at"])
        w.writeheader()
        w.writerows(rows)
    return str(path)


os.environ["DW_TOUR_RATINGS_CSV"] = _build_tour_ratings_fixture()

from streamlit.testing.v1 import AppTest  # noqa: E402


def _run():
    return AppTest.from_file(APP, default_timeout=90).run()


def _selectbox_options(at):
    return [list(sb.options) for sb in at.selectbox]


def test_app_boots_without_exception():
    at = _run()
    assert not at.exception, at.exception


def test_reviews_count_selectbox():
    at = _run()
    # A selectbox offering 50/75/100/150/All, defaulting to 50.
    show = [sb for sb in at.selectbox if [str(o) for o in sb.options] ==
            ["50", "75", "100", "150", "All"]]
    assert show, f"reviews-count selectbox not found; saw {_selectbox_options(at)}"
    assert str(show[0].value) == "50"


def test_reviews_count_all_renders_full_feed():
    at = _run()
    at.radio(key="rev_period").set_value("All").run()
    # Selecting "All" should render every in-scope review, not just the first 150.
    at.selectbox(key="rev_show_n").set_value("All").run()
    assert not at.exception, at.exception
    # Far more note text areas than the old 150 cap (one per rendered card).
    note_areas = [ta for ta in at.text_area if ta.label == "Internal note"]
    assert len(note_areas) > 150, f"only {len(note_areas)} cards rendered under 'All'"


def test_reviews_widgets_present_with_plain_responded_label():
    at = _run()
    # Widen the period so the feed isn't empty, then inspect the cards.
    at.radio(key="rev_period").set_value("All").run()
    assert not at.exception, at.exception

    labels = [cb.label for cb in at.checkbox]
    assert "Mark as responded" in labels, f"checkbox labels: {labels}"
    assert not any("✅" in (l or "") for l in labels), "responded label still has emoji"

    note_areas = [ta for ta in at.text_area if ta.label == "Internal note"]
    assert note_areas, "no internal-note text areas rendered"


def test_internal_note_saves_and_persists():
    at = _run()
    at.radio(key="rev_period").set_value("All").run()
    notes = [ta for ta in at.text_area if ta.label == "Internal note"]
    assert notes
    notes[0].set_value("checked passport policy with guide").run()
    assert not at.exception, at.exception
    # The note file should now exist and contain the text.
    notes_csv = Path(os.environ["DW_NOTES_CSV"])
    assert notes_csv.exists()
    assert "checked passport policy with guide" in notes_csv.read_text()


def test_all_tabs_render_without_exception():
    at = _run()
    for tab_idx in range(4):  # Reviews, Analytics, Health, Guides
        at.button(key=f"tabbtn_{tab_idx}").click().run()
        assert not at.exception, f"tab {tab_idx} raised: {at.exception}"


def test_guides_tab_has_reassign_and_alerts():
    at = _run()
    at.button(key="tabbtn_3").click().run()
    assert not at.exception, at.exception
    # Widen the guide period to All so the selected guide definitely has
    # in-period reviews (and thus renders reassign popovers), regardless of
    # which guide sorts first or how recent the data is.
    at.radio(key="guide_period").set_value("All").run()
    assert not at.exception, at.exception
    # The per-guide feed offers a manual reassignment selectbox.
    has_reassign = any(sb.label == "Attributed guide" for sb in at.selectbox)
    assert has_reassign, "no 'Attributed guide' reassignment selectbox in Guides tab"
    # The per-guide feed has the same 50/75/100/150/All pagination control.
    has_show_all = any([str(o) for o in sb.options] == ["50", "75", "100", "150", "All"]
                       for sb in at.selectbox)
    assert has_show_all, "Guides feed missing the [50/75/100/150/All] Show selectbox"
    # Alerts panel renders something (error/warning for unhealthy guides, or a
    # success when all clear) — i.e. no crash and the panel exists.
    assert at.error or at.warning or at.success


def test_guides_kpi_summary_present():
    at = _run()
    at.button(key="tabbtn_3").click().run()
    assert not at.exception, at.exception
    blob = " ".join(m.value for m in at.markdown)
    for label in ("Matched reviews", "Weighted avg", "Below 5★", "Below 3★",
                  "In alert", "Attention"):
        assert label in blob, f"KPI summary card '{label}' not found in Guides tab"


def _all_text(at):
    parts = []
    for attr in ("markdown", "subheader", "header", "title", "caption"):
        try:
            parts += [e.value for e in getattr(at, attr)]
        except Exception:
            pass
    return " ".join(parts)


def test_reviews_default_feed_shows_all_platforms():
    # Regression: with the default period, the feed must not be dominated by one
    # platform. getyourguide (the bulk of reviews) uses real review dates and is
    # stale, so a short default period hid it entirely — leaving only guruwalk
    # (whose display_date is its scrape date). Render everything and confirm a
    # non-guruwalk platform badge is present at the default period.
    at = _run()
    at.selectbox(key="rev_show_n").set_value("All").run()
    assert not at.exception, at.exception
    assert "GetYourGuide" in _all_text(at), \
        "getyourguide reviews missing from the default-period feed"


def test_reviews_tab_has_assign_guide():
    at = _run()
    at.radio(key="rev_period").set_value("All").run()
    assert not at.exception, at.exception
    # The per-review "Assign guide" expander exposes a guide selectbox.
    has_assign = any(sb.label == "Attributed guide" for sb in at.selectbox)
    assert has_assign, "no 'Attributed guide' assign selectbox in Reviews tab"


def test_claude_analysis_moved_to_tour_health():
    at = _run()
    # Analytics (tab 1) should NO LONGER contain the Claude analysis section.
    at.button(key="tabbtn_1").click().run()
    assert not at.exception, at.exception
    assert "Analyze with Claude" not in _all_text(at), \
        "Analyze with Claude still present in Analytics tab"
    # Tour Health (tab 2) SHOULD now contain it.
    at.button(key="tabbtn_2").click().run()
    assert not at.exception, at.exception
    assert "Analyze with Claude" in _all_text(at), \
        "Analyze with Claude missing from Tour Health tab"


def test_analytics_ratings_by_platform_table():
    # Analytics tab shows the "Ratings by platform per tour" table fed by
    # data/tour_ratings.csv: platform/Overall cells hold a bare two-decimal
    # rating (no "(count)"), plus week/year comparison columns.
    at = _run()
    at.button(key="tabbtn_1").click().run()
    assert not at.exception, at.exception
    assert "Ratings by platform per tour" in _all_text(at), \
        "pivot subheader missing from Analytics tab"

    pivots = [d.value for d in at.dataframe
              if "Tour" in d.value.columns and "Overall" in d.value.columns]
    assert pivots, "Ratings-by-platform table not found"
    pivot = pivots[0]
    assert "vs last week" in pivot.columns and "vs last year" in pivot.columns, \
        f"comparison columns missing; saw {list(pivot.columns)}"

    import re as _re
    # Platform + Overall cells: "-" or a platform-published rating shown as-is
    # (default float repr, 1–2 decimals): "5.0", "4.8", "4.87" — never "4.80".
    rating_re = _re.compile(r"^(-|\d\.\d{1,2})$")
    compare_cols = {"vs last week", "vs last year"}
    rating_cols = [c for c in pivot.columns if c not in {"Tour"} | compare_cols]
    for col in rating_cols:
        for val in pivot[col]:
            assert rating_re.match(str(val)), \
                f"unexpected rating cell '{val}' in column '{col}'"
    # Comparison cells: — (no data), 0.00 (no change), or ▲/▼ + two decimals.
    delta_re = _re.compile(r"^(—|0\.00|[▲▼]\d\.\d{2})$")
    for col in compare_cols:
        for val in pivot[col]:
            assert delta_re.match(str(val)), \
                f"unexpected comparison cell '{val}' in column '{col}'"


def test_analytics_rating_comparisons_compute():
    # The synthetic-history fixture adds a lower reading ~8 days and ~370 days
    # ago for one tour, so both comparison columns must show an ▲ delta for it
    # (proving the week/year diff logic runs, not just renders "—").
    at = _run()
    at.button(key="tabbtn_1").click().run()
    assert not at.exception, at.exception
    pivot = next(d.value for d in at.dataframe
                 if "vs last week" in d.value.columns)
    up_week = [v for v in pivot["vs last week"] if str(v).startswith("▲")]
    up_year = [v for v in pivot["vs last year"] if str(v).startswith("▲")]
    assert up_week, f"no ▲ 'vs last week' delta computed; saw {list(pivot['vs last week'])}"
    assert up_year, f"no ▲ 'vs last year' delta computed; saw {list(pivot['vs last year'])}"


# ---------------------------------------------------------------------------
# Minimal runner (so it works without pytest)
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
