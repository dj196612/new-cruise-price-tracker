from __future__ import annotations

import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

SEARCH_API = "https://www.carnival.com.au/cruisesearch/api/search"
BOOK_API = "https://www.carnival.com.au/booking-api/api/v1.0/book"
INITIAL_DATA_API = "https://www.carnival.com.au/booking-api/api/v1.0/initialdata/"
PORTS = ("SYD", "BNE", "MEL")
PAGE_SIZE = 50

CABIN_CODES = {
    "IS": "interior",
    "OS": "oceanview",
    "OB": "balcony",
    "SU": "suite",
}


def _headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-AU,en;q=0.9",
        "referer": "https://www.carnival.com.au/cruise-search",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
    }


async def _get_json(request, *, port: str, guests: int, page: int) -> dict | None:
    params = {
        "pageNumber": str(page),
        "numadults": str(guests),
        "port": port,
        "pagesize": str(PAGE_SIZE),
        "sort": "FromPrice",
        "showBest": "true",
        "async": "true",
        "currency": "AUD",
        "locality": "7",
        "client": "cruisesearch",
        "senior": "false",
    }
    for attempt in range(1, 3):
        try:
            response = await request.get(SEARCH_API, params=params, headers=_headers(), timeout=30000)
            if response.status != 200:
                print(
                    f"Carnival search HTTP {response.status} port={port} guests={guests} "
                    f"page={page} attempt={attempt}/2"
                )
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                return None
            return await response.json()
        except PlaywrightTimeoutError:
            print(
                f"Carnival search timeout port={port} guests={guests} "
                f"page={page} attempt={attempt}/2"
            )
            if attempt < 2:
                await asyncio.sleep(1.5)
        except Exception as exc:
            print(
                f"Carnival search error port={port} guests={guests} page={page}: "
                f"{type(exc).__name__}: {exc}"
            )
            return None
    return None


async def _collect_catalogue(request, *, port: str, guests: int) -> tuple[dict[str, dict], dict[str, dict]]:
    sailings: dict[str, dict] = {}
    itineraries: dict[str, dict] = {}
    page = 1
    last_page = 1

    while page <= last_page:
        body = await _get_json(request, port=port, guests=guests, page=page)
        if not body:
            break
        results = body.get("results") or {}
        last_page = int(results.get("lastPage") or 1)
        rows = results.get("itineraries") or []

        for itin in rows:
            code = (itin.get("code") or "").strip()
            dep = (itin.get("departurePortCode") or "").upper()
            if not code or dep != port:
                continue
            itineraries[code] = itin
            for sailing in itin.get("sailings") or []:
                sail_id = str(sailing.get("sailingId") or "").strip()
                sail_date = (sailing.get("departureDate") or "")[:10]
                if not sail_id or not sail_date:
                    continue
                key = f"{code}:{sail_id}:{sail_date}"
                sailings[key] = sailing

        print(
            f"Carnival catalogue port={port} guests={guests} page={page}/{last_page}: "
            f"{len(rows)} itineraries, {len(sailings)} sailings"
        )
        page += 1

    return sailings, itineraries


def _payload(*, sailing: dict, itin: dict, guests: int) -> dict:
    sail_date = (sailing.get("departureDate") or "")[:10]
    duration = int(itin.get("dur") or 0)
    ship_code = (itin.get("shipCode") or "").strip()
    sailing_id = int(sailing.get("sailingId"))

    return {
        "sailingId": sailing_id,
        "sailDate": sail_date,
        "durationDays": duration,
        "shipCode": ship_code,
        "currencyCode": "AUD",
        "cabins": [
            {
                "qualifiers": {
                    "numberOfGuests": guests,
                    "countryOfResidency": "AU",
                    "isMilitary": False,
                    "isSenior": True,
                    "isPastGuest": False,
                    "pastGuestNumber": None,
                    "stateOfResidency": None,
                    "accessibility": None,
                    "tierCode": None,
                    "couponCode": None,
                },
                "metaCode": None,
                "phantomMetaCode": None,
                "stateroomTypeCode": None,
                "rateCode": None,
                "price": None,
                "deckCode": None,
                "locationCode": None,
                "roomNumber": None,
                "cabinToRelease": None,
                "categoryCode": None,
                "upgradeFrom": None,
            }
        ],
        "flowOption": None,
        "currentCabinIndex": 0,
        "preselectedMetaCode": None,
        "sortMetasByAscPrice": False,
        "encryptedBookingNumber": None,
        "connectingCabinsIndicator": False,
        "previousCabinSelectedCategoryCode": None,
        "previousCabinSelectedRateCode": None,
    }


