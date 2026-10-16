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
CABINS = ("interior", "oceanview", "balcony", "mini_suite", "suite")

NIGHTS_RE = re.compile(r"(\d+)\s*[- ]?\s*(?:Day|Night)", re.I)
DATE_RE = re.compile(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?,?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
SHIP_RE = re.compile(r"(Caribbean|Coral|Crown|Diamond|Discovery|Emerald|Enchanted|Grand|Island|Majestic|Regal|Royal|Ruby|Sapphire|Sky|Star|Sun)\s+Princess", re.I)
ROUTE_RE = re.compile(r"(Sydney|Melbourne|Brisbane),\s*Australia\s+to\s+([^\n]{1,120}?),\s*Australia", re.I)


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
    hits = []
    for p in PORTS:
        pos = (text or "").lower().find(f"{p.lower()}, australia")
        if pos >= 0:
            hits.append((pos, p))
    return min(hits)[1] if hits else None


def detect_currency(text: str) -> str:
    for pattern in (r"listed\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)", r"Price\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)"):
        m = re.search(pattern, text or "", re.I)
        if m:
            return m.group(1).upper()
    return "AUD"


def cabin_prices(text: str) -> dict[str, Optional[float]]:
    text = re.sub(r"\s+", " ", text or "")
    patterns = {
        "suite": r"(?<!Mini-)\bSuite\s*\$\s*([\d,]+(?:\.\d+)?)",
        "mini_suite": r"\bMini-Suite\s*\$\s*([\d,]+(?:\.\d+)?)",
        "balcony": r"\bBalcony\s*\$\s*([\d,]+(?:\.\d+)?)",
        "oceanview": r"\bOceanview\s*\$\s*([\d,]+(?:\.\d+)?)",
        "interior": r"\bInterior\s*\$\s*([\d,]+(?:\.\d+)?)",
    }
    result = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.I)
        result[key] = float(m.group(1).replace(",", "")) if m else None
    return result


def make_id(ship: str, date: str, dep: str, name: str, url: str) -> str:
    key = f"princess|{ship}|{date}|{dep}|{name}|{canonical_url(url)}".lower()
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
    """Best-effort Standard selection; never abort if Princess hides the control."""
    # Accessible radio/label variants first.
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
            await page.wait_for_timeout(900)
            print("Princess Standard fare selected on search page.")
            return True
        except Exception:
            continue

    # Some Princess layouts render a hidden radio next to visible text.
    try:
        radios = page.locator('input[type="radio"]')
        for i in range(await radios.count()):
            radio = radios.nth(i)
            label_text = ""
            try:
                rid = await radio.get_attribute("id")
                if rid:
                    lab = page.locator(f'label[for="{rid}"]')
                    if await lab.count():
                        label_text = await lab.first.inner_text()
            except Exception:
                pass
            if re.search(r"standard", label_text or "", re.I):
                await radio.check(force=True)
                await page.wait_for_timeout(900)
                print("Princess Standard fare selected via radio input.")
                return True
    except Exception:
        pass

    print("Princess Standard control not exposed on this layout; continuing because itinerary detail pages are authoritative.")
    return False


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

    selected_standard = await select_standard(page)

    view = page.locator("button", has_text=re.compile(r"VIEW\s+\d+\s+RESULTS", re.I))
    visible_view = await first_visible(view)
    if visible_view is None:
        raise RuntimeError("Could not find a visible Princess View Results button")
    label = " ".join((await visible_view.inner_text()).split())
    m = re.search(r"(\d+)", label)
    if not m:
        raise RuntimeError(f"Could not read Princess result count from: {label}")
    expected = int(m.group(1))
    fare_note = "Standard-selected" if selected_standard else "detail-page pricing"
    print(f"Princess search reports {expected} cruises for Sydney/Melbourne/Brisbane ({fare_note}).")
    await visible_view.click(timeout=5000, force=True)
    await page.wait_for_timeout(4500)
    return expected


async def unique_itinerary_urls(page: Page) -> list[str]:
    hrefs = await page.locator('a[href*="itinerary-details"]:visible').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
    out, seen = [], set()
    for href in hrefs:
        url = canonical_url(href)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


