from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

GRAPH_URL = "https://www.celebritycruises.com/graph"
ROOM_URL = "https://www.celebritycruises.com/itinerary/api/v1/sailings"
PORTS = ("SYD",)
PAGE_SIZE = 250

QUERY = """query cruiseSearch_Cruises($filters: String, $qualifiers: String, $sort: CruiseSearchSort, $pagination: CruiseSearchPagination) {
  cruiseSearch(filters: $filters, qualifiers: $qualifiers, sort: $sort, pagination: $pagination) {
    results {
      total
      cruises {
        id
        productViewLink
        masterSailing {
          itinerary {
            name
            code
            totalNights
            departurePort { code name countryCode }
            destination { code name }
            ship { code name }
          }
        }
        sailings { id sailDate startDate endDate bookingLink itinerary { code } }
      }
    }
  }
}"""


def _catalogue_headers() -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "adrum": "isAjax:true",
        "apollographql-client-name": "cruise-search-widget",
        "app-name": "graph",
        "brand": "C",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "country": "USA",
        "countryalpha2code": "US",
        "currency": "USD",
        "language": "en",
        "office": "MIA",
        "pragma": "no-cache",
        "request-timeout": "20",
        "referer": "https://www.celebritycruises.com/",
        "x-session-id": str(uuid.uuid4()),
    }


def _package_code(sailing: dict) -> str | None:
    code = ((sailing.get("itinerary") or {}).get("code") or "").strip()
    if code:
        return code
    sailing_id = sailing.get("id") or ""
    if "_" in sailing_id:
        return sailing_id.rsplit("_", 1)[0]
    return None


def _room_summary(sailing: dict) -> dict:
    out = {}
    mapping = {
        "INTERIOR": "interior",
        "OUTSIDE": "oceanview",
        "BALCONY": "balcony",
        "DELUXE": "suite",
    }
    for room in sailing.get("rooms") or []:
        mapped = mapping.get((room.get("code") or "").upper())
        if not mapped:
            continue
        pricing = room.get("pricing") or {}
        amount = pricing.get("amount")
        total = pricing.get("total")
        if amount is None and total is None:
            continue
        out[mapped] = {
            "per_person": amount,
            "total": total,
            "category": room.get("categoryCode"),
        }
    return out


async def _catalogue_page(request, skip: int) -> tuple[int, list[dict]]:
    payload = {
        "operationName": "cruiseSearch_Cruises",
        "variables": {
            "filters": "{}",
            "sort": {"by": "RECOMMENDED"},
            "pagination": {"count": PAGE_SIZE, "skip": skip},
        },
        "query": QUERY,
    }
    response = await request.post(
        GRAPH_URL,
        headers=_catalogue_headers(),
        data=json.dumps(payload),
        timeout=30000,
    )
    if response.status != 200:
        raise RuntimeError(f"Celebrity catalogue HTTP {response.status} at skip={skip}")
    body = await response.json()
    if body.get("errors"):
        raise RuntimeError(f"Celebrity catalogue GraphQL errors: {body['errors']}")
    results = (((body.get("data") or {}).get("cruiseSearch") or {}).get("results") or {})
    return int(results.get("total") or 0), results.get("cruises") or []


async def _search_all(request) -> list[dict]:
    total = None
    skip = 0
    local: list[dict] = []
    while total is None or skip < total:
        page_total, cruises = await _catalogue_page(request, skip)
        if total is None:
            total = page_total
            print(f"Celebrity catalogue total: {total}")
        if not cruises:
            break
        for cruise in cruises:
            itinerary = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
            dep = ((itinerary.get("departurePort") or {}).get("code") or "").upper()
            if dep in PORTS:
                local.append(cruise)
        print(
            f"Celebrity catalogue page skip={skip}: {len(cruises)} groups, "
            f"Sydney groups so far={len(local)}"
        )
        skip += len(cruises)
    return local


