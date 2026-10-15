from __future__ import annotations
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Page, Locator

SEARCH_URL = "https://www.princess.com/cruise-search/results/?resType=C"
PORTS = ("Sydney", "Brisbane", "Melbourne")

MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
NIGHTS_RE = re.compile(r"(\d+)\s*[- ]?\s*(?:Day|Night)", re.I)
DATE_RE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)?,?\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.I
)

def money(text: str) -> Optional[float]:
    m = MONEY_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None

def normalise_port(text: str) -> Optional[str]:
    low = (text or "").lower()
    for p in PORTS:
        if p.lower() in low:
            return p
    return None

def parse_date(text: str) -> Optional[str]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    raw = f"{m.group(2)} {m.group(3)} {m.group(4)}"
    return datetime.strptime(raw, "%b %d %Y").date().isoformat()

def make_id(ship: str, date: str, dep: str, name: str) -> str:
    key = f"princess|{ship}|{date}|{dep}|{name}".lower()
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    return f"PRINCESS-{digest}"

async def dismiss_noise(page: Page):
    for label in ("Accept All Cookies", "Accept", "I Agree", "Close"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if await btn.count():
                await btn.first.click(timeout=1500)
        except Exception:
            pass

async def choose_guest_count(page: Page, guests: int):
    """
    Best-effort text-driven guest control.
    Princess changes its React markup periodically, so this deliberately avoids brittle CSS classes.
    """
    await dismiss_noise(page)
    guest_triggers = [
        page.get_by_text(re.compile(r"Guests?:\s*\d+", re.I)),
        page.get_by_role("button", name=re.compile(r"Guest", re.I)),
        page.get_by_text(re.compile(r"\d+\s*Guest", re.I)),
    ]
    opened = False
    for loc in guest_triggers:
        try:
            if await loc.count():
                await loc.first.click(timeout=2500)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    # First try direct "1 Guest" / "2 Guests" options.
    for pattern in (
        rf"^{guests}\s*Guests?$",
        rf"{guests}\s*Guests?",
    ):
        try:
            option = page.get_by_text(re.compile(pattern, re.I))
            if await option.count():
                await option.last.click(timeout=2000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

    # Fallback: locate adult row and +/- controls.
    try:
        adults = page.get_by_text(re.compile(r"Adults?", re.I)).first
        container = adults.locator("xpath=..")
        plus = container.get_by_role("button", name=re.compile(r"(increase|plus|\+)", re.I))
        minus = container.get_by_role("button", name=re.compile(r"(decrease|minus|-)", re.I))
        # Princess normally starts at 1 adult.
        if guests == 2 and await plus.count():
            await plus.first.click()
            await page.wait_for_timeout(1500)
            return True
        if guests == 1 and await minus.count():
            # Only click if UI exposes a decrementable state.
            try:
                await minus.first.click(timeout=1000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False

async def candidate_cards(page: Page):
    # Search for cards by content rather than framework-generated class names.
    selectors = [
        "article",
        "[data-testid*='cruise']",
        "[class*='cruise-card']",
        "[class*='CruiseCard']",
        "[class*='search-result']",
    ]
    for s in selectors:
        loc = page.locator(s)
        if await loc.count() >= 2:
            return loc

    # Fallback: work upwards from "Cruise Details" buttons.
    buttons = page.get_by_text(re.compile(r"Cruise Details|View Cruise", re.I))
    if await buttons.count():
        return buttons.locator("xpath=ancestor::*[self::article or self::div][1]")
    return page.locator(".__no_cards__")

async def extract_cards(page: Page, guests: int):
    cards = await candidate_cards(page)
    results = []
    seen = set()

    for i in range(await cards.count()):
        card = cards.nth(i)
        try:
            text = " ".join((await card.inner_text()).split())
        except Exception:
            continue
        if "day" not in text.lower() and "night" not in text.lower():
            continue

        dep = normalise_port(text)
        if dep not in PORTS:
            continue

        n = NIGHTS_RE.search(text)
        nights = int(n.group(1)) if n else None
        departure_date = parse_date(text)

        # name: first line/segment containing Day/Night is usually the itinerary title
        name_match = re.search(r"([^|]{0,80}\b\d+\s*[- ]?(?:Day|Night)[^|]{0,100})", text, re.I)
        name = name_match.group(1).strip(" -") if name_match else text[:100]

        ship = ""
        ship_match = re.search(r"(Grand|Royal|Crown|Discovery|Diamond|Emerald|Coral|Island|Majestic|Regal|Ruby|Sapphire|Sky|Sun|Star)\s+Princess", text, re.I)
        if ship_match:
            ship = ship_match.group(0)

        # Prefer "from $X" style price.
        price = None
        fm = re.search(r"(?:from|interior from)\D{0,15}\$\s*([\d,]+(?:\.\d+)?)", text, re.I)
        if fm:
            price = float(fm.group(1).replace(",", ""))
        else:
            price = money(text)

        url = SEARCH_URL
        try:
            links = card.locator("a")
            for j in range(await links.count()):
                href = await links.nth(j).get_attribute("href")
                label = (await links.nth(j).inner_text()).strip()
                if href and ("cruise" in href.lower() or "details" in label.lower()):
                    url = href if href.startswith("http") else "https://www.princess.com" + href
                    break
        except Exception:
            pass

        key = (name, departure_date, ship, dep)
        if key in seen:
            continue
        seen.add(key)

        record = {
            "line": "Princess",
            "name": name,
            "ship": ship or None,
            "departure_port": dep,
            "arrival_port": None,
            "departure_date": departure_date,
            "nights": nights,
            "itinerary": text[:500],
            "url": url,
            "available": "sold out" not in text.lower(),
            "currency": "AUD",
        }
        record["cruise_id"] = make_id(ship or "UNKNOWN", departure_date or "UNKNOWN", dep, name)
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
            merged[key].update({k:v for k,v in r.items() if v is not None})
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
        await page.wait_for_timeout(5000)
        await dismiss_noise(page)

        # Existing Princess search state can default to 1 guest.
        await choose_guest_count(page, 1)
        await page.wait_for_timeout(2500)
        one = await extract_cards(page, 1)

        await choose_guest_count(page, 2)
        await page.wait_for_timeout(3000)
        two = await extract_cards(page, 2)

        records = merge(one, two)

        if not records:
            await page.screenshot(path=str(debug / "princess-no-results.png"), full_page=True)
            (debug / "princess-no-results.html").write_text(
                await page.content(), encoding="utf-8"
            )

        await browser.close()
        return records
