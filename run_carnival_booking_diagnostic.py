from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from collectors.carnival import SEARCH_API, _headers

TARGET_DATE = "2027-12-13"
TARGET_TITLE = "phillip island"
TARGET_PORT = "SYD"


async def _find_sailing(request):
    for senior in (False, True):
        for page in range(1, 5):
            params = {
                "pageNumber": str(page),
                "numadults": "1",
                "port": TARGET_PORT,
                "pagesize": "50",
                "sort": "FromPrice",
                "showBest": "true",
                "async": "true",
                "currency": "AUD",
                "locality": "7",
                "client": "cruisesearch",
                "senior": "true" if senior else "false",
            }
            response = await request.get(SEARCH_API, params=params, headers=_headers(), timeout=30000)
            print(f"CARNIVAL_BOOKING_SEARCH senior={senior} page={page} status={response.status}")
            if response.status != 200:
                continue
            body = await response.json()
            results = body.get("results") or {}
            for itin in results.get("itineraries") or []:
                title = (itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle") or "").lower()
                if TARGET_TITLE not in title:
                    continue
                for sailing in itin.get("sailings") or []:
                    date = (sailing.get("departureDate") or "")[:10]
                    if date != TARGET_DATE:
                        continue
                    print("CARNIVAL_BOOKING_MATCH " + json.dumps({
                        "senior": senior,
                        "title": itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle"),
                        "ship": itin.get("shipName"),
                        "code": itin.get("code"),
                        "sailingId": sailing.get("sailingId"),
                        "date": date,
                        "searchRooms": sailing.get("rooms"),
                        "sailingURL": sailing.get("sailingURL"),
                    }, separators=(",", ":")))
                    return itin, sailing, senior
            if page >= int(results.get("lastPage") or 1):
                break
    raise RuntimeError("Target Carnival sailing not found")


async def _probe_booking(browser, sailing_url: str):
    page = await browser.new_page(locale="en-AU")
    captured = []

    async def on_response(response):
        try:
            if len(captured) >= 40:
                return
            parsed = urlparse(response.url)
            if not parsed.netloc.endswith("carnival.com.au"):
                return
            low = response.url.lower()
            if not any(k in low for k in ("booking", "/api/", "room", "rate", "price", "fare", "stateroom", "category")):
                return
            content_type = (response.headers.get("content-type") or "").lower()
            item = {"status": response.status, "url": response.url, "content_type": content_type}
            if "json" in content_type:
                try:
                    text = await response.text()
                    item["body"] = " ".join(text.split())[:12000]
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            captured.append(item)
        except Exception:
            pass

    page.on("response", on_response)
    url = "https://www.carnival.com.au" + sailing_url if sailing_url.startswith("/") else sailing_url
    print(f"CARNIVAL_BOOKING_OPEN {url}")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    print(f"CARNIVAL_BOOKING_STATUS {response.status if response else 'none'} final={page.url}")
    await page.wait_for_timeout(10000)

    # Continue through the rate qualifier screen if it is present, without using VIFP.
    for selector in (
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('View Rates')",
    ):
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=700):
                await btn.click()
                print(f"CARNIVAL_BOOKING_CLICK {selector}")
                await page.wait_for_timeout(8000)
                break
        except Exception:
            pass

    body = " ".join((await page.locator("body").inner_text(timeout=5000)).split())
    print(f"CARNIVAL_BOOKING_BODY {body[:9000]}")

    for item in captured:
        print("CARNIVAL_BOOKING_RESPONSE " + json.dumps(item, separators=(",", ":")))

    await page.close()


async def main():
    async with async_playwright() as p:
        request = await p.request.new_context(extra_http_headers=_headers())
        _, sailing, _ = await _find_sailing(request)
        await request.dispose()

        browser = await p.chromium.launch(headless=True)
        await _probe_booking(browser, sailing.get("sailingURL") or "")
        await browser.close()

    raise RuntimeError("Carnival booking diagnostic complete")


if __name__ == "__main__":
    asyncio.run(main())