async def _price(request, sailing: dict, adults: int) -> dict | None:
    package = _package_code(sailing)
    sail_date = (sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
    if not package or not sail_date:
        return None

    params = {
        "packageCode": package,
        "sailDate": sail_date,
        "adults": str(adults),
        "children": "0",
        "countryCode": "AUS",
        "currencyCode": "AUD",
        "languageCode": "en",
        "officeCode": "SYD",
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-AU,en;q=0.9",
        "referer": "https://www.celebritycruises.com/au/cruises",
    }

    response = None
    for attempt in range(1, 3):
        try:
            response = await request.get(
                ROOM_URL,
                params=params,
                headers=headers,
                timeout=30000,
            )
            break
        except PlaywrightTimeoutError:
            print(
                f"Celebrity price timeout attempt={attempt}/2 adults={adults} "
                f"package={package} date={sail_date}"
            )
            if attempt < 2:
                await asyncio.sleep(1.5)
        except Exception as exc:
            print(
                f"Celebrity price request error adults={adults} package={package} "
                f"date={sail_date}: {type(exc).__name__}: {exc}"
            )
            return None

    if response is None:
        print(
            f"Celebrity price skipped after retries: adults={adults} "
            f"package={package} date={sail_date}"
        )
        return None
    if response.status == 404:
        return None
    if response.status != 200:
        print(
            f"Celebrity price HTTP {response.status}: adults={adults} "
            f"package={package} date={sail_date}"
        )
        return None
    try:
        body = await response.json()
    except Exception:
        return None

    target_id = f"{package}_{sail_date}"
    exact = next(
        (
            s for s in (body.get("sailings") or [])
            if s.get("id") == target_id
            or (
                s.get("packageCode") == package
                and (s.get("sailDate") or s.get("startDate")) == sail_date
            )
        ),
        None,
    )
    return _room_summary(exact) if exact else None


def _au_url(cruise: dict, sailing: dict) -> str:
    product = (cruise.get("productViewLink") or "").lstrip("/")
    path = product.split("?", 1)[0]
    if path.startswith("au/"):
        path = path[3:]
    package = _package_code(sailing) or ""
    sail_date = (sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
    query = urlencode({
        "sailDate": sail_date,
        "packageCode": package,
        "groupId": cruise.get("id") or "",
        "country": "AUS",
        "currency": "AUD",
    })
    if path:
        return f"https://www.celebritycruises.com/au/{path}?{query}"
    return "https://www.celebritycruises.com/au/cruises"


def _price_value(summary: dict | None, cabin: str, field: str) -> float | None:
    if not summary:
        return None
    value = (summary.get(cabin) or {}).get(field)
    return float(value) if isinstance(value, (int, float)) else None


async def collect(headless: bool = True) -> list[dict]:
    del headless
    async with async_playwright() as p:
        request = await p.request.new_context(
            extra_http_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"
            }
        )
        cruises = await _search_all(request)
        records: dict[str, dict] = {}
        priced_double = 0
        priced_solo = 0

        for cruise in cruises:
            itinerary = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
            dep = ((itinerary.get("departurePort") or {}).get("code") or "").upper()
            if dep != "SYD":
                continue

            for sailing in cruise.get("sailings") or []:
                package = _package_code(sailing)
                sail_date = (sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
                if not package or not sail_date:
                    continue

                solo = await _price(request, sailing, 1)
                double = await _price(request, sailing, 2)
                if solo:
                    priced_solo += 1
                if double:
                    priced_double += 1

                cruise_id = f"Celebrity:{package}:{sail_date}"
                one_interior = (
                    _price_value(solo, "interior", "total")
                    or _price_value(solo, "interior", "per_person")
                )
                two_interior_pp = _price_value(double, "interior", "per_person")
                two_interior_total = _price_value(double, "interior", "total")

                records[cruise_id] = {
                    "cruise_id": cruise_id,
                    "line": "Celebrity Cruises",
                    "name": itinerary.get("name"),
                    "ship": (itinerary.get("ship") or {}).get("name"),
                    "departure_port": "SYD",
                    "arrival_port": None,
                    "departure_date": sail_date,
                    "nights": itinerary.get("totalNights"),
                    "itinerary": (itinerary.get("destination") or {}).get("name"),
                    "url": _au_url(cruise, sailing),
                    "available": bool(solo or double),
                    "currency": "AUD",
                    "one_guest_price": one_interior,
                    "two_guest_pp_price": two_interior_pp,
                    "two_guest_total": two_interior_total,
                    "one_guest_interior": one_interior,
                    "one_guest_oceanview": _price_value(solo, "oceanview", "total") or _price_value(solo, "oceanview", "per_person"),
                    "one_guest_balcony": _price_value(solo, "balcony", "total") or _price_value(solo, "balcony", "per_person"),
                    "one_guest_mini_suite": None,
                    "one_guest_suite": _price_value(solo, "suite", "total") or _price_value(solo, "suite", "per_person"),
                    "two_guest_interior_pp": two_interior_pp,
                    "two_guest_oceanview_pp": _price_value(double, "oceanview", "per_person"),
                    "two_guest_balcony_pp": _price_value(double, "balcony", "per_person"),
                    "two_guest_mini_suite_pp": None,
                    "two_guest_suite_pp": _price_value(double, "suite", "per_person"),
                }
                print(
                    f"Celebrity {sail_date} SYD {itinerary.get('name')} | "
                    f"solo={'yes' if solo else 'no'} double={'yes' if double else 'no'}"
                )

        await request.dispose()

    out = sorted(
        records.values(),
        key=lambda r: (r.get("departure_date") or "", r.get("name") or ""),
    )
    if not out:
        raise RuntimeError("Celebrity collector returned no Sydney sailings")
    if priced_double == 0:
        raise RuntimeError("Celebrity collector obtained no exact 2-adult AUD pricing")
    print(
        f"Celebrity production result: {len(out)} sailings, "
        f"{priced_double} with 2-adult pricing, {priced_solo} with solo pricing"
    )
    return out
