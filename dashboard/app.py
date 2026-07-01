"""
Streamlit review dashboard for Discover Walks (Paris tour company).

Loads data/reviews.csv (the append-only scraper output) and presents it across
three tabs. The theme follows the viewer's system light/dark preference, with a
manual sidebar toggle to force light or dark (neutral, translucent card colours;
charts themed via st.plotly_chart):

  Tab 1 — Reviews
    Quick period buttons (default 7d), a sort selector (newest / lowest /
    highest), and the per-review feed. Empty reviews show "(no comment)".
    Reviews below 5★ are part of the response workflow: 1-3★ get a red "needs
    reply" badge, 4★ a yellow "needs attention" badge, and any can be marked
    "responded" (persisted to data/responses.csv → green badge). 5★ reviews
    get no badge. Every card keeps a compact "Draft reply" button.

  Tab 2 — Analytics
    Period-over-period KPI cards (this period vs the previous equal window),
    a volume + average-rating chart with weekly/monthly/yearly toggle, the
    per-platform and rating-distribution charts, and an "Analyze with Claude"
    section (general + per-tour) that streams a summary of themes, complaints,
    praised guides, and trends.

  Tab 3 — Health
    A period selector (default 7d) driving an auto-generated alerts panel and a
    per-tour health table: review count, avg rating, trend vs the previous
    equal period, a "Below 3★" count (reviews under 3 stars), response rate,
    and a 🟢/🟡/🔴 status from the period average.

Filters: platform and tour apply globally; the star-rating filter scopes the
Reviews feed only (so Analytics/Health averages and statuses stay accurate).

The Anthropic API key is read from st.secrets["ANTHROPIC_API_KEY"] and used for
both reply drafting and analysis (model claude-sonnet-4-6). The dashboard runs
fine without it — only the Claude-powered features are disabled.

Run:
    streamlit run dashboard/app.py
"""

import html
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from guide_match import attach_guides

try:
    from guide_match import apply_overrides
except ImportError:
    # Resilience for Streamlit Cloud: if a stale guide_match.py (one predating
    # apply_overrides) is ever deployed alongside this app.py, degrade
    # gracefully — skip manual overrides — instead of crashing the whole
    # dashboard at import time. The normal path uses the real function.
    def apply_overrides(reviews, overrides, date_col="review_date"):
        return reviews

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REVIEWS_FILE = DATA_DIR / "reviews.csv"
BOOKINGS_FILE = DATA_DIR / "bookings.csv"  # TourDash bookings → guide attribution
# Overridable so tests don't write to the real logs.
RESPONSES_FILE = Path(os.environ.get("DW_RESPONSES_CSV", str(DATA_DIR / "responses.csv")))
RESPONSES_COLS = ["platform", "tour_name", "reviewer_name", "review_date", "responded_at"]
NOTES_FILE = Path(os.environ.get("DW_NOTES_CSV", str(DATA_DIR / "notes.csv")))
NOTES_COLS = ["platform", "tour_name", "reviewer_name", "review_date", "note", "updated_at"]
OVERRIDES_FILE = Path(os.environ.get("DW_OVERRIDES_CSV", str(DATA_DIR / "guide_overrides.csv")))
OVERRIDES_COLS = ["platform", "tour_name", "reviewer_name", "review_date", "guide"]

# Both Claude features use Sonnet per the product spec.
REPLY_MODEL = "claude-sonnet-4-6"
ANALYSIS_MODEL = "claude-sonnet-4-6"

TODAY = date.today()
LOW_MAX = 2  # ratings <= this are "low"

# Shared accent palette (same hues as the rankings dashboard).
PALETTE = ["#E63946", "#457B9D", "#2A9D8F", "#E9C46A", "#9B5DE5", "#F15BB5", "#00BBF9"]

PLATFORMS = {
    "freetour":     {"label": "Freetour",     "color": "#E63946"},
    "guruwalk":     {"label": "GuruWalk",     "color": "#2A9D8F"},
    "getyourguide": {"label": "GetYourGuide", "color": "#457B9D"},
    "tripadvisor":  {"label": "Tripadvisor",  "color": "#E9C46A"},
    "google":       {"label": "Google",       "color": "#9B5DE5"},
}

DISCOVER_WALKS_VOICE = """\
You are the guest-relations voice of Discover Walks, a Paris walking-tour \
company known for warm, knowledgeable local guides. You draft public replies \
to customer reviews.

Guidelines:
- Always write your reply in English, regardless of the review's language.
- Tone: friendly, warm, and professional -- never stiff or corporate, never \
sycophantic or over-apologetic.
- Thank the reviewer by first name if one is given, and reference something \
specific they mentioned (the guide, the neighbourhood, the experience).
- For positive reviews: be gracious and invite them back.
- For critical reviews (1-2 stars): acknowledge their experience sincerely, \
apologise where warranted, avoid excuses, and offer to make it right \
(invite them to reach out to the team).
- Keep it concise: 2-4 sentences.
- Sign off as "The Discover Walks team" (translated to the review's language).
- Output ONLY the reply text -- no preamble, quotes, or subject line.
"""

ANALYSIS_VOICE = """\
You are a customer-experience analyst for Discover Walks, a Paris walking-tour \
company. You are given a digest of customer reviews (ratings + text, across \
booking platforms). Produce a concise, well-structured analysis in Markdown \
with these sections:

- **Top themes** — what customers consistently mention (positive and negative).
- **Recurring complaints** — specific issues that appear more than once, with a \
rough sense of how often.
- **Most-praised guides** — first names of guides who are repeatedly praised.
- **Trends** — any shift over time in review volume or sentiment.
- **Recommended actions** — 2-3 concrete suggestions.

Quote short phrases where useful, stay specific, and keep it under ~350 words. \
If the data is thin, say so plainly rather than inventing patterns.
"""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_reviews() -> pd.DataFrame:
    """Load reviews.csv as a typed DataFrame (cached 5 min)."""
    if not REVIEWS_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(REVIEWS_FILE, dtype=str).fillna("")
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    # format="mixed" parses each value independently. Some platforms (e.g.
    # guruwalk) only provide year-month dates like "2026-06"; without this,
    # pandas infers a single "%Y-%m-%d" format and coerces those to NaT,
    # which dropna would then silently remove (hiding the whole platform).
    df["review_date"] = pd.to_datetime(
        df["review_date"], errors="coerce", format="mixed"
    )
    df = df.dropna(subset=["review_date"])
    df["platform_label"] = df["platform"].map(
        lambda p: PLATFORMS.get(p, {}).get("label", p.title())
    )

    # guruwalk only exposes month precision ("2026-06"), which parses to the 1st
    # of the month — a misleading exact date. For those reviews we use the scrape
    # timestamp as the effective date instead. `display_date` drives the card
    # date, period filtering, and sorting so display and filtering stay in sync;
    # `review_date` is preserved for the response-tracking identity key.
    scraped = (
        pd.to_datetime(df["scraped_at"], errors="coerce", utc=True)
        .dt.tz_localize(None)
        if "scraped_at" in df.columns
        else pd.Series(pd.NaT, index=df.index)
    )
    month_precision = (
        (df["platform"] == "guruwalk")
        & (df["review_date"].dt.day == 1)
        & scraped.notna()
    )
    df["display_date"] = df["review_date"]
    df.loc[month_precision, "display_date"] = scraped[month_precision]

    return df.sort_values("display_date", ascending=False).reset_index(drop=True)


