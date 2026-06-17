"""
TripAdvisor review scraper -- NOT YET IMPLEMENTED.

Will scrape reviews from the Discover Walks Paris attraction/operator pages on
tripadvisor.com via base_scraper utilities, with platform="tripadvisor".

CAUTION: TripAdvisor is aggressively bot-protected (fingerprinting, CAPTCHAs).
Expect to need slower pacing (longer polite_delay bounds), a realistic
user-agent/viewport, and possibly playwright-stealth. Reviews are paginated;
each page must be expanded ("Read more") before extracting full text.

RECON RESULT (2026-06-10): hard-blocked from this environment. Plain
Playwright Chromium gets HTTP 403 + empty body on any tripadvisor.com URL,
both headless and headed with --disable-blink-features=AutomationControlled
and a realistic UA/viewport. DuckDuckGo and Bing also serve bot challenges
from this machine, so the block is fingerprint/IP-level (DataDome tier).
Plain Playwright will NOT work here. Realistic options:
  a) Owner export: Discover Walks can export its own reviews from the
     TripAdvisor Management Center; write an importer instead of a scraper.
  b) playwright-stealth (free) -- unverified whether it passes.
  c) Defer until the scraper runs from a different network (e.g. GitHub
     Actions IPs -- also commonly blocked by TA, so test first).

Run standalone once implemented:
    python scrapers/tripadvisor_scraper.py
"""
