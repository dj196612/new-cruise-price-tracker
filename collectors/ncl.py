from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

SEARCH_API = "https://www.ncl.com/au/en/api/v2/vacations/search"
SAILINGS_API = "https://www.ncl.com/au/en/api/vacation-builder/v2/itinerary/{code}/sailings"
PAGE_SIZE = 50


def _headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-AU,en;q=0.9",
        "referer": "https://www.ncl.com/au/en/vacations?embPorts=SYD",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
    }


async def _get_json(request, url: str, *, params: dict | None = None, label: str = "request"):
    for attempt in range(1, 3):
        try:
            response = await request.get(url, params=params, headers=_headers(), timeout=30000)
            if response.status != 200:
                print(f"NCL {label} HTTP {response.status} attempt={attempt}/2")
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                return None
            return await response.json()
        except PlaywrightTimeoutError:
            print(f"NCL {label} timeout attempt={attempt}/2")
            if attempt < 2:
                await asyncio.sleep(1.5)
        except Exception as exc:
            print(f"NCL {label} error: {type(exc).__name__}: {exc}")
            return None
    return None


async def _search_all(request) -> list[dict]:
    offset = 0
    found: dict[str, dict] = {}
    while True:
        body = await _get_json(
            request,
            SEARCH_API,
            params={
                "embPorts": "SYD",
                "guests": "2",
                "filterConfig": "search-filters-configuration",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
            label=f"search offset={offset}",
        )
        if not body:
            break
        rows = body.get("itineraries") or []
        for item in rows:
            code = (item.get("code") or "").strip()
            dep = ((item.get("embarkationPort") or {}).get("code") or "").upper()
            if code and dep == "SYD":
                found[code] = item
        print(f"NCL search offset={offset}: {len(rows)} rows, Sydney itineraries={len(found)}")
        if len(rows) < PAGE_SIZE:
            break
        offset += len(rows)
    return list(found.values())


async def _sailings(request, code: str, guests: int) -> list[dict]:
    body = await _get_json(
        request,
        SAILINGS_API.format(code=code),
        params={"numberOfGuests": str(guests)},
        label=f"sailings code={code} guests={guests}",
    )
    return (body or {}).get("results") or []


def _cabin_prices(result: dict | None) -> dict[str, float | None]:
    prices = {"interior": None, "oceanview": None, "balcony": None, "suite": None}
    mapping = {
        "INSIDE": "interior",
        "OCEANVIEW": "oceanview",
        "BALCONY": "balcony",
        "SUITE": "suite",
    }
    if not result:
        return prices
    for room in result.get("stateroomTypesPricing") or []:
        key = mapping.get((room.get("code") or "").upper())
        if not key or (room.get("status") or "").upper() != "AVAILABLE":
            continue
        value = room.get("combinedPrice")
        if isinstance(value, (int, float)):
            prices[key] = float(value)
    return prices


def _sailing_key(result: dict) -> str | None:
    sailing = result.get("sailing") or {}
    date = (sailing.get("sailStartDate") or sailing.get("vacationStartDate") or "")[:10]
    return date or None


def _builder_url(code: str, package_id: str | None, guests: int = 2) -> str:
    query = {"itineraryCode": code, "guests": str(guests)}
    if package_id:
        query["packageId"] = package_id
    return "https://www.ncl.com/au/en/vacation-builder?" + urlencode(query)


async def collect(headless: bool = True) -> list[dict]:
    del headless
    async with async_playwright() as p:
        request = await p.request.new_context(extra_http_headers=_headers())
        itineraries = await _search_all(request)
        if not itineraries:
            await request.dispose()
            raise RuntimeError("NCL collector found no Sydney itineraries")

        records: dict[str, dict] = {}
        priced_double = 0
        priced_solo = 0

        for item in itineraries:
            code = (item.get("code") or "").strip()
            if not code:
                continue
            solo_rows = await _sailings(request, code, 1)
            double_rows = await _sailings(request, code, 2)
            solo_by_date = {_sailing_key(r): r for r in solo_rows if _sailing_key(r)}
            double_by_date = {_sailing_key(r): r for r in double_rows if _sailing_key(r)}
            all_dates = sorted(set(solo_by_date) | set(double_by_date))

            ship = (item.get("ship") or {}).get("title")
            nights = (item.get("duration") or {}).get("days")
            title = (item.get("title") or "").rstrip(":")
            ports = [p.get("title") for p in (item.get("portsOfCall") or []) if p.get("title")]
            itinerary_text = " · ".join(ports)

            for sail_date in all_dates:
                solo_result = solo_by_date.get(sail_date)
                double_result = double_by_date.get(sail_date)
                solo = _cabin_prices(solo_result)
                double = _cabin_prices(double_result)
                if any(v is not None for v in solo.values()):
                    priced_solo += 1
                if any(v is not None for v in double.values()):
                    priced_double += 1

                source = (double_result or solo_result or {}).get("sailing") or {}
                package_id = source.get("packageId")
                cruise_id = f"NCL:{code}:{sail_date}"
                records[cruise_id] = {
                    "cruise_id": cruise_id,
                    "line": "Norwegian Cruise Line",
                    "name": title,
                    "ship": ship,
                    "departure_port": "SYD",
                    "arrival_port": None,
                    "departure_date": sail_date,
                    "nights": nights,
                    "itinerary": itinerary_text,
                    "url": _builder_url(code, package_id),
                    "available": any(v is not None for v in solo.values()) or any(v is not None for v in double.values()),
                    "currency": "AUD",
                    "fare_basis": "NCL Fare",
                    "one_guest_price": solo["interior"],
                    "two_guest_pp_price": double["interior"],
                    "two_guest_total": double["interior"] * 2 if double["interior"] is not None else None,
                    "one_guest_interior": solo["interior"],
                    "one_guest_oceanview": solo["oceanview"],
                    "one_guest_balcony": solo["balcony"],
                    "one_guest_mini_suite": None,
                    "one_guest_suite": solo["suite"],
                    "two_guest_interior_pp": double["interior"],
                    "two_guest_oceanview_pp": double["oceanview"],
                    "two_guest_balcony_pp": double["balcony"],
                    "two_guest_mini_suite_pp": None,
                    "two_guest_suite_pp": double["suite"],
                }
                print(
                    f"NCL {sail_date} SYD {title} | "
                    f"solo={'yes' if any(v is not None for v in solo.values()) else 'no'} "
                    f"double={'yes' if any(v is not None for v in double.values()) else 'no'}"
                )

        await request.dispose()

    out = sorted(records.values(), key=lambda r: (r.get("departure_date") or "", r.get("name") or ""))
    if not out:
        raise RuntimeError("NCL collector returned no Sydney sailings")
    if priced_double == 0:
        raise RuntimeError("NCL collector obtained no 2-guest AUD cabin pricing")
    print(
        f"NCL production result: {len(out)} sailings, "
        f"{priced_double} with 2-guest pricing, {priced_solo} with solo pricing"
    )
    return out
