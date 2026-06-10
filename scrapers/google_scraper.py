"""
Google reviews scraper -- NOT YET IMPLEMENTED.

Will scrape Google Maps reviews for the Discover Walks Paris business listing
via base_scraper utilities, with platform="google".

Notes for implementation: no paid APIs (Places API is out). That means
driving Google Maps in Playwright: open the place page, click the Reviews
tab, and scroll the reviews panel to lazy-load more. Google's DOM class
names are obfuscated and change frequently -- prefer aria-labels and
data-review-id attributes over class selectors. Consent wall
(consent.google.com) must be dismissed first on EU IPs.

Run standalone once implemented:
    python scrapers/google_scraper.py
"""
