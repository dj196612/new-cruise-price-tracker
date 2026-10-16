from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

SOLO_URL = "https://www.royalcaribbean.com/aus/en/cruises?search=adults:1|children:0|departurePort:BNE,SYD&country=AUS&market=aus&language=en"
DOUBLE_URL = "https://www.royalcaribbean.com/aus/en/cruises?search=adults:2|children:0|departurePort:BNE,SYD&country=AUS&market=aus&language=en"


def _interesting(url: str) -> bool:
    u = (url or "").lower()
    return any(token in u for token in (
        "cruise", "sailing", "search", "price", "pricing", "fare", "itinerary",
        "product", "package", "voyage", "departure", "ship", "stateroom",
    ))


async def _probe(url: str, label: str, headless: bool = True):
    print(f"RC_DIAG_BEGIN {label} {url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        page = await context.new_page()
        seen = set()

        async def on_request(request):
            if not _interesting(request.url):
                return
            key = (request.method, request.url, request.post_data or "")
            if key in seen:
                return
            seen.add(key)
            post = re.sub(r"\s+", " ", request.post_data or "")[:1600]
            print(f"RC_REQ {label} {request.method} {request.url}")
            if post:
                print(f"RC_REQ_BODY {label} {post}")

        async def on_response(response):
            if not _interesting(response.url):
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            try:
                text = await response.text()
            except Exception:
                return
            flat = re.sub(r"\s+", " ", text)
            print(f"RC_RESP {label} {response.status} {response.url}")
            print(f"RC_RESP_JSON {label} {flat[:2400]}")

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(12000)

        # Best-effort cookie dismissal.
        for name in ("Accept All Cookies", "Accept Cookies", "Accept", "I Agree"):
            try:
                button = page.get_by_role("button", name=re.compile(name, re.I))
                if await button.count() and await button.first.is_visible():
                    await button.first.click(timeout=1200)
                    break
            except Exception:
                pass

        # Trigger lazy loading/virtualised result lists.
        for _ in range(5):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        body = " ".join((await page.locator("body").inner_text()).split())
        print(f"RC_PAGE_URL {label} {page.url}")
        print(f"RC_TITLE {label} {await page.title()}")
        print(f"RC_BODY {label} {body[:5000]}")

        hrefs = await page.locator("a[href]").evaluate_all(
            "els => els.map(e => e.href).filter(Boolean)"
        )
        cruise_links = []
        for href in hrefs:
            h = href.lower()
            if any(k in h for k in ("cruise", "sailing", "itinerary", "booking")):
                if href not in cruise_links:
                    cruise_links.append(href)
        print(f"RC_LINK_COUNT {label} {len(cruise_links)}")
        for href in cruise_links[:80]:
            print(f"RC_LINK {label} {href}")

        # Show visible text for likely result cards/containers.
        candidates = page.locator("article, [class*='card'], [class*='sailing'], [class*='cruise'], [data-testid]")
        printed = 0
        for i in range(min(await candidates.count(), 200)):
            item = candidates.nth(i)
            try:
                if not await item.is_visible():
                    continue
                text = " ".join((await item.inner_text(timeout=500)).split())
                if len(text) < 40:
                    continue
                if not re.search(r"\$|AUD|night|day|Sydney|Brisbane", text, re.I):
                    continue
                print(f"RC_CARD {label} {text[:1600]}")
                printed += 1
                if printed >= 20:
                    break
            except Exception:
                pass

        cookies = await context.cookies()
        for c in cookies:
            name = (c.get("name") or "").lower()
            if any(k in name for k in ("country", "market", "locale", "currency", "geo", "region")):
                print(f"RC_COOKIE {label} {c.get('name')}={c.get('value')} domain={c.get('domain')}")

        try:
            local_storage = await page.evaluate("Object.fromEntries(Object.entries(localStorage))")
            session_storage = await page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
            print(f"RC_LOCAL_STORAGE {label} {json.dumps(local_storage)[:3000]}")
            print(f"RC_SESSION_STORAGE {label} {json.dumps(session_storage)[:3000]}")
        except Exception as exc:
            print(f"RC_STORAGE_ERROR {label} {exc}")

        await browser.close()
        print(f"RC_DIAG_END {label}")


async def diagnostic(headless: bool = True):
    await _probe(SOLO_URL, "SOLO", headless=headless)
    await _probe(DOUBLE_URL, "DOUBLE", headless=headless)


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError(
        "Royal Caribbean diagnostic complete. Inspect RC_* lines before enabling publication."
    )
