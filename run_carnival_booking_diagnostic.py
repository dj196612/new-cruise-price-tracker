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


async def _click_continue(page, stage: str):
    for selector in (
        "button:has-text('CONTINUE')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('View Rates')",
    ):
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1200):
                await btn.click()
                print(f"CARNIVAL_BOOKING_CLICK stage={stage} selector={selector}")
                await page.wait_for_timeout(7000)
                return True
        except Exception:
            pass
    print(f"CARNIVAL_BOOKING_NO_CONTINUE stage={stage}")
    return False


async def _probe_booking(browser, sailing_url: str):
    page = await browser.new_page(locale="en-AU")
    captured = []

    async def on_response(response):
        try:
            if len(captured) >= 80:
                return
            parsed = urlparse(response.url)
            if not parsed.netloc.endswith("carnival.com.au"):
                return
            low = response.url.lower()
            if any(x in low for x in (".css", ".js", ".svg", ".woff", ".png", ".jpg", "componentresources")):
                return
            if not any(k in low for k in ("booking-api", "cruisesearch/api", "/api/", "room", "rate", "price", "fare", "stateroom", "category", "offer")):
                return
            content_type = (response.headers.get("content-type") or "").lower()
            item = {"status": response.status, "url": response.url, "content_type": content_type}
            if "json" in content_type:
                try:
                    text = await response.text()
                    item["body"] = " ".join(text.split())[:18000]
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
    await page.wait_for_timeout(7000)

    # Rooms/Guests -> Rate Qualifiers.
    await _click_continue(page, "rooms-guests")

    # Apply the same eligible 55+ qualifier the tracker is intended to use.
    senior_applied = False
    for selector in (
        "label:has-text('55+ years old')",
        "text=55+ years old",
    ):
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=1200):
                await loc.click()
                senior_applied = True
                print("CARNIVAL_BOOKING_55_PLUS_APPLIED")
                break
        except Exception:
            pass
    if not senior_applied:
        print("CARNIVAL_BOOKING_55_PLUS_NOT_FOUND")

    # Rate Qualifiers -> Room Category.
    await _click_continue(page, "rate-qualifiers")

    body = " ".join((await page.locator("body").inner_text(timeout=5000)).split())
    print(f"CARNIVAL_BOOKING_FINAL_URL {page.url}")
    print(f"CARNIVAL_BOOKING_BODY {body[:12000]}")

    seen = set()
    for item in captured:
        key = (item.get("status"), item.get("url"), item.get("body", "")[:200])
        if key in seen:
            continue
        seen.add(key)
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
