from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from . import princess as base

PROBE_URL = "https://www.princess.com/itinerary-details?voyageCode=A716A"


def _interesting(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in (
        "price", "fare", "cabin", "stateroom", "voyage", "itinerary", "booking", "availability", "api", "graphql"
    ))


async def _probe_pricing_calls(headless: bool = True):
    print("Running Princess pricing API diagnostic for voyage A716A...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        page = await context.new_page()

        seen = set()

        async def log_response(response):
            url = response.url
            if not _interesting(url) or url in seen:
                return
            seen.add(url)
            ctype = (response.headers or {}).get("content-type", "")
            print(f"PRINCESS_NET {response.status} {ctype} {url}")
            if "json" in ctype.lower():
                try:
                    text = await response.text()
                    flat = re.sub(r"\s+", " ", text)
                    if re.search(r"AUD|USD|Interior|Balcony|Mini-Suite|Suite|price|fare", flat, re.I):
                        print("PRINCESS_JSON", flat[:1800])
                except Exception as exc:
                    print(f"PRINCESS_JSON_READ_ERROR {exc}")

        page.on("response", log_response)
        await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(20000)

        body = " ".join((await page.locator("body").inner_text()).split())
        print("PRINCESS_PAGE_SNIPPET", body[:2200])

        cookies = await context.cookies()
        for c in cookies:
            name = (c.get("name") or "").lower()
            if any(k in name for k in ("country", "locale", "currency", "market", "region", "geo", "lang")):
                print(f"PRINCESS_COOKIE {c.get('name')}={c.get('value')} domain={c.get('domain')}")

        await browser.close()


async def collect(headless: bool = True):
    # Temporary diagnostic mode: probe one known voyage so we can identify the
    # actual Princess fare API and Australian-market parameter without waiting
    # for a full 93-cruise scrape.
    await _probe_pricing_calls(headless=headless)
    raise RuntimeError(
        "Princess fare API diagnostic complete. Inspect PRINCESS_NET / PRINCESS_JSON lines in the workflow log."
    )
