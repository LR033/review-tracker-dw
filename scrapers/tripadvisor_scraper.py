"""
TripAdvisor review scraper -- NOT YET IMPLEMENTED.

Will scrape reviews from the Discover Walks Paris attraction/operator pages on
tripadvisor.com via base_scraper utilities, with platform="tripadvisor".

CAUTION: TripAdvisor is aggressively bot-protected (fingerprinting, CAPTCHAs).
Expect to need slower pacing (longer polite_delay bounds), a realistic
user-agent/viewport, and possibly playwright-stealth. Reviews are paginated;
each page must be expanded ("Read more") before extracting full text.

Run standalone once implemented:
    python scrapers/tripadvisor_scraper.py
"""
