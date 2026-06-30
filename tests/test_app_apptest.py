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

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "dashboard" / "app.py")
sys.path.insert(0, str(ROOT / "dashboard"))  # so `import guide_match` resolves

# Redirect dashboard-written CSVs to a temp dir BEFORE the app module runs.
_TMP = tempfile.mkdtemp(prefix="dw_apptest_")
os.environ["DW_RESPONSES_CSV"] = str(Path(_TMP) / "responses.csv")
os.environ["DW_NOTES_CSV"] = str(Path(_TMP) / "notes.csv")
os.environ["DW_OVERRIDES_CSV"] = str(Path(_TMP) / "guide_overrides.csv")

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
    # A selectbox offering 50/75/100/150, defaulting to 50.
    show = [sb for sb in at.selectbox if [str(o) for o in sb.options] ==
            ["50", "75", "100", "150"]]
    assert show, f"reviews-count selectbox not found; saw {_selectbox_options(at)}"
    assert str(show[0].value) == "50"


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
    # The per-guide feed offers a manual reassignment selectbox.
    has_reassign = any(sb.label == "Attributed guide" for sb in at.selectbox)
    assert has_reassign, "no 'Attributed guide' reassignment selectbox in Guides tab"
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
