"""
Streamlit review dashboard -- NOT YET IMPLEMENTED.

Will load data/reviews.csv and show, per platform and per tour:
- review volume over time
- rating distribution and rolling average rating
- latest reviews feed (filterable by platform, tour, rating)
- platform comparison (avg rating, review count)

Follow the conventions of the rankings dashboard
(~/freetour-tracker/dashboard.py): plotly graph_objects, st.cache_data with a
TTL for data loading, sidebar filters for platform and date range.

Run:
    streamlit run dashboard/app.py
"""
