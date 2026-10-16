import asyncio
from urllib.parse import urlsplit, urlunsplit

import collectors.princess as princess
from export_json import main as export_json
from storage import save_observations


def _canonical_url_keep_query(url: str) -> str:
    """Keep Princess ?voyageCode=... while stripping only the #fragment."""
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme or "https",
        parts.netloc or "www.princess.com",
        parts.path.rstrip("/"),
        parts.query,
        "",
    ))


async def _all_itinerary_urls(page):
    """Princess virtualises results, so do not restrict discovery to :visible links."""
    hrefs = await page.locator('a[href*="itinerary-details"]').evaluate_all(
        "els => els.map(e => e.href).filter(Boolean)"
    )
    out = []
    seen = set()
    for href in hrefs:
        url = _canonical_url_keep_query(href)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# Patch the Princess adapter at runtime so all downstream identity/detail logic
# preserves voyageCode and sees the full virtualised catalogue.
princess.canonical_url = _canonical_url_keep_query
princess.unique_itinerary_urls = _all_itinerary_urls
collect = princess.collect


async def main():
    print("Collecting Princess cruises...")
    records = await collect(headless=True)
    print(f"Princess collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit(
            "No Princess cruises were found. Check debug/princess-no-results.png "
            "and debug/princess-no-results.html for the current Princess page."
        )

    save_observations(records, line="Princess")
    export_json()


if __name__ == "__main__":
    asyncio.run(main())
