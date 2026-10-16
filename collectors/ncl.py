from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

SEARCH_API = "https://www.ncl.com/au/en/api/v2/vacations/search"
SAMPLE_ITINERARY = "SPIRIT4SYDHBASYD"


async def _search_page(request, guests: int):
    params = {
        "embPorts": "SYD",
        "guests": str(guests),
        "filterConfig": "search-filters-configuration",
        "limit": "50",
        "offset": "0",
    }
    response = await request.get(SEARCH_API, params=params, timeout=30000)
    print(f"NCL_SEARCH_STATUS guests={guests} status={response.status}")
    try:
        body = await response.json()
    except Exception:
        print(f"NCL_SEARCH_NON_JSON guests={guests}")
        return []
    itineraries = body.get("itineraries") or []
    print(f"NCL_SEARCH_COUNT guests={guests} count={len(itineraries)}")
    for item in itineraries[:5]:
        print("NCL_SEARCH_SAMPLE " + json.dumps({
            "guests": guests,
            "code": item.get("code"),
            "packageId": item.get("packageId"),
            "title": item.get("title"),
            "ship": (item.get("ship") or {}).get("title"),
            "embarkation": (item.get("embarkationPort") or {}).get("code"),
            "duration": (item.get("duration") or {}).get("days"),
            "currency": item.get("currencyCode"),
            "combinedPrice": item.get("combinedPrice"),
            "basePrice": item.get("basePrice"),
            "objectId": item.get("objectId"),
        }, separators=(",", ":")))
    return itineraries


async def _probe_builder(browser, guests: int, itinerary_code: str):
    page = await browser.new_page(locale="en-AU")
    captured: list[dict] = []

    async def on_response(response):
        try:
            url = response.url
            ct = (response.headers.get("content-type") or "").lower()
            low = url.lower()
            if "/api/" not in low and not any(k in low for k in ("price", "sailing", "stateroom", "cabin", "vacation")):
                return
            item = {"status": response.status, "url": url, "content_type": ct}
            if "json" in ct:
                try:
                    text = await response.text()
                    item["body"] = " ".join(text.split())[:12000]
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            captured.append(item)
        except Exception:
            pass

    page.on("response", on_response)
    url = f"https://www.ncl.com/au/en/vacation-builder?itineraryCode={itinerary_code}&guests={guests}"
    print(f"NCL_BUILDER_START guests={guests} url={url}")
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"NCL_BUILDER_STATUS guests={guests} status={response.status if response else 'none'} final={page.url}")
        await page.wait_for_timeout(12000)
    except Exception as exc:
        print(f"NCL_BUILDER_ERROR guests={guests} {type(exc).__name__}: {exc}")

    try:
        body = " ".join((await page.locator("body").inner_text(timeout=5000)).split())
        print(f"NCL_BUILDER_BODY guests={guests} {body[:8000]}")
    except Exception as exc:
        print(f"NCL_BUILDER_BODY_ERROR guests={guests} {type(exc).__name__}: {exc}")

    seen = set()
    print(f"NCL_BUILDER_NETWORK guests={guests} count={len(captured)}")
    for item in captured:
        key = (item.get("status"), item.get("url"), item.get("body", "")[:200])
        if key in seen:
            continue
        seen.add(key)
        print("NCL_BUILDER_RESPONSE " + json.dumps(item, separators=(",", ":")))
    await page.close()


async def diagnostic(headless: bool = True):
    async with async_playwright() as p:
        request = await p.request.new_context(extra_http_headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
            "Accept-Language": "en-AU,en;q=0.9",
            "Referer": "https://www.ncl.com/au/en/vacations",
        })
        for guests in (1, 2):
            await _search_page(request, guests)
        await request.dispose()

        browser = await p.chromium.launch(headless=headless)
        for guests in (1, 2):
            await _probe_builder(browser, guests, SAMPLE_ITINERARY)
            await asyncio.sleep(1)
        await browser.close()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError("NCL diagnostic complete. Inspect NCL_* lines before publication.")