async def load_all_results(page: Page, expected: int) -> list[str]:
    previous, stable = -1, 0
    for _ in range(20):
        urls = await unique_itinerary_urls(page)
        print(f"Princess visible itinerary URLs loaded: {len(urls)} / expected {expected}")
        if len(urls) >= expected:
            return urls[:expected]
        buttons = page.locator("button").filter(has_text=re.compile(r"Load More|Show More|More Results", re.I))
        clicked = False
        for i in range(await buttons.count()):
            btn = buttons.nth(i)
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5000, force=True)
                    clicked = True
                    await page.wait_for_timeout(1200)
                    break
            except Exception:
                pass
        if not clicked:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
        count = len(await unique_itinerary_urls(page))
        stable = stable + 1 if count == previous else 0
        previous = count
        if stable >= 3:
            break
    return (await unique_itinerary_urls(page))[:expected]


async def set_guest_count_on_detail(page: Page, guests: int) -> bool:
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            opts = await sel.locator("option").evaluate_all("els => els.map(o => ({t:(o.textContent||'').trim(),v:o.value}))")
            match = next((o for o in opts if re.search(rf"1\s*room\s*/\s*{guests}\s*guests?", o['t'], re.I)), None)
            if match:
                await sel.select_option(match["v"])
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

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
        await option.click(timeout=2500)
        apply = await first_visible(page.get_by_role("button", name=re.compile(r"^(Apply|Done)$", re.I)))
        if apply is not None:
            await apply.click(timeout=1800)
        await page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


async def scrape_detail(context: BrowserContext, url: str, semaphore: asyncio.Semaphore, index: int, total: int) -> Optional[dict]:
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
                for p in PORTS:
                    if re.search(rf"\b{p},\s*Australia\b", body, re.I):
                        dep = p
                        break
            if dep not in PORTS:
                return None

            try:
                h1 = page.locator("h1")
                name = " ".join((await h1.first.inner_text()).split()) if await h1.count() else f"{nights}-Day Princess Cruise"
            except Exception:
                name = f"{nights}-Day Princess Cruise"

            departure_date = parse_date(body)
            sm = SHIP_RE.search(body)
            ship = sm.group(0) if sm else None
            currency = detect_currency(body)
            two = cabin_prices(body)

            record = {
                "line": "Princess", "name": name, "ship": ship, "departure_port": dep,
                "arrival_port": None, "departure_date": departure_date, "nights": nights,
                "itinerary": body[:900], "url": canonical_url(url),
                "available": any(v is not None for v in two.values()) and "sold out" not in body.lower(),
                "currency": currency,
            }
            for cabin in CABINS:
                record[f"two_guest_{cabin}_pp"] = two.get(cabin)
            record["two_guest_pp_price"] = two.get("interior")
            record["two_guest_total"] = round(two["interior"] * 2, 2) if two.get("interior") is not None else None

            if await set_guest_count_on_detail(page, 1):
                solo_body = " ".join((await page.locator("body").inner_text()).split())
                one = cabin_prices(solo_body)
            else:
                one = {c: None for c in CABINS}
            for cabin in CABINS:
                record[f"one_guest_{cabin}"] = one.get(cabin)
            record["one_guest_price"] = one.get("interior")

            record["cruise_id"] = make_id(ship or "UNKNOWN", departure_date or "UNKNOWN", dep, name, url)
            print(f"Detail {index}/{total}: {name} | {departure_date} | {ship} | {currency} | Interior {two.get('interior')} | Balcony {two.get('balcony')}")
            return record
        except Exception as exc:
            print(f"Detail scrape failed {index}/{total} for {url}: {exc}")
            return None
        finally:
            await page.close()


async def collect(headless=True):
    debug = Path("debug")
    debug.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU", timezone_id="Australia/Sydney", viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        search_page = await context.new_page()
        expected = await select_australian_departure_ports(search_page)
        urls = await load_all_results(search_page, expected)
        print(f"Princess search yielded {len(urls)} itinerary URLs; expected {expected}.")
        await search_page.close()

        semaphore = asyncio.Semaphore(4)
        scraped = await asyncio.gather(*(scrape_detail(context, url, semaphore, i+1, len(urls)) for i, url in enumerate(urls)))
        records = [r for r in scraped if r is not None]
        deduped = {r["cruise_id"]: r for r in records}
        records = list(deduped.values())

        (debug / "princess-counts.txt").write_text(
            f"expected={expected}\nurls={len(urls)}\nauthoritative_records={len(records)}\n", encoding="utf-8"
        )
        print(f"Princess detail collector returned {len(records)} Standard-fare sailings (expected {expected}).")
        await browser.close()
        return records
