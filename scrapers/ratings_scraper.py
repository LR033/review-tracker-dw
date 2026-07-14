"""
Aggregate-rating scraper for Discover Walks tours.

Unlike the per-review scrapers, this fetches the *official* aggregate star
rating each platform publishes on a tour page (the headline "4.8 out of 5"
figure), one row per platform+tour, and writes it to data/tour_ratings.csv.
The dashboard's "Ratings by platform per tour" table reads this file so it shows
each platform's own published rating rather than an average recomputed from the
subset of reviews we manage to scrape.

Sources (verified against the live sites on 2026-07-14):

- GetYourGuide: the tour page embeds a schema.org Product with an
  ``aggregateRating`` (ratingValue on a /5 scale, e.g. 4.82). Tours are
  enumerated from the Discover Walks supplier page. Cloudflare 403s every
  headless variant, so this runs *headed* real Chrome (same as
  getyourguide_scraper.py) and needs a display — not for CI.

- Freetour: the tour page embeds a schema.org Product/Event with an
  ``aggregateRating`` on a /10 scale (bestRating 10, e.g. 9.5); we normalise
  to /5. Enumerated + scraped headless.

- GuruWalk: no JSON-LD; the reviews section (``[data-testid='reviews']``) leads
  with the aggregate ("4.87\\n1277 reviews") on a /5 scale. On GuruWalk the
  company trades as "Charing Cross Tours". Enumerated + scraped headless.

Ratings are normalised to a /5 scale and rounded to 2 decimals. Output is
upserted by (platform, tour_name): a fresh run replaces a tour's row in place
and leaves other platforms untouched, so one platform failing never wipes the
others' last-known ratings.

Run standalone:
    python scrapers/ratings_scraper.py
    python scrapers/ratings_scraper.py --only freetour,guruwalk   # skip headed GYG
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow "python scrapers/ratings_scraper.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright

from base_scraper import polite_delay, retry
import freetour_scraper as ft
import guruwalk_scraper as gw
import getyourguide_scraper as gyg

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RATINGS_FILE = DATA_DIR / "tour_ratings.csv"
FIELDNAMES = ["platform", "tour_name", "rating", "scraped_at"]


# ---------------------------------------------------------------------------
# Rating extraction
# ---------------------------------------------------------------------------

_JSONLD_JS = (
    '() => Array.from(document.querySelectorAll(\'script[type="application/ld+json"]\'))'
    ".map(s => s.textContent)"
)


def _walk(node):
    """Yield every dict nested anywhere inside a parsed JSON-LD blob."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


async def _jsonld_aggregate(page):
    """Return an aggregate rating normalised to /5 from the page's JSON-LD.

    Looks for any object carrying an ``aggregateRating`` (or an AggregateRating
    object itself), reads ``ratingValue``/``bestRating`` and rescales to /5
    (Freetour publishes on /10, GetYourGuide on /5). Returns a float or None.
    """
    for raw in await page.evaluate(_JSONLD_JS):
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _walk(data):
            agg = obj.get("aggregateRating") if isinstance(obj, dict) else None
            if agg is None and obj.get("@type") == "AggregateRating":
                agg = obj
            if not isinstance(agg, dict):
                continue
            try:
                value = float(agg.get("ratingValue"))
            except (TypeError, ValueError):
                continue
            try:
                best = float(agg.get("bestRating") or 5)
            except (TypeError, ValueError):
                best = 5.0
            if best <= 0:
                best = 5.0
            return round(value / best * 5, 2)
    return None


async def _guruwalk_aggregate(page):
    """Return GuruWalk's headline aggregate (/5) from the reviews section, or None."""
    val = await page.evaluate(
        """() => {
            const rs = document.querySelector("[data-testid='reviews']");
            if (!rs) return null;
            const t = (rs.innerText || '').trim();
            // Header leads with the aggregate, e.g. "4.87\\n1277 reviews".
            const m = t.match(/^\\s*(\\d+(?:\\.\\d+)?)/);
            const hasReviews = /\\breviews?\\b/i.test(t);
            return (m && hasReviews) ? m[1] : null;
        }"""
    )
    try:
        return round(float(val), 2) if val is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-platform scrapers
# ---------------------------------------------------------------------------

