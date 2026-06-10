"""
Viator review scraper -- NOT YET IMPLEMENTED.

Will scrape reviews from Discover Walks product pages on viator.com via
base_scraper utilities, with platform="viator".

Notes for implementation: Viator (TripAdvisor-owned) shares much of
TripAdvisor's bot protection -- see the cautions in tripadvisor_scraper.py.
Review pagination uses a "Show more reviews" button rather than URL paging.

Run standalone once implemented:
    python scrapers/viator_scraper.py
"""
