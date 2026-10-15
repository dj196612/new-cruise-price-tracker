from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright, BrowserContext, Page

LANDING_URL = "https://www.princess.com/cruise-search/"
PORTS = ("Sydney", "Melbourne", "Brisbane")

NIGHTS_RE = re.compile(r"(\d+)\s*[- ]?\s*(?:Day|Night)", re.I)
DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?,?\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.I,
)
SHIP_RE = re.compile(
    r"(Caribbean|Coral|Crown|Diamond|Discovery|Emerald|Enchanted|Grand|Island|Majestic|Regal|Royal|Ruby|Sapphire|Sky|Star|Sun)\s+Princess",
    re.I,
)
ROUTE_RE = re.compile(
    r"(Sydney|Melbourne|Brisbane),\s*Australia\s+to\s+([^\n]{1,120}?),\s*Australia",
    re.I,
)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.princess.com", parts.path.rstrip("/"), "", ""))


def parse_date(text: str) -> Optional[str]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()


def departure_port(text: str) -> Optional[str]:
    m = ROUTE_RE.search(text or "")
    if m:
        return m.group(1).title()
    before_ports = re.split(r"\b\d+\s+Ports?:|\b0\s+Ports?:", text or "", maxsplit=1, flags=re.I)[0]
    hits = []
    for p in PORTS:
        pos = before_ports.lower().find(f"{p.lower()}, australia")
        if pos >= 0:
            hits.append((pos, p))
    return min(hits)[1] if hits else None


def interior_price(text: str) -> Optional[float]:
    # Detail page is authoritative. Prefer the explicit "Interior from" price.
    patterns = (
        r"Interior\s+from\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
        r"\bInterior\s*\$\s*([\d,]+(?:\.\d{1,2})?)",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def detect_currency(text: str) -> str:
    # Princess Australian itinerary pages state "listed in AUD".
    for pattern in (
        r"listed\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)",
        r"Price\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)",
    ):
        m = re.search(pattern, text or "", re.I)
        if m:
            return m.group(1).upper()
    return "AUD"


def cruise_name(text: str, nights: int) -> str:
    m = re.search(r"(\d+\s*[- ]?Day\s+[^\n]{2,140})", text or "", re.I)
    if m:
        value = re.sub(r"\s+", " ", m.group(1)).strip()
        # Stop at common detail-page controls if they were captured on the same line.
        value = re.split(r"What's included|Twice as Nice|Share|Thu,|Fri,|Sat,|Sun,|Mon,|Tue,|Wed,", value, 1, flags=re.I)[0].strip()
        return value
    return f"{nights}-Day Princess Cruise"


def make_id(ship: str, date: str, dep: str, name: str, url: str) -> str:
    key = f"princess|{ship}|{date}|{dep}|{name}|{canonical_url(url)}".lower()
    return f"PRINCESS-{hashlib.sha1(key.encode()).hexdigest()[:14]}"


async def dismiss_noise(page: Page):
    for label in ("Accept All Cookies", "Accept Cookies", "Accept", "I Agree", "Close"):
        try:
            loc = page.get_by_role("button", name=re.compile(label, re.I))
            for i in range(await loc.count()):
                item = loc.nth(i)
                if await item.is_visible():
                    await item.click(timeout=1200)
                    break
        except Exception:
            pass


async def first_visible(locator):
    for i in range(await locator.count()):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            pass
    return None


async def select_australian_departure_ports(page: Page) -> int:
    await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(4000)
    await dismiss_noise(page)

    trigger = await first_visible(page.get_by_text(re.compile(r"^Departure Port$", re.I)))
    if trigger is not None:
        await trigger.click(timeout=4000)
        await page.wait_for_timeout(700)

    for port in PORTS:
        target = await first_visible(page.get_by_text(re.compile(rf"^{port},\s*Australia$", re.I)))
        if target is None:
            raise RuntimeError(f"Could not select departure port: {port}")
        await target.click(timeout=3000)
        await page.wait_for_timeout(300)

    view = page.locator("button", has_text=re.compile(r"VIEW\s+\d+\s+RESULTS", re.I))
    visible_view = await first_visible(view)
    if visible_view is None:
        raise RuntimeError("Could not find a visible Princess View Results button")

    label = " ".join((await visible_view.inner_text()).split())
    m = re.search(r"(\d+)", label)
    if not m:
        raise RuntimeError(f"Could not read Princess result count from: {label}")
    expected = int(m.group(1))
    print(f"Princess search reports {expected} matching cruises for Sydney/Melbourne/Brisbane.")

    await visible_view.scroll_into_view_if_needed()
    try:
        await visible_view.click(timeout=5000)
    except Exception:
        await visible_view.click(timeout=5000, force=True)
    await page.wait_for_timeout(4500)
    return expected


async def unique_itinerary_urls(page: Page) -> list[str]:
    hrefs = await page.locator('a[href*="itinerary-details"]').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
    out = []
    seen = set()
    for href in hrefs:
        url = canonical_url(href)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


async def load_all_results(page: Page, expected: int) -> list[str]:
    previous = -1
    stable = 0
    for _ in range(20):
        urls = await unique_itinerary_urls(page)
        print(f"Princess canonical itinerary URLs loaded: {len(urls)} / expected {expected}")
        if len(urls) >= expected:
            # Princess can render hidden/mobile duplicates. The visible result count is authoritative.
            return urls[:expected]

        buttons = page.locator("button").filter(has_text=re.compile(r"Load More|Show More|More Results", re.I))
        clicked = False
        for i in range(await buttons.count()):
            btn = buttons.nth(i)
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5000)
                    clicked = True
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        if not clicked:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        count = len(await unique_itinerary_urls(page))
        if count == previous:
            stable += 1
        else:
            stable = 0
        previous = count
        if stable >= 3:
            break

    urls = await unique_itinerary_urls(page)
    return urls[:expected]


