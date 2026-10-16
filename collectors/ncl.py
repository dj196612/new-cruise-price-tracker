from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from playwright.async_api import async_playwright

URLS = {
    1: "https://www.ncl.com/au/en/vacations?embPorts=SYD&guests=1",
    2: "https://www.ncl.com/au/en/vacations?embPorts=SYD&guests=2",
}

KEYWORDS = (
    "vacation", "cruise", "itiner", "search", "price", "fare", "sailing",
    "package", "stateroom", "cabin", "voyage"
)


def _interesting(url: str, content_type: str) -> bool:
    low = url.lower()
    ct = (content_type or "").lower()
    if "json" in ct or "graphql" in low or "/api/" in low:
        return True
    return any(k in low for k in KEYWORDS)


async def _probe(browser, guests: int):
    page = await browser.new_page(locale="en-AU")
    captured: list[dict] = []

    async def on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if not _interesting(response.url, ct):
                return
            item = {
                "status": response.status,
                "url": response.url,
                "content_type": ct,
            }
            if "json" in ct.lower():
                try:
                    text = await response.text()
                    item["body"] = " ".join(text.split())[:5000]
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            captured.append(item)
        except Exception:
            pass

    page.on("response", on_response)
    url = URLS[guests]
    print(f"NCL_PAGE_START guests={guests} url={url}")
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"NCL_PAGE_STATUS guests={guests} status={response.status if response else 'none'} final={page.url}")
        await page.wait_for_timeout(12000)
    except Exception as exc:
        print(f"NCL_PAGE_ERROR guests={guests} {type(exc).__name__}: {exc}")

    try:
        body = " ".join((await page.locator("body").inner_text(timeout=5000)).split())
        print(f"NCL_BODY guests={guests} {body[:12000]}")
    except Exception as exc:
        print(f"NCL_BODY_ERROR guests={guests} {type(exc).__name__}: {exc}")

    try:
        links = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
        useful = []
        seen = set()
        for href in links:
            host = urlparse(href).netloc
            if "ncl.com" not in host:
                continue
            if href in seen:
                continue
            seen.add(href)
            low = href.lower()
            if any(k in low for k in ("cruise", "vacation", "itinerary", "booking")):
                useful.append(href)
        print(f"NCL_LINKS guests={guests} {json.dumps(useful[:80])}")
    except Exception as exc:
        print(f"NCL_LINKS_ERROR guests={guests} {type(exc).__name__}: {exc}")

    deduped = []
    seen_urls = set()
    for item in captured:
        key = (item.get("status"), item.get("url"), item.get("body", "")[:200])
        if key in seen_urls:
            continue
        seen_urls.add(key)
        deduped.append(item)
    print(f"NCL_NETWORK guests={guests} count={len(deduped)}")
    for item in deduped[:120]:
        print("NCL_RESPONSE " + json.dumps(item, separators=(",", ":")))

    await page.close()


async def diagnostic(headless: bool = True):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        for guests in (1, 2):
            await _probe(browser, guests)
            await asyncio.sleep(1)
        await browser.close()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError("NCL diagnostic complete. Inspect NCL_* lines before publication.")
