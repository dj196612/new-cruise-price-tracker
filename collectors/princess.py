from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright, BrowserContext, Page

LANDING_URL = "https://www.princess.com/en-au/cruise-search/"
PORTS = ("Sydney", "Melbourne", "Brisbane")
CABINS = ("interior", "oceanview", "balcony", "mini_suite", "suite")

TITLE_RE = re.compile(r"(\d+)\s*[- ]?Day\s+(.{2,120})", re.I)
DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?,?\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),?\s+(20\d{2})",
    re.I,
)
SHIP_RE = re.compile(
    r"(Caribbean|Coral|Crown|Diamond|Discovery|Emerald|Enchanted|Grand|Island|Majestic|Regal|Royal|Ruby|Sapphire|Sky|Star|Sun)\s+Princess",
    re.I,
)
ROUTE_RE = re.compile(r"(Sydney|Melbourne|Brisbane),\s*Australia\s+to\s+([^\n]{1,120}?),\s*Australia", re.I)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    # Keep voyageCode query; remove only the fragment.
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.princess.com", parts.path.rstrip("/"), parts.query, ""))


def au_detail_url(url: str) -> str:
    parts = urlsplit(canonical_url(url))
    path = parts.path
    if not path.startswith("/en-au/"):
        path = "/en-au" + (path if path.startswith("/") else "/" + path)
    return urlunsplit(("https", "www.princess.com", path, parts.query, ""))


def parse_date(text: str) -> Optional[str]:
    for m in DATE_RE.finditer(text or ""):
        year = int(m.group(3))
        if 2026 <= year <= 2035:
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%b %d %Y").date().isoformat()
            except ValueError:
                continue
    return None


def departure_port(text: str) -> Optional[str]:
    m = ROUTE_RE.search(text or "")
    if m:
        return m.group(1).title()
    # Roundtrip form.
    m = re.search(r"Roundtrip\s+from\s+(Sydney|Melbourne|Brisbane),\s*Australia", text or "", re.I)
    if m:
        return m.group(1).title()
    hits = []
    for p in PORTS:
        pos = (text or "").lower().find(f"{p.lower()}, australia")
        if pos >= 0:
            hits.append((pos, p))
    return min(hits)[1] if hits else None


def detect_currency(text: str) -> str:
    for pattern in (
        r"listed\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)",
        r"Price\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)",
        r"prices?\s+are.*?in\s+(AUD|USD|NZD|CAD|GBP|EUR)",
    ):
        m = re.search(pattern, text or "", re.I)
        if m:
            return m.group(1).upper()
    return "AUD"


def cabin_prices(text: str) -> dict[str, Optional[float]]:
    flat = re.sub(r"\s+", " ", text or "")
    patterns = {
        "suite": r"(?<!Mini-)\bSuite\s*\$\s*([\d,]+(?:\.\d+)?)",
        "mini_suite": r"\bMini-Suite\s*\$\s*([\d,]+(?:\.\d+)?)",
        "balcony": r"\bBalcony\s*\$\s*([\d,]+(?:\.\d+)?)",
        "oceanview": r"\bOceanview\s*\$\s*([\d,]+(?:\.\d+)?)",
        "interior": r"\bInterior\s*\$\s*([\d,]+(?:\.\d+)?)",
    }
    out = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, flat, re.I)
        out[key] = float(m.group(1).replace(",", "")) if m else None
    return out


def make_id(voyage_code: str, departure_date: Optional[str]) -> str:
    key = f"princess|{voyage_code}|{departure_date or ''}".lower()
    return f"PRINCESS-{hashlib.sha1(key.encode()).hexdigest()[:14]}"


async def first_visible(locator):
    for i in range(await locator.count()):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            pass
    return None


async def dismiss_noise(page: Page):
    for label in ("Accept All Cookies", "Accept Cookies", "Accept", "I Agree", "Close"):
        try:
            item = await first_visible(page.get_by_role("button", name=re.compile(label, re.I)))
            if item is not None:
                await item.click(timeout=1200)
        except Exception:
            pass