def _booking_prices(body: dict, guests: int) -> dict[str, float | None]:
    out = {"interior": None, "oceanview": None, "balcony": None, "suite": None}
    cabins = body.get("cabins") or []
    if not cabins:
        return out

    options = (cabins[0].get("options") or {}).get("metas") or []
    for meta in options:
        target = CABIN_CODES.get((meta.get("code") or "").upper())
        if not target or meta.get("isSoldOut"):
            continue

        value = meta.get("selfServiceDisplayPrice")
        if not isinstance(value, (int, float)):
            value = meta.get("price")

        if not isinstance(value, (int, float)):
            guest_rows = ((meta.get("priceDetails") or {}).get("guestPrices") or [])
            guest_values = [x.get("price") for x in guest_rows if isinstance(x.get("price"), (int, float))]
            if guest_values:
                value = sum(float(x) for x in guest_values) / max(guests, 1)

        if isinstance(value, (int, float)):
            out[target] = float(value)

    return out


def _absolute_url(path: str | None) -> str:
    if not path:
        return "https://www.carnival.com.au/cruise-search"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return "https://www.carnival.com.au" + (path if path.startswith("/") else "/" + path)


def _session_url(sailing: dict, guests: int) -> str:
    url = _absolute_url(sailing.get("sailingURL"))
    if "numGuests=" in url:
        import re
        url = re.sub(r"numGuests=\d+", f"numGuests={guests}", url)
    if "isOver55=N" in url:
        url = url.replace("isOver55=N", "isOver55=Y")
    return url


async def _initialise_booking_session(request, *, sailing: dict, itin: dict, guests: int) -> str:
    sail_id = sailing.get("sailingId")
    ship_code = (itin.get("shipCode") or "").strip()
    booking_url = _session_url(sailing, guests)

    response = await request.get(booking_url, headers=_headers(), timeout=30000)
    if response.status not in (200, 301, 302):
        raise RuntimeError(
            f"Carnival booking session init failed for sailing {sail_id}, guests={guests}: HTTP {response.status}"
        )

    initial = await request.get(
        INITIAL_DATA_API,
        params={"sailingID": str(sail_id), "shipCode": ship_code},
        headers={**_headers(), "referer": booking_url},
        timeout=30000,
    )
    if initial.status != 200:
        raise RuntimeError(
            f"Carnival initialdata failed for sailing {sail_id}, guests={guests}: HTTP {initial.status}"
        )
    return booking_url


async def _booking_price_set(request, *, sailing: dict, itin: dict, guests: int) -> dict[str, float | None]:
    payload = _payload(sailing=sailing, itin=itin, guests=guests)
    sail_id = sailing.get("sailingId")
    sail_date = (sailing.get("departureDate") or "")[:10]

    for attempt in range(1, 4):
        try:
            booking_url = await _initialise_booking_session(request, sailing=sailing, itin=itin, guests=guests)
            headers = {
                **_headers(),
                "accept": "application/json",
                "content-type": "application/json",
                "referer": booking_url.replace("/booking?", "/booking/qualifiers?"),
            }
            response = await request.post(BOOK_API, data=payload, headers=headers, timeout=30000)
            if response.status != 200:
                body = ""
                try:
                    body = " ".join((await response.text()).split())[:500]
                except Exception:
                    pass
                print(
                    f"Carnival booking HTTP {response.status} sailing={sail_id} date={sail_date} "
                    f"guests={guests} attempt={attempt}/3 body={body}"
                )
                if attempt < 3:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                raise RuntimeError(
                    f"Carnival booking API failed for sailing {sail_id} {sail_date}, guests={guests}: HTTP {response.status}"
                )

            body = await response.json()
            prices = _booking_prices(body, guests)
            if not any(v is not None for v in prices.values()):
                raise RuntimeError(
                    f"Carnival booking API returned no cabin prices for sailing {sail_id} {sail_date}, guests={guests}"
                )
            return prices
        except PlaywrightTimeoutError:
            print(
                f"Carnival booking timeout sailing={sail_id} date={sail_date} "
                f"guests={guests} attempt={attempt}/3"
            )
            if attempt < 3:
                await asyncio.sleep(2.0 * attempt)
                continue
            raise
        except Exception:
            if attempt < 3:
                await asyncio.sleep(2.0 * attempt)
                continue
            raise

    raise RuntimeError(f"Carnival booking API exhausted retries for sailing {sail_id} {sail_date}, guests={guests}")


