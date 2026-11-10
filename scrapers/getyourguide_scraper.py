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
- Pages are scraped in en-US so titles are English (the dashboard is
  English-facing); the reviews stay in each reviewer's original language.
- GYG's display title for an activity is NOT stable across runs (it alternates
  between variants even at en-US, and changes outright when a tour is renamed).
  tour_name is therefore canonicalized by the stable activity ID via
  CANONICAL_TITLES (see the map near the top); the volatile page title is only a
  fallback for unmapped activities. Historical rows were migrated to these
  canonical titles on 2026-09-12.

Run standalone:
    python scrapers/getyourguide_scraper.py
"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

# Allow "python scrapers/getyourguide_scraper.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from base_scraper import (
    _key,
    existing_keys,
    is_duplicate,
    load_existing_reviews,
    polite_delay,
    retry,
    save_review,
)

SUPPLIER_URL = "https://www.getyourguide.com/discover-walks-s2584/"
PLATFORM = "getyourguide"
PARIS_LAT, PARIS_LON = 48.8566, 2.3522

# GetYourGuide serves inconsistent display titles for the same activity between
# runs (e.g. Marais alternates "Paris: Marais without crowds. Guided Tour." and
# "Paris: Marais Guided Tour Without the Crowds"), even pinned to en-US. That
# breaks the (platform, tour_name, reviewer, date) dedup key. The activity ID
# (the trailing t<digits> in the URL) is stable, so we canonicalize tour_name by
# it: whatever variant the page shows, we store the title below. Unknown IDs
# fall back to the scraped title (a new tour just needs adding here). When GYG
# renames a tour we keep its CURRENT title as canonical and migrate old rows.
CANONICAL_TITLES = {
    592477:  "Paris: Montmartre Guided Walking Tour",
    629577:  "Paris: Marais Guided Tour Without the Crowds",
    16624:   "Paris: Flea Market Insider's Tour in an Extra-Small Group",
    691539:  "Notre-Dame to Louvre: Paris' Best in 90 Minutes with a Local",
    1288952: "Paris: Evening at Montmartre with a Local Guide",
    715113:  "Paris for Art Lovers: Montmartre and Renoir’s Gardens",
    782599:  "Places Parisians Love - Classic Treasures and Hidden Gems",
    788155:  "Paris Left Bank: Writers, Revolution, and Black Coffee",
}

# GYG delisted some activities from the supplier page (`/discover-walks-s2584/`)
# so enumeration no longer finds them (2026-11-09: t1288952 + t782599 dropped,
# only 6 of 8 remained). We still want their reviews, so for any canonical ID
# the supplier page omits we fall back to its last-known activity URL (recorded
# from data/reviews.csv). scrape_activity_reviews() verifies the page still
# exists and skips quietly if GYG has fully retired it.
FALLBACK_ACTIVITY_URLS = {
    1288952: "https://www.getyourguide.com/paris-l16/paris-montmartre-at-twilight-tour-with-a-local-guide-t1288952/",
    782599:  "https://www.getyourguide.com/paris-l16/hidden-gems-of-paris-small-group-walking-tour-t782599/",
}

BLOCKS_ENDPOINT = (
    "https://travelers-api.getyourguide.com/user-interface/activity-details-page/blocks"
)
# Bump the site's default loadMore page size of 3 up to 10 -- the server now
# 400s ("Missing payload") for any reviewsLimit above 10 (verified 2026-11-10).
PAGE_LIMIT = 10
MAX_PAGES = 40    # safety cap (offset is server-capped at 300; 40*10 clears it)

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


def _seed_body_payload_only(activity_id: int) -> dict:
    """First reviews page without the ``events`` block.

    The events-based seed (above) 400s mid-session on high-volume tours once the
    page has fired its own reviews POST. This payload-only form -- the same shape
    the server returns for pagination -- is accepted in that state, and still
    pins newest-first via ``selectedReviewsSortingOrder`` (so the newest reviews
    stay inside the offset-300 cap). Used as a fallback when the events seed 400s.
    """
    return {
        "payload": {
            "activityId": activity_id,
            "templateName": "ActivityDetails",
            "contentIdentifier": "paginated-reviews-with-filters",
            "selectedReviewsSortingOrder": "date_desc",
            "selectedReviewsFilters": {"categories": [], "ratings": []},
            "additionalDetailsSelectedLanguage": "en-US",
            "participantsLanguage": "en-US",
            "hasButtonBeenClickedWithValidForm": False,
            "reviewsExperiments": [],
        },
    }


