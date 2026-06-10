"""
GetYourGuide review scraper -- NOT YET IMPLEMENTED.

Will scrape reviews from Discover Walks activity pages on getyourguide.com
via base_scraper utilities, with platform="getyourguide".

Notes for implementation: GYG renders reviews client-side and paginates them;
the review section often loads through an XHR that can be observed in
DevTools -- intercepting that JSON response via page.on("response") may be
more robust than DOM scraping. No paid API use.

Run standalone once implemented:
    python scrapers/getyourguide_scraper.py
"""
