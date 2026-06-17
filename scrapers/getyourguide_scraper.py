"""
GetYourGuide review scraper for Discover Walks activities.

Strategy (verified against the live site on 2026-06-17):
1. Load the Discover Walks supplier page (SUPPLIER_URL) and collect activity
   cards: anchors whose href matches ``-t<digits>/``; the card title lives in
   a ``[class*="title"]`` element inside the anchor. The trailing ``t<digits>``
   is the numeric activityId used by the review API.
2. Open each activity page and drive GYG's own "activity-details-page/blocks"
   endpoint *from inside the page* (an in-page fetch inherits the Cloudflare
   clearance AND the app's required headers; plain HTTP gets 403, and the
   endpoint 400s without the ``visitor-platform`` / ``visitor-id`` headers the
   app sets). The reviews widget is a server-driven block tree:

     POST https://travelers-api.getyourguide.com/user-interface
          /activity-details-page/blocks

   - Seed request: contentIdentifier "paginated-reviews-with-filters" with an
     ``events`` entry carrying ``sortingOverride: "date_desc"`` -> returns the
     first 10 reviews (newest first) plus a ``loadMore`` block.
   - Each ``loadMore`` block carries the exact payload for the next page
     (contentIdentifier "next-reviews-page", ``reviewsOffset``/``reviewsLimit``).
     We echo it back, overriding ``reviewsLimit`` to 30 to cut the request
     count (the site's own default is 3), until no ``loadMore`` block remains.
   - A review lives in a block of ``type: "review"`` with: ``reviewId``,
     ``rating`` (1-5), ``message.text`` (often empty -- rating-only reviews
     are common), ``author.title.text`` ("<first name> – <country>" when the
     reviewer is public, else "Voyageur·se GetYourGuide – <country>"), and a
     clean ISO timestamp in ``onImpressionTrackingEvent.properties.review_date``.
3. Save new reviews via base_scraper.save_review() with platform="getyourguide".

Bot protection (Cloudflare):
- Headless Chromium AND headless real Chrome get 403 on every page.
- *Headed* real Chrome (channel="chrome" + --disable-blink-features=
  AutomationControlled) passes. This scraper therefore runs headed and needs
  a display -- it is NOT in the CI workflow; run it from a desktop machine.

Limitations:
- The endpoint hard-caps pagination at offset 300, so at most ~300 of the most
  recent reviews per activity are reachable (Marais has 503 total; we get the
  newest ~310). Sorting date_desc means new reviews always land in that window,
  so daily runs keep accumulating history; only the pre-cap backlog is lost.
- Reviewer names carry a first name only when public, otherwise just country
  ("Voyageur·se GetYourGuide – <country>"). Because the dedup key is
  (platform, tour_name, reviewer_name, review_date) at day granularity, two
  anonymous reviewers from the same country reviewing the same tour on the same
  day collide (same class of caveat as guruwalk's month-granularity dates).
  Rare in practice -- ~1.4% of rows on the first full run.
- Pages are scraped in en-US so tour_name is the English title (the dashboard
  is English-facing). The reviews themselves stay in each reviewer's original
  language. tour_name is stable as long as the locale is pinned -- changing the
  locale changes the titles and will create new dedup keys.

Run standalone:
    python scrapers/getyourguide_scraper.py
"""

import asyncio
import re
import sys
from pathlib import Path

# Allow "python scrapers/getyourguide_scraper.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from base_scraper import (
    existing_keys,
    load_existing_reviews,
    polite_delay,
    retry,
    save_review,
)

SUPPLIER_URL = "https://www.getyourguide.com/discover-walks-s2584/"
PLATFORM = "getyourguide"
PARIS_LAT, PARIS_LON = 48.8566, 2.3522

BLOCKS_ENDPOINT = (
    "https://travelers-api.getyourguide.com/user-interface/activity-details-page/blocks"
)
PAGE_LIMIT = 30   # override the site's default loadMore page size of 3
MAX_PAGES = 40    # safety cap (offset is server-capped at 300)

# Headers the GYG app attaches to the blocks POST that the plain request lacks
# (visitor-platform / visitor-id, etc.). We don't hardcode them: a request
# handler scrapes them off the page's own blocks POST and we forward the lot,
# minus the few hop-by-hop / auto-managed ones.
_SKIP_HEADERS = {"host", "content-length", "connection", "cookie"}

COLLECT_ACTIVITIES_JS = """
() => {
  const seen = new Set();
  return Array.from(document.querySelectorAll('a[href]'))
    .filter(a => /-t\\d+\\/?(\\?|$)/.test(a.href))
    .map(a => ({
      url: a.href.split('?')[0],
      title: a.querySelector('h1,h2,h3,h4,[class*="title" i]')?.innerText?.trim()
             || (a.innerText || '').trim().split('\\n')[0],
    }))
    .filter(x => x.title && !seen.has(x.url) && seen.add(x.url));
}
"""

# Runs in the page so the POST rides the Cloudflare-cleared session.
POST_BLOCKS_JS = """
async ({ endpoint, headers, body }) => {
  const r = await fetch(endpoint, {
    method: 'POST',
    headers: Object.assign({ 'content-type': 'application/json' }, headers),
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`blocks ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return r.json();
}
"""


def _activity_id(url: str):
    """'.../paris-le-marais-...-t629577/' -> 629577 (None if no match)."""
    m = re.search(r"-t(\d+)/?$", url)
    return int(m.group(1)) if m else None