def _itinerary_text(itin: dict) -> str:
    ports = [p for p in (itin.get("portsToDisplay") or []) if p]
    if ports:
        return " · ".join(ports)
    schedule = ((itin.get("leadSailing") or {}).get("schedule") or [])
    names = [x.get("port") for x in schedule if x.get("port") and "sea" not in x.get("port", "").lower()]
    return " · ".join(names)


def _booking_url(itin: dict, sailing: dict) -> str:
    url = sailing.get("sailingURL") or itin.get("itineraryURLWithSailing") or itin.get("itineraryURL")
    if url and "isOver55=N" in url:
        url = url.replace("isOver55=N", "isOver55=Y")
    return _absolute_url(url)


async def collect(headless: bool = True) -> list[dict]:
    del headless
    async with async_playwright() as p:
        request = await p.request.new_context(extra_http_headers=_headers())

        records: dict[str, dict] = {}
        priced_solo = 0
        priced_double = 0

        for port in PORTS:
            one_catalogue, one_itins = await _collect_catalogue(request, port=port, guests=1)
            two_catalogue, two_itins = await _collect_catalogue(request, port=port, guests=2)

            all_itins = {}
            all_itins.update(one_itins)
            all_itins.update(two_itins)

            catalogue = dict(two_catalogue)
            catalogue.update(one_catalogue)
            keys = sorted(catalogue)
            results = []

            for index, key in enumerate(keys, start=1):
                code = key.split(":", 1)[0]
                itin = all_itins.get(code) or {}
                sailing = catalogue[key]

                solo = await _booking_price_set(request, sailing=sailing, itin=itin, guests=1)
                await asyncio.sleep(0.15)
                double = await _booking_price_set(request, sailing=sailing, itin=itin, guests=2)
                results.append((key, itin, sailing, solo, double))

                if index % 20 == 0 or index == len(keys):
                    print(f"Carnival booking pricing port={port}: {index}/{len(keys)} sailings")
                await asyncio.sleep(0.15)

            for key, itin, source, solo, double in results:
                code = key.split(":", 1)[0]
                sail_date = (source.get("departureDate") or "")[:10]
                if not sail_date:
                    continue

                if any(v is not None for v in solo.values()):
                    priced_solo += 1
                if any(v is not None for v in double.values()):
                    priced_double += 1

                sailing_id = str(source.get("sailingId") or "")
                cruise_id = f"Carnival:{code}:{sailing_id}:{sail_date}"

                records[cruise_id] = {
                    "cruise_id": cruise_id,
                    "line": "Carnival Cruise Line",
                    "name": itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle") or "Carnival Cruise",
                    "ship": itin.get("shipName"),
                    "departure_port": port,
                    "arrival_port": None,
                    "departure_date": sail_date,
                    "nights": itin.get("dur"),
                    "itinerary": _itinerary_text(itin),
                    "url": _booking_url(itin, source),
                    "available": any(v is not None for v in solo.values()) or any(v is not None for v in double.values()),
                    "currency": "AUD",
                    "fare_basis": "Carnival bookable fare (best available incl. 55+)",
                    "one_guest_price": solo.get("interior"),
                    "two_guest_pp_price": double.get("interior"),
                    "two_guest_total": double.get("interior") * 2 if double.get("interior") is not None else None,
                    "one_guest_interior": solo.get("interior"),
                    "one_guest_oceanview": solo.get("oceanview"),
                    "one_guest_balcony": solo.get("balcony"),
                    "one_guest_mini_suite": None,
                    "one_guest_suite": solo.get("suite"),
                    "two_guest_interior_pp": double.get("interior"),
                    "two_guest_oceanview_pp": double.get("oceanview"),
                    "two_guest_balcony_pp": double.get("balcony"),
                    "two_guest_mini_suite_pp": None,
                    "two_guest_suite_pp": double.get("suite"),
                }

            print(f"Carnival booking pricing completed for port={port}: {len(results)} sailings")

        await request.dispose()

    out = sorted(records.values(), key=lambda r: (r.get("departure_date") or "", r.get("departure_port") or "", r.get("name") or ""))
    if not out:
        raise RuntimeError("Carnival collector returned no Australian sailings")
    if priced_double == 0:
        raise RuntimeError("Carnival collector obtained no 2-guest AUD booking pricing")

    print(
        f"Carnival production result: {len(out)} sailings, "
        f"{priced_double} with 2-guest booking pricing, {priced_solo} with solo booking pricing"
    )
    return out
