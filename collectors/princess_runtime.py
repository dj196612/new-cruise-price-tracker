from __future__ import annotations

import json
import re

from playwright.async_api import async_playwright

from . import princess as base

PROBE_URL = "https://www.princess.com/itinerary-details?voyageCode=A716A"
LOCALE_URL = "https://gw.api.princess.com/pcl-web/internal/guest/p1.0/locale"

AU_LOCALE = {
    "locale": {
        "zipCode": "2000",
        "ipAddress": "203.0.113.1",
        "status": "AU",
        "country": "AU",
        "countryPhone": "13 24 28",
        "specialOffers": "true",
        "brochures": "true",
        "lastUpdated": "1792121231",
        "regionCode": "NSW",
        "timezone": "Australia/Sydney",
        "defaultHomeCity": "SYD",
        "primaryCurrency": "AUD",
        "secondaryCurrency": "NZD",
        "aircity": "SYD",
        "isEU": "false",
        "isIntl": True,
    }
}


def _interesting(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in (
        "guest/p1.0/locale", "ube?", "agencies/", "products?", "pricing/v1/cruises"
    ))


async def _probe_au_market(headless: bool = True):
    print("Running Princess forced-AU market diagnostic for voyage A716A...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )

        await context.add_cookies([
            {"name": "cookie_geo_country", "value": "AU", "domain": ".princess.com", "path": "/"},
            {"name": "cookie_url_country", "value": "AU", "domain": ".princess.com", "path": "/"},
        ])

        async def force_locale(route):
            print("PRINCESS_AU overriding locale endpoint with AU/AUD")
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(AU_LOCALE),
            )

        await context.route(LOCALE_URL, force_locale)
        page = await context.new_page()
        seen = set()

        async def log_response(response):
            url = response.url
            if not _interesting(url) or url in seen:
                return
            seen.add(url)
            ctype = (response.headers or {}).get("content-type", "")
            print(f"PRINCESS_AU_NET {response.status} {url}")
            if "json" in ctype.lower():
                try:
                    text = await response.text()
                    flat = re.sub(r"\s+", " ", text)
                    if "pricing/v1/cruises" in url.lower():
                        m = re.search(r'"fareCurrency"\s*:\s*"([A-Z]{3})"', flat)
                        print("PRINCESS_AU_PRICING_CURRENCY", m.group(1) if m else "UNKNOWN")
                        print("PRINCESS_AU_PRICING_JSON", flat[:2200])
                    elif any(x in url.lower() for x in ("ube?", "products?", "agencies/")):
                        print("PRINCESS_AU_JSON", flat[:1600])
                except Exception as exc:
                    print(f"PRINCESS_AU_JSON_READ_ERROR {exc}")

        page.on("response", log_response)
        await page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(20000)

        body = " ".join((await page.locator("body").inner_text()).split())
        price_line = re.search(r"Price in (AUD|USD|NZD).*?includes taxes and fees", body, re.I)
        print("PRINCESS_AU_PAGE_CURRENCY", price_line.group(1).upper() if price_line else "UNKNOWN")
        print("PRINCESS_AU_PAGE_SNIPPET", body[:1500])

        cookies = await context.cookies()
        for c in cookies:
            name = (c.get("name") or "").lower()
            if any(k in name for k in ("country", "locale", "currency", "market", "region", "geo")):
                print(f"PRINCESS_AU_COOKIE {c.get('name')}={c.get('value')} domain={c.get('domain')}")

        await browser.close()


async def collect(headless: bool = True):
    await _probe_au_market(headless=headless)
    raise RuntimeError(
        "Princess forced-AU diagnostic complete. Inspect PRINCESS_AU_* lines in the workflow log."
    )