def _seed_body(activity_id: int) -> dict:
    """First reviews page, sorted newest-first."""
    return {
        "events": [
            {
                "event": {
                    "type": "reviewsFiltersSelected",
                    "emitterId": "paginated-reviews-with-filters",
                    "payload": {
                        "categories": [],
                        "ratings": [],
                        "sortingOverride": "date_desc",
                    },
                }
            }
        ],
        "payload": {
            "activityId": activity_id,
            "templateName": "ActivityDetails",
            "contentIdentifier": "paginated-reviews-with-filters",
            "additionalDetailsSelectedLanguage": "en-US",
            "participantsLanguage": "en-US",
            "hasButtonBeenClickedWithValidForm": False,
            "reviewsExperiments": [],
        },
    }


def _collect(node, reviews: dict, loadmores: list) -> None:
    """Walk the block tree, harvesting review blocks (by id) and loadMore payloads."""
    if isinstance(node, dict):
        if node.get("type") == "review" and node.get("reviewId"):
            props = (node.get("onImpressionTrackingEvent") or {}).get("properties", {})
            author = (node.get("author") or {}).get("title") or {}
            reviews[node["reviewId"]] = {
                "rating": node.get("rating"),
                "name": (author.get("text") or "").strip() or "anonymous",
                "date": (props.get("review_date") or "")[:10],
                "text": " ".join((node.get("message") or {}).get("text", "").split()),
            }
        if node.get("type") == "loadMore" and node.get("payload"):
            loadmores.append(node["payload"])
        for v in node.values():
            _collect(v, reviews, loadmores)
    elif isinstance(node, list):
        for v in node:
            _collect(v, reviews, loadmores)


async def find_activities(page) -> list:
    """Return [(title, url, activity_id), ...] from the supplier page."""
    print(f"Loading {SUPPLIER_URL} ...")
    await retry(lambda: page.goto(SUPPLIER_URL, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(5_000)

    if "Error" in (await page.title()):
        raise RuntimeError(
            "Supplier page served the Cloudflare error page -- "
            "fingerprint or IP is being blocked."
        )

    cards = await page.evaluate(COLLECT_ACTIVITIES_JS)
    activities = []
    for c in cards:
        aid = _activity_id(c["url"])
        if aid is None:
            continue
        activities.append((c["title"], c["url"], aid))
        print(f"  Found activity: {c['title']} (t{aid})")
    return activities


async def scrape_activity_reviews(page, headers: dict, title: str, url: str, activity_id: int) -> list:
    """Paginate the full review history for one activity via the blocks endpoint."""
    print(f"\nScraping reviews: {title}")
    print(f"  {url}")
    await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(3_000)

    # Scroll so the app fires its own blocks POST -- the request handler in
    # scrape() harvests the headers we need to forward.
    for _ in range(8):
        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(500)
    for _ in range(20):
        if headers:
            break
        await page.wait_for_timeout(500)
    if not headers:
        print("  Never observed a blocks POST -- cannot derive headers; skipping.")
        return []

    reviews, body = {}, _seed_body(activity_id)
    for _ in range(MAX_PAGES):
        data = await retry(
            lambda: page.evaluate(
                POST_BLOCKS_JS, {"endpoint": BLOCKS_ENDPOINT, "headers": headers, "body": body}
            )
        )
        before = len(reviews)
        loadmores = []
        _collect(data, reviews, loadmores)
        if not loadmores:
            break
        if len(reviews) == before:  # no new ids -> server has nothing more
            break
        payload = loadmores[0]
        payload["reviewsLimit"] = PAGE_LIMIT
        body = {"payload": payload}
        await polite_delay(0.4, 0.9)
    print(f"  Reviews fetched: {len(reviews)}")

    out = []
    for rid, r in reviews.items():
        rating = r["rating"]
        out.append(
            {
                "platform": PLATFORM,
                "tour_name": title,
                "rating": f"{rating:g}" if rating is not None else "",
                "reviewer_name": r["name"],
                "review_text": r["text"],
                "review_date": r["date"],
                "url": url,
            }
        )
    print(f"  Parsed {len(out)} reviews.")
    return out


async def scrape() -> list:
    """Scrape reviews for every Discover Walks activity on getyourguide.com."""
    all_reviews = []
    forward_headers: dict = {}

    async def on_request(req):
        # Capture the forwardable header set from the FIRST blocks POST the
        # page's app fires, then stop. Capturing once (not refreshing on every
        # POST) is load-bearing: later app POSTs -- and the scraper's own
        # in-page pagination fetches -- carry a thinner header set that makes
        # the reviews endpoint ignore the offset and only return the ~10
        # highlighted reviews, cutting deep pagination short. The headers we
        # need (visitor-id, visitor-platform, x-gyg-*) are session-global, so
        # the first capture is valid for every activity.
        if "activity-details-page/blocks" not in req.url or req.method != "POST":
            return
        if forward_headers:
            return
        try:
            hdrs = await req.all_headers()
        except Exception:
            return
        for k, v in hdrs.items():
            if k.lower() not in _SKIP_HEADERS and not k.startswith(":"):
                forward_headers[k] = v

    async with async_playwright() as pw:
        # Headed real Chrome is required: Cloudflare 403s every headless
        # variant we tried (see module docstring).
        browser = await pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            geolocation={"latitude": PARIS_LAT, "longitude": PARIS_LON},
            permissions=["geolocation"],
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await context.new_page()
        page.on("request", lambda r: asyncio.ensure_future(on_request(r)))

        activities = await find_activities(page)
        print(f"\nDiscover Walks activities found: {len(activities)}")

        for title, url, aid in activities:
            await polite_delay()
            try:
                all_reviews.extend(
                    await scrape_activity_reviews(page, forward_headers, title, url, aid)
                )
            except Exception as exc:
                print(f"  Skipping activity after repeated failures: {exc}")

        await browser.close()
    return all_reviews


async def main() -> None:
    print("=" * 60)
    print("GetYourGuide -- Discover Walks Review Scraper")
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
