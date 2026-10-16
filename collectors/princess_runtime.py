from __future__ import annotations

import asyncio
import copy
import json
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


def _solo_cabin_prices(data: dict, fare_type: str = "BESTVALUE") -> dict[str, float | None]:
    """Return the cheapest available one-person fare for each cabin meta."""
    result = {c: None for c in base.CABINS}

    for product in data.get("products", []):
        for cruise in product.get("cruises", []):
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
                    if (category.get("status") or "").upper() not in {"Y", "G", "B", "A"}:
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

    return result


async def _capture_pricing_session(context, seed_url: str):
    page = await context.new_page()
    captured = {"headers": None, "payload": None}

    async def on_request(request):
        if (
            request.method == "POST"
            and request.url.startswith(PRICING_PREFIX)
            and captured["headers"] is None
        ):
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


async def _fetch_one_solo(context, headers: dict, template_payload: dict, record: dict, semaphore: asyncio.Semaphore):
    code = _voyage_code(record)
    if not code or record.get("two_guest_interior_pp") is None:
        return

    async with semaphore:
        payload = copy.deepcopy(template_payload)
        booking = payload.setdefault("booking", {})
        booking["currencyCode"] = "AUD"
        booking["guests"] = [{"country": "AU", "homeCity": "SYD"}]

        agency = booking.setdefault("bookingAgency", {})
        agency.update({
            "id": "DIRECTAU",
            "bookingCompany": "PA",
            "currency": "AUD",
            "country": "AU",
        })

        payload.setdefault("filters", {})["cruises"] = []
        flags = payload.setdefault("retrieveFlags", {})
        flags["fareType"] = "BESTVALUE"
        flags["additionalGuestFare"] = True

        url = PRICING_PREFIX + code
        try:
            response = await context.request.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=30000,
            )
            if response.status != 200:
                print(f"Solo API {code}: HTTP {response.status}")
                return

            data = json.loads(await response.text())
            prices = _solo_cabin_prices(data, "BESTVALUE")
            for cabin in base.CABINS:
                record[f"one_guest_{cabin}"] = prices.get(cabin)
            record["one_guest_price"] = prices.get("interior")

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
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(base.AU_LOCALE),
            )

        await context.route(base.LOCALE_URL, force_locale)
        headers, template_payload = await _capture_pricing_session(context, seed_url)

        semaphore = asyncio.Semaphore(6)
        await asyncio.gather(*(
            _fetch_one_solo(context, headers, template_payload, record, semaphore)
            for record in priced
        ))

        await browser.close()


async def collect(headless: bool = True):
    # Keep the proven 2-person browser scrape untouched. Disable the unreliable
    # guest-picker path, then enrich those records with Princess's own one-person
    # pricing API using the authenticated/session headers captured from the page.
    original_set_guest = base.set_guest_count_on_detail
    base.set_guest_count_on_detail = _disable_page_solo
    try:
        records = await base.collect(headless=headless)
    finally:
        base.set_guest_count_on_detail = original_set_guest

    priced = [r for r in records if r.get("two_guest_interior_pp") is not None]
    if not priced:
        raise RuntimeError(
            "Princess returned no usable cabin prices. Refusing to overwrite the live dashboard."
        )

    non_aud = [r for r in priced if (r.get("currency") or "").upper() != "AUD"]
    if non_aud:
        currencies = sorted({(r.get("currency") or "UNKNOWN").upper() for r in non_aud})
        raise RuntimeError(
            f"Princess returned non-AUD pricing ({', '.join(currencies)}). "
            "Refusing to overwrite the live dashboard."
        )

    await _populate_solo_prices(records, headless=headless)

    solo_priced = sum(1 for r in records if r.get("one_guest_interior") is not None)
    print(f"Princess solo Interior pricing captured for {solo_priced}/{len(records)} cruises.")

    if solo_priced == 0:
        raise RuntimeError(
            "Princess returned no usable solo pricing. Refusing to publish an empty 1-person view."
        )

    return records
