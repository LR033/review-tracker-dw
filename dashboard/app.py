"""
Streamlit review dashboard for Discover Walks (Paris tour company).

Loads data/reviews.csv (the append-only scraper output) and presents it across
three tabs, with a dark theme and Plotly charts matching the rankings dashboard
(~/freetour-tracker/dashboard.py):

  Tab 1 — Reviews
    Quick period buttons (7d/30d/90d/1y/All), a sort selector
    (newest / lowest / highest), and the per-review feed. Each card can be
    marked "responded" (persisted to data/responses.csv); responded reviews
    get a green badge, unresponded 1-2★ reviews a red "needs reply" badge.
    Every card keeps the "Draft reply with Claude" button.

  Tab 2 — Analytics
    Period-over-period KPI cards (this period vs the previous equal window),
    a volume + average-rating chart with weekly/monthly/yearly toggle, the
    per-platform and rating-distribution charts, and an "Analyze with Claude"
    section (general + per-tour) that streams a summary of themes, complaints,
    praised guides, and trends.

  Tab 3 — Health
    An auto-generated alerts panel and a per-tour health table (last 30 days):
    review count, avg rating, trend vs the previous 30 days, low-review count,
    response rate, and a 🟢/🟡/🔴 status from the last-30-day average.

Filters: platform and tour apply globally; the star-rating filter scopes the
Reviews feed only (so Analytics/Health averages and statuses stay accurate).

The Anthropic API key is read from st.secrets["ANTHROPIC_API_KEY"] and used for
both reply drafting and analysis (model claude-sonnet-4-6). The dashboard runs
fine without it — only the Claude-powered features are disabled.

Run:
    streamlit run dashboard/app.py
"""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REVIEWS_FILE = DATA_DIR / "reviews.csv"
# Overridable so tests don't write to the real responses log.
RESPONSES_FILE = Path(os.environ.get("DW_RESPONSES_CSV", str(DATA_DIR / "responses.csv")))
RESPONSES_COLS = ["platform", "tour_name", "reviewer_name", "review_date", "responded_at"]

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
    df["review_date"] = pd.to_datetime(df["review_date"], errors="coerce")
    df = df.dropna(subset=["review_date"])
    df["platform_label"] = df["platform"].map(
        lambda p: PLATFORMS.get(p, {}).get("label", p.title())
    )
    return df.sort_values("review_date", ascending=False).reset_index(drop=True)


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


def platform_badge(platform: str, label: str) -> str:
    color = PLATFORMS.get(platform, {}).get("color", "#888")
    return (
        f'<span style="background:{color};color:#fff;border-radius:6px;'
        f'padding:2px 9px;font-size:11px;font-weight:600;letter-spacing:.3px;">{label}</span>'
    )


def _pill(text: str, bg: str) -> str:
    return (
        f'<span style="background:{bg};color:#fff;border-radius:6px;'
        f'padding:2px 8px;font-size:11px;font-weight:600;">{text}</span>'
    )


def health_status(avg: float, n: int) -> tuple:
    """(emoji, label) from last-30-day average rating."""
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
    dd = d["review_date"].dt.date
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


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Discover Walks — Reviews", page_icon="🗼", layout="wide")