BOOKINGS_LOOKBACK_MONTHS = 18  # only recent bookings are needed for matching


@st.cache_data(ttl=3600)
def load_bookings() -> pd.DataFrame:
    """Load recent TourDash bookings (empty frame if the file is absent).

    Cached for an hour and limited to the last ``BOOKINGS_LOOKBACK_MONTHS`` so
    the guide-matching lookup stays bounded as bookings.csv grows over time
    (reviews are recent, so older bookings can't match anything anyway).
    """
    cols = ["booking_id", "tour_name", "tour_date", "guide", "contact_name",
            "platform", "booked_adults", "attended_adults", "status"]
    if not BOOKINGS_FILE.exists():
        return pd.DataFrame(columns=cols)
    bdf = pd.read_csv(BOOKINGS_FILE, dtype=str).fillna("")
    bdf["tour_date"] = pd.to_datetime(bdf["tour_date"], errors="coerce")
    bdf = bdf.dropna(subset=["tour_date"])
    # "Discover Walks" is a company-level booking, not a real guide attribution.
    bdf = bdf[bdf["guide"].astype(str).str.strip().str.lower() != "discover walks"]
    cutoff = pd.Timestamp(TODAY) - pd.DateOffset(months=BOOKINGS_LOOKBACK_MONTHS)
    return bdf[bdf["tour_date"] >= cutoff].reset_index(drop=True)


@st.cache_data(ttl=3600)
def load_reviews_with_guides() -> pd.DataFrame:
    """Reviews with guide attribution attached.

    Guide matching is the expensive step (fuzzy name matching over thousands of
    bookings), so it lives here behind an hour-long cache instead of running on
    every Streamlit rerun. Returns the reviews frame plus `guide` and
    `match_method` columns.
    """
    reviews = load_reviews()
    if reviews.empty:
        return reviews
    return attach_guides(reviews, load_bookings(), date_col="review_date")


# ---------------------------------------------------------------------------
# Response tracking (data/responses.csv)
# ---------------------------------------------------------------------------

def _norm_key(platform, tour, reviewer, date_str) -> tuple:
    """Identity of a review — matches the scrapers' dedup key."""
    return (
        str(platform).strip().lower(),
        str(tour).strip().lower(),
        str(reviewer).strip().lower(),
        str(date_str).strip(),
    )


def row_key(row) -> tuple:
    return _norm_key(
        row["platform"], row["tour_name"], row["reviewer_name"],
        row["review_date"].strftime("%Y-%m-%d"),
    )


@st.cache_data(ttl=5)
def load_responses() -> dict:
    """Return {review_key: responded_at_iso} from responses.csv."""
    if not RESPONSES_FILE.exists():
        return {}
    try:
        rdf = pd.read_csv(RESPONSES_FILE, dtype=str).fillna("")
    except Exception:
        return {}
    out = {}
    for _, r in rdf.iterrows():
        out[_norm_key(r.get("platform", ""), r.get("tour_name", ""),
                      r.get("reviewer_name", ""), r.get("review_date", ""))] = \
            r.get("responded_at", "")
    return out


