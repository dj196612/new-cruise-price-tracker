from __future__ import annotations

import json
import uuid

from playwright.async_api import async_playwright

GRAPH_URL = "https://www.celebritycruises.com/graph"
ROOM_URL = "https://www.celebritycruises.com/itinerary/api/v1/sailings"
PORTS = ("SYD",)

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


def _headers() -> dict[str, str]:
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
        out[mapped] = {
            "per_person": pricing.get("amount"),
            "total": pricing.get("total"),
            "category": room.get("categoryCode"),
        }
    return out


async def _search(request):
    payload = {
        "operationName": "cruiseSearch_Cruises",
        "variables": {
            "filters": "{}",
            "sort": {"by": "RECOMMENDED"},
            "pagination": {"count": 250, "skip": 0},
        },
        "query": QUERY,
    }
    response = await request.post(
        GRAPH_URL,
        headers=_headers(),
        data=json.dumps(payload),
        timeout=30000,
    )
    text = await response.text()
    print(f"CELEB_GRAPH_STATUS {response.status}")
    try:
        data = json.loads(text)
    except Exception:
        print(f"CELEB_GRAPH_NON_JSON {' '.join(text.split())[:2000]}")
        return []
    if data.get("errors"):
        print(f"CELEB_GRAPH_ERRORS {json.dumps(data['errors'])[:4000]}")
        return []

    results = (((data.get("data") or {}).get("cruiseSearch") or {}).get("results") or {})
    cruises = results.get("cruises") or []
    local = []
    for cruise in cruises:
        itinerary = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
        dep = ((itinerary.get("departurePort") or {}).get("code") or "").upper()
        if dep in PORTS:
            local.append(cruise)
    print(
        f"CELEB_GRAPH_TOTAL {results.get('total')} "
        f"CELEB_GRAPH_CRUISES {len(cruises)} CELEB_LOCAL_CRUISES {len(local)}"
    )
    return local


async def _price(request, sailing: dict, adults: int):
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
    response = await request.get(
        ROOM_URL,
        params=params,
        headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-AU,en;q=0.9",
            "referer": "https://www.celebritycruises.com/au/cruises",
        },
        timeout=30000,
    )
    text = await response.text()
    print(f"CELEB_PRICE_STATUS adults={adults} package={package} date={sail_date} status={response.status}")
    try:
        body = json.loads(text)
    except Exception:
        print(f"CELEB_PRICE_NON_JSON adults={adults} {' '.join(text.split())[:2000]}")
        return None

    target_id = f"{package}_{sail_date}"
    returned = body.get("sailings") or []
    exact = next(
        (
            s for s in returned
            if s.get("id") == target_id
            or (s.get("packageCode") == package and (s.get("sailDate") or s.get("startDate")) == sail_date)
        ),
        None,
    )
    if exact is None:
        alternatives = [
            {
                "id": s.get("id"),
                "packageCode": s.get("packageCode"),
                "sailDate": s.get("sailDate") or s.get("startDate"),
            }
            for s in returned[:10]
        ]
        print(
            f"CELEB_PRICE_NO_EXACT_MATCH adults={adults} package={package} date={sail_date} "
            + json.dumps(alternatives, separators=(",", ":"))
        )
        return None

    summary = _room_summary(exact)
    print(
        f"CELEB_PRICE_EXACT_MATCH adults={adults} package={package} date={sail_date} "
        + json.dumps(summary, separators=(",", ":"))
    )
    return summary


async def diagnostic(headless: bool = True):
    del headless
    async with async_playwright() as p:
        request = await p.request.new_context(
            extra_http_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"
            }
        )
        cruises = await _search(request)
        sample = None
        for cruise in cruises:
            itinerary = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
            sailings = cruise.get("sailings") or []
            if not sailings:
                continue
            print(
                "CELEB_SAMPLE_CRUISE "
                + json.dumps({
                    "id": cruise.get("id"),
                    "name": itinerary.get("name"),
                    "ship": (itinerary.get("ship") or {}).get("name"),
                    "departure": (itinerary.get("departurePort") or {}).get("name"),
                    "departureCode": (itinerary.get("departurePort") or {}).get("code"),
                    "nights": itinerary.get("totalNights"),
                    "sailing": sailings[0],
                }, separators=(",", ":"))
            )
            sample = sailings[0]
            break

        if sample:
            solo = await _price(request, sample, 1)
            double = await _price(request, sample, 2)
            print(
                "CELEB_OCCUPANCY_RESULT "
                + json.dumps({"solo_available": solo is not None, "double_available": double is not None})
            )
        else:
            print("CELEB_NO_SAMPLE_SAILING")
        await request.dispose()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError(
        "Celebrity Cruises API diagnostic complete. Inspect CELEB_* lines before publication."
    )
