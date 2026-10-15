from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

SEARCH_URL = "https://www.princess.com/cruise-search/search?resType=C&sortBy=priceLowToHigh"
PORTS = ("Sydney", "Brisbane", "Melbourne")

MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
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
PORT_RE = re.compile(r"(?:^|[•,])\s*([^•]+?),\s*Australia\s*(?=•|,|$)", re.I)


def money(text: str) -> Optional[float]:
    m = MONEY_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_date(text: str) -> Optional[str]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return datetime.strptime(raw, "%b %d %Y").date().isoformat()


def departure_port(text: str) -> Optional[str]:
    # Princess lists the departure port first in each itinerary string.
    m = PORT_RE.search(text or "")
    if m:
        first = m.group(1).strip()
        for p in PORTS:
            if first.lower() == p.lower():
                return p

    # Conservative fallback for cards whose accessible text omits separators.
    beginning = (text or "")[:220].lower()
    for p in PORTS:
        if f"{p.lower()}, australia" in beginning:
            return p
    return None


def make_id(ship: str, date: str, dep: str, name: str, url: str = "") -> str:
    key = f"princess|{ship}|{date}|{dep}|{name}|{url}".lower()
    digest = hashlib.sha1(key.encode()).hexdigest()[:14]
    return f"PRINCESS-{digest}"


