from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from collectors import royal_caribbean as rc
from collectors import celebrity as ce

TARGETS = [
    ("Royal Caribbean", rc, "2027-04-14", "4877"),
    ("Celebrity Cruises", ce, "2029-03-09", "3239"),
]


def _relevant(url: str) -> bool:
    low = url.lower()
    if any(x in low for x in (".js", ".css", ".svg", ".png", ".jpg", ".webp", ".woff", "analytics", "adobe", "tealium")):
        return False
    return any(x in low for x in (
        "/api/", "graphql", "graph", "price", "fare", "room", "stateroom",
        "sailing", "availability", "booking", "guest", "cruise"
    ))


async def _find_target(request, module, target_date: str):
    cruises = await module._search_all(request)
    for cruise in cruises:
        for sailing in cruise.get("sailings") or []:
            date = (sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
            if date == target_date:
                return cruise, sailing
    raise RuntimeError(f"Target sailing not found: {target_date}")


async def _dismiss_cookies(page):
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept All Cookies')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
    ):
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=500):
                await button.click(force=True)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass


async def _select_one_guest(page):
    candidates = [
        page.get_by_text("Number of guests", exact=False).first,
        page.get_by_text("Guests", exact=True).first,
        page.locator("[data-testid*='guest' i]").first,
    ]
    opened = False
    for item in candidates:
        try:
            if await item.is_visible(timeout=1500):
                await item.click(force=True)
                await page.wait_for_timeout(800)
                opened = True
                break
        except Exception:
            pass

    for text in ("1 Guest", "1 guest", "1 Adult", "1 adult"):
        try:
            option = page.get_by_text(text, exact=True).last
            if await option.is_visible(timeout=1200):
                await option.click(force=True)
                await page.wait_for_timeout(7000)
                print(f"RCX_SOLO_GUEST_SELECTED text={text}")
                return True
        except Exception:
            pass

    # Some versions use +/- controls rather than a menu. Log page text so the
    # control shape is visible in the diagnostic output.
    print(f"RCX_SOLO_GUEST_SELECT_FAILED opened={opened}")
    return False


async def _open_room_types(page):
    for text in ("Select room type", "Room type", "Stateroom"):
        try:
            item = page.get_by_text(text, exact=False).first
            if await item.is_visible(timeout=1200):
                await item.click(force=True)
                await page.wait_for_timeout(5000)
                return True
        except Exception:
            pass
    return False


async def _probe(browser, line: str, module, url: str, expected_price: str):
    page = await browser.new_page(locale="en-AU")
    requests = []
    responses = []

    def on_request(req):
        if not _relevant(req.url):
            return
        try:
            requests.append({
                "method": req.method,
                "url": req.url,
                "post_data": (req.post_data or "")[:12000],
                "headers": {k: v for k, v in req.headers.items() if k.lower() in {"content-type", "referer", "origin", "country", "currency", "office", "brand"}},
            })
        except Exception:
            pass

    async def on_response(resp):
        if not _relevant(resp.url) or len(responses) >= 160:
            return
        item = {"status": resp.status, "url": resp.url, "content_type": resp.headers.get("content-type", "")}
        try:
            ctype = item["content_type"].lower()
            if "json" in ctype or "text" in ctype:
                text = await resp.text()
                compact = " ".join(text.split())
                if len(compact) <= 30000 or expected_price in compact:
                    item["body"] = compact[:30000]
                else:
                    item["body"] = compact[:6000]
        except Exception:
            pass
        responses.append(item)

    page.on("request", on_request)
    page.on("response", on_response)

    print(f"RCX_SOLO_OPEN line={line} url={url}")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    print(f"RCX_SOLO_STATUS line={line} status={response.status if response else 'none'} final={page.url}")
    await page.wait_for_timeout(9000)
    await _dismiss_cookies(page)
    await _select_one_guest(page)
    await _open_room_types(page)
    await page.wait_for_timeout(5000)

    body = " ".join((await page.locator("body").inner_text(timeout=10000)).split())
    print(f"RCX_SOLO_BODY line={line} expected={expected_price} found={expected_price in body} text={body[:12000]}")

    for item in requests:
        print("RCX_SOLO_REQUEST " + json.dumps({"line": line, **item}, separators=(",", ":")))
    for item in responses:
        body_text = item.get("body", "")
        if expected_price in body_text or any(k in item["url"].lower() for k in ("price", "fare", "room", "sailing", "booking", "guest", "/api/")):
            print("RCX_SOLO_RESPONSE " + json.dumps({"line": line, **item, "contains_expected": expected_price in body_text}, separators=(",", ":")))

    await page.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for line, module, target_date, expected in TARGETS:
            request = await p.request.new_context(extra_http_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"
            })
            cruise, sailing = await _find_target(request, module, target_date)
            await request.dispose()
            url = module._au_url(cruise, sailing)
            itinerary = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
            print("RCX_SOLO_TARGET " + json.dumps({
                "line": line,
                "date": target_date,
                "name": itinerary.get("name"),
                "ship": (itinerary.get("ship") or {}).get("name"),
                "sailing": sailing,
                "url": url,
            }, separators=(",", ":")))
            await _probe(browser, line, module, url, expected)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
