"""
GuruWalk review scraper -- NOT YET IMPLEMENTED.

Will scrape reviews for Discover Walks tours on guruwalk.com, following the
same pattern as freetour_scraper.py:

1. Find the provider's tour pages from the Paris search results
   (https://www.guruwalk.com/a/search?vertical=free-tour&hub=paris&langs=en).
2. Visit each tour page and parse review blocks (reviewer, rating, date, text).
3. Save new reviews via base_scraper.save_review() with platform="guruwalk".

Reference: the rankings tracker (~/freetour-tracker/guruwalk_tracker.py) has
working card selectors for the search page ([class*='group/card'],
.line-clamp-2 for title, .line-clamp-1 for provider) and a "Load more"
button loop.

Run standalone once implemented:
    python scrapers/guruwalk_scraper.py
"""
