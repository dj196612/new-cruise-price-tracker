from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlencode

from playwright.async_api import async_playwright

PORTS = ("SYD", "BNE", "MEL")
BASE_SEARCH = "https://www.carnival.com.au/cruise-search"


def _search_url(port: str, guests: int) -> str:
    params = {
        "embkCode": port,
        "numAdults": str(guests),
        "pageNumber": "1",
        "pageSize": "8",
        "showBest": "true",
        "sort": "FromPrice",
        "useSuggestions": "true",
    }
    return BASE_SEARCH + "?" + urlencode(params)


def _safe_url(url: str) -> str:
    vifp = os.getenv("CARNIVAL_VIFP", "").strip()
    return url.replace(vifp, "<CARNIVAL_VIFP>") if vifp else url


def _safe_text(text: str) -> str:
    vifp = os.getenv("CARNIVAL_VIFP", "").strip()
    return text.replace(vifp, "<CARNIVAL_VIFP>") if vifp else text


async def _capture(page, label: str):
    captured: list[dict] = []

    async def on_response(response):
        try:
            url = response.url
            low = url.lower()
            content_type = (response.headers.get("content-type") or "").lower()
            if not any(k in low for k in (
                "/api/", "price", "rate", "fare", "booking", "sailing", "cruise-search", "qualifier"
            )):
                return
            item = {
                "status": response.status,
                "url": _safe_url(url),
                "content_type": content_type,
            }
            if "json" in content_type:
                try:
                    text = await response.text()
                    item["body"] = _safe_text(" ".join(text.split())[:14000])
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            captured.append(item)
        except Exception:
            pass

    page.on("response", on_response)
    return captured


async def _probe_search(browser, port: str, guests: int):
    page = await browser.new_page(locale="en-AU")
    captured = await _capture(page, f"search-{port}-{guests}")
    url = _search_url(port, guests)
    print(f"CARNIVAL_SEARCH_START port={port} guests={guests} url={url}")
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(
            f"CARNIVAL_SEARCH_STATUS port={port} guests={guests} "
            f"status={response.status if response else 'none'} final={_safe_url(page.url)}"
        )
        await page.wait_for_timeout(9000)
    except Exception as exc:
        print(f"CARNIVAL_SEARCH_ERROR port={port} guests={guests} {type(exc).__name__}: {exc}")

    try:
        body = _safe_text(" ".join((await page.locator("body").inner_text(timeout=5000)).split()))
        print(f"CARNIVAL_SEARCH_BODY port={port} guests={guests} {body[:7000]}")
    except Exception as exc:
        print(f"CARNIVAL_SEARCH_BODY_ERROR port={port} guests={guests} {type(exc).__name__}: {exc}")

    seen = set()
    for item in captured:
        key = (item.get("status"), item.get("url"), item.get("body", "")[:200])
        if key in seen:
            continue
        seen.add(key)
        print("CARNIVAL_SEARCH_RESPONSE " + json.dumps(item, separators=(",", ":")))
    await page.close()


async def _probe_member_rate(browser):
    vifp = os.getenv("CARNIVAL_VIFP", "").strip()
    if not vifp:
        print("CARNIVAL_MEMBER_SKIPPED missing CARNIVAL_VIFP secret")
        return

    page = await browser.new_page(locale="en-AU")
    captured = await _capture(page, "member")

    # Use a broad Brisbane 1-adult search because Carnival Encounter has frequent local sailings.
    search_url = _search_url("BNE", 1)
    print("CARNIVAL_MEMBER_START search=BNE guests=1")
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)

        # Click the first visible cruise/date/book-style action we can find.
        selectors = [
            "a:has-text('Select')",
            "button:has-text('Select')",
            "a:has-text('Book')",
            "button:has-text('Book')",
            "a:has-text('View Dates')",
            "button:has-text('View Dates')",
            "a:has-text('Show')",
            "button:has-text('Show')",
        ]
        clicked = False
        for selector in selectors:
            loc = page.locator(selector).first
            try:
                if await loc.is_visible(timeout=1200):
                    await loc.click()
                    clicked = True
                    print(f"CARNIVAL_MEMBER_CLICK selector={selector}")
                    break
            except Exception:
                pass
        if not clicked:
            print("CARNIVAL_MEMBER_NO_INITIAL_CTA")

        await page.wait_for_timeout(7000)

        # If a rate-qualifier step is present, apply the user's observed qualifiers.
        try:
            country = page.get_by_text("Country of Residence", exact=False).first
            if await country.is_visible(timeout=1500):
                print("CARNIVAL_MEMBER_RATE_QUALIFIERS_VISIBLE")
        except Exception:
            pass

        # Tick 55+ where available.
        for text in ("55+ years old", "55+", "55 years old"):
            try:
                label = page.get_by_text(text, exact=False).first
                if await label.is_visible(timeout=700):
                    await label.click()
                    print("CARNIVAL_MEMBER_55_PLUS_APPLIED")
                    break
            except Exception:
                pass

        # Tick guest number option and fill the number without ever printing it.
        for text in ("Add Carnival Guest #", "Carnival Guest #", "VIFP"):
            try:
                label = page.get_by_text(text, exact=False).first
                if await label.is_visible(timeout=700):
                    await label.click()
                    break
            except Exception:
                pass

        filled = False
        for selector in (
            "input[placeholder*='Carnival Guest']",
            "input[aria-label*='Carnival Guest']",
            "input[name*='guest']",
            "input[id*='guest']",
            "input[name*='vifp']",
            "input[id*='vifp']",
        ):
            try:
                inp = page.locator(selector).first
                if await inp.is_visible(timeout=700):
                    await inp.fill(vifp)
                    filled = True
                    print("CARNIVAL_MEMBER_VIFP_FILLED")
                    break
            except Exception:
                pass
        if not filled:
            inputs = page.locator("input")
            for i in range(min(await inputs.count(), 30)):
                inp = inputs.nth(i)
                try:
                    placeholder = (await inp.get_attribute("placeholder") or "").lower()
                    aria = (await inp.get_attribute("aria-label") or "").lower()
                    if "guest" in placeholder or "guest" in aria or "vifp" in placeholder or "vifp" in aria:
                        await inp.fill(vifp)
                        filled = True
                        print("CARNIVAL_MEMBER_VIFP_FILLED_FALLBACK")
                        break
                except Exception:
                    pass

        # Continue/apply if present.
        for selector in (
            "button:has-text('Continue')",
            "button:has-text('Apply')",
            "button:has-text('Next')",
            "button:has-text('View Rates')",
        ):
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=700):
                    await btn.click()
                    print(f"CARNIVAL_MEMBER_APPLY selector={selector}")
                    break
            except Exception:
                pass

        await page.wait_for_timeout(10000)
        body = _safe_text(" ".join((await page.locator("body").inner_text(timeout=5000)).split()))
        print(f"CARNIVAL_MEMBER_BODY {body[:9000]}")
    except Exception as exc:
        print(f"CARNIVAL_MEMBER_ERROR {type(exc).__name__}: {exc}")

    seen = set()
    for item in captured:
        key = (item.get("status"), item.get("url"), item.get("body", "")[:200])
        if key in seen:
            continue
        seen.add(key)
        print("CARNIVAL_MEMBER_RESPONSE " + json.dumps(item, separators=(",", ":")))
    await page.close()


async def diagnostic(headless: bool = True):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        for port in PORTS:
            for guests in (1, 2):
                await _probe_search(browser, port, guests)
                await asyncio.sleep(0.5)
        await _probe_member_rate(browser)
        await browser.close()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError("Carnival diagnostic complete. Inspect CARNIVAL_* lines before publication.")
