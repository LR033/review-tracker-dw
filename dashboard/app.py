"""
Streamlit review dashboard for Discover Walks (Paris tour company).

Loads data/reviews.csv (the append-only output of the platform scrapers) and
presents, with a dark theme and Plotly charts matching the rankings dashboard
(~/freetour-tracker/dashboard.py):

  - Sidebar filters: platform, tour, star rating, date range.
  - KPI cards: total reviews, average rating, % 5-star, reviews this month.
  - Reviews feed: newest first, one card per review, low ratings flagged red.
  - Charts: average rating by platform, weekly review volume, rating histogram.
  - Claude reply drafting: a button on each review drafts a response in the
    Discover Walks brand voice via the Anthropic API (model claude-sonnet-4-6),
    shown in an expander beneath the review.

The Anthropic API key is read from st.secrets["ANTHROPIC_API_KEY"]. The
dashboard runs fine without it -- only the reply-drafting button is disabled.

Run:
    streamlit run dashboard/app.py
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REVIEWS_FILE = Path(__file__).resolve().parent.parent / "data" / "reviews.csv"

# Model is fixed per the product spec; reply drafting uses Sonnet for cost.
REPLY_MODEL = "claude-sonnet-4-6"

# Shared accent palette (same hues as the rankings dashboard).
PALETTE = ["#E63946", "#457B9D", "#2A9D8F", "#E9C46A", "#9B5DE5", "#F15BB5", "#00BBF9"]

# Per-platform display label + badge colour.
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
- Write in the SAME LANGUAGE as the review (French review -> French reply, \
English -> English, etc.).
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
    df["review_date"] = pd.to_datetime(df["review_date"], errors="coerce")
    df = df.dropna(subset=["review_date"])
    df["platform_label"] = df["platform"].map(
        lambda p: PLATFORMS.get(p, {}).get("label", p.title())
    )
    return df.sort_values("review_date", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Anthropic reply drafting
# ---------------------------------------------------------------------------

@st.cache_resource
def get_anthropic_client():
    """Build the Anthropic client from st.secrets, or None if unavailable.

    Returns (client, error_message). Cached so we build it once per session.
    """
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None, 'ANTHROPIC_API_KEY not set in st.secrets — add it to .streamlit/secrets.toml.'
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


# ---------------------------------------------------------------------------
# Small presentation helpers
# ---------------------------------------------------------------------------

def stars(rating: float) -> str:
    """Render a numeric rating as filled/half/empty stars."""
    if pd.isna(rating):
        return "—"
    full = int(rating)
    half = (rating - full) >= 0.5
    return "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0))


def platform_badge(platform: str, label: str) -> str:
    color = PLATFORMS.get(platform, {}).get("color", "#888")
    return (
        f'<span style="background:{color};color:#fff;border-radius:6px;'
        f'padding:2px 9px;font-size:11px;font-weight:600;'
        f'letter-spacing:.3px;">{label}</span>'
    )


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Discover Walks — Reviews", page_icon="🗼", layout="wide")

st.markdown(
    """
    <style>
    .kpi {
        background: #1a1d24;
        border-radius: 12px;
        padding: 16px 20px;
        border-left: 5px solid;
        min-height: 96px;
    }
    .kpi .kpi-label { font-size: 12px; color: #9aa0a6; margin-bottom: 6px; }
    .kpi .kpi-value { font-size: 30px; font-weight: 700; line-height: 1; color: #f1f1f1; }
    .kpi .kpi-sub   { font-size: 11px; color: #9aa0a6; margin-top: 6px; }

    .review-card {
        background: #1a1d24;
        border-radius: 12px;
        padding: 14px 18px;
        margin-bottom: 4px;
        border-left: 4px solid #2A9D8F;
    }
    .review-card.low { border-left-color: #E63946; background: #2a1416; }
    .review-card .rc-head { font-size: 13px; color: #c8ccd0; margin-bottom: 4px; }
    .review-card .rc-stars { font-size: 15px; color: #E9C46A; }
    .review-card.low .rc-stars { color: #ff6b6b; }
    .review-card .rc-tour { color: #9aa0a6; font-size: 12px; margin: 2px 0 6px; }
    .review-card .rc-text { color: #e8e8e8; font-size: 14px; line-height: 1.45; }
    </style>
    """,
    unsafe_allow_html=True,
)

df = load_reviews()

st.title("🗼 Discover Walks — Review Tracker")
st.caption("Aggregated customer reviews across booking platforms · drafted replies powered by Claude.")

if df.empty:
    st.warning("No reviews found in `data/reviews.csv`. Run the scrapers first.")
    st.stop()

# ---------------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------------

st.sidebar.header("Filters")

platform_opts = [p for p in PLATFORMS if p in set(df["platform"])]
platform_opts += [p for p in df["platform"].unique() if p not in platform_opts]
sel_platforms = st.sidebar.multiselect(
    "Platform",
    options=platform_opts,
    default=platform_opts,
    format_func=lambda p: PLATFORMS.get(p, {}).get("label", p.title()),
)
if not sel_platforms:
    sel_platforms = platform_opts  # treat "none selected" as "all"

# Tours depend on the chosen platforms.
tour_opts = sorted(df[df["platform"].isin(sel_platforms)]["tour_name"].unique())
sel_tours = st.sidebar.multiselect("Tour", options=tour_opts, default=tour_opts)
if not sel_tours:
    sel_tours = tour_opts

min_rating, max_rating = st.sidebar.slider(
    "Star rating", min_value=1.0, max_value=5.0, value=(1.0, 5.0), step=0.5
)

min_d = df["review_date"].min().date()
max_d = df["review_date"].max().date()
date_range = st.sidebar.date_input(
    "Date range",
    value=(max(min_d, max_d - timedelta(days=365)), max_d),
    min_value=min_d,
    max_value=max_d,
)
if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start_d, end_d = date_range
else:
    start_d, end_d = min_d, max_d

# ---------------------------------------------------------------------------
# Apply filters
# ---------------------------------------------------------------------------

mask = (
    df["platform"].isin(sel_platforms)
    & df["tour_name"].isin(sel_tours)
    & df["rating"].between(min_rating, max_rating)
    & (df["review_date"].dt.date >= start_d)
    & (df["review_date"].dt.date <= end_d)
)
fdf = df[mask].copy()

if fdf.empty:
    st.info("No reviews match the current filters.")
    st.stop()

# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------

total = len(fdf)
avg_rating = fdf["rating"].mean()
pct_5 = (fdf["rating"] >= 5).mean() * 100 if total else 0
this_month = fdf[
    (fdf["review_date"].dt.year == max_d.year)
    & (fdf["review_date"].dt.month == max_d.month)
]
month_label = max_d.strftime("%B %Y")

kpis = [
    ("Total reviews", f"{total:,}", "matching filters", PALETTE[1]),
    ("Average rating", f"{avg_rating:.2f}" if pd.notna(avg_rating) else "—", "out of 5", PALETTE[2]),
    ("5-star reviews", f"{pct_5:.0f}%", "of filtered reviews", PALETTE[3]),
    ("Reviews this month", f"{len(this_month):,}", month_label, PALETTE[0]),
]
cols = st.columns(4)
for col, (label, value, sub, color) in zip(cols, kpis):
    col.markdown(
        f'<div class="kpi" style="border-color:{color}">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )

st.divider()

# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

DARK = dict(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=0, r=10, t=30, b=0),
)

c1, c2 = st.columns(2)

with c1:
    st.subheader("Average rating by platform")
    by_plat = (
        fdf.groupby("platform_label")["rating"]
        .agg(["mean", "count"])
        .reset_index()
        .sort_values("mean")
    )
    bar_colors = [
        PLATFORMS.get(
            next((k for k, v in PLATFORMS.items() if v["label"] == lbl), ""), {}
        ).get("color", "#888")
        for lbl in by_plat["platform_label"]
    ]
    fig_p = go.Figure(
        go.Bar(
            x=by_plat["mean"],
            y=by_plat["platform_label"],
            orientation="h",
            marker_color=bar_colors,
            text=[f"{m:.2f} ({n})" for m, n in zip(by_plat["mean"], by_plat["count"])],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>Avg %{x:.2f} · %{text}<extra></extra>",
        )
    )
    fig_p.update_layout(height=300, xaxis=dict(range=[0, 5.4], title="Average rating"), **DARK)
    st.plotly_chart(fig_p, use_container_width=True)

with c2:
    st.subheader("Rating distribution")
    dist = fdf["rating"].dropna().value_counts().sort_index()
    fig_h = go.Figure(
        go.Bar(
            x=dist.index.astype(str),
            y=dist.values,
            marker_color=PALETTE[4],
            hovertemplate="%{x}★ — %{y} reviews<extra></extra>",
        )
    )
    fig_h.update_layout(
        height=300, xaxis=dict(title="Rating"), yaxis=dict(title="Reviews"), **DARK
    )
    st.plotly_chart(fig_h, use_container_width=True)

st.subheader("Review volume over time (weekly)")
weekly = (
    fdf.set_index("review_date")
    .groupby(pd.Grouper(freq="W"))
    .size()
    .reset_index(name="count")
)
fig_v = go.Figure(
    go.Scatter(
        x=weekly["review_date"],
        y=weekly["count"],
        mode="lines+markers",
        line=dict(color=PALETTE[6], width=2.5),
        marker=dict(size=5),
        fill="tozeroy",
        fillcolor="rgba(0,187,249,0.12)",
        hovertemplate="Week of %{x|%Y-%m-%d}<br>%{y} reviews<extra></extra>",
    )
)
fig_v.update_layout(height=320, xaxis=dict(title="Week"), yaxis=dict(title="Reviews"), **DARK)
st.plotly_chart(fig_v, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Reviews feed
# ---------------------------------------------------------------------------

st.subheader("Reviews feed")

client, client_err = get_anthropic_client()
if client_err:
    st.caption(f"💬 Claude reply drafting disabled — {client_err}")

head = st.columns([3, 1])
head[0].caption(f"{total:,} reviews match your filters · newest first.")
show_n = head[1].number_input(
    "Show", min_value=5, max_value=200, value=min(25, total), step=5,
    label_visibility="collapsed",
)

feed = fdf.head(int(show_n))

for idx, row in feed.iterrows():
    is_low = pd.notna(row["rating"]) and row["rating"] <= 2
    date_str = row["review_date"].strftime("%d %b %Y")
    name = row["reviewer_name"] or "Anonymous"
    text = row["review_text"] or "<em>(rating only — no written review)</em>"

    st.markdown(
        f'<div class="review-card {"low" if is_low else ""}">'
        f'<div class="rc-head">{platform_badge(row["platform"], row["platform_label"])} '
        f'&nbsp;<span class="rc-stars">{stars(row["rating"])}</span> '
        f'&nbsp;<b>{name}</b> &nbsp;·&nbsp; {date_str}</div>'
        f'<div class="rc-tour">{row["tour_name"]}</div>'
        f'<div class="rc-text">{text}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    reply_key = f"reply_{idx}"
    btn_col, _ = st.columns([1, 4])
    if btn_col.button(
        "✍️ Draft reply with Claude",
        key=f"btn_{idx}",
        disabled=client is None,
        use_container_width=True,
    ):
        with st.spinner("Drafting reply…"):
            try:
                st.session_state[reply_key] = draft_reply(row.to_dict())
            except Exception as exc:  # surface API/SDK errors in the UI
                st.session_state[reply_key] = f"__error__{exc}"

    if reply_key in st.session_state:
        val = st.session_state[reply_key]
        with st.expander("Suggested reply", expanded=True):
            if val.startswith("__error__"):
                st.error(val[len("__error__"):])
            else:
                st.write(val)
                st.caption(f"Drafted by {REPLY_MODEL} · review and edit before posting.")

st.caption(
    f"Showing {min(int(show_n), total):,} of {total:,} filtered reviews · "
    f"data range {min_d} → {max_d}."
)