async def select_standard(page: Page) -> bool:
    candidates = [
        page.get_by_role("radio", name=re.compile(r"Princess Standard|Standard", re.I)),
        page.get_by_label(re.compile(r"Princess Standard|Standard", re.I)),
        page.get_by_text(re.compile(r"Princess Standard", re.I)),
    ]
    for loc in candidates:
        item = await first_visible(loc)
        if item is None:
            continue
        try:
            await item.click(timeout=3000, force=True)
            await page.wait_for_timeout(800)
            print("Princess Standard selected on search page.")
            return True
        except Exception:
            pass
    print("Princess Standard control not exposed; detail-page pricing will be used.")
    return False


async def select_australian_departure_ports(page: Page) -> int:
    await page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(4500)
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

    await select_standard(page)

    view = page.locator("button", has_text=re.compile(r"VIEW\s+\d+\s+RESULTS", re.I))
    visible_view = await first_visible(view)
    if visible_view is None:
        raise RuntimeError("Could not find Princess View Results button")
    label = " ".join((await visible_view.inner_text()).split())
    m = re.search(r"(\d+)", label)
    if not m:
        raise RuntimeError(f"Could not read result count from {label}")
    expected = int(m.group(1))
    print(f"Princess search reports {expected} cruises.")
    await visible_view.click(timeout=5000, force=True)
    await page.wait_for_timeout(4500)
    return expected


async def collect_search_items(page: Page, expected: int) -> list[dict]:
    items: dict[str, dict] = {}
    stable = 0
    previous = -1

    for _ in range(30):
        links = page.locator('a[href*="itinerary-details"]')
        count = await links.count()
        for i in range(count):
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            url = canonical_url(href if href.startswith("http") else "https://www.princess.com" + href)
            parts = urlsplit(url)
            q = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
            voyage_code = q.get("voyageCode")
            if not voyage_code:
                continue

            text = ""
            node = link
            for _ in range(9):
                try:
                    node = node.locator("xpath=..")
                    candidate = " ".join((await node.inner_text(timeout=800)).split())
                    if re.search(r"\d+\s*[- ]?Day", candidate, re.I) and re.search(r"Sydney|Melbourne|Brisbane", candidate, re.I):
                        text = candidate
                        break
                except Exception:
                    continue

            title_match = TITLE_RE.search(text)
            nights = int(title_match.group(1)) if title_match else None
            name = f"{title_match.group(1)}-Day {title_match.group(2).strip()}" if title_match else "Princess Cruise"
            name = re.split(r"Save|Sydney, Australia|Melbourne, Australia|Brisbane, Australia", name, 1, flags=re.I)[0].strip()
            dep = departure_port(text)
            items[voyage_code] = {
                "voyage_code": voyage_code,
                "url": url,
                "name": name,
                "nights": nights,
                "departure_port": dep,
                "search_text": text[:900],
            }

        if len(items) >= expected:
            break

        buttons = page.locator("button").filter(has_text=re.compile(r"Load More|Show More|More Results", re.I))
        clicked = False
        for i in range(await buttons.count()):
            btn = buttons.nth(i)
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=4000, force=True)
                    clicked = True
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                pass
        if not clicked:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        if len(items) == previous:
            stable += 1
        else:
            stable = 0
        previous = len(items)
        if stable >= 4:
            break

    print(f"Princess search metadata collected: {len(items)} / expected {expected}")
    return list(items.values())[:expected]


async def wait_for_cabin_prices(page: Page, timeout_seconds: int = 20) -> str:
    body = ""
    for _ in range(timeout_seconds * 2):
        body = " ".join((await page.locator("body").inner_text()).split())
        prices = cabin_prices(body)
        if any(v is not None for v in prices.values()):
            return body
        await page.wait_for_timeout(500)
    return body


