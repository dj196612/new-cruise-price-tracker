from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

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
    r"(Sydney|Melbourne|Brisbane),\s*Australia\s+to\s+([^\n]{1,100}?),\s*Australia",
    re.I,
)


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


def current_price(text: str) -> Optional[float]:
    for pattern in (
        r"\bNow\s*\$\s*([\d,]+(?:\.\d+)?)",
        r"\bWas\s*\$\s*[\d,]+(?:\.\d+)?\s*(?:Now\s*)?\$\s*([\d,]+(?:\.\d+)?)",
        r"Interior\s+from\D{0,20}\$\s*([\d,]+(?:\.\d+)?)",
        r"\bfrom\D{0,20}\$\s*([\d,]+(?:\.\d+)?)",
        r"\$\s*([\d,]+(?:\.\d+)?)",
    ):
        m = re.search(pattern, text or "", re.I)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def detect_currency(text: str) -> str:
    m = re.search(r"Price\s+in\s+(AUD|USD|NZD|CAD|GBP|EUR)", text or "", re.I)
    return m.group(1).upper() if m else "AUD"


def cruise_name(text: str, nights: int) -> str:
    m = re.search(
        r"(\d+\s*[- ]?Day\s+.{2,120}?)(?=(?:Save)?(?:Sydney|Melbourne|Brisbane),\s*Australia\s+to)",
        text,
        re.I,
    )
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else f"{nights}-Day Princess Cruise"


def make_id(ship: str, date: str, dep: str, name: str, url: str = "") -> str:
    key = f"princess|{ship}|{date}|{dep}|{name}|{url}".lower()
    return f"PRINCESS-{hashlib.sha1(key.encode()).hexdigest()[:14]}"