def set_responded(row, responded: bool) -> None:
    """Add or remove a review's response record in responses.csv."""
    key_fields = (
        str(row["platform"]), str(row["tour_name"]), str(row["reviewer_name"]),
        row["review_date"].strftime("%Y-%m-%d"),
    )
    target = _norm_key(*key_fields)

    if RESPONSES_FILE.exists():
        rdf = pd.read_csv(RESPONSES_FILE, dtype=str).fillna("")
    else:
        rdf = pd.DataFrame(columns=RESPONSES_COLS)

    if not rdf.empty:
        keep = rdf.apply(
            lambda r: _norm_key(r["platform"], r["tour_name"],
                                r["reviewer_name"], r["review_date"]) != target,
            axis=1,
        )
        rdf = rdf[keep]

    if responded:
        rdf = pd.concat([rdf, pd.DataFrame([{
            "platform": key_fields[0], "tour_name": key_fields[1],
            "reviewer_name": key_fields[2], "review_date": key_fields[3],
            "responded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }])], ignore_index=True)

    RESPONSES_FILE.parent.mkdir(parents=True, exist_ok=True)
    rdf.to_csv(RESPONSES_FILE, index=False)
    load_responses.clear()


# ---------------------------------------------------------------------------
# Internal notes (data/notes.csv)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5)
def load_notes() -> dict:
    """Return {review_key: note_text} from notes.csv."""
    if not NOTES_FILE.exists():
        return {}
    try:
        ndf = pd.read_csv(NOTES_FILE, dtype=str).fillna("")
    except Exception:
        return {}
    out = {}
    for _, r in ndf.iterrows():
        out[_norm_key(r.get("platform", ""), r.get("tour_name", ""),
                      r.get("reviewer_name", ""), r.get("review_date", ""))] = \
            r.get("note", "")
    return out


def save_note(row, note_text: str) -> None:
    """Upsert one review's internal note in notes.csv (empty note removes it)."""
    key_fields = (
        str(row["platform"]), str(row["tour_name"]), str(row["reviewer_name"]),
        row["review_date"].strftime("%Y-%m-%d"),
    )
    target = _norm_key(*key_fields)

    if NOTES_FILE.exists():
        ndf = pd.read_csv(NOTES_FILE, dtype=str).fillna("")
    else:
        ndf = pd.DataFrame(columns=NOTES_COLS)

    if not ndf.empty:
        keep = ndf.apply(
            lambda r: _norm_key(r["platform"], r["tour_name"],
                                r["reviewer_name"], r["review_date"]) != target,
            axis=1,
        )
        ndf = ndf[keep]

    if note_text and note_text.strip():
        ndf = pd.concat([ndf, pd.DataFrame([{
            "platform": key_fields[0], "tour_name": key_fields[1],
            "reviewer_name": key_fields[2], "review_date": key_fields[3],
            "note": note_text.strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }])], ignore_index=True)

    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ndf.to_csv(NOTES_FILE, index=False)
    load_notes.clear()


def _save_note_cb(row, note_key: str) -> None:
    """on_change callback for a note text_area — persists the edited text."""
    save_note(row, st.session_state.get(note_key, ""))


# ---------------------------------------------------------------------------
# Manual guide overrides (data/guide_overrides.csv)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5)
def load_overrides() -> pd.DataFrame:
    """Return guide_overrides.csv as a DataFrame (empty if absent)."""
    if not OVERRIDES_FILE.exists():
        return pd.DataFrame(columns=OVERRIDES_COLS)
    try:
        return pd.read_csv(OVERRIDES_FILE, dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=OVERRIDES_COLS)


def save_guide_override(row, guide) -> None:
    """Upsert one review's manual guide override (``None``/"" clears the guide)."""
    key_fields = (
        str(row["platform"]), str(row["tour_name"]), str(row["reviewer_name"]),
        row["review_date"].strftime("%Y-%m-%d"),
    )
    target = _norm_key(*key_fields)

    odf = load_overrides()
    if not odf.empty:
        keep = odf.apply(
            lambda r: _norm_key(r["platform"], r["tour_name"],
                                r["reviewer_name"], r["review_date"]) != target,
            axis=1,
        )
        odf = odf[keep]

    odf = pd.concat([odf, pd.DataFrame([{
        "platform": key_fields[0], "tour_name": key_fields[1],
        "reviewer_name": key_fields[2], "review_date": key_fields[3],
        "guide": "" if guide in (None, "None") else str(guide),
    }])], ignore_index=True)

    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    odf.to_csv(OVERRIDES_FILE, index=False)
    load_overrides.clear()


# ---------------------------------------------------------------------------
# Anthropic (reply drafting + analysis)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_anthropic_client():
    """Build the Anthropic client from st.secrets. Returns (client, error)."""
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None, "ANTHROPIC_API_KEY not set in st.secrets — add it to .streamlit/secrets.toml."
    if not api_key:
        return None, "ANTHROPIC_API_KEY is empty in st.secrets."
    try:
        import anthropic
    except ImportError:
        return None, "The `anthropic` package is not installed (pip install anthropic)."
    return anthropic.Anthropic(api_key=api_key), None


def draft_reply(review: dict) -> str:
    """Draft a brand-voice reply to one review via the Anthropic API."""
    client, err = get_anthropic_client()
    if client is None:
        raise RuntimeError(err)

    rating = review.get("rating")
    rating_str = f"{rating:g}/5" if pd.notna(rating) else "no rating"
    user_block = (
        f"Platform: {review.get('platform_label', '')}\n"
        f"Tour: {review.get('tour_name', '')}\n"
        f"Reviewer: {review.get('reviewer_name') or 'Anonymous'}\n"
        f"Rating: {rating_str}\n"
        f"Review:\n{review.get('review_text') or '(no written review — rating only)'}"
    )
    message = client.messages.create(
        model=REPLY_MODEL,
        max_tokens=1024,
        system=DISCOVER_WALKS_VOICE,
        messages=[{"role": "user", "content": user_block}],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


def build_digest(d: pd.DataFrame, scope_label: str, max_reviews: int = 150) -> str:
    """Compact text digest of a review set for Claude analysis (token-bounded)."""
    if d.empty:
        return f"No reviews in scope ({scope_label})."
    parts = [
        f"Scope: {scope_label}",
        f"Total reviews: {len(d)}",
        f"Average rating: {d['rating'].mean():.2f}/5",
    ]
    dist = d["rating"].round().value_counts().sort_index()
    parts.append("Rating counts: " + ", ".join(f"{int(k)}★={v}" for k, v in dist.items()))
    pt = d.groupby("tour_name")["rating"].agg(["count", "mean"]).sort_values("count", ascending=False)
    parts.append(
        "By tour (count, avg): "
        + "; ".join(f"{t} ({int(r['count'])}, {r['mean']:.2f})" for t, r in pt.iterrows())
    )
    parts.append(f"Date span: {d['review_date'].min().date()} to {d['review_date'].max().date()}")
    parts.append("\nRecent review samples (rating | tour | date | text):")
    for _, r in d.sort_values("review_date", ascending=False).head(max_reviews).iterrows():
        txt = (r["review_text"] or "").strip().replace("\n", " ")
        if not txt:
            continue
        parts.append(f"- {r['rating']:g}★ | {r['tour_name']} | {r['review_date'].date()} | {txt[:240]}")
    return "\n".join(parts)


def stream_analysis(user_content: str):
    """Yield text chunks of a streamed Claude analysis."""
    client, err = get_anthropic_client()
    if client is None:
        raise RuntimeError(err)
    with client.messages.stream(
        model=ANALYSIS_MODEL,
        max_tokens=2000,
        system=ANALYSIS_VOICE,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        for text in stream.text_stream:
            yield text


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def stars(rating: float) -> str:
    if pd.isna(rating):
        return "—"
    full = int(rating)
    half = (rating - full) >= 0.5
    return "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0))


# Neutral slate-blue for all platform badges (change 7): calm and uniform
# rather than the previous per-platform colours (which included an aggressive
# red). The platform name still identifies the source.
BADGE_SLATE = "#5B7A99"


def platform_badge(platform: str, label: str) -> str:
    return (
        f'<span style="background:{BADGE_SLATE};color:#fff;border-radius:6px;'
        f'padding:2px 9px;font-size:11px;font-weight:600;letter-spacing:.3px;">{label}</span>'
    )


def _pill(text: str, bg: str, fg: str = "#fff") -> str:
    return (
        f'<span style="background:{bg};color:{fg};border-radius:6px;'
        f'padding:2px 8px;font-size:11px;font-weight:600;">{text}</span>'
    )


def health_status(avg: float, n: int) -> tuple:
    """(emoji, label) from the selected-period average rating."""
    if n == 0 or pd.isna(avg):
        return "⚪", "No recent data"
    if avg >= 4.8:
        return "🟢", "Healthy"
    if avg >= 4.5:
        return "🟡", "Needs attention"
    return "🔴", "Critical"


def window(d: pd.DataFrame, days: int, offset: int = 0) -> pd.DataFrame:
    """Reviews in (TODAY-offset-days, TODAY-offset]."""
    hi = TODAY - timedelta(days=offset)
    lo = hi - timedelta(days=days)
    dd = d["display_date"].dt.date
    return d[(dd > lo) & (dd <= hi)]


def delta_html(delta, higher_is_good=True, fmt="{:+.2f}", suffix="vs previous") -> str:
    if delta is None:
        return f'<span class="kpi-sub">— {suffix}</span>'
    if abs(delta) < 1e-9:
        return f'<span class="kpi-sub">— no change</span>'
    good = (delta > 0) == higher_is_good
    color = "#2A9D8F" if good else "#E63946"
    arrow = "▲" if delta > 0 else "▼"
    return f'<span style="color:{color};font-size:12px;">{arrow} {fmt.format(delta)} {suffix}</span>'


def kpi_card(col, label, value, delta_markup, color):
    col.markdown(
        f'<div class="kpi" style="border-color:{color}">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-sub">{delta_markup}</div></div>',
        unsafe_allow_html=True,
    )


def theme_css(mode: str) -> str:
    """CSS that forces a light or dark palette, overriding the system default.

    Used by the manual sidebar toggle (change 3). Backgrounds are forced with
    !important; the base text colour is set without !important so inline badge
    colours still win. The translucent card colours adapt on their own.
    """
    if mode == "light":
        bg, sidebar_bg, fg = "#ffffff", "#f3f4f6", "#1a1a1a"
    else:
        bg, sidebar_bg, fg = "#0e1117", "#1a1d24", "#e8e8e8"
    return f"""
    <style>
    .stApp {{ background-color: {bg} !important; color: {fg}; }}
    [data-testid="stHeader"] {{ background-color: {bg} !important; }}
    [data-testid="stSidebar"] {{ background-color: {sidebar_bg} !important; }}
    [data-testid="stAppViewContainer"] .stMarkdown,
    [data-testid="stSidebar"] {{ color: {fg}; }}
    </style>
    """


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Discover Walks — Reviews", page_icon="🗼", layout="wide")

st.markdown(
    """
    <style>
    /* Neutral, translucent colours so cards work in both light and dark mode.
       Text colour is inherited from Streamlit's theme (we only tune opacity),
       so nothing is hardcoded to a single mode. */
    .kpi {
        background: rgba(128,128,128,0.10); border-radius: 12px; padding: 16px 20px;
        border-left: 5px solid; min-height: 100px;
    }
    .kpi .kpi-label { font-size: 12px; opacity: 0.65; margin-bottom: 6px; }
    .kpi .kpi-value { font-size: 30px; font-weight: 700; line-height: 1; }
    .kpi .kpi-sub   { font-size: 11px; opacity: 0.65; margin-top: 8px; }

    .review-card {
        background: rgba(128,128,128,0.10); border-radius: 12px; padding: 14px 18px;
        margin-bottom: 4px; border-left: 4px solid #2A9D8F;
    }
    .review-card.low { border-left-color: #E63946; background: rgba(230,57,70,0.10); }
    .review-card .rc-head { font-size: 13px; opacity: 0.9; margin-bottom: 4px; }
    .review-card .rc-stars { font-size: 15px; color: #E0A030; }
    .review-card.low .rc-stars { color: #E63946; }
    .review-card .rc-tour { opacity: 0.6; font-size: 12px; margin: 2px 0 6px; }
    .review-card .rc-text { font-size: 16px; line-height: 1.45; }
    .rc-note { font-style: italic; opacity: 0.7; font-size: 13px; margin-top: 8px; }

    /* Review-card container (keys: revcard_*, revcardlow_*): the box that wraps
       the review text AND its controls (responded checkbox, note input) so they
       read as one card. Mirrors .review-card styling. */
    div[class*="st-key-revcard_"], div[class*="st-key-revcardlow_"] {
        background: rgba(128,128,128,0.10); border-radius: 12px;
        padding: 12px 18px 8px; margin-bottom: 10px; border-left: 4px solid #2A9D8F;
    }
    div[class*="st-key-revcardlow_"] {
        border-left-color: #E63946; background: rgba(230,57,70,0.10);
    }
    /* Compact the note textarea inside the card. */
    div[class*="st-key-revcard_"] textarea,
    div[class*="st-key-revcardlow_"] textarea { font-size: 13px; }

    /* Tab navigation — st.button styled as real tabs (keys: tabbtn_0..n).
       Translucent neutrals + inherited text colour adapt to light/dark. */
    div[class*="st-key-tabbtn_"] button {
        border: 1px solid rgba(128,128,128,0.30);
        border-bottom: 3px solid transparent;
        border-radius: 10px 10px 0 0;
        background: rgba(128,128,128,0.08);
        font-size: 16px;
        font-weight: 600;
        padding: 12px 4px;
        transition: none;
    }
    div[class*="st-key-tabbtn_"] button:hover {
        background: rgba(128,128,128,0.18);
    }
    /* Active tab (rendered as a primary button) */
    div[class*="st-key-tabbtn_"] button[kind="primary"],
    div[class*="st-key-tabbtn_"] button[data-testid="stBaseButton-primary"] {
        background: rgba(128,128,128,0.18) !important;
        border-color: rgba(128,128,128,0.30) !important;
        border-bottom: 3px solid #5B7A99 !important;
        font-size: 18px;
    }

    /* Compact "Draft reply" buttons (keys: draftbtn_*) */
    div[class*="st-key-draftbtn_"] button {
        font-size: 12px;
        padding: 1px 10px;
        min-height: 0;
    }

    /* Narrower sidebar — ~75% of the default ~336px (change 5). */
    [data-testid="stSidebar"] {
        width: 252px !important;
        min-width: 252px !important;
        max-width: 252px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Manual light/dark override (change 3). Default to light (matches config.toml
# base="light"); the sidebar toggle flips st.session_state["theme_mode"] and the
# CSS below re-applies on rerun.
if "theme_mode" not in st.session_state:
    st.session_state.theme_mode = "light"
st.markdown(theme_css(st.session_state.theme_mode), unsafe_allow_html=True)

# No hardcoded template/background: st.plotly_chart(theme="streamlit") (the
# default) themes the chart to match the active light/dark Streamlit theme.
CHART_LAYOUT = dict(margin=dict(l=0, r=10, t=30, b=0))

# Plotly.js options for st.plotly_chart(..., config=...). Charts default to
# container width (use_container_width=True), so no width kwarg is needed.
PLOTLY_CONFIG = {"displayModeBar": False, "responsive": True}

# Reviews + guide attribution, both behind caches (the fuzzy matching is too
# slow to run on every rerun — see load_reviews_with_guides).
df = load_reviews_with_guides()
# Manual overrides are cheap and applied fresh each rerun, so a saved
# reassignment shows immediately (they take priority over auto-matching).
df = apply_overrides(df, load_overrides())
responded = load_responses()
notes = load_notes()

st.title("🗼 Discover Walks — Review Tracker")
st.caption("Aggregated customer reviews across booking platforms · drafting & analysis powered by Claude.")

if df.empty:
    st.warning("No reviews found in `data/reviews.csv`. Run the scrapers first.")
    st.stop()

client, client_err = get_anthropic_client()

# ---------------------------------------------------------------------------
# Sidebar filters (platform + tour are global; rating scopes the feed)
# ---------------------------------------------------------------------------

# Light/dark toggle (change 3): the button shows the mode it switches TO.
_mode = st.session_state.theme_mode
if st.sidebar.button(
    "☀️ Light mode" if _mode == "dark" else "🌙 Dark mode",
    key="theme_toggle", width="stretch",
):
    st.session_state.theme_mode = "light" if _mode == "dark" else "dark"
    st.rerun()

st.sidebar.header("Filters")

# Empty selection shows an "All …" placeholder and is treated as all (change 4).
platform_opts = [p for p in PLATFORMS if p in set(df["platform"])]
platform_opts += [p for p in df["platform"].unique() if p not in platform_opts]
sel_platforms = st.sidebar.multiselect(
    "Platform", options=platform_opts, default=[], placeholder="All platforms",
    format_func=lambda p: PLATFORMS.get(p, {}).get("label", p.title()),
)
if not sel_platforms:
    sel_platforms = platform_opts

tour_opts = sorted(df[df["platform"].isin(sel_platforms)]["tour_name"].unique())
sel_tours = st.sidebar.multiselect(
    "Tour", options=tour_opts, default=[], placeholder="All tours",
)
if not sel_tours:
    sel_tours = tour_opts

min_rating, max_rating = st.sidebar.slider(
    "Star rating (feed only)", min_value=1.0, max_value=5.0, value=(1.0, 5.0), step=0.5
)
st.sidebar.caption("Platform & tour apply everywhere; the rating slider scopes the Reviews feed.")

if client_err:
    st.sidebar.caption(f"💬 Claude features disabled — {client_err}")

# Base scope (platform + tour) used by Analytics & Health.
bdf = df[df["platform"].isin(sel_platforms) & df["tour_name"].isin(sel_tours)].copy()
if bdf.empty:
    st.info("No reviews match the selected platforms/tours.")
    st.stop()

# st.tabs() has no API to set the active tab, so it snaps back to the first tab
# on every rerun. We render our own tab bar from st.button (one per tab) and
# keep the active tab in session_state, so the selection persists across reruns.
# The active tab is drawn as a primary button and styled distinctly via CSS.
TAB_LABELS = ["📋 Reviews", "📊 Analytics", "🩺 Health", "🧑‍🏫 Guides"]
if "active_tab" not in st.session_state:
    st.session_state.active_tab = TAB_LABELS[0]

nav_cols = st.columns(len(TAB_LABELS))
for i, label in enumerate(TAB_LABELS):
    is_active = st.session_state.active_tab == label
    if nav_cols[i].button(
        label,
        key=f"tabbtn_{i}",
        type="primary" if is_active else "secondary",
        width="stretch",
    ) and not is_active:
        st.session_state.active_tab = label
        st.rerun()
active_tab = st.session_state.active_tab
st.divider()

# ===========================================================================
# TAB 1 — REVIEWS
# ===========================================================================

if active_tab == "📋 Reviews":
    PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "All": None}

    c1, c2 = st.columns([2, 1])
    period = c1.radio("Period", list(PERIOD_DAYS), index=0, horizontal=True, key="rev_period")
    sort_order = c2.selectbox(
        "Sort", ["Newest first", "Lowest rated", "Highest rated"], key="rev_sort"
    )

    feed = bdf[bdf["rating"].between(min_rating, max_rating)].copy()
    days = PERIOD_DAYS[period]
    if days is not None:
        feed = feed[feed["display_date"].dt.date > (TODAY - timedelta(days=days))]

    if sort_order == "Newest first":
        feed = feed.sort_values("display_date", ascending=False)
    elif sort_order == "Lowest rated":
        feed = feed.sort_values(["rating", "display_date"], ascending=[True, False])
    else:
        feed = feed.sort_values(["rating", "display_date"], ascending=[False, False])

    total = len(feed)
    head = st.columns([3, 1])
    head[0].caption(f"{total:,} reviews in scope · period {period} · {sort_order.lower()}.")
    show_n = head[1].selectbox(
        "Show", [50, 75, 100, 150, "All"], index=0,
        label_visibility="collapsed", key="rev_show_n",
    )
    feed_shown = feed if show_n == "All" else feed.head(int(show_n))

    if total == 0:
        st.info("No reviews match the current filters and period.")

    for idx, row in feed_shown.iterrows():
        rating = row["rating"]
        below5 = pd.notna(rating) and rating < 5            # tracked for responses
        needs_reply = pd.notna(rating) and rating <= 3      # 1-3★ urgent (red)
        needs_attn = below5 and not needs_reply             # 3<r<5 → 4★ (yellow)
        rkey = row_key(row)
        is_resp = rkey in responded
        existing_note = notes.get(rkey, "")
        date_str = row["display_date"].strftime("%d %b %Y")
        name = row["reviewer_name"] or "Anonymous"
        text = row["review_text"] or "<em>(no comment)</em>"

        # Three-tier badge (change 1). 5★ reviews get no badge and aren't part
        # of the response-tracking workflow.
        badge = ""
        if below5 and is_resp:
            badge = " &nbsp;" + _pill("✓ Responded", "#2A9D8F")
        elif needs_reply:
            badge = " &nbsp;" + _pill("⚠ Needs reply", "#E63946")
        elif needs_attn:
            badge = " &nbsp;" + _pill("⚠ Needs attention", "#E9C46A", fg="#5a4500")

        # The whole review (content + controls) lives in a keyed container so
        # the responded checkbox and note input render inside the card, not
        # in a detached row below it. The container provides the card box (CSS
        # on st-key-revcard*); the inner div is transparent and only supplies
        # the rc-* text styling.
        note_html = (
            f'<div class="rc-note">📝 {html.escape(existing_note)}</div>'
            if existing_note else ""
        )
        card_key = f"revcardlow_{idx}" if needs_reply else f"revcard_{idx}"
        reply_key = f"reply_{idx}"
        with st.container(key=card_key):
            # Header row: badge / stars / name / date on the left, the compact
            # "Draft reply" button on the right (same line).
            hcol, dcol = st.columns([4, 1], vertical_alignment="center")
            hcol.markdown(
                f'<div class="review-card{" low" if needs_reply else ""}" '
                f'style="background:none;border:none;padding:0;margin:0;">'
                f'<div class="rc-head">'
                f'{platform_badge(row["platform"], row["platform_label"])} '
                f'&nbsp;<span class="rc-stars">{stars(rating)}</span> '
                f'&nbsp;<b>{name}</b> &nbsp;·&nbsp; {date_str}{badge}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if dcol.button(
                "✍️ Draft reply", key=f"draftbtn_{idx}", disabled=client is None,
            ):
                with st.spinner("Drafting reply…"):
                    try:
                        st.session_state[reply_key] = draft_reply(row.to_dict())
                    except Exception as exc:
                        st.session_state[reply_key] = f"__error__{exc}"

            # Body: tour, review text, and the saved note (italic) if any.
            st.markdown(
                f'<div class="review-card{" low" if needs_reply else ""}" '
                f'style="background:none;border:none;padding:0;margin:0;">'
                f'<div class="rc-tour">{row["tour_name"]}</div>'
                f'<div class="rc-text">{text}</div>'
                f'{note_html}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Mark-as-responded checkbox — tracked for all reviews below 5★;
            # 5★ reviews don't need a response, so no checkbox.
            if below5:
                cb_key = f"resp_{idx}"
                if cb_key not in st.session_state:
                    st.session_state[cb_key] = is_resp
                checked = st.checkbox("Mark as responded", key=cb_key)
                if checked != is_resp:
                    set_responded(row, checked)
                    st.rerun()

            # Internal note — collapsed expander; saved on change (blur /
            # Ctrl+Enter). A 📝 label (vs ＋) signals a note is already saved.
            note_key = f"note_{idx}"
            if note_key not in st.session_state:
                st.session_state[note_key] = existing_note
            note_label = "📝 Internal note" if existing_note else "＋ Internal note"
            with st.expander(note_label, expanded=False):
                st.text_area(
                    "Internal note", key=note_key, height=68,
                    label_visibility="collapsed",
                    placeholder="Internal note (visible only here)…",
                    on_change=_save_note_cb, args=(row, note_key),
                )

            if reply_key in st.session_state:
                val = st.session_state[reply_key]
                with st.expander("Suggested reply", expanded=True):
                    if val.startswith("__error__"):
                        st.error(val[len("__error__"):])
                    else:
                        st.write(val)
                        st.caption(f"Drafted by {REPLY_MODEL} · review and edit before posting.")

# ===========================================================================
# TAB 2 — ANALYTICS
# ===========================================================================

elif active_tab == "📊 Analytics":
    AN_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    an_period = st.radio(
        "Comparison period", list(AN_PERIODS), index=0, horizontal=True, key="an_period"
    )
    n_days = AN_PERIODS[an_period]
    cur = window(bdf, n_days, offset=0)
    prev = window(bdf, n_days, offset=n_days)
    st.caption(
        f"Comparing the last {an_period} against the {an_period} before it "
        f"(platform/tour filters applied; all ratings)."
    )

    def _avg(d):
        return d["rating"].mean() if len(d) else float("nan")

    def _pct5(d):
        return (d["rating"] >= 5).mean() * 100 if len(d) else float("nan")

    def _low(d):
        return int((d["rating"] <= LOW_MAX).sum())

    cur_avg, prev_avg = _avg(cur), _avg(prev)
    cur_p5, prev_p5 = _pct5(cur), _pct5(prev)

    k = st.columns(4)
    kpi_card(
        k[0], "Average rating",
        f"{cur_avg:.2f}" if pd.notna(cur_avg) else "—",
        delta_html(None if (pd.isna(cur_avg) or pd.isna(prev_avg)) else cur_avg - prev_avg),
        PALETTE[2],
    )
    kpi_card(
        k[1], "Reviews", f"{len(cur):,}",
        delta_html(len(cur) - len(prev), fmt="{:+d}"),
        PALETTE[1],
    )
    kpi_card(
        k[2], "5-star share",
        f"{cur_p5:.0f}%" if pd.notna(cur_p5) else "—",
        delta_html(None if (pd.isna(cur_p5) or pd.isna(prev_p5)) else cur_p5 - prev_p5,
                   fmt="{:+.0f} pts"),
        PALETTE[3],
    )
    kpi_card(
        k[3], "Low reviews (1-2★)", f"{_low(cur):,}",
        delta_html(_low(cur) - _low(prev), higher_is_good=False, fmt="{:+d}"),
        PALETTE[0],
    )

    st.divider()

    # Volume + average rating over time, with granularity toggle.
    st.subheader("Volume & average rating over time")
    gran = st.radio("Granularity", ["Weekly", "Monthly", "Yearly"], index=0,
                    horizontal=True, key="an_gran")
    freq = {"Weekly": "W", "Monthly": "ME", "Yearly": "YE"}[gran]
    grp = bdf.set_index("review_date").groupby(pd.Grouper(freq=freq))
    vol = grp.size()
    avg = grp["rating"].mean()
    fig_t = go.Figure()
    fig_t.add_bar(x=vol.index, y=vol.values, name="Reviews",
                  marker_color=PALETTE[1], opacity=0.55,
                  hovertemplate="%{x|%Y-%m-%d}<br>%{y} reviews<extra></extra>")
    fig_t.add_scatter(x=avg.index, y=avg.values, name="Avg rating", yaxis="y2",
                      mode="lines+markers", line=dict(color=PALETTE[3], width=3),
                      hovertemplate="%{x|%Y-%m-%d}<br>avg %{y:.2f}<extra></extra>")
    fig_t.update_layout(
        height=360,
        yaxis=dict(title="Reviews"),
        yaxis2=dict(title="Avg rating", overlaying="y", side="right", range=[0, 5.2]),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        **CHART_LAYOUT,
    )
    st.plotly_chart(fig_t, config=PLOTLY_CONFIG)

    cc1, cc2 = st.columns(2)
    with cc1:
        st.subheader("Average rating by platform")
        by_plat = (
            bdf.groupby("platform_label")["rating"].agg(["mean", "count"])
            .reset_index().sort_values("mean")
        )
        bar_colors = [
            PLATFORMS.get(next((kk for kk, v in PLATFORMS.items() if v["label"] == lbl), ""), {})
            .get("color", "#888")
            for lbl in by_plat["platform_label"]
        ]
        fig_p = go.Figure(go.Bar(
            x=by_plat["mean"], y=by_plat["platform_label"], orientation="h",
            marker_color=bar_colors,
            text=[f"{m:.2f} ({n})" for m, n in zip(by_plat["mean"], by_plat["count"])],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Avg %{x:.2f}<extra></extra>",
        ))
        fig_p.update_layout(height=300, xaxis=dict(range=[0, 5.4], title="Average rating"), **CHART_LAYOUT)
        st.plotly_chart(fig_p, config=PLOTLY_CONFIG)

    with cc2:
        st.subheader("Rating distribution")
        dist = bdf["rating"].dropna().value_counts().sort_index()
        fig_h = go.Figure(go.Bar(
            x=dist.index.astype(str), y=dist.values, marker_color=PALETTE[4],
            hovertemplate="%{x}★ — %{y} reviews<extra></extra>",
        ))
        fig_h.update_layout(height=300, xaxis=dict(title="Rating"), yaxis=dict(title="Reviews"), **CHART_LAYOUT)
        st.plotly_chart(fig_h, config=PLOTLY_CONFIG)

    st.divider()

    # ---- Analyze with Claude -------------------------------------------------
    st.subheader("🔍 Analyze with Claude")
    if client_err:
        st.caption(f"Disabled — {client_err}")

    gen_col, tour_col = st.columns(2)

    with gen_col:
        st.markdown("**General analysis** — all reviews in the current scope.")
        GKEY = "analysis_general"
        if st.button("Analyze all reviews", key="an_general", disabled=client is None):
            with st.expander("Claude analysis", expanded=True):
                try:
                    scope = f"{len(bdf)} reviews across {bdf['platform'].nunique()} platforms"
                    full = st.write_stream(stream_analysis(build_digest(bdf, scope)))
                    st.session_state[GKEY] = full
                except Exception as exc:
                    st.session_state[GKEY] = f"__error__{exc}"
                    st.error(str(exc))
        elif GKEY in st.session_state:
            with st.expander("Claude analysis", expanded=True):
                v = st.session_state[GKEY]
                st.error(v[len("__error__"):]) if v.startswith("__error__") else st.markdown(v)

    with tour_col:
        st.markdown("**Per-tour analysis** — pick one tour.")
        tour_choice = st.selectbox(
            "Tour", sorted(bdf["tour_name"].unique()), key="an_tour_choice"
        )
        TKEY = f"analysis_tour::{tour_choice}"
        if st.button("Analyze this tour", key="an_tour", disabled=client is None):
            tdf = bdf[bdf["tour_name"] == tour_choice]
            with st.expander(f"Claude analysis — {tour_choice}", expanded=True):
                try:
                    full = st.write_stream(
                        stream_analysis(build_digest(tdf, f"Tour: {tour_choice} ({len(tdf)} reviews)"))
                    )
                    st.session_state[TKEY] = full
                except Exception as exc:
                    st.session_state[TKEY] = f"__error__{exc}"
                    st.error(str(exc))
        elif TKEY in st.session_state:
            with st.expander(f"Claude analysis — {tour_choice}", expanded=True):
                v = st.session_state[TKEY]
                st.error(v[len("__error__"):]) if v.startswith("__error__") else st.markdown(v)

# ===========================================================================
# TAB 3 — HEALTH
# ===========================================================================

elif active_tab == "🩺 Health":
    HEALTH_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "All": None}
    h_period = st.radio(
        "Period", list(HEALTH_PERIODS), index=0, horizontal=True, key="health_period"
    )
    h_days = HEALTH_PERIODS[h_period]
    DROP_THRESHOLD = 0.2  # min avg-rating drop vs the previous equal period to alert

    def _cur_prev(g):
        """Current and previous equal-length windows for the selected period."""
        if h_days is None:                       # "All" → no previous window
            return g, g.iloc[0:0]
        return window(g, h_days, 0), window(g, h_days, h_days)

    # Build per-tour stats over the selected period (vs the prior equal period).
    rows = []
    alerts = []
    for (plat, tour), g in bdf.groupby(["platform", "tour_name"]):
        cur, prev = _cur_prev(g)
        n = len(cur)
        if n == 0:
            continue  # only tours active in the selected period

        avg = cur["rating"].mean()
        prev_avg = prev["rating"].mean() if len(prev) else float("nan")
        trend = (avg - prev_avg) if pd.notna(prev_avg) else None
        below3 = int((cur["rating"] < 3).sum())  # change 2: reviews under 3★
        responded_n = sum(1 for _, r in cur.iterrows() if row_key(r) in responded)
        resp_rate = responded_n / n if n else 0.0
        emoji, label = health_status(avg, n)
        label_full = PLATFORMS.get(plat, {}).get("label", plat.title())

        rows.append({
            "Status": f"{emoji} {label}",
            "Tour": tour,
            "Platform": label_full,
            "Reviews": n,
            "Avg": round(avg, 2),
            "Trend": "—" if trend is None else f"{'▲' if trend >= 0 else '▼'} {abs(trend):.2f}",
            "Below 3★": below3,
            "Response rate": f"{resp_rate*100:.0f}%",
            "_sev": 0 if emoji == "🔴" else 1 if emoji == "🟡" else 2,
            "_avg": avg,
        })

        # Alerts reflect the selected period (change 3). The per-tour warning
        # fires for reviews below 3★ (change 2); averages/status are unchanged.
        if avg < 4.5:
            alerts.append(("🔴", f"**{tour}** ({label_full}): average {avg:.2f} over the selected period (below 4.5)."))
        if below3 >= 1:
            alerts.append(("🟡", f"**{tour}** ({label_full}): {below3} review(s) below 3★ in the selected period."))
        if trend is not None and trend <= -DROP_THRESHOLD:
            alerts.append(("🟡", f"**{tour}** ({label_full}): rating down {abs(trend):.2f} vs the previous period."))

    # SLA alert: unanswered reviews below 5★ (change 1) older than 48h, in period.
    cutoff = TODAY - timedelta(days=2)
    sla = bdf[(bdf["rating"] < 5) & (bdf["review_date"].dt.date <= cutoff)]
    if h_days is not None:
        sla = sla[sla["review_date"].dt.date > (TODAY - timedelta(days=h_days))]
    overdue = [r for _, r in sla.iterrows() if row_key(r) not in responded]
    if overdue:
        alerts.append((
            "⚪",
            f"{len(overdue)} unanswered sub-5★ review(s) older than 48h in the selected "
            f"period — see the Reviews tab.",
        ))

    st.subheader("Alerts")
    if not alerts:
        st.success("✅ No active alerts — all tracked tours look healthy.")
    else:
        order = {"🔴": 0, "🟡": 1, "⚪": 2}
        for level, msg in sorted(alerts, key=lambda a: order[a[0]]):
            {"🔴": st.error, "🟡": st.warning, "⚪": st.info}[level](f"{level} {msg}")

    period_label = "all time" if h_days is None else f"last {h_period}"
    st.subheader(f"Tour health — {period_label}")
    if not rows:
        st.info("No reviews in the selected period for the selected platforms/tours.")
    else:
        hdf = pd.DataFrame(rows).sort_values(["_sev", "_avg"]).drop(columns=["_sev", "_avg"])
        st.dataframe(hdf, width="stretch", hide_index=True)
        st.caption(
            "Status from the period average: 🟢 4.8–5.0 · 🟡 4.5–4.7 · 🔴 below 4.5. "
            "“Below 3★” counts every review under 3 stars; trend compares against the "
            "previous equal period."
        )

# ===========================================================================
# TAB 4 — GUIDES
# ===========================================================================

else:  # 🧑‍🏫 Guides
    GUIDE_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "All": None}
    g_period = st.radio(
        "Period", list(GUIDE_PERIODS), index=2, horizontal=True, key="guide_period"
    )
    g_days = GUIDE_PERIODS[g_period]

    # Only reviews that matched a guide (via TourDash bookings) are in scope here.
    gdf = bdf[bdf["guide"].notna() & (bdf["guide"].astype(str) != "")].copy()

    if gdf.empty:
        st.info(
            "No reviews are matched to a guide yet. Guides come from "
            "`data/bookings.csv` (the TourDash pull); a review is attributed when "
            "its tour name fuzzy-matches a booking within ±1 day. Run "
            "`scrapers/tourdash_scraper.py` and check the date overlap if this "
            "stays empty."
        )
    else:
        def _cur_prev_g(g):
            """Current and previous equal-length windows for the selected period."""
            if g_days is None:                       # "All" → no previous window
                return g, g.iloc[0:0]
            return window(g, g_days, 0), window(g, g_days, g_days)

        # Per-guide stats over the selected period (vs the prior equal period).
        rows = []
        guide_alerts = []
        for guide, g in gdf.groupby("guide"):
            cur, prev = _cur_prev_g(g)
            n = len(cur)
            if n == 0:
                continue  # only guides active in the selected period

            avg = cur["rating"].mean()
            prev_avg = prev["rating"].mean() if len(prev) else float("nan")
            trend = (avg - prev_avg) if pd.notna(prev_avg) else None
            below5 = int((cur["rating"] < 5).sum())
            below4 = int((cur["rating"] < 4).sum())
            below3 = int((cur["rating"] < 3).sum())
            emoji, _label = health_status(avg, n)

            rows.append({
                "Status": emoji,
                "Guide": guide,
                "Reviews": n,
                "Avg": round(avg, 2),
                "Below 5★": below5,
                "Below 3★": below3,
                "Trend": "—" if trend is None
                         else f"{'▲' if trend >= 0 else '▼'} {abs(trend):.2f}",
                "_sev": 0 if emoji == "🔴" else 1 if emoji == "🟡" else 2,
                "_avg": avg,
            })

            # Alerts use the SAME thresholds/colours as the health table:
            # 🔴 avg<4.5 → st.error, 🟡 4.5–4.7 → st.warning. A guide with any
            # review below 4★ also surfaces (as 🟡 if its average is otherwise
            # healthy), so weak individual reviews aren't hidden by a good mean.
            low_detail = f" ({below4} review(s) below 4★)" if below4 else ""
            if emoji == "🔴":
                guide_alerts.append((
                    "🔴",
                    f"**{guide}**: average {avg:.2f} over {n} reviews (below 4.5)"
                    f"{low_detail}.",
                ))
            elif emoji == "🟡" or below4 > 0:
                guide_alerts.append((
                    "🟡",
                    f"**{guide}**: average {avg:.2f} over {n} reviews{low_detail}.",
                ))

        st.subheader("Guide alerts")
        if not guide_alerts:
            st.success("✅ No guide alerts — every active guide is 🟢 and has no review below 4★.")
        else:
            order = {"🔴": 0, "🟡": 1}
            for level, msg in sorted(guide_alerts, key=lambda a: order[a[0]]):
                {"🔴": st.error, "🟡": st.warning}[level](f"{level} {msg}")

        period_label = "all time" if g_days is None else f"last {g_period}"
        st.subheader(f"Guide health — {period_label}")
        if not rows:
            st.info("No guide-matched reviews in the selected period.")
        else:
            # KPI summary of the health-table columns for the selected period.
            total_reviews = sum(r["Reviews"] for r in rows)
            total_b5 = sum(r["Below 5★"] for r in rows)
            total_b3 = sum(r["Below 3★"] for r in rows)
            n_red = sum(1 for r in rows if r["_sev"] == 0)
            n_yellow = sum(1 for r in rows if r["_sev"] == 1)
            wavg = (sum(r["Avg"] * r["Reviews"] for r in rows) / total_reviews
                    if total_reviews else float("nan"))

            kc = st.columns(6)
            kpi_card(kc[0], "Matched reviews", f"{total_reviews:,}",
                     f"across {len(rows)} guides", PALETTE[1])
            kpi_card(kc[1], "Weighted avg",
                     f"{wavg:.2f}" if pd.notna(wavg) else "—",
                     "by review count", PALETTE[2])
            kpi_card(kc[2], "Below 5★", f"{total_b5:,}", "all guides", PALETTE[3])
            kpi_card(kc[3], "Below 3★", f"{total_b3:,}", "all guides", "#E63946")
            kpi_card(kc[4], "In alert 🔴", f"{n_red}", "avg below 4.5", "#E63946")
            kpi_card(kc[5], "Attention 🟡", f"{n_yellow}", "avg 4.5–4.7", "#E9C46A")

            gh = pd.DataFrame(rows).sort_values(["_sev", "_avg"]).drop(columns=["_sev", "_avg"])
            st.dataframe(gh, width="stretch", hide_index=True)
            st.caption(
                "One row per guide with reviews in the period. Status from the period "
                "average: 🟢 4.8–5.0 · 🟡 4.5–4.7 · 🔴 below 4.5. Trend compares against "
                "the previous equal period."
            )

        st.divider()

        # ---- Per-guide review feed + Claude analysis ------------------------
        st.subheader("Per-guide reviews")
        guide_names = sorted(gdf["guide"].unique())
        sel_guide = st.selectbox("Guide", guide_names, key="guide_feed_select")

        gsel = gdf[gdf["guide"] == sel_guide]
        if g_days is not None:
            gsel = gsel[gsel["display_date"].dt.date > (TODAY - timedelta(days=g_days))]
        gsel = gsel.sort_values("display_date", ascending=False)

        n_sel = len(gsel)
        avg_sel = gsel["rating"].mean() if n_sel else float("nan")
        st.caption(
            f"{n_sel} matched review(s) for **{sel_guide}** · period {g_period}"
            + (f" · avg {avg_sel:.2f}" if pd.notna(avg_sel) else "")
        )

        # Analyze this guide with Claude (recurring complaints / praise / patterns).
        AKEY = f"analysis_guide::{sel_guide}"
        if st.button("🔍 Analyze this guide", key="guide_analyze", disabled=client is None):
            with st.expander(f"Claude analysis — {sel_guide}", expanded=True):
                try:
                    content = (
                        "You are analysing the reviews for a single Discover Walks tour "
                        f"guide, {sel_guide}. Identify recurring complaints, recurring "
                        "praise, and behavioural patterns specific to this guide, and "
                        "flag anything that needs a manager's attention.\n\n"
                        + build_digest(gsel, f"Guide {sel_guide} ({n_sel} reviews)")
                    )
                    full = st.write_stream(stream_analysis(content))
                    st.session_state[AKEY] = full
                except Exception as exc:
                    st.session_state[AKEY] = f"__error__{exc}"
                    st.error(str(exc))
        elif AKEY in st.session_state:
            with st.expander(f"Claude analysis — {sel_guide}", expanded=True):
                v = st.session_state[AKEY]
                st.error(v[len("__error__"):]) if v.startswith("__error__") else st.markdown(v)

        # All known guides (from bookings + anything already attributed), for the
        # manual-reassignment selectbox.
        known_guides = sorted(
            g for g in set(load_bookings()["guide"]).union(df["guide"].dropna())
            if str(g).strip()
        )

        # Feed pagination — same [50/75/100/150/All] control as the Reviews tab.
        g_show_n = st.columns([3, 1])[1].selectbox(
            "Show", [50, 75, 100, 150, "All"], index=0,
            label_visibility="collapsed", key="guide_show_n",
        )
        gsel_shown = gsel if g_show_n == "All" else gsel.head(int(g_show_n))

        if n_sel == 0:
            st.info("No reviews for this guide in the selected period.")
        for rid, row in gsel_shown.iterrows():
            rating = row["rating"]
            low = pd.notna(rating) and rating < 3
            date_str = row["display_date"].strftime("%d %b %Y")
            name = row["reviewer_name"] or "Anonymous"
            text = row["review_text"] or "<em>(no comment)</em>"
            method = row.get("match_method") or "—"
            st.markdown(
                f'<div class="review-card {"low" if low else ""}">'
                f'<div class="rc-head">'
                f'{platform_badge(row["platform"], row["platform_label"])} '
                f'&nbsp;<span class="rc-stars">{stars(rating)}</span> '
                f'&nbsp;<b>{name}</b> &nbsp;·&nbsp; {date_str}</div>'
                f'<div class="rc-tour">{row["tour_name"]}</div>'
                f'<div class="rc-text">{text}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Manual guide reassignment (writes an override that beats matching).
            cur_guide = row["guide"]
            opts = ["None"] + known_guides
            if cur_guide and cur_guide not in opts:
                opts = ["None", cur_guide] + known_guides
            default_idx = opts.index(cur_guide) if cur_guide in opts else 0
            with st.popover("Reassign guide"):
                st.caption(f"Currently **{cur_guide or 'None'}** (via {method}).")
                new_guide = st.selectbox(
                    "Attributed guide", opts, index=default_idx, key=f"ovr_sel_{rid}",
                )
                if st.button("Save override", key=f"ovr_save_{rid}"):
                    save_guide_override(row, new_guide)
                    st.rerun()