async def dismiss_noise(page: Page):
    for label in ("Accept All Cookies", "Accept Cookies", "Accept", "I Agree", "Close"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count():
                await btn.first.click(timeout=1200)
        except Exception:
            pass


async def load_all_results(page: Page):
    # Search results are progressively loaded. Keep pressing Load More until it disappears.
    for _ in range(30):
        try:
            button = page.get_by_role("button", name=re.compile(r"^Load More$", re.I))
            if not await button.count() or not await button.first.is_visible():
                break
            await button.first.click(timeout=5000)
            await page.wait_for_timeout(1200)
        except Exception:
            break


async def choose_guest_count(page: Page, guests: int) -> bool:
    """Best-effort guest selection. Failure is non-fatal; cards can still be collected."""
    await dismiss_noise(page)

    triggers = [
        page.get_by_text(re.compile(r"Guests?:\s*\d+", re.I)),
        page.get_by_role("button", name=re.compile(r"Rooms?\s*&\s*guests?|Guests?", re.I)),
        page.get_by_text(re.compile(r"\d+\s*guests? selected", re.I)),
    ]
    for trigger in triggers:
        try:
            if await trigger.count() and await trigger.first.is_visible():
                await trigger.first.click(timeout=2000)
                break
        except Exception:
            continue
    else:
        return False

    for pattern in (rf"^{guests}\s*Guests?$", rf"{guests}\s*guests? selected"):
        try:
            option = page.get_by_text(re.compile(pattern, re.I))
            if await option.count() and await option.last.is_visible():
                await option.last.click(timeout=2000)
                apply = page.get_by_role("button", name=re.compile(r"^Apply$", re.I))
                if await apply.count() and await apply.last.is_visible():
                    await apply.last.click(timeout=2000)
                await page.wait_for_timeout(1800)
                return True
        except Exception:
            pass
    return False


async def result_cards(page: Page):
    # New Princess search renders itinerary names as H2 headings with a View Details action.
    headings = page.locator("h2").filter(has_text=re.compile(r"\d+\s*[- ]?Day", re.I))
    if await headings.count():
        cards = []
        for i in range(await headings.count()):
            heading = headings.nth(i)
            # Walk up until the element contains the result action.
            node = heading
            selected = None
            for _ in range(8):
                node = node.locator("xpath=..")
                try:
                    txt = await node.inner_text(timeout=1000)
                except Exception:
                    continue
                if re.search(r"View Details|Cruise Details", txt, re.I):
                    selected = node
                    break
            if selected is not None:
                cards.append(selected)
        if cards:
            return cards

    # Compatibility fallbacks for alternate Princess deployments.
    for selector in (
        "article",
        "[data-testid*='cruise']",
        "[class*='cruise-card']",
        "[class*='CruiseCard']",
        "[class*='search-result']",
    ):
        loc = page.locator(selector)
        if await loc.count() >= 2:
            return [loc.nth(i) for i in range(await loc.count())]

    buttons = page.get_by_text(re.compile(r"View Details|Cruise Details", re.I))
    cards = []
    for i in range(await buttons.count()):
        node = buttons.nth(i)
        for _ in range(8):
            node = node.locator("xpath=..")
            try:
                txt = await node.inner_text(timeout=1000)
            except Exception:
                continue
            if NIGHTS_RE.search(txt):
                cards.append(node)
                break
    return cards


async def extract_cards(page: Page, guests: int):
    cards = await result_cards(page)
    results = []
    seen = set()

    for card in cards:
        try:
            raw = await card.inner_text(timeout=2500)
            text = " ".join(raw.split())
        except Exception:
            continue

        n = NIGHTS_RE.search(text)
        if not n:
            continue

        dep = departure_port(text)
        if dep not in PORTS:
            continue

        nights = int(n.group(1))

        name = None
        try:
            h2 = card.locator("h2")
            if await h2.count():
                name = " ".join((await h2.first.inner_text()).split())
        except Exception:
            pass
        if not name:
            m = re.search(r"(\d+\s*[- ]?Day\s+.{2,100}?)(?=\d+\s+ports?|Price|View Details|$)", text, re.I)
            name = m.group(1).strip() if m else f"{nights}-Day Princess Cruise"

        departure_date = parse_date(text)

        ship = None
        sm = SHIP_RE.search(text)
        if sm:
            ship = sm.group(0)

        # AU Princess normally labels the displayed fare as Interior from $X.
        price = None
        for pattern in (
            r"Interior\s+from\D{0,30}\$\s*([\d,]+(?:\.\d+)?)",
            r"from\D{0,20}\$\s*([\d,]+(?:\.\d+)?)",
            r"\$\s*([\d,]+(?:\.\d+)?)",
        ):
            pm = re.search(pattern, text, re.I)
            if pm:
                price = float(pm.group(1).replace(",", ""))
                break

        url = SEARCH_URL
        try:
            links = card.locator("a")
            for j in range(await links.count()):
                href = await links.nth(j).get_attribute("href")
                label = " ".join((await links.nth(j).inner_text()).split())
                if href and ("itinerary" in href.lower() or "view details" in label.lower() or "cruise" in href.lower()):
                    url = href if href.startswith("http") else "https://www.princess.com" + href
                    break
        except Exception:
            pass

        # Use options/select text as a second chance to obtain date/ship when hidden from card text.
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
            "itinerary": text[:800],
            "url": url,
            "available": "sold out" not in text.lower() and "unavailable" not in text.lower(),
            "currency": "AUD",
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
    for r in one + two:
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
        )
        page = await context.new_page()
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(7000)
        await dismiss_noise(page)

        # Wait for the new Princess result page instead of assuming cards are ready after DOMContentLoaded.
        try:
            await page.get_by_text(re.compile(r"Search Results|We found \d+ cruises", re.I)).first.wait_for(timeout=15000)
        except Exception:
            pass

        await load_all_results(page)

        # Capture default pricing first. Princess AU normally defaults to two guests.
        two = await extract_cards(page, 2)

        # Then attempt to switch to one guest. If the search UI does not expose the control,
        # keep the two-guest results and leave solo pricing blank rather than losing all cruises.
        one = []
        if await choose_guest_count(page, 1):
            await load_all_results(page)
            one = await extract_cards(page, 1)

        records = merge(one, two)

        # Always save a lightweight diagnostics snapshot; on failures the workflow uploads it.
        (debug / "princess-page-url.txt").write_text(page.url, encoding="utf-8")
        (debug / "princess-page-text.txt").write_text((await page.locator("body").inner_text())[:250000], encoding="utf-8")

        if not records:
            await page.screenshot(path=str(debug / "princess-no-results.png"), full_page=True)
            (debug / "princess-no-results.html").write_text(await page.content(), encoding="utf-8")

        await browser.close()
        return records
