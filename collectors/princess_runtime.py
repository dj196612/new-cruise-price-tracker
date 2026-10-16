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


async def _api_post(context, headers: dict, payload: dict) -> dict:
    replay_headers = dict(headers)
    for key in [
        "content-length", "host", ":authority", ":method", ":path", ":scheme",
        "accept-encoding", "cookie",
    ]:
        replay_headers.pop(key, None)

    response = await context.request.post(
        PRICING_URL,
        headers=replay_headers,
        data=json.dumps(payload),
        timeout=30000,
    )
    text = await response.text()
    print("SOLO_API_STATUS", response.status)
    if response.status != 200:
        print("SOLO_API_ERROR_BODY", text[:6000])
        return {}
    return json.loads(text)


async def collect(headless: bool = True):
    print("Running Princess one-guest pricing replay diagnostic for A716A")

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

        captured = {"headers": None, "post": None}

        async def capture_request(request):
            if request.url == PRICING_URL and request.method == "POST" and captured["headers"] is None:
                captured["headers"] = await request.all_headers()
                captured["post"] = request.post_data
                safe = {
                    k: v for k, v in captured["headers"].items()
                    if k.lower() not in {"cookie", "authorization"}
                }
                print("SOLO_API_CAPTURED_HEADERS", json.dumps(safe, sort_keys=True)[:12000])
                print("SOLO_API_CAPTURED_POST", (captured["post"] or "")[:8000])

        page.on("request", capture_request)

        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90000)
        await base.dismiss_noise(page)
        body = await base.wait_for_cabin_prices(page, 25)
        print("SOLO_API_VISIBLE_2P", json.dumps(base.cabin_prices(body), sort_keys=True))

        for _ in range(20):
            if captured["headers"]:
                break
            await page.wait_for_timeout(250)

        if not captured["headers"]:
            raise RuntimeError("Could not capture Princess pricing request headers")

        two_bestvalue = await _api_post(context, captured["headers"], _payload(2, "BESTVALUE"))
        print("SOLO_API_2P_BESTVALUE", json.dumps(_summarise(two_bestvalue), sort_keys=True)[:20000])

        one_bestvalue = await _api_post(context, captured["headers"], _payload(1, "BESTVALUE"))
        print("SOLO_API_1P_BESTVALUE", json.dumps(_summarise(one_bestvalue), sort_keys=True)[:20000])

        one_bestfare = await _api_post(context, captured["headers"], _payload(1, "BESTFARE"))
        print("SOLO_API_1P_BESTFARE", json.dumps(_summarise(one_bestfare), sort_keys=True)[:20000])

        await page.close()
        await browser.close()

    raise RuntimeError(
        "Princess one-guest replay diagnostic complete. Inspect SOLO_API_1P_BESTVALUE and SOLO_API_1P_BESTFARE."
    )
