from __future__ import annotations

import asyncio
import copy
import json
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from . import princess as base


PRICING_PREFIX = "https://gw.api.princess.com/pcl-web/internal/caps/pc/pricing/v1/cruises/"
META_TO_CABIN = {
    "I": "interior",
    "O": "oceanview",
    "B": "balcony",
    "M": "mini_suite",
    "S": "suite",
}


async def _disable_page_solo(_page, _guests: int) -> bool:
    """Base collector gets 2-person prices only; solo is filled from the pricing API."""
    return False


def _voyage_code(record: dict) -> str | None:
    code = record.get("voyage_code")
    if code:
        return str(code)
    try:
        values = parse_qs(urlsplit(record.get("url") or "").query).get("voyageCode")
        return values[0] if values else None
    except Exception:
        return None


_DATE_KEYS = ("saildate", "departuredate", "embarkdate", "embarkationdate", "startdate", "departdate")


def _to_iso(value) -> str | None:
    raw = str(value or "")
    m = re.match(r"^(20\d{2})-?(\d{2})-?(\d{2})", raw)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (2026 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _find_voyage_date(obj, code: str, depth: int = 0) -> str | None:
    """Walk a Princess JSON feed for the object describing this voyage code and return its sail date."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        ids = {str(obj.get(k)).strip().upper() for k in ("cruiseId", "voyageCode", "voyageId", "productId", "id", "code") if obj.get(k)}
        if code.upper() in ids:
            for key, value in obj.items():
                if key.lower() in _DATE_KEYS:
                    iso = _to_iso(value)
                    if iso:
                        return iso
        for value in obj.values():
            if isinstance(value, (dict, list)):
                found = _find_voyage_date(value, code, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for value in obj[:2000]:
            found = _find_voyage_date(value, code, depth + 1)
            if found:
                return found
    return None


async def _voyage_header_text(page, h1_text: str, body: str) -> str:
    """Return text local to the current itinerary title, not the whole Princess page."""
    best = h1_text or ""
    try:
        h1 = page.locator("h1").first
        node = h1
        for _ in range(8):
            node = node.locator("xpath=..")
            text = " ".join((await node.inner_text(timeout=1000)).split())
            if not text:
                continue

            # Keep the smallest useful ancestor around this voyage title.
            if h1_text and h1_text.lower() not in text.lower():
                continue
            if len(text) < 6000:
                best = text

            if base.parse_date(text) and base.SHIP_RE.search(text):
                return text

            if len(text) > 12000:
                break
    except Exception:
        pass

    # DOM structures can vary. As a second local-only fallback, take a short
    # slice immediately after this itinerary title in the rendered page text.
    if h1_text:
        pos = body.lower().find(h1_text.lower())
        if pos >= 0:
            local = body[pos:pos + 2500]
            if base.parse_date(local) or base.SHIP_RE.search(local):
                return local

    return best


async def _scrape_detail_fixed(context, item: dict, semaphore: asyncio.Semaphore, index: int, total: int):
    """Princess detail scraper with voyage-local ship/date extraction."""
    async with semaphore:
        page = await context.new_page()
        feed_dates: list[str] = []

        async def sniff_date(resp):
            if feed_dates or "princess.com" not in resp.url or "json" not in (resp.headers.get("content-type") or ""):
                return
            try:
                found = _find_voyage_date(await resp.json(), item["voyage_code"])
            except Exception:
                return
            if found:
                feed_dates.append(found)

        page.on("response", sniff_date)
        try:
            url = base.au_detail_url(item["url"])
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await base.dismiss_noise(page)
            body = await base.wait_for_cabin_prices(page, 20)

            two = base.cabin_prices(body)
            currency = base.detect_currency(body)

            h1_text = ""
            try:
                h1 = page.locator("h1")
                if await h1.count():
                    h1_text = " ".join((await h1.first.inner_text()).split())
            except Exception:
                pass

            name = h1_text or item.get("name") or "Princess Cruise"
            tm = base.TITLE_RE.search(name)
            nights = int(tm.group(1)) if tm else item.get("nights")
            if not nights or nights > 120:
                nights = item.get("nights")

            header_text = await _voyage_header_text(page, h1_text, body)
            departure_date = base.parse_date(header_text)
            if not departure_date:
                await page.wait_for_timeout(300)
                if feed_dates:
                    departure_date = feed_dates[0]
                    print(f"Date for {item['voyage_code']} taken from Princess data feed: {departure_date}")
                else:
                    print(f"Date missing for {item['voyage_code']}: not in page header or data feeds")
            ship_match = base.SHIP_RE.search(header_text)
            ship = ship_match.group(0) if ship_match else None
            dep = item.get("departure_port") or base.departure_port(header_text) or base.departure_port(body)

            one = {c: None for c in base.CABINS}
            if await base.set_guest_count_on_detail(page, 1):
                solo_body = await base.wait_for_cabin_prices(page, 15)
                one = base.cabin_prices(solo_body)

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
            for cabin in base.CABINS:
                record[f"two_guest_{cabin}_pp"] = two.get(cabin)
                record[f"one_guest_{cabin}"] = one.get(cabin)
            record["two_guest_pp_price"] = two.get("interior")
            record["one_guest_price"] = one.get("interior")
            record["two_guest_total"] = round(two["interior"] * 2, 2) if two.get("interior") is not None else None
            record["cruise_id"] = base.make_id(item["voyage_code"], departure_date)

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


# The 2-guest price on the dashboard is scraped from the page as Princess
# Standard (fare_basis "Princess Standard", Plus shown as a per-night add-on),
# so solo must be requested on the same fare type or solo premium % compares
# Standard against Plus.
SOLO_FARE_TYPE = "BESTFARE"
BOOKABLE_STATUSES = {"Y", "G", "B", "A"}
_ID_KEYS = ("cruiseId", "voyageCode", "productId", "id", "code")


def _ids(obj: dict) -> set[str]:
    return {str(obj.get(k)).strip().upper() for k in _ID_KEYS if obj.get(k)}


def _base_code(code: str) -> str | None:
    """A714B -> A714, 1735A -> 1735 (segment / combined voyages)."""
    m = re.match(r"^(.*\d)[A-Z]+$", code.upper())
    return m.group(1) if m else None


def _matching_cruises(data: dict, code: str) -> tuple[list[dict], list[str]]:
    """Return only the cruise(s) for this voyage code, never a mix of segments.

    Matches at cruise level first, then product level. If the response holds a
    single cruise it is used as-is (the per-code endpoint's normal case).
    """
    code = code.upper()
    products = data.get("products") or []
    all_cruises = [(p, c) for p in products for c in (p.get("cruises") or [])]
    seen = sorted({i for p, c in all_cruises for i in (_ids(c) | _ids(p))})

    exact = [c for p, c in all_cruises if code in _ids(c)]
    if exact:
        return exact, seen
    by_product = [c for p, c in all_cruises if code in _ids(p)]
    if by_product:
        return by_product, seen
    if len(all_cruises) == 1:
        return [all_cruises[0][1]], seen
    return [], seen


_CHARGE_HINTS = ("tax", "fee", "ncf", "port", "charge", "gov")
PRICE_MODES = ("fare", "fare+charges", "avg", "avg+charges")
_price_mode = {"value": "fare", "scale": 1.0}  # chosen by _calibrate_two_guest


def _amount(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("amount", "value", "total"):
            found = _amount(value.get(key))
            if found is not None:
                return found
    return None


def _guest_charges(guest: dict) -> float:
    total = 0.0
    for key, value in guest.items():
        low = key.lower()
        if "fare" in low or not any(h in low for h in _CHARGE_HINTS):
            continue
        amount = _amount(value)
        if amount:
            total += amount
    return total


def _guest_price(guests: list[dict], mode: str) -> float | None:
    fares = [_amount(g.get("fare")) for g in guests]
    if not fares or any(f is None for f in fares):
        return None
    if mode.startswith("avg"):
        value = sum(fares) / len(fares)
        if mode.endswith("+charges"):
            value += sum(_guest_charges(g) for g in guests) / len(guests)
        return value
    first_index = next((i for i, g in enumerate(guests) if str(g.get("id")) == "1"), 0)
    value = fares[first_index]
    if mode.endswith("+charges"):
        value += _guest_charges(guests[first_index])
    return value


def _sample_guest(cruises: list[dict]) -> str:
    for cruise in cruises:
        for fare in ((cruise.get("pricing") or {}).get("fares") or []):
            for category in fare.get("categories") or []:
                guests = category.get("guests") or []
                if guests:
                    cat_keys = sorted(k for k in category if k != "guests")
                    return f"category keys={cat_keys}; guests={json.dumps(guests)[:500]}"
    return "no guests found"


def _solo_cabin_prices(cruises: list[dict], fare_type: str = SOLO_FARE_TYPE, mode: str | None = None) -> tuple[dict[str, float | None], Counter]:
    result = {c: None for c in base.CABINS}
    statuses: Counter = Counter()

    for cruise in cruises:
        pricing = cruise.get("pricing") or {}
        for fare in pricing.get("fares", []):
            if (fare.get("fareType") or "").upper() != fare_type.upper():
                continue
            for category in fare.get("categories", []):
                category_id = str(category.get("id") or "")
                if not category_id:
                    continue
                cabin = META_TO_CABIN.get(category_id[0].upper())
                if cabin is None:
                    continue
                status = (category.get("status") or "").upper()
                statuses[status or "?"] += 1
                if status not in BOOKABLE_STATUSES:
                    continue
                guests = category.get("guests") or []
                if not guests:
                    continue
                value = _guest_price(guests, mode or _price_mode["value"])
                if value is None:
                    continue
                if mode is None:
                    value = round(value * _price_mode["scale"], 2)
                current = result[cabin]
                if current is None or value < current:
                    result[cabin] = value
    return result, statuses


def _sail_date(cruises: list[dict]) -> str | None:
    for cruise in cruises:
        raw = str(cruise.get("sailDate") or cruise.get("departureDate") or "")
        digits = re.sub(r"\D", "", raw)[:8]
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return None


async def _capture_pricing_session(context, seed_url: str):
    page = await context.new_page()
    captured = {"headers": None, "payload": None}

    async def on_request(request):
        if request.method == "POST" and request.url.startswith(PRICING_PREFIX) and captured["headers"] is None:
            captured["headers"] = await request.all_headers()
            try:
                captured["payload"] = json.loads(request.post_data or "{}")
            except Exception:
                captured["payload"] = None

    page.on("request", on_request)
    await page.goto(seed_url, wait_until="domcontentloaded", timeout=90000)
    await base.dismiss_noise(page)
    await base.wait_for_cabin_prices(page, 25)

    for _ in range(30):
        if captured["headers"] and captured["payload"]:
            break
        await page.wait_for_timeout(250)
    await page.close()

    if not captured["headers"] or not captured["payload"]:
        raise RuntimeError("Could not capture Princess pricing request needed for solo fares")

    headers = dict(captured["headers"])
    for key in [
        "content-length", "host", ":authority", ":method", ":path", ":scheme",
        "accept-encoding", "cookie",
    ]:
        headers.pop(key, None)
    return headers, captured["payload"]


def _solo_payload(template_payload: dict, guests: int = 1, fare_flag: str | None = None) -> dict:
    payload = copy.deepcopy(template_payload)
    booking = payload.setdefault("booking", {})
    booking["currencyCode"] = "AUD"
    booking["guests"] = [{"country": "AU", "homeCity": "SYD"} for _ in range(guests)]
    booking.setdefault("bookingAgency", {}).update({
        "id": "DIRECTAU",
        "bookingCompany": "PA",
        "currency": "AUD",
        "country": "AU",
    })
    payload.setdefault("filters", {})["cruises"] = []
    flags = payload.setdefault("retrieveFlags", {})
    flags["fareType"] = fare_flag or SOLO_FARE_TYPE
    flags["additionalGuestFare"] = True
    return payload


class _PricingSession:
    """Captured Princess pricing headers, refreshed once when they start returning 403.

    The captured request carries a short-lived session token; long runs began
    getting HTTP 403 part-way through, so re-capture from a fresh page load.
    """

    def __init__(self, context, seed_url: str, headers: dict, template: dict):
        self.context = context
        self.seed_url = seed_url
        self.headers = headers
        self.template = template
        self.generation = 0
        self.refreshes = 0
        self.two_guest_ok = False
        self._lock = asyncio.Lock()

    async def refresh(self, seen_generation: int) -> bool:
        async with self._lock:
            if self.generation != seen_generation:
                return True  # another request already refreshed it
            if self.refreshes >= 40:
                return False
            try:
                self.headers, self.template = await _capture_pricing_session(self.context, self.seed_url)
            except Exception as exc:
                print(f"Solo API session refresh failed: {exc}")
                return False
            self.generation += 1
            self.refreshes += 1
            print(f"Solo API session refreshed after HTTP 403 (refresh {self.refreshes})")
            return True


async def _post_pricing(session: "_PricingSession", payload: dict, path_code: str) -> tuple[int, dict | None]:
    for attempt in (1, 2):
        generation = session.generation
        response = await session.context.request.post(
            PRICING_PREFIX + path_code,
            headers=session.headers,
            data=json.dumps(payload),
            timeout=30000,
        )
        if response.status == 403 and attempt == 1 and await session.refresh(generation):
            continue
        if response.status != 200:
            return response.status, None
        return 200, json.loads(await response.text())
    return 403, None


async def _fetch_fares(session: "_PricingSession", code: str, guests: int):
    """Price one voyage for `guests` guests. Returns (prices, statuses, cruises, data, notes)."""
    attempts = [code]
    parent = _base_code(code)
    if parent:
        attempts.append(parent)
    notes: list[str] = []
    prices, statuses, cruises, data, seen = None, Counter(), [], None, []
    for path_code in attempts:
        # Some voyages return 403 when requested on the Standard flag but price
        # normally on the Plus flag; the response carries every fare type
        # either way and we still read the Standard (BESTFARE) fare from it.
        for flag in (SOLO_FARE_TYPE, "BESTVALUE"):
            status, data = await _post_pricing(session, _solo_payload(session.template, guests, flag), path_code)
            if status == 200:
                if flag != SOLO_FARE_TYPE:
                    notes.append(f"requested with {flag} flag after HTTP 403")
                break
            notes.append(f"{path_code} {flag}: HTTP {status}")
            if status != 403:
                break
        if not data:
            continue
        cruises, seen = _matching_cruises(data, code)
        if not cruises:
            continue
        prices, statuses = _solo_cabin_prices(cruises)
        if any(v is not None for v in prices.values()):
            if path_code != code:
                notes.append(f"priced via parent code {path_code}")
            break
    if not cruises:
        notes.append(f"no matching cruise in response; ids returned={seen[:12]}")
    return prices, statuses, cruises, data, notes


async def _fetch_one_solo(session: "_PricingSession", record: dict, semaphore: asyncio.Semaphore):
    code = _voyage_code(record)
    if not code:
        return

    async with semaphore:
        try:
            # 2 guests from the API when the page scrape found no 2-guest price.
            if record.get("two_guest_interior_pp") is None and session.two_guest_ok:
                two, _, two_cruises, two_data, notes = await _fetch_fares(session, code, 2)
                if two and any(v is not None for v in two.values()):
                    for cabin in base.CABINS:
                        if record.get(f"two_guest_{cabin}_pp") is None:
                            record[f"two_guest_{cabin}_pp"] = two.get(cabin)
                    record["two_guest_pp_price"] = record.get("two_guest_interior_pp")
                    if record["two_guest_pp_price"] is not None:
                        record["two_guest_total"] = round(record["two_guest_pp_price"] * 2, 2)
                    record["available"] = True
                    record["currency"] = record.get("currency") or "AUD"
                    print(f"Solo API {code}: 2-guest filled from pricing API: Interior {two.get('interior')} | Balcony {two.get('balcony')}")
                else:
                    print(f"Solo API {code}: no 2-guest price from pricing API either ({'; '.join(notes) or 'no bookable fare'})")

            prices, statuses, cruises, data, notes = await _fetch_fares(session, code, 1)
            for note in notes:
                print(f"Solo API {code}: {note}")
            if not cruises:
                if any("HTTP 403" in n for n in notes):
                    record["_solo_403"] = True
                return
            if not prices or not any(v is not None for v in prices.values()):
                fare_types = sorted({(f.get("fareType") or "?") for c in cruises for f in ((c.get("pricing") or {}).get("fares") or [])})
                print(f"Solo API {code}: no bookable {SOLO_FARE_TYPE} solo fare; category statuses={dict(statuses)}; fare types offered={fare_types}")
                return

            for cabin in base.CABINS:
                record[f"one_guest_{cabin}"] = prices.get(cabin)
            record["one_guest_price"] = prices.get("interior")
            if prices.get("interior") is None:
                print(f"Solo API {code}: no Interior solo fare (other cabins priced)")
            if not record.get("departure_date"):
                api_date = _sail_date(cruises) or (_find_voyage_date(data, code) if data else None)
                if api_date:
                    record["departure_date"] = api_date
                    record["cruise_id"] = base.make_id(code, api_date)
                    print(f"Solo API {code}: departure date filled from pricing API -> {api_date}")
            print(
                f"Solo API {code}: Interior {prices.get('interior')} | "
                f"Balcony {prices.get('balcony')} | Suite {prices.get('suite')}"
            )
        except Exception as exc:
            print(f"Solo API {code} failed: {exc}")


def _currency_hints(obj, depth: int = 0, found: set | None = None) -> set:
    found = set() if found is None else found
    if depth > 8 or len(found) > 20:
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            if "currenc" in key.lower() and isinstance(value, (str, int, float)):
                found.add(f"{key}={value}")
            elif isinstance(value, (dict, list)):
                _currency_hints(value, depth + 1, found)
    elif isinstance(obj, list):
        for value in obj[:20]:
            _currency_hints(value, depth + 1, found)
    return found


async def _calibrate_two_guest(session: "_PricingSession", seeded: list[dict]) -> None:
    """Pick how to read API prices so they match the page's 2-guest Standard price.

    The page price may include taxes/fees that the API's `fare` excludes; the
    same reading is then used for solo prices so both sides are comparable.
    """
    samples = []  # (page price, cruises)
    for record in seeded[:8]:
        code = _voyage_code(record)
        try:
            _, _, cruises, data, _ = await _fetch_fares(session, code, 2)
        except Exception:
            continue
        if cruises and record.get("two_guest_interior_pp"):
            samples.append((record["two_guest_interior_pp"], cruises))
            if len(samples) == 1:
                print(f"Princess calibration response currency fields: {sorted(_currency_hints(data))}")
    if samples:
        print(f"Princess calibration sample: {_sample_guest(samples[0][1])}")
        req = _solo_payload(session.template, 2)
        print(f"Princess calibration request: booking={json.dumps({k: v for k, v in req.get('booking', {}).items() if k != 'guests'})[:300]} "
              f"other currency fields={sorted(_currency_hints({k: v for k, v in req.items() if k != 'booking'}))}")

    best = None
    fare_ratios: list[float] = []
    for mode in PRICE_MODES:
        ratios = []
        for page, cruises in samples:
            api = _solo_cabin_prices(cruises, mode=mode)[0].get("interior")
            if api:
                ratios.append(api / page)
        if len(ratios) < 3:
            continue
        ratios.sort()
        median = ratios[len(ratios) // 2]
        if mode == "fare":
            fare_ratios = ratios
        print(f"Princess calibration mode={mode}: median API/page ratio {median:.3f} over {len(ratios)} voyages (range {ratios[0]:.3f}-{ratios[-1]:.3f})")
        if 0.97 <= median <= 1.03 and (best is None or abs(median - 1) < abs(best[1] - 1)):
            best = (mode, median)

    _price_mode["scale"] = 1.0
    if best:
        _price_mode["value"] = best[0]
        session.two_guest_ok = True
        print(f"Princess calibration: using mode={best[0]} for solo and 2-guest API prices (ratio {best[1]:.3f})")
    elif len(fare_ratios) >= 5 and fare_ratios[-1] / fare_ratios[0] <= 1.02 and 0.4 <= fare_ratios[len(fare_ratios) // 2] <= 0.95:
        # Every sample differs from the page by the same factor: the API is
        # pricing in another currency. Convert to the page's AUD with the
        # factor measured on this run.
        median = fare_ratios[len(fare_ratios) // 2]
        _price_mode["value"] = "fare"
        _price_mode["scale"] = 1 / median
        session.two_guest_ok = True
        print(
            f"Princess calibration: API prices are a constant {median:.3f}x the page's AUD price "
            f"(range {fare_ratios[0]:.3f}-{fare_ratios[-1]:.3f}); converting API prices x{1 / median:.4f} to AUD"
        )
    else:
        _price_mode["value"] = "fare"
        session.two_guest_ok = False
        print("Princess calibration: no reading matches the page price; solo uses API fare only and missing 2-guest prices are NOT filled")


async def _populate_solo_prices(records: list[dict], headless: bool):
    targets = [r for r in records if _voyage_code(r)]
    seeded = [r for r in targets if r.get("two_guest_interior_pp") is not None]
    if not seeded:
        return

    seed_url = seeded[0].get("url")
    if not seed_url:
        return

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
        headers, template_payload = await _capture_pricing_session(context, seed_url)
        session = _PricingSession(context, seed_url, headers, template_payload)
        await _calibrate_two_guest(session, seeded)
        semaphore = asyncio.Semaphore(3)
        await asyncio.gather(*(
            _fetch_one_solo(session, record, semaphore)
            for record in targets
        ))

        # Second pass for voyages blocked by 403s: fresh session, one at a time.
        retry = [r for r in targets if r.get("_solo_403")]
        if retry:
            print(f"Solo API second pass: {len(retry)} voyages hit HTTP 403; retrying slowly with a fresh session")
            await asyncio.sleep(20)
            await session.refresh(session.generation)
            single = asyncio.Semaphore(1)
            for record in retry:
                record.pop("_solo_403", None)
                await _fetch_one_solo(session, record, single)
                await asyncio.sleep(1.5)
            still = sum(1 for r in retry if r.get("_solo_403"))
            print(f"Solo API second pass: {len(retry) - still}/{len(retry)} recovered")
        for record in targets:
            record.pop("_solo_403", None)
        await browser.close()


def _validate_metadata(records: list[dict]):
    dated = [r.get("departure_date") for r in records if r.get("departure_date")]
    ships = [r.get("ship") for r in records if r.get("ship")]

    if not dated:
        raise RuntimeError("Princess returned no departure dates. Refusing to publish undated cruise data.")

    date_counts = Counter(dated)
    repeated_date, repeated_count = date_counts.most_common(1)[0]
    if repeated_count > 10:
        raise RuntimeError(
            f"Princess metadata looks invalid: departure date {repeated_date} appeared on "
            f"{repeated_count} cruises. Refusing to publish."
        )

    # A716A is our known-good Princess sentinel: Grand Princess, 2 Sep 2027.
    sentinel = next((r for r in records if _voyage_code(r) == "A716A"), None)
    if sentinel is None:
        raise RuntimeError("Princess validation voyage A716A was not returned.")
    if sentinel.get("ship") != "Grand Princess" or sentinel.get("departure_date") != "2027-09-02":
        raise RuntimeError(
            "Princess voyage metadata validation failed for A716A: "
            f"ship={sentinel.get('ship')!r}, date={sentinel.get('departure_date')!r}. "
            "Expected Grand Princess / 2027-09-02."
        )

    print(f"Princess metadata validated: {len(dated)}/{len(records)} dates, {len(ships)}/{len(records)} ships.")


async def collect(headless: bool = True):
    # Replace only the unreliable metadata portion of the base detail scraper.
    original_set_guest = base.set_guest_count_on_detail
    original_scrape_detail = base.scrape_detail
    base.set_guest_count_on_detail = _disable_page_solo
    base.scrape_detail = _scrape_detail_fixed
    try:
        records = await base.collect(headless=headless)
    finally:
        base.set_guest_count_on_detail = original_set_guest
        base.scrape_detail = original_scrape_detail

    priced = [r for r in records if r.get("two_guest_interior_pp") is not None]
    if not priced:
        raise RuntimeError("Princess returned no usable cabin prices. Refusing to overwrite the live dashboard.")

    non_aud = [r for r in priced if (r.get("currency") or "").upper() != "AUD"]
    if non_aud:
        currencies = sorted({(r.get("currency") or "UNKNOWN").upper() for r in non_aud})
        raise RuntimeError(
            f"Princess returned non-AUD pricing ({', '.join(currencies)}). Refusing to overwrite the live dashboard."
        )

    _validate_metadata(records)
    await _populate_solo_prices(records, headless=headless)

    solo_priced = sum(1 for r in records if r.get("one_guest_interior") is not None)
    print(f"Princess solo Interior pricing captured for {solo_priced}/{len(records)} cruises.")
    if solo_priced == 0:
        raise RuntimeError("Princess returned no usable solo pricing. Refusing to publish an empty 1-person view.")

    return records
