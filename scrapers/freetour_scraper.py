"""
Freetour.com review scraper for Discover Walks Paris tours.

Strategy:
1. Load https://www.freetour.com/paris and collect the listing cards
   (same selectors as the proven rankings tracker), clicking "Show more"
   until all 5 Discover Walks tours are found or results run out.
2. Visit each tour page, scroll to trigger lazy loading, and parse the
   review blocks (reviewer, rating, date, text).
3. Save only reviews not already present in data/reviews.csv
   (deduplicated on platform + tour + reviewer + date).

Run standalone:
    python scrapers/freetour_scraper.py

Review-card structure (verified against the live site on 2026-06-10):

    <div class="tour-details__rating review-card">
      <div class="tour-details__rating-stars" style="--rating: 9">  <- /10 scale
      <div class="tour-details__rating-name">by Peter Royal</div>
      <div class="tour-details__rating-date">Reviewed on Jun 06, 2026</div>
      <div class="js-review-text" data-text="...full text...">

Ratings are converted from freetour's 10-point scale to /5 (e.g. 9 -> 4.5)
so the rating column is comparable across platforms.
"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# Allow "python scrapers/freetour_scraper.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from base_scraper import (
    existing_keys,
    load_existing_reviews,
    polite_delay,
    retry,
    save_review,
)

BASE_URL = "https://www.freetour.com/paris"
PLATFORM = "freetour"
PROVIDER_KEYWORD = "discover walks"
EXPECTED_TOUR_COUNT = 5  # Discover Walks currently lists 5 Paris tours

PARIS_LAT = 48.8566
PARIS_LON = 2.3522

# Verified selectors (live site, 2026-06-10).
REVIEW_CONTAINER_SELECTOR = "[class*='review-card']"
REVIEWER_SELECTOR = ".tour-details__rating-name"
DATE_SELECTOR = ".tour-details__rating-date"
TEXT_SELECTOR = ".js-review-text"
TEXT_FALLBACK_SELECTOR = ".text-content"
RATING_STARS_SELECTOR = "[class*='rating-stars']"

_RATING_RE = re.compile(r"--rating:\s*(\d+(?:\.\d+)?)")


async def _extract_reviewer(block) -> str:
    node = await block.query_selector(REVIEWER_SELECTOR)
    if not node:
        return ""
    name = (await node.inner_text()).strip()
    return re.sub(r"^by\s+", "", name, flags=re.IGNORECASE)


async def _extract_date(block) -> str:
    """'Reviewed on Jun 06, 2026' -> '2026-06-06' (raw text if unparseable)."""
    node = await block.query_selector(DATE_SELECTOR)
    if not node:
        return ""
    raw = (await node.inner_text()).strip()
    cleaned = re.sub(r"^Reviewed on\s+", "", raw, flags=re.IGNORECASE)
    try:
        return datetime.strptime(cleaned, "%b %d, %Y").date().isoformat()
    except ValueError:
        return raw


async def _extract_text(block) -> str:
    """Full review text from the data-text attribute (no 'Read more' suffix)."""
    node = await block.query_selector(TEXT_SELECTOR)
    if node:
        full = await node.get_attribute("data-text")
        if full and full.strip():
            return " ".join(full.split())
    node = await block.query_selector(TEXT_FALLBACK_SELECTOR)
    if node:
        txt = (await node.inner_text()).strip()
        return " ".join(re.sub(r"\s*Read more\s*$", "", txt).split())
    return ""


async def _extract_rating(block) -> str:
    """Read the --rating CSS variable (/10) and convert to the /5 scale."""
    node = await block.query_selector(RATING_STARS_SELECTOR)
    if not node:
        return ""
    style = await node.get_attribute("style") or ""
    m = _RATING_RE.search(style)
    if not m:
        return ""
    out_of_5 = float(m.group(1)) / 2
    return f"{out_of_5:g}"  # 4.5 stays 4.5, 5.0 becomes 5


async def _dismiss_gdpr(page) -> None:
    try:
        gdpr_btn = page.locator("#gdpr button").first
        if await gdpr_btn.count() > 0:
            await gdpr_btn.wait_for(state="visible", timeout=3_000)
            await gdpr_btn.click()
            await page.wait_for_timeout(800)
            print("  Dismissed GDPR banner.")
    except Exception:
        pass


async def find_discover_walks_tours(page) -> list:
    """Return [(title, absolute_url), ...] for Discover Walks cards in Paris."""
    print(f"Loading {BASE_URL} ...")
    await retry(lambda: page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000))
    try:
        await page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_500)

    await _dismiss_gdpr(page)

    found: dict = {}  # title -> url, insertion-ordered

    async def collect() -> None:
        cards = await page.query_selector_all(".city-tour.js-city-tour")
        for card in cards:
            title_el = await card.query_selector(".city-tour__title")
            provider_el = await card.query_selector(".city-tour__provider-name")
            title = (await title_el.inner_text()).strip() if title_el else ""
            provider = (await provider_el.inner_text()).strip() if provider_el else ""
            if PROVIDER_KEYWORD not in provider.lower() or title in found:
                continue
            link_el = await card.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else None
            if href:
                found[title] = urljoin(BASE_URL, href)
                print(f"  Found tour: {title}")

    await collect()

    # Click "Show more" until we have all expected tours or run out of results.
    while len(found) < EXPECTED_TOUR_COUNT:
        show_more = page.locator(".filters-button__container .filters-button").first
        if await show_more.count() == 0 or not await show_more.is_visible():
            break
        try:
            await show_more.scroll_into_view_if_needed()
            await show_more.click(force=True)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(1_000)
        except Exception:
            break
        await collect()

    return list(found.items())


async def scrape_tour_reviews(page, title: str, url: str) -> list:
    """Scrape all visible reviews on one tour page."""
    print(f"\nScraping reviews: {title}")
    print(f"  {url}")
    await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(2_000)
    await _dismiss_gdpr(page)

    # Scroll down in steps to trigger lazily loaded review sections.
    for _ in range(6):
        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(600)

    blocks = await page.query_selector_all(REVIEW_CONTAINER_SELECTOR)
    if not blocks:
        print("  No review blocks found -- selectors may need updating for this page.")
        return []
    print(f"  Found {len(blocks)} review blocks.")

    reviews = []
    for block in blocks:
        reviewer = await _extract_reviewer(block)
        review_date = await _extract_date(block)
        text = await _extract_text(block)
        rating = await _extract_rating(block)

        if not reviewer and not text:
            continue  # decorative / empty block

        reviews.append(
            {
                "platform": PLATFORM,
                "tour_name": title,
                "rating": rating,
                "reviewer_name": reviewer,
                "review_text": text,
                "review_date": review_date,
                "url": url,
            }
        )

    print(f"  Parsed {len(reviews)} reviews.")
    return reviews


async def scrape() -> list:
    """Scrape reviews for every Discover Walks tour on freetour.com/paris."""
    all_reviews = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            geolocation={"latitude": PARIS_LAT, "longitude": PARIS_LON},
            permissions=["geolocation"],
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()

        tours = await find_discover_walks_tours(page)
        print(f"\nDiscover Walks tours found: {len(tours)}")

        for title, url in tours:
            await polite_delay()
            try:
                all_reviews.extend(await scrape_tour_reviews(page, title, url))
            except Exception as exc:
                print(f"  Skipping tour after repeated failures: {exc}")

        await browser.close()
    return all_reviews


async def main() -> None:
    print("=" * 60)
    print("Freetour.com -- Discover Walks Review Scraper")
    print("=" * 60)

    existing = load_existing_reviews()
    keys = existing_keys(existing)
    print(f"Existing reviews on file: {len(existing)}")

    reviews = await scrape()

    new_count = sum(1 for r in reviews if save_review(r, keys))
    print("\n--- Summary ---")
    print(f"  Reviews scraped: {len(reviews)}")
    print(f"  New reviews saved: {new_count}")
    print(f"  Duplicates skipped: {len(reviews) - new_count}")
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