async def dismiss_noise(page: Page):
    for label in ("Accept All Cookies", "Accept Cookies", "Accept", "I Agree", "Close"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            for i in range(await btn.count()):
                item = btn.nth(i)
                if await item.is_visible():
                    await item.click(timeout=1500)
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
    await page.wait_for_timeout(4500)
    await dismiss_noise(page)

    trigger = await first_visible(page.get_by_text(re.compile(r"^Departure Port$", re.I)))
    if trigger is not None:
        await trigger.click(timeout=4000)
        await page.wait_for_timeout(800)

    for port in PORTS:
        target = await first_visible(page.get_by_text(re.compile(rf"^{port},\s*Australia$", re.I)))
        if target is None:
            raise RuntimeError(f"Could not select departure port: {port}")
        await target.click(timeout=3000)
        await page.wait_for_timeout(350)

    view = page.locator("button", has_text=re.compile(r"VIEW\s+\d+\s+RESULTS", re.I))
    visible_view = await first_visible(view)
    if visible_view is None:
        raise RuntimeError("Could not find a visible Princess View Results button")

    label = " ".join((await visible_view.inner_text()).split())
    m = re.search(r"(\d+)", label)
    if not m:
        raise RuntimeError(f"Could not read result count from: {label}")
    expected = int(m.group(1))
    print(f"Princess search reports {expected} matching cruises for Sydney/Melbourne/Brisbane.")

    await visible_view.scroll_into_view_if_needed()
    try:
        await visible_view.click(timeout=5000)
    except Exception:
        await visible_view.click(timeout=5000, force=True)

    await page.wait_for_timeout(5000)
    print(f"Princess results URL: {page.url}")
    return expected


async def visible_result_count(page: Page) -> int:
    headings = page.locator("h2").filter(has_text=re.compile(r"\d+\s*[- ]?Day", re.I))
    count = await headings.count()
    if count:
        return count
    return await page.get_by_text(re.compile(r"View Details|Cruise Details", re.I)).count()


async def load_all_results(page: Page, expected: int) -> int:
    previous = -1
    stable_rounds = 0

    for _ in range(20):
        count = await visible_result_count(page)
        print(f"Princess cards currently loaded: {count} / expected {expected}")
        if count >= expected:
            return count

        clicked = False
        candidates = page.locator("button").filter(has_text=re.compile(r"Load More|Show More|More Results", re.I))
        for i in range(await candidates.count()):
            btn = candidates.nth(i)
            try:
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    before = count
                    try:
                        await btn.click(timeout=5000)
                    except Exception:
                        await btn.click(timeout=5000, force=True)
                    clicked = True
                    for _ in range(12):
                        await page.wait_for_timeout(500)
                        if await visible_result_count(page) > before:
                            break
                    break
            except Exception:
                continue

        if not clicked:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        new_count = await visible_result_count(page)
        if new_count == previous:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous = new_count

        if stable_rounds >= 3:
            break

    return await visible_result_count(page)


async def choose_guest_count(page: Page, guests: int) -> bool:
    await dismiss_noise(page)
    for trigger in (
        page.get_by_text(re.compile(r"Guests?:\s*\d+", re.I)),
        page.get_by_role("button", name=re.compile(r"Rooms?\s*&\s*guests?|Guests?", re.I)),
        page.get_by_text(re.compile(r"\d+\s*guests? selected", re.I)),
    ):
        item = await first_visible(trigger)
        if item is not None:
            try:
                await item.click(timeout=2000)
                break
            except Exception:
                pass
    else:
        return False

    for pattern in (rf"^{guests}\s*Guests?$", rf"{guests}\s*guests? selected"):
        option = await first_visible(page.get_by_text(re.compile(pattern, re.I)))
        if option is not None:
            try:
                await option.click(timeout=2000)
                apply = await first_visible(page.get_by_role("button", name=re.compile(r"^Apply$", re.I)))
                if apply is not None:
                    await apply.click(timeout=2000)
                await page.wait_for_timeout(2000)
                return True
            except Exception:
                pass
    return False


async def result_cards(page: Page):
    headings = page.locator("h2").filter(has_text=re.compile(r"\d+\s*[- ]?Day", re.I))
    cards = []
    for i in range(await headings.count()):
        node = headings.nth(i)
        selected = None
        for _ in range(10):
            node = node.locator("xpath=..")
            try:
                txt = await node.inner_text(timeout=1000)
            except Exception:
                continue
            if re.search(r"View Details|Cruise Details", txt, re.I) and NIGHTS_RE.search(txt):
                selected = node
                break
        if selected is not None:
            cards.append(selected)
    return cards


async def extract_cards(page: Page, guests: int):
    cards = await result_cards(page)
    results = []
    seen = set()

    for card in cards:
        try:
            text = " ".join((await card.inner_text(timeout=2500)).split())
        except Exception:
            continue

        n = NIGHTS_RE.search(text)
        if not n:
            continue
        nights = int(n.group(1))
        dep = departure_port(text)
        if dep not in PORTS:
            continue

        name = None
        try:
            h2 = card.locator("h2")
            if await h2.count():
                name = " ".join((await h2.first.inner_text()).split())
        except Exception:
            pass
        if not name or not NIGHTS_RE.search(name):
            name = cruise_name(text, nights)

        departure_date = parse_date(text)
        ship_match = SHIP_RE.search(text)
        ship = ship_match.group(0) if ship_match else None
        price = current_price(text)
        currency = detect_currency(text)

        url = LANDING_URL
        try:
            links = card.locator("a")
            for j in range(await links.count()):
                href = await links.nth(j).get_attribute("href")
                label = " ".join((await links.nth(j).inner_text()).split())
                if href and ("itinerary" in href.lower() or "view details" in label.lower()):
                    url = href if href.startswith("http") else "https://www.princess.com" + href
                    break
        except Exception:
            pass

        if departure_date is None:
            try:
                option_text = " ".join(await card.locator("option").all_inner_texts())
                departure_date = parse_date(option_text)
                if ship is None:
                    sm = SHIP_RE.search(option_text)
                    ship = sm.group(0) if sm else None
            except Exception:
                pass

        identity = (name, departure_date, ship, dep, url)
        if identity in seen:
            continue
        seen.add(identity)

        record = {
            "line": "Princess",
            "name": name,
            "ship": ship,
            "departure_port": dep,
            "arrival_port": None,
            "departure_date": departure_date,
            "nights": nights,
            "itinerary": text[:900],
            "url": url,
            "available": "sold out" not in text.lower() and "unavailable" not in text.lower(),
            "currency": currency,
        }
        record["cruise_id"] = make_id(ship or "UNKNOWN", departure_date or "UNKNOWN", dep, name, url)

        if guests == 1:
            record["one_guest_price"] = price
        else:
            record["two_guest_pp_price"] = price
            record["two_guest_total"] = round(price * 2, 2) if price is not None else None
        results.append(record)

    return results


def merge(one, two):
    merged = {}
    for r in two + one:
        key = r["cruise_id"]
        if key not in merged:
            merged[key] = r.copy()
        else:
            merged[key].update({k: v for k, v in r.items() if v is not None})
    return list(merged.values())


async def collect(headless=True):
    debug = Path("debug")
    debug.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        page = await context.new_page()

        expected = await select_australian_departure_ports(page)
        loaded = await load_all_results(page, expected)
        print(f"Princess result loading finished at {loaded} visible cards (expected {expected}).")

        two = await extract_cards(page, 2)
        print(f"Parsed {len(two)} two-guest Princess sailings.")

        one = []
        if await choose_guest_count(page, 1):
            await load_all_results(page, expected)
            one = await extract_cards(page, 1)
            print(f"Parsed {len(one)} one-guest Princess sailings.")

        records = merge(one, two)

        (debug / "princess-page-url.txt").write_text(page.url, encoding="utf-8")
        (debug / "princess-page-text.txt").write_text((await page.locator("body").inner_text())[:300000], encoding="utf-8")
        (debug / "princess-counts.txt").write_text(
            f"expected={expected}\nloaded={loaded}\ntwo_guest_parsed={len(two)}\none_guest_parsed={len(one)}\nmerged={len(records)}\n",
            encoding="utf-8",
        )

        if not records:
            try:
                await page.screenshot(path=str(debug / "princess-no-results.png"), full_page=False, timeout=10000)
            except Exception as exc:
                print(f"Diagnostic screenshot failed: {exc}")

        await browser.close()
        return records