async def scrape_freetour() -> list:
    print("\n" + "=" * 60 + "\nFreetour — aggregate ratings\n" + "=" * 60)
    out = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            geolocation={"latitude": ft.PARIS_LAT, "longitude": ft.PARIS_LON},
            permissions=["geolocation"], locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"},
        )
        page = await ctx.new_page()
        tours = await ft.find_discover_walks_tours(page)
        print(f"\nDiscover Walks tours found: {len(tours)}")
        for title, url in tours:
            await polite_delay()
            try:
                await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
                await page.wait_for_timeout(2_500)
                await ft._dismiss_gdpr(page)
                rating = await _jsonld_aggregate(page)
            except Exception as exc:
                print(f"  Skipping {title}: {exc}")
                continue
            if rating is None:
                print(f"  No aggregate rating found: {title}")
                continue
            print(f"  {title}: {rating}")
            out.append({"platform": ft.PLATFORM, "tour_name": title, "rating": rating})
        await browser.close()
    return out


async def scrape_guruwalk() -> list:
    print("\n" + "=" * 60 + "\nGuruWalk — aggregate ratings\n" + "=" * 60)
    out = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()
        tours = await gw.find_charing_cross_tours(page)
        print(f"\nCharing Cross tours found: {len(tours)}")
        for title, url in tours:
            await polite_delay()
            try:
                await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
                await page.wait_for_timeout(2_500)
                for _ in range(6):
                    await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
                    await page.wait_for_timeout(400)
                rating = await _guruwalk_aggregate(page)
            except Exception as exc:
                print(f"  Skipping {title}: {exc}")
                continue
            if rating is None:
                print(f"  No aggregate rating found: {title}")
                continue
            print(f"  {title}: {rating}")
            out.append({"platform": gw.PLATFORM, "tour_name": title, "rating": rating})
        await browser.close()
    return out


async def scrape_getyourguide() -> list:
    print("\n" + "=" * 60 + "\nGetYourGuide — aggregate ratings\n" + "=" * 60)
    out = []
    async with async_playwright() as pw:
        # Headed real Chrome: Cloudflare 403s every headless variant (see
        # getyourguide_scraper.py). Needs a display; not for CI.
        browser = await pw.chromium.launch(
            headless=False, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            geolocation={"latitude": gyg.PARIS_LAT, "longitude": gyg.PARIS_LON},
            permissions=["geolocation"], locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()
        activities = await gyg.find_activities(page)
        print(f"\nDiscover Walks activities found: {len(activities)}")
        for title, url, _aid in activities:
            await polite_delay()
            try:
                await retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=60_000))
                await page.wait_for_timeout(3_000)
                rating = await _jsonld_aggregate(page)
            except Exception as exc:
                print(f"  Skipping {title}: {exc}")
                continue
            if rating is None:
                print(f"  No aggregate rating found: {title}")
                continue
            print(f"  {title}: {rating}")
            out.append({"platform": gyg.PLATFORM, "tour_name": title, "rating": rating})
        await browser.close()
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_ratings(rows: list) -> int:
    """Upsert scraped rows into tour_ratings.csv by (platform, tour_name).

    Existing rows for platforms/tours we didn't scrape are preserved, so a
    single platform failing never drops the others' last-known ratings.
    Returns the number of rows written for this run.
    """
    if not rows:
        return 0
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    existing = {}
    if RATINGS_FILE.exists():
        with open(RATINGS_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing[(r["platform"], r["tour_name"])] = r

    for row in rows:
        existing[(row["platform"], row["tour_name"])] = {
            "platform": row["platform"],
            "tour_name": row["tour_name"],
            "rating": f"{row['rating']:g}",
            "scraped_at": scraped_at,
        }

    ordered = sorted(existing.values(), key=lambda r: (r["platform"], r["tour_name"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RATINGS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(ordered)
    return len(rows)


SCRAPERS = {
    "freetour": scrape_freetour,
    "guruwalk": scrape_guruwalk,
    "getyourguide": scrape_getyourguide,
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape official aggregate tour ratings.")
    parser.add_argument(
        "--only", default="",
        help="comma-separated subset of platforms to run "
             "(freetour, guruwalk, getyourguide). Default: all.",
    )
    args = parser.parse_args()
    selected = [p.strip() for p in args.only.split(",") if p.strip()] or list(SCRAPERS)

    print("=" * 60)
    print("Discover Walks — Aggregate Tour Rating Scraper")
    print("=" * 60)

    all_rows = []
    for name in selected:
        fn = SCRAPERS.get(name)
        if fn is None:
            print(f"Unknown platform '{name}' — skipping.")
            continue
        try:
            all_rows.extend(await fn())
        except Exception as exc:
            print(f"Platform {name} failed: {exc}")

    written = save_ratings(all_rows)
    print("\n--- Summary ---")
    print(f"  Ratings scraped: {len(all_rows)}")
    print(f"  Rows upserted into {RATINGS_FILE.name}: {written}")
    for row in sorted(all_rows, key=lambda r: (r["platform"], r["tour_name"])):
        print(f"    {row['platform']:<13} {row['rating']:<5} {row['tour_name']}")
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
