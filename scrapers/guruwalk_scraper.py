"""
GuruWalk review scraper for Charing Cross Paris tours.

On GuruWalk the company operates as "Charing Cross Tours" (its Freetour.com
brand is Discover Walks -- same company, two brands).

Strategy:
1. Load the Paris search results and collect Charing Cross tour links,
   clicking "Load more" until results stop growing (selectors proven by
   the rankings tracker).
2. Visit each tour page and parse the review carousel inside
   [data-testid='reviews'] (verified against the live site on 2026-06-10):

     <div data-testid="reviews">
       <button ...>                                  <- one card per review
         <span class="typography-body-large">Jane</span>
         <span ...>Traveled as couple</span> / "Booking verified"
         <span ...>Jun 2026</span>                   <- month granularity only
         <svg><path d="m12 17.275...">               <- full star
         <svg><path d="M12 7.125v7.8...">            <- half star
         ...review text...

3. Save new reviews via base_scraper.save_review() with platform="guruwalk".

Limitations:
- The carousel exposes only the ~5 most recent reviews per tour (the full
  history sits behind a modal with pagination -- not scraped yet).
- review_date is month granularity (e.g. "2026-06"); GuruWalk doesn't show
  the day. Dedup key (name + month) can collide if the same first name
  reviews the same tour twice in one month.

Run standalone:
    python scrapers/guruwalk_scraper.py
"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

# Allow "python scrapers/guruwalk_scraper.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from base_scraper import (
    existing_keys,
    load_existing_reviews,
    polite_delay,
    retry,
    save_review,
)

SEARCH_URL = "https://www.guruwalk.com/a/search?vertical=free-tour&hub=paris&langs=en"
PLATFORM = "guruwalk"
PROVIDER_KEYWORD = "charing cross"

# JS evaluated on each tour page to extract the review cards. Star rating is
# derived from SVG path shapes: full stars start with "m12 17.275", half
# stars contain "7.125v7.8".
EXTRACT_REVIEWS_JS = """
() => {
  const cards = Array.from(
    document.querySelectorAll("[data-testid='reviews'] button")
  ).filter(b => b.querySelector('span.typography-body-large'));
  return cards.map(c => {
    const name =
      c.querySelector('span.typography-body-large')?.innerText?.trim() || '';
    const spanTexts = Array.from(c.querySelectorAll('span'))
      .map(s => s.innerText.trim());
    const date = spanTexts.find(t => /^[A-Z][a-z]{2,8} \\d{4}$/.test(t)) || '';
    let full = 0, half = 0;
    for (const p of c.querySelectorAll('svg path')) {
      const d = p.getAttribute('d') || '';
      if (d.startsWith('m12 17.275')) full++;
      else if (d.includes('7.125v7.8')) half++;
    }
    const skip = new Set([name, date, 'Booking verified', 'Read more', 'Show less']);
    const text = c.innerText
      .split('\\n')
      .map(l => l.trim())
      .filter(l => l && !skip.has(l) && !/^Traveled/.test(l) && !/^Guided by\\b/.test(l))
      .join(' ');
    return { name, date, full, half, text };
  });
}
"""


def _iso_month(raw: str) -> str:
    """'Jun 2026' -> '2026-06' (raw text if unparseable)."""
    try:
        return datetime.strptime(raw, "%b %Y").strftime("%Y-%m")
    except ValueError:
        return raw


async def find_charing_cross_tours(page) -> list:
    """Return [(title, url), ...] for Charing Cross tours in Paris."""
    print(f"Loading {SEARCH_URL} ...")
    await retry(lambda: page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(4_000)

    # Load all results (button disappears / count stops growing when done).
    while True:
        btn = page.locator('button:has-text("Load more")').first
        if await btn.count() == 0 or not await btn.is_visible():
            break
        prev = await page.evaluate(
            "() => document.querySelectorAll('[class*=\"group/card\"]').length"
        )
        await btn.scroll_into_view_if_needed()
        await btn.click()
        new = prev
        for _ in range(8):
            await page.wait_for_timeout(1_000)
            new = await page.evaluate(
                "() => document.querySelectorAll('[class*=\"group/card\"]').length"
            )
            if new > prev:
                break
        if new == prev:
            break

    tours = await page.evaluate(
        """(keyword) =>
        Array.from(document.querySelectorAll("[class*='group/card']"))
            .map(c => ({
                title: c.querySelector('.line-clamp-2')?.innerText?.trim() || '',
                provider: c.querySelector('.line-clamp-1')?.innerText?.trim() || '',
                href: c.querySelector('a')?.href || '',
            }))
            .filter(x => x.provider.toLowerCase().includes(keyword) && x.href)
        """,
        PROVIDER_KEYWORD,
    )

    seen, result = set(), []
    for t in tours:
        if t["title"] not in seen:
            seen.add(t["title"])
            result.append((t["title"], t["href"]))
            print(f"  Found tour: {t['title']}")
    return result


async def scrape_tour_reviews(page, title: str, url: str) -> list:
    """Scrape the visible review carousel on one tour page."""
    print(f"\nScraping reviews: {title}")
    print(f"  {url}")
    await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(3_000)

    # Scroll down so the lazily rendered reviews section mounts.
    for _ in range(8):
        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(600)

    raw = await page.evaluate(EXTRACT_REVIEWS_JS)
    if not raw:
        print("  No review cards found -- selectors may need updating.")
        return []
    print(f"  Found {len(raw)} review cards.")

    reviews = []
    for r in raw:
        if not r["name"] and not r["text"]:
            continue
        stars = r["full"] + 0.5 * r["half"]
        reviews.append(
            {
                "platform": PLATFORM,
                "tour_name": title,
                "rating": f"{stars:g}" if stars else "",
                "reviewer_name": r["name"],
                "review_text": " ".join(r["text"].split()),
                "review_date": _iso_month(r["date"]),
                "url": url,
            }
        )
    print(f"  Parsed {len(reviews)} reviews.")
    return reviews


async def scrape() -> list:
    """Scrape reviews for every Charing Cross tour on guruwalk.com/paris."""
    all_reviews = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await context.new_page()

        tours = await find_charing_cross_tours(page)
        print(f"\nCharing Cross tours found: {len(tours)}")

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
    print("GuruWalk -- Charing Cross Review Scraper")
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