async def set_guest_count_on_detail(page: Page, guests: int) -> bool:
    current = await first_visible(page.get_by_text(re.compile(r"1\s*room\s*/\s*\d+\s*guests?", re.I)))
    if current is None:
        return False
    try:
        await current.click(timeout=2500)
        await page.wait_for_timeout(300)
    except Exception:
        return False
    option = await first_visible(page.get_by_text(re.compile(rf"1\s*room\s*/\s*{guests}\s*guests?", re.I)))
    if option is None:
        return False
    try:
        await option.click(timeout=2500, force=True)
        apply = await first_visible(page.get_by_role("button", name=re.compile(r"^(Apply|Done)$", re.I)))
        if apply is not None:
            await apply.click(timeout=1800, force=True)
        await page.wait_for_timeout(800)
        return True
    except Exception:
        return False


async def scrape_detail(context: BrowserContext, item: dict, semaphore: asyncio.Semaphore, index: int, total: int) -> Optional[dict]:
    async with semaphore:
        page = await context.new_page()
        try:
            url = au_detail_url(item["url"])
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await dismiss_noise(page)
            body = await wait_for_cabin_prices(page, 20)

            two = cabin_prices(body)
            currency = detect_currency(body)

            # Header/detail metadata only. Search card remains the primary source for name/nights/port.
            h1_text = ""
            try:
                h1 = page.locator("h1")
                if await h1.count():
                    h1_text = " ".join((await h1.first.inner_text()).split())
            except Exception:
                pass
            name = h1_text or item.get("name") or "Princess Cruise"
            tm = TITLE_RE.search(name)
            nights = int(tm.group(1)) if tm else item.get("nights")
            if not nights or nights > 120:
                nights = item.get("nights")

            departure_date = parse_date(body)
            sm = SHIP_RE.search(body)
            ship = sm.group(0) if sm else None
            dep = item.get("departure_port") or departure_port(body)

            one = {c: None for c in CABINS}
            if await set_guest_count_on_detail(page, 1):
                solo_body = await wait_for_cabin_prices(page, 15)
                one = cabin_prices(solo_body)

            record = {
                "line": "Princess",
                "name": name,
                "ship": ship,
                "departure_port": dep,
                "arrival_port": None,
                "departure_date": departure_date,
                "nights": nights,
                "itinerary": item.get("search_text") or body[:900],
                "url": item["url"],
                "available": any(v is not None for v in two.values()),
                "currency": currency,
                "voyage_code": item["voyage_code"],
            }
            for cabin in CABINS:
                record[f"two_guest_{cabin}_pp"] = two.get(cabin)
                record[f"one_guest_{cabin}"] = one.get(cabin)
            record["two_guest_pp_price"] = two.get("interior")
            record["one_guest_price"] = one.get("interior")
            record["two_guest_total"] = round(two["interior"] * 2, 2) if two.get("interior") is not None else None
            record["cruise_id"] = make_id(item["voyage_code"], departure_date)

            print(
                f"Detail {index}/{total}: {item['voyage_code']} | {name} | {departure_date} | "
                f"{ship} | {currency} | Interior {two.get('interior')} | Balcony {two.get('balcony')}"
            )
            return record
        except Exception as exc:
            print(f"Detail scrape failed {index}/{total} for {item.get('voyage_code')}: {exc}")
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
        items = await collect_search_items(search_page, expected)
        await search_page.close()

        semaphore = asyncio.Semaphore(3)
        scraped = await asyncio.gather(
            *(scrape_detail(context, item, semaphore, i + 1, len(items)) for i, item in enumerate(items))
        )
        records = [r for r in scraped if r is not None]

        (debug / "princess-counts.txt").write_text(
            f"expected={expected}\nsearch_items={len(items)}\nrecords={len(records)}\npriced={sum(1 for r in records if r.get('two_guest_interior_pp') is not None)}\n",
            encoding="utf-8",
        )
        print(f"Princess collector returned {len(records)} records, {sum(1 for r in records if r.get('two_guest_interior_pp') is not None)} with Interior pricing.")
        await browser.close()
        return records