async def set_guest_count_on_detail(page: Page, guests: int) -> bool:
    current = await first_visible(page.get_by_text(re.compile(r"1\s*room\s*/\s*\d+\s*guests?", re.I)))
    if current is None:
        return False
    try:
        await current.click(timeout=2500)
        await page.wait_for_timeout(400)
    except Exception:
        return False

    # Princess commonly exposes a direct 1 room / 1 guest option after opening the control.
    for pattern in (
        rf"1\s*room\s*/\s*{guests}\s*guests?",
        rf"^{guests}\s*guests?$",
        rf"^{guests}\s*guest$",
    ):
        option = await first_visible(page.get_by_text(re.compile(pattern, re.I)))
        if option is not None:
            try:
                await option.click(timeout=2500)
                apply = await first_visible(page.get_by_role("button", name=re.compile(r"^(Apply|Done)$", re.I)))
                if apply is not None:
                    await apply.click(timeout=2000)
                await page.wait_for_timeout(1300)
                return True
            except Exception:
                pass
    return False


async def scrape_detail(context: BrowserContext, url: str, semaphore: asyncio.Semaphore) -> Optional[dict]:
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1800)
            await dismiss_noise(page)
            body = " ".join((await page.locator("body").inner_text()).split())

            n = NIGHTS_RE.search(body)
            if not n:
                return None
            nights = int(n.group(1))
            dep = departure_port(body)
            if dep not in PORTS:
                # The detail page may omit the 'to' phrase; retain only Australian departures.
                for p in PORTS:
                    if re.search(rf"\b{p},\s*Australia\b", body, re.I):
                        dep = p
                        break
            if dep not in PORTS:
                return None

            name = None
            try:
                h1 = page.locator("h1")
                if await h1.count():
                    name = " ".join((await h1.first.inner_text()).split())
            except Exception:
                pass
            if not name:
                name = cruise_name(body, nights)

            departure_date = parse_date(body)
            ship_match = SHIP_RE.search(body)
            ship = ship_match.group(0) if ship_match else None
            currency = detect_currency(body)
            two_price = interior_price(body)

            # Best-effort solo fare from the same authoritative detail page.
            one_price = None
            if await set_guest_count_on_detail(page, 1):
                solo_body = " ".join((await page.locator("body").inner_text()).split())
                one_price = interior_price(solo_body)
                # Currency must remain whatever Princess states on the detail page.
                currency = detect_currency(solo_body) or currency

            record = {
                "line": "Princess",
                "name": name,
                "ship": ship,
                "departure_port": dep,
                "arrival_port": None,
                "departure_date": departure_date,
                "nights": nights,
                "itinerary": body[:900],
                "url": canonical_url(url),
                "available": two_price is not None and "sold out" not in body.lower() and "unavailable" not in body.lower(),
                "currency": currency,
                "one_guest_price": one_price,
                "two_guest_pp_price": two_price,
                "two_guest_total": round(two_price * 2, 2) if two_price is not None else None,
            }
            record["cruise_id"] = make_id(ship or "UNKNOWN", departure_date or "UNKNOWN", dep, name, url)
            print(f"Detail: {name} | {departure_date} | {ship} | {currency} | 2 guests {two_price} | 1 guest {one_price}")
            return record
        except Exception as exc:
            print(f"Detail scrape failed for {url}: {exc}")
            return None
        finally:
            await page.close()


async def collect(headless=True):
    debug = Path("debug")
    debug.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )

        search_page = await context.new_page()
        expected = await select_australian_departure_ports(search_page)
        urls = await load_all_results(search_page, expected)
        print(f"Princess search yielded {len(urls)} canonical itinerary URLs; expected {expected}.")
        (debug / "princess-counts.txt").write_text(f"expected={expected}\ncanonical_urls={len(urls)}\n", encoding="utf-8")
        await search_page.close()

        if not urls:
            await browser.close()
            return []

        semaphore = asyncio.Semaphore(3)
        scraped = await asyncio.gather(*(scrape_detail(context, url, semaphore) for url in urls))
        records = [r for r in scraped if r is not None]

        # Defensive dedupe by canonical sailing identity.
        deduped = {}
        for r in records:
            deduped[r["cruise_id"]] = r
        records = list(deduped.values())

        print(f"Princess detail collector returned {len(records)} authoritative sailings (expected {expected}).")
        (debug / "princess-counts.txt").write_text(
            f"expected={expected}\ncanonical_urls={len(urls)}\nauthoritative_records={len(records)}\n",
            encoding="utf-8",
        )

        await browser.close()
        return records