class SilentZeroError(RuntimeError):
    """A tour's page advertises reviews but we parsed none.

    Raised so a parser that silently stops matching (as happened when GYG
    migrated the reviews widget to the SDUI block schema below) can never again
    look like a successful empty scrape -- see FAIL-LOUD handling in main().
    """


# --- New reviews widget schema (verified 2026-11-09) -----------------------
# GYG no longer returns clean ``type:"review"`` blocks. Each review is now a
# tree of generic SDUI atoms whose block ids are namespaced
# ``review-review-list-<reviewId>-...``. We group blocks by that reviewId and
# read the fields back out of the atoms:
#   - rating  = number of FILLED star icons (compact-star coloured
#               ``decorative-guiding-red``; empty stars are ``surface-secondary``)
#   - author  = "<name> – <country>" textAtom (sibling "-0-text" of the date)
#   - date    = "Month DD, YYYY - Verified booking" textAtom -> ISO day
#   - text    = the review-body textAtom (empty for rating-only reviews; GYG
#               puts the full body in both the collapsed and expanded branches)
_REVIEW_ID_RE = re.compile(r"review-review-list-(\d+)")
_DATE_RE = re.compile(r"^[A-Z][a-zÀ-ÿ]+ \d{1,2}, \d{4}")
_STAR_FILLED_COLOR = "decorative-guiding-red"
_BODY_IGNORE = {"Was this helpful?"}