st.markdown(
    """
    <style>
    .kpi {
        background: #1a1d24; border-radius: 12px; padding: 16px 20px;
        border-left: 5px solid; min-height: 100px;
    }
    .kpi .kpi-label { font-size: 12px; color: #9aa0a6; margin-bottom: 6px; }
    .kpi .kpi-value { font-size: 30px; font-weight: 700; line-height: 1; color: #f1f1f1; }
    .kpi .kpi-sub   { font-size: 11px; color: #9aa0a6; margin-top: 8px; }

    .review-card {
        background: #1a1d24; border-radius: 12px; padding: 14px 18px;
        margin-bottom: 4px; border-left: 4px solid #2A9D8F;
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

DARK = dict(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=0, r=10, t=30, b=0),
)

df = load_reviews()
responded = load_responses()

st.title("🗼 Discover Walks — Review Tracker")
st.caption("Aggregated customer reviews across booking platforms · drafting & analysis powered by Claude.")

if df.empty:
    st.warning("No reviews found in `data/reviews.csv`. Run the scrapers first.")
    st.stop()

client, client_err = get_anthropic_client()

# ---------------------------------------------------------------------------
# Sidebar filters (platform + tour are global; rating scopes the feed)
# ---------------------------------------------------------------------------

st.sidebar.header("Filters")

platform_opts = [p for p in PLATFORMS if p in set(df["platform"])]
platform_opts += [p for p in df["platform"].unique() if p not in platform_opts]
sel_platforms = st.sidebar.multiselect(
    "Platform", options=platform_opts, default=platform_opts,
    format_func=lambda p: PLATFORMS.get(p, {}).get("label", p.title()),
)
if not sel_platforms:
    sel_platforms = platform_opts

tour_opts = sorted(df[df["platform"].isin(sel_platforms)]["tour_name"].unique())
sel_tours = st.sidebar.multiselect("Tour", options=tour_opts, default=tour_opts)
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

tab_reviews, tab_analytics, tab_health = st.tabs(["📋 Reviews", "📊 Analytics", "🩺 Health"])

# ===========================================================================
# TAB 1 — REVIEWS
# ===========================================================================

with tab_reviews:
    PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "All": None}

    c1, c2 = st.columns([2, 1])
    period = c1.radio("Period", list(PERIOD_DAYS), index=3, horizontal=True, key="rev_period")
    sort_order = c2.selectbox(
        "Sort", ["Newest first", "Lowest rated", "Highest rated"], key="rev_sort"
    )

    feed = bdf[bdf["rating"].between(min_rating, max_rating)].copy()
    days = PERIOD_DAYS[period]
    if days is not None:
        feed = feed[feed["review_date"].dt.date > (TODAY - timedelta(days=days))]

    if sort_order == "Newest first":
        feed = feed.sort_values("review_date", ascending=False)
    elif sort_order == "Lowest rated":
        feed = feed.sort_values(["rating", "review_date"], ascending=[True, False])
    else:
        feed = feed.sort_values(["rating", "review_date"], ascending=[False, False])

    total = len(feed)
    head = st.columns([3, 1])
    head[0].caption(f"{total:,} reviews in scope · period {period} · {sort_order.lower()}.")
    show_n = head[1].number_input(
        "Show", min_value=5, max_value=200, value=min(25, max(total, 5)), step=5,
        label_visibility="collapsed",
    )

    if total == 0:
        st.info("No reviews match the current filters and period.")

    for idx, row in feed.head(int(show_n)).iterrows():
        is_low = pd.notna(row["rating"]) and row["rating"] <= LOW_MAX
        rkey = row_key(row)
        is_resp = rkey in responded
        date_str = row["review_date"].strftime("%d %b %Y")
        name = row["reviewer_name"] or "Anonymous"
        text = row["review_text"] or "<em>(rating only — no written review)</em>"

        badge = ""
        if is_resp:
            badge = " &nbsp;" + _pill("✓ Responded", "#2A9D8F")
        elif is_low:
            badge = " &nbsp;" + _pill("⚠ Needs reply", "#E63946")

        st.markdown(
            f'<div class="review-card {"low" if is_low else ""}">'
            f'<div class="rc-head">{platform_badge(row["platform"], row["platform_label"])} '
            f'&nbsp;<span class="rc-stars">{stars(row["rating"])}</span> '
            f'&nbsp;<b>{name}</b> &nbsp;·&nbsp; {date_str}{badge}</div>'
            f'<div class="rc-tour">{row["tour_name"]}</div>'
            f'<div class="rc-text">{text}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        ctrl = st.columns([1, 1, 3])

        # Mark-as-responded checkbox (persists to responses.csv).
        cb_key = f"resp_{idx}"
        if cb_key not in st.session_state:
            st.session_state[cb_key] = is_resp
        checked = ctrl[0].checkbox("✅ Mark as responded", key=cb_key)
        if checked != is_resp:
            set_responded(row, checked)
            st.rerun()

        # Draft reply with Claude.
        reply_key = f"reply_{idx}"
        if ctrl[1].button(
            "✍️ Draft reply", key=f"btn_{idx}", disabled=client is None,
            width="stretch",
        ):
            with st.spinner("Drafting reply…"):
                try:
                    st.session_state[reply_key] = draft_reply(row.to_dict())
                except Exception as exc:
                    st.session_state[reply_key] = f"__error__{exc}"

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

with tab_analytics:
    AN_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    an_period = st.radio(
        "Comparison period", list(AN_PERIODS), index=1, horizontal=True, key="an_period"
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
        **DARK,
    )
    st.plotly_chart(fig_t, width="stretch")

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
        fig_p.update_layout(height=300, xaxis=dict(range=[0, 5.4], title="Average rating"), **DARK)
        st.plotly_chart(fig_p, width="stretch")

    with cc2:
        st.subheader("Rating distribution")
        dist = bdf["rating"].dropna().value_counts().sort_index()
        fig_h = go.Figure(go.Bar(
            x=dist.index.astype(str), y=dist.values, marker_color=PALETTE[4],
            hovertemplate="%{x}★ — %{y} reviews<extra></extra>",
        ))
        fig_h.update_layout(height=300, xaxis=dict(title="Rating"), yaxis=dict(title="Reviews"), **DARK)
        st.plotly_chart(fig_h, width="stretch")

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

with tab_health:
    DROP_THRESHOLD = 0.2  # min avg-rating drop (vs prev 30d) to raise an alert

    # Build per-tour stats over the last 30 days (vs the prior 30 days).
    rows = []
    alerts = []
    for (plat, tour), g in bdf.groupby(["platform", "tour_name"]):
        last30 = window(g, 30, 0)
        prev30 = window(g, 30, 30)
        last7 = window(g, 7, 0)
        n = len(last30)
        if n == 0:
            continue  # "last 30 days" table → only active tours

        avg = last30["rating"].mean()
        prev_avg = prev30["rating"].mean() if len(prev30) else float("nan")
        trend = (avg - prev_avg) if pd.notna(prev_avg) else None
        low30 = int((last30["rating"] <= LOW_MAX).sum())
        low7 = int((last7["rating"] <= LOW_MAX).sum())
        responded_30 = sum(1 for _, r in last30.iterrows() if row_key(r) in responded)
        resp_rate = responded_30 / n if n else 0.0
        emoji, label = health_status(avg, n)
        label_full = PLATFORMS.get(plat, {}).get("label", plat.title())

        rows.append({
            "Status": f"{emoji} {label}",
            "Tour": tour,
            "Platform": label_full,
            "Reviews (30d)": n,
            "Avg (30d)": round(avg, 2),
            "Trend": "—" if trend is None else f"{'▲' if trend >= 0 else '▼'} {abs(trend):.2f}",
            "Low (1-2★)": low30,
            "Response rate": f"{resp_rate*100:.0f}%",
            "_sev": 0 if emoji == "🔴" else 1 if emoji == "🟡" else 2,
            "_avg": avg,
        })

        if avg < 4.5:
            alerts.append(("🔴", f"**{tour}** ({label_full}): average {avg:.2f} over the last 30 days (below 4.5)."))
        if low7 >= 2:
            alerts.append(("🟡", f"**{tour}** ({label_full}): {low7} low reviews (1-2★) in the last 7 days."))
        if trend is not None and trend <= -DROP_THRESHOLD:
            alerts.append(("🟡", f"**{tour}** ({label_full}): rating down {abs(trend):.2f} vs the previous 30 days."))

    # SLA alert: low reviews (last 30d) with no logged response after >48h.
    cutoff = TODAY - timedelta(days=2)
    recent_low = bdf[(bdf["rating"] <= LOW_MAX) & (bdf["review_date"].dt.date <= cutoff)]
    recent_low = recent_low[recent_low["review_date"].dt.date > (TODAY - timedelta(days=30))]
    overdue = [r for _, r in recent_low.iterrows() if row_key(r) not in responded]
    if overdue:
        alerts.append((
            "⚪",
            f"{len(overdue)} low review(s) (1-2★) from the last 30 days have no logged "
            f"response after 48h — see the Reviews tab.",
        ))

    st.subheader("Alerts")
    if not alerts:
        st.success("✅ No active alerts — all tracked tours look healthy.")
    else:
        order = {"🔴": 0, "🟡": 1, "⚪": 2}
        for level, msg in sorted(alerts, key=lambda a: order[a[0]]):
            {"🔴": st.error, "🟡": st.warning, "⚪": st.info}[level](f"{level} {msg}")

    st.subheader("Tour health — last 30 days")
    if not rows:
        st.info("No reviews in the last 30 days for the selected platforms/tours.")
    else:
        hdf = pd.DataFrame(rows).sort_values(["_sev", "_avg"]).drop(columns=["_sev", "_avg"])
        st.dataframe(hdf, width="stretch", hide_index=True)
        st.caption(
            "Status from last-30-day average: 🟢 4.8–5.0 · 🟡 4.5–4.7 · 🔴 below 4.5. "
            "Trend compares the last 30 days with the previous 30."
        )
