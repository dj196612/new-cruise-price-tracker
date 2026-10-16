from __future__ import annotations

import json

from playwright.async_api import async_playwright

from . import princess as base


TEST_URL = "https://www.princess.com/itinerary-details?voyageCode=A716A"
PRICING_URL = "https://gw.api.princess.com/pcl-web/internal/caps/pc/pricing/v1/cruises/A716A"


def _payload(guest_count: int, fare_type: str = "BESTVALUE") -> dict:
    return {
        "booking": {
            "currencyCode": "AUD",
            "guests": [
                {"country": "AU", "homeCity": "SYD"}
                for _ in range(guest_count)
            ],
            "promos": [],
            "couponCodes": [],
            "bookingAgency": {
                "id": "DIRECTAU",
                "bookingCompany": "PA",
                "currency": "AUD",
                "country": "AU",
            },
        },
        "filters": {
            "availabilities": ["Y", "G", "B"],
            "cruises": [],
            "cruiseType": "C",
            "meta": "I",
            "itinPorts": [],
            "subTrades": [],
        },
        "leadInBy": "itins",
        "retrieveFlags": {
            "additionalGuestFare": True,
            "averageFare": False,
            "brochureFare": False,
            "averageBrochureFare": True,
            "includeMisc": False,
            "fareType": fare_type,
            "roundUpFare": True,
            "subMeta": True,
            "zones": True,
            "includeTfpe": True,
        },
    }


def _summarise(data: dict) -> dict:
    out = {"currency": None, "fare_types": {}}
    for product in data.get("products", []):
        for cruise in product.get("cruises", []):
            pricing = cruise.get("pricing") or {}
            out["currency"] = pricing.get("fareCurrency") or out["currency"]
            for fare in pricing.get("fares", []):
                fare_type = fare.get("fareType") or "UNKNOWN"
                cats = []
                for category in fare.get("categories", []):
                    guests = category.get("guests") or []
                    cats.append({
                        "category": category.get("id"),
                        "status": category.get("status"),
                        "fare_guest_1": guests[0].get("fare") if guests else None,
                        "base_guest_1": guests[0].get("baseFare") if guests else None,
                        "ssv_guest_1": guests[0].get("ssv") if guests else None,
                        "minimum": category.get("minimum"),
                        "maximum": category.get("maximum"),
                    })
                out["fare_types"][fare_type] = cats
    return out


async def _api_post(page, payload: dict) -> dict:
    result = await page.evaluate(
        """async ({url, payload}) => {
            const response = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'content-type': 'application/json',
                    'accept': 'application/json, text/plain, */*'
                },
                body: JSON.stringify(payload)
            });
            const text = await response.text();
            return {status: response.status, text};
        }""",
        {"url": PRICING_URL, "payload": payload},
    )
    print("SOLO_API_STATUS", result["status"])
    if result["status"] != 200:
        print("SOLO_API_ERROR_BODY", result["text"][:6000])
        return {}
    return json.loads(result["text"])


async def collect(headless: bool = True):
    print("Running direct Princess one-guest pricing API diagnostic for A716A")

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
        page = await context.new_page()
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90000)
        await base.dismiss_noise(page)
        body = await base.wait_for_cabin_prices(page, 25)
        print("SOLO_API_VISIBLE_2P", json.dumps(base.cabin_prices(body), sort_keys=True))

        two_bestvalue = await _api_post(page, _payload(2, "BESTVALUE"))
        print("SOLO_API_2P_BESTVALUE", json.dumps(_summarise(two_bestvalue), sort_keys=True)[:20000])

        one_bestvalue = await _api_post(page, _payload(1, "BESTVALUE"))
        print("SOLO_API_1P_BESTVALUE", json.dumps(_summarise(one_bestvalue), sort_keys=True)[:20000])

        one_bestfare = await _api_post(page, _payload(1, "BESTFARE"))
        print("SOLO_API_1P_BESTFARE", json.dumps(_summarise(one_bestfare), sort_keys=True)[:20000])

        await page.close()
        await browser.close()

    raise RuntimeError(
        "Princess one-guest API diagnostic complete. Inspect SOLO_API_1P_BESTVALUE and SOLO_API_1P_BESTFARE."
    )