def _iter_blocks(node):
    """Yield every SDUI block dict (those carrying ``__entity == "block"``)."""
    if isinstance(node, dict):
        if node.get("__entity") == "block":
            yield node
        for v in node.values():
            yield from _iter_blocks(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_blocks(v)


def _parse_review_date(text: str) -> str:
    """'November 8, 2026 - Verified booking' -> '2026-11-08' ('' if unparseable).

    Pages are fetched en-US, so month names are always English.
    """
    head = text.split(" - ")[0].strip()
    try:
        return datetime.strptime(head, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_reviews(data) -> dict:
    """Extract ``{reviewId: {rating, name, date, text}}`` from a blocks response.

    Pure function over the parsed JSON so it can be unit-tested against a saved
    fixture. See the schema notes above.
    """
    groups: dict = {}
    for b in _iter_blocks(data):
        m = _REVIEW_ID_RE.search(str(b.get("id", "")))
        if m:
            groups.setdefault(m.group(1), []).append(b)

    reviews: dict = {}
    for rid, blocks in groups.items():
        rating = sum(
            1
            for b in blocks
            if b.get("type") == "iconAtom"
            and "star" in str(b.get("iconName", "")).lower()
            and b.get("color") == _STAR_FILLED_COLOR
        )
        texts = [
            (str(b.get("id", "")), b.get("text", ""))
            for b in blocks
            if b.get("type") == "textAtom" and (b.get("text") or "").strip()
        ]

        # date line (e.g. "November 8, 2026 - Verified booking")
        date_id = date_text = ""
        for bid, t in texts:
            if _DATE_RE.match(t.strip()):
                date_id, date_text = bid, t.strip()
                break

        # author "<name> – <country>" is the sibling "-0-text" of the date's
        # "-1-text"; fall back to the lone "<x> – <y>" atom if ids ever change.
        name = ""
        if date_id.endswith("-1-text"):
            author_id = date_id[: -len("-1-text")] + "-0-text"
            name = next((t for bid, t in texts if bid == author_id), "")
        if not name:
            name = next(
                (t for bid, t in texts if " – " in t and bid != date_id and len(t) <= 80),
                "",
            )
        name = name.strip() or "anonymous"

        # body = the longest remaining atom: excludes the avatar initial (1 char),
        # the author line, the date line and the "Was this helpful?" widget.
        body_candidates = [
            t
            for bid, t in texts
            if bid != date_id and t.strip() != name and t not in _BODY_IGNORE and len(t) > 1
        ]
        text = " ".join(max(body_candidates, key=len).split()) if body_candidates else ""

        reviews[rid] = {
            "rating": rating,
            "name": name,
            "date": _parse_review_date(date_text) if date_text else "",
            "text": text,
        }
    return reviews


def _find_loadmores(data) -> list:
    """Return the loadMore payloads in a blocks response (drives pagination)."""
    return [b["payload"] for b in _iter_blocks(data)
            if b.get("type") == "loadMore" and b.get("payload")]


async def _post_blocks(page, headers: dict, body: dict, attempts: int, base_delay: float):
    """POST one blocks request from inside the page, with retry/backoff."""
    return await retry(
        lambda: page.evaluate(
            POST_BLOCKS_JS, {"endpoint": BLOCKS_ENDPOINT, "headers": headers, "body": body}
        ),
        attempts=attempts,
        base_delay=base_delay,
    )


def _is_retired(exc) -> bool:
    """True when a blocks call 404s because GYG retired the activity."""
    msg = str(exc)
    return "404" in msg and "cannot find activity" in msg


def _blocks_code(exc) -> str:
    """'blocks 400' extracted from an evaluate error, for tidy logging."""
    m = re.search(r"blocks \d+", str(exc))
    return m.group(0) if m else "request failed"


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
        # Canonicalize by stable activity ID; GYG's display title is volatile.
        title = CANONICAL_TITLES.get(aid, c["title"])
        activities.append((title, c["url"], aid))
        extra = "" if aid in CANONICAL_TITLES else "  [unmapped title — add to CANONICAL_TITLES]"
        print(f"  Found activity: {title} (t{aid}){extra}")

    # Add any canonical activity the supplier page has delisted, via its
    # last-known URL. Existence is verified later; a retired page is skipped.
    enumerated = {aid for _, _, aid in activities}
    for aid, url in FALLBACK_ACTIVITY_URLS.items():
        if aid in enumerated or aid not in CANONICAL_TITLES:
            continue
        title = CANONICAL_TITLES[aid]
        activities.append((title, url, aid))
        print(f"  Added delisted activity (will verify page exists): {title} (t{aid})")
    return activities


# Reads the "N reviews" count GYG renders in the activity header, for FAIL-LOUD.
_DOM_COUNT_JS = r"""
() => {
  const m = document.body.innerText.match(/([\d.,]+)\s+reviews?\b/i);
  return m ? parseInt(m[1].replace(/[.,]/g, ''), 10) : null;
}
"""


async def scrape_activity_reviews(page, headers: dict, title: str, url: str, activity_id: int) -> list:
    """Paginate the full review history for one activity via the blocks endpoint.

    Raises SilentZeroError if the page advertises a positive review count but we
    parse none. Returns [] (no error) when GYG has retired the activity page.
    """
    print(f"\nScraping reviews: {title}")
    print(f"  {url}")
    resp = await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
    await page.wait_for_timeout(3_000)
    if (resp is not None and resp.status >= 400) or "Error" in (await page.title()):
        status = resp.status if resp is not None else "?"
        print(f"  Activity page gone (HTTP {status}) -- delisted; skipping.")
        return []

    # Scroll so the app fires its own blocks POST -- the request handler in
    # scrape() harvests the headers we need to forward.
    for _ in range(12):
        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(600)
    for _ in range(20):
        if headers:
            break
        await page.wait_for_timeout(500)
    if not headers:
        print("  Never observed a blocks POST -- cannot derive headers; skipping.")
        return []

    # Small settle after header capture before firing the seed.
    await page.wait_for_timeout(2_500)

    # Seed (first page). The blocks endpoint 400s ("Missing payload")
    # intermittently on either the events seed or the pagination call. The seed
    # MUST land (it is newest-first, so it carries the genuinely-new reviews):
    # try the events form fast, fall back to the payload-only form (also
    # date_desc). A 404 means the activity is retired -> skip. If both forms
    # fail, re-raise so scrape() records it and the run exits non-zero.
    try:
        data = await _post_blocks(page, headers, _seed_body(activity_id), 2, 1.0)
    except Exception as exc:
        if _is_retired(exc):
            print(f"  Blocks endpoint 404 for activity {activity_id} -- retired; skipping.")
            return []
        print(f"  Events seed {_blocks_code(exc)}; falling back to payload-only seed.")
        try:
            data = await _post_blocks(page, headers, _seed_body_payload_only(activity_id), 5, 3.0)
        except Exception as exc2:
            if _is_retired(exc2):
                print(f"  Blocks endpoint 404 for activity {activity_id} -- retired; skipping.")
                return []
            raise

    reviews = dict(parse_reviews(data))
    loadmores = _find_loadmores(data)

    # Paginate the rest best-effort. The next-reviews-page call 400s
    # intermittently too; because the seed is newest-first, stopping early still
    # captures every genuinely-new review, so keep what we have rather than
    # discarding the tour on a mid-pagination failure.
    for _ in range(MAX_PAGES):
        if not loadmores:
            break
        payload = loadmores[0]
        payload["reviewsLimit"] = PAGE_LIMIT
        await polite_delay(0.4, 0.9)
        try:
            data = await _post_blocks(page, headers, {"payload": payload}, 5, 3.0)
        except Exception as exc:
            print(f"  Pagination stopped early at {len(reviews)} reviews ({_blocks_code(exc)}).")
            break
        before = len(reviews)
        reviews.update(parse_reviews(data))
        loadmores = _find_loadmores(data)
        if len(reviews) == before:  # no new ids -> server has nothing more
            break
    print(f"  Reviews fetched: {len(reviews)}")

    # FAIL LOUD: a visible review count with zero parsed means the parser broke
    # (a silent 0 must never look like success -- that is how the SDUI migration
    # went unnoticed). A genuinely review-less or retired page reads count 0/None.
    review_count = None
    try:
        review_count = await page.evaluate(_DOM_COUNT_JS)
    except Exception:
        pass
    if not reviews and review_count:
        raise SilentZeroError(
            f"{title}: page advertises {review_count} reviews but parsed 0 "
            f"(reviews-widget schema likely changed again)"
        )

    out = []
    for r in reviews.values():
        rating = r["rating"]
        out.append(
            {
                "platform": PLATFORM,
                "tour_name": title,
                "rating": f"{rating:g}" if rating else "",
                "reviewer_name": r["name"],
                "review_text": r["text"],
                "review_date": r["date"],
                "url": url,
            }
        )
    print(f"  Parsed {len(out)} reviews.")
    return out


async def scrape() -> tuple:
    """Scrape reviews for every Discover Walks activity on getyourguide.com.

    Returns ``(all_reviews, failures)`` where ``failures`` holds the FAIL-LOUD
    messages for any tour that advertised reviews but parsed none.
    """
    all_reviews = []
    failures: list = []
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
            except SilentZeroError as exc:
                # Record, don't swallow: surfaced as a non-zero exit in main().
                print(f"  FAIL-LOUD: {exc}")
                failures.append(str(exc))
            except Exception as exc:
                # Couldn't scrape this tour at all (persistent request error).
                # Record it too -- a tour we failed to fetch must not let the
                # run look like a clean success.
                print(f"  FAIL-LOUD: {title}: repeated request failures: {exc}")
                failures.append(f"{title}: repeated request failures")

        await browser.close()
    return all_reviews, failures


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    print("=" * 60)
    print("GetYourGuide -- Discover Walks Review Scraper" + ("  [DRY RUN]" if dry_run else ""))
    print("=" * 60)

    existing = load_existing_reviews()
    keys = existing_keys(existing)
    print(f"Existing reviews on file: {len(existing)}")

    reviews, failures = await scrape()

    # Tally parsed + new/duplicate per tour. A dry run computes the split
    # against the existing keys WITHOUT writing anything to reviews.csv.
    per_tour: dict = {}
    new_count = 0
    for r in reviews:
        stats = per_tour.setdefault(r["tour_name"], {"parsed": 0, "new": 0, "dup": 0})
        stats["parsed"] += 1
        if dry_run:
            is_new = not is_duplicate(r, keys)
            if is_new:
                keys.add(_key(r))  # so same-run repeats count as duplicates too
        else:
            is_new = save_review(r, keys)
        if is_new:
            stats["new"] += 1
            new_count += 1
        else:
            stats["dup"] += 1

    print("\n--- Per-tour" + (" (dry run, nothing saved)" if dry_run else "") + " ---")
    print(f"  {'tour':<58}{'parsed':>8}{'new':>6}{'dup':>6}")
    for tour in sorted(per_tour):
        s = per_tour[tour]
        print(f"  {tour[:58]:<58}{s['parsed']:>8}{s['new']:>6}{s['dup']:>6}")

    print("\n--- Summary ---")
    print(f"  Reviews scraped: {len(reviews)}")
    print(f"  {'Would be new' if dry_run else 'New reviews saved'}: {new_count}")
    print(f"  Duplicates skipped: {len(reviews) - new_count}")

    if failures:
        print("\nFAIL: tours advertised reviews but parsed zero:")
        for f in failures:
            print(f"   - {f}")
        print("\nExiting non-zero so this never looks like a successful empty scrape.")
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
