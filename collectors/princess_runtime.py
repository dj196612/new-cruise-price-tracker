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


def _solo_cabin_prices(cruises: list[dict], fare_type: str = SOLO_FARE_TYPE) -> tuple[dict[str, float | None], Counter]:
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
                value = guests[0].get("fare")
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
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


def _solo_payload(template_payload: dict) -> dict:
    payload = copy.deepcopy(template_payload)
    booking = payload.setdefault("booking", {})
    booking["currencyCode"] = "AUD"
    booking["guests"] = [{"country": "AU", "homeCity": "SYD"}]
    booking.setdefault("bookingAgency", {}).update({
        "id": "DIRECTAU",
        "bookingCompany": "PA",
        "currency": "AUD",
        "country": "AU",
    })
    payload.setdefault("filters", {})["cruises"] = []
    flags = payload.setdefault("retrieveFlags", {})
    flags["fareType"] = SOLO_FARE_TYPE
    flags["additionalGuestFare"] = True
    return payload


async def _post_pricing(context, headers: dict, payload: dict, path_code: str) -> dict | None:
    response = await context.request.post(
        PRICING_PREFIX + path_code,
        headers=headers,
        data=json.dumps(payload),
        timeout=30000,
    )
    if response.status != 200:
        print(f"Solo API {path_code}: HTTP {response.status}")
        return None
    return json.loads(await response.text())


async def _fetch_one_solo(context, headers: dict, template_payload: dict, record: dict, semaphore: asyncio.Semaphore):
    code = _voyage_code(record)
    if not code or record.get("two_guest_interior_pp") is None:
        return

    async with semaphore:
        payload = _solo_payload(template_payload)
        try:
            # Segment / combined voyages (A714B, 1735A ...) may only be priced
            # under the parent code, so retry with it and pick the exact cruise.
            attempts = [code]
            parent = _base_code(code)
            if parent:
                attempts.append(parent)

            prices, statuses, cruises, seen = None, Counter(), [], []
            for path_code in attempts:
                data = await _post_pricing(context, headers, payload, path_code)
                if not data:
                    continue
                cruises, seen = _matching_cruises(data, code)
                if not cruises:
                    continue
                prices, statuses = _solo_cabin_prices(cruises)
                if prices.get("interior") is not None or any(v is not None for v in prices.values()):
                    if path_code != code:
                        print(f"Solo API {code}: priced via parent code {path_code}")
                    break

            if not cruises:
                print(f"Solo API {code}: no matching cruise in response; ids returned={seen[:12]}")
                return
            if not prices or not any(v is not None for v in prices.values()):
                print(f"Solo API {code}: no bookable {SOLO_FARE_TYPE} solo fare; category statuses={dict(statuses)}")
                return

            for cabin in base.CABINS:
                record[f"one_guest_{cabin}"] = prices.get(cabin)
            record["one_guest_price"] = prices.get("interior")
            if not record.get("departure_date"):
                api_date = _sail_date(cruises)
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


async def _populate_solo_prices(records: list[dict], headless: bool):
    priced = [r for r in records if r.get("two_guest_interior_pp") is not None and _voyage_code(r)]
    if not priced:
        return

    seed_url = priced[0].get("url")
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
        semaphore = asyncio.Semaphore(6)
        await asyncio.gather(*(
            _fetch_one_solo(context, headers, template_payload, record, semaphore)
            for record in priced
        ))
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
