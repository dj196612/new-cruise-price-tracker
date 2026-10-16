from __future__ import annotations

import json
import re

from playwright.async_api import async_playwright

from . import princess as base


TEST_URL = "https://www.princess.com/itinerary-details?voyageCode=A716A"
WATCH_RE = re.compile(r"pricing|fare|guest|occup|room|booking|quote|availability|caps|ube", re.I)


async def _first_visible(locator):
    for i in range(await locator.count()):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            pass
    return None


async def _storage(page):
    try:
        return await page.evaluate(
            """() => ({
                local: Object.fromEntries(Object.entries(localStorage)),
                session: Object.fromEntries(Object.entries(sessionStorage))
            })"""
        )
    except Exception as exc:
        return {"error": str(exc)}


async def _dump_cookies(context, label: str):
    cookies = await context.cookies()
    interesting = []
    for c in cookies:
        n = (c.get("name") or "").lower()
        if any(k in n for k in ("guest", "room", "occup", "book", "fare", "cruise", "geo", "country", "ube")):
            interesting.append(f"{c.get('name')}={c.get('value')}")
    print(f"SOLO_DIAG_{label}_COOKIES " + " | ".join(interesting))


async def _click_one_guest(page) -> bool:
    current = await _first_visible(
        page.get_by_text(re.compile(r"1\s*room\s*/\s*\d+\s*guests?", re.I))
    )
    if current is None:
        print("SOLO_DIAG guest control not found")
        return False

    await current.click(timeout=3000, force=True)
    await page.wait_for_timeout(500)

    clicked = False
    candidates = [
        page.get_by_role("button", name=re.compile(r"(decrease|decrement|minus|remove).*(adult|guest)", re.I)),
        page.get_by_role("button", name=re.compile(r"(adult|guest).*(decrease|decrement|minus|remove)", re.I)),
        page.locator('button[aria-label*="decrease" i]'),
        page.locator('button[aria-label*="decrement" i]'),
        page.locator('button[aria-label*="minus" i]'),
        page.locator('button[title*="decrease" i]'),
    ]
    for loc in candidates:
        button = await _first_visible(loc)
        if button is None:
            continue
        try:
            print("SOLO_DIAG clicking decrement:", await button.get_attribute("aria-label"), await button.get_attribute("title"))
            await button.click(timeout=2500, force=True)
            clicked = True
            break
        except Exception:
            pass

    if not clicked:
        adults = await _first_visible(page.get_by_text(re.compile(r"^Adults?$", re.I)))
        if adults is not None:
            node = adults
            for depth in range(6):
                try:
                    node = node.locator("xpath=..")
                    buttons = node.locator("button")
                    visible = []
                    for i in range(await buttons.count()):
                        b = buttons.nth(i)
                        if await b.is_visible() and await b.is_enabled():
                            visible.append(b)
                    if len(visible) >= 2:
                        print(f"SOLO_DIAG clicking Adults-row first button at depth {depth + 1}")
                        await visible[0].click(timeout=2500, force=True)
                        clicked = True
                        break
                except Exception:
                    continue

    if not clicked:
        print("SOLO_DIAG decrement not found")
        return False

    # Log panel text before applying, because it often reveals the internal count.
    try:
        body = " ".join((await page.locator("body").inner_text()).split())
        m = re.search(r"Adults.{0,120}", body, re.I)
        if m:
            print("SOLO_DIAG_ADULTS_TEXT", m.group(0))
    except Exception:
        pass

    for label in (r"^Apply$", r"^Done$", r"^Save$", r"^Update$", r"^Continue$"):
        button = await _first_visible(page.get_by_role("button", name=re.compile(label, re.I)))
        if button is not None:
            try:
                print("SOLO_DIAG applying guest change with", await button.inner_text())
                await button.click(timeout=2500, force=True)
                break
            except Exception:
                pass

    for _ in range(20):
        try:
            text = " ".join((await page.locator("body").inner_text()).split())
            if re.search(r"1\s*room\s*/\s*1\s*guest", text, re.I):
                print("SOLO_DIAG occupancy confirmed: 1 room / 1 guest")
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)

    print("SOLO_DIAG occupancy did not confirm")
    return False


async def collect(headless: bool = True):
    print("Running one-voyage Princess solo network diagnostic for A716A")

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
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(base.AU_LOCALE))

        await context.route(base.LOCALE_URL, force_locale)

        page = await context.new_page()
        phase = {"value": "LOAD"}

        def on_request(request):
            if not WATCH_RE.search(request.url):
                return
            try:
                post = request.post_data or ""
            except Exception:
                post = ""
            # Limit large payloads while preserving all occupancy-related content.
            if len(post) > 5000:
                post = post[:5000] + "...<truncated>"
            print(
                f"SOLO_DIAG_REQ_{phase['value']} method={request.method} url={request.url} post={post}"
            )

        async def on_response(response):
            if not WATCH_RE.search(response.url):
                return
            print(f"SOLO_DIAG_RESP_{phase['value']} status={response.status} url={response.url}")

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90000)
        await base.dismiss_noise(page)
        body = await base.wait_for_cabin_prices(page, 25)
        print("SOLO_DIAG_2P_PRICES", json.dumps(base.cabin_prices(body), sort_keys=True))
        print("SOLO_DIAG_STORAGE_BEFORE", json.dumps(await _storage(page), sort_keys=True)[:12000])
        await _dump_cookies(context, "BEFORE")

        phase["value"] = "GUEST_CHANGE"
        ok = await _click_one_guest(page)
        print("SOLO_DIAG_GUEST_CHANGE_OK", ok)
        await page.wait_for_timeout(8000)

        try:
            body_after = " ".join((await page.locator("body").inner_text()).split())
            print("SOLO_DIAG_1P_VISIBLE_PRICES", json.dumps(base.cabin_prices(body_after), sort_keys=True))
        except Exception as exc:
            print("SOLO_DIAG_1P_VISIBLE_ERROR", exc)

        print("SOLO_DIAG_STORAGE_AFTER", json.dumps(await _storage(page), sort_keys=True)[:12000])
        await _dump_cookies(context, "AFTER")

        await page.close()
        await browser.close()

    raise RuntimeError(
        "Princess solo diagnostic complete. Inspect SOLO_DIAG_REQ_GUEST_CHANGE and storage/cookie deltas."
    )
