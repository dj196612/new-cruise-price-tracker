from __future__ import annotations

import json
import uuid

from playwright.async_api import async_playwright

GRAPH_URL = "https://www.royalcaribbean.com/graph"
ROOM_URL = "https://www.royalcaribbean.com/itinerary/api/v1/sailings"
PORTS = ("BNE", "SYD")

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
    # Royal's public catalogue is reliably populated using its standard
    # anonymous US search context. This is metadata discovery only; pricing
    # below is explicitly requested for Australia in AUD.
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "adrum": "isAjax:true",
        "apollographql-client-name": "cruise-search-widget",
        "app-name": "graph",
        "brand": "R",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "country": "USA",
        "countryalpha2code": "US",
        "currency": "USD",
        "language": "en",
        "office": "MIA",
        "pragma": "no-cache",
        "request-timeout": "20",
        "referer": "https://www.royalcaribbean.com/",
        "x-session-id": str(uuid.uuid4()),
    }


def _package_code(sailing_id: str | None) -> str | None:
    if not sailing_id or "_" not in sailing_id:
        return None
    prefix, suffix = sailing_id.rsplit("_", 1)
    if len(suffix) == 10 and suffix[4] == "-" and suffix[7] == "-":
        return prefix
    return None


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
    response = await request.post(GRAPH_URL, headers=_headers(), data=json.dumps(payload), timeout=30000)
    text = await response.text()
    print(f"RC_GRAPH_STATUS {response.status}")
    try:
        data = json.loads(text)
    except Exception:
        print(f"RC_GRAPH_NON_JSON {' '.join(text.split())[:2000]}")
        return []
    if data.get("errors"):
        print(f"RC_GRAPH_ERRORS {json.dumps(data['errors'])[:4000]}")
        return []
    results = (((data.get("data") or {}).get("cruiseSearch") or {}).get("results") or {})
    cruises = results.get("cruises") or []
    local = []
    for cruise in cruises:
        it = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
        dep = ((it.get("departurePort") or {}).get("code") or "").upper()
        if dep in PORTS:
            local.append(cruise)
    print(f"RC_GRAPH_TOTAL {results.get('total')} RC_GRAPH_CRUISES {len(cruises)} RC_LOCAL_CRUISES {len(local)}")
    return local


def _room_summary(sailing: dict) -> dict:
    out = {}
    for room in sailing.get("rooms") or []:
        code = (room.get("code") or "").upper()
        mapped = {
            "INTERIOR": "interior",
            "OUTSIDE": "oceanview",
            "BALCONY": "balcony",
            "DELUXE": "suite",
        }.get(code)
        if not mapped:
            continue
        pricing = room.get("pricing") or {}
        out[mapped] = {
            "per_person": pricing.get("amount"),
            "total": pricing.get("total"),
            "category": room.get("categoryCode"),
        }
    return out


async def _price(request, sailing: dict, adults: int):
    sailing_id = sailing.get("id")
    package = _package_code(sailing_id)
    sail_date = (sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
    if not package or not sail_date:
        print(f"RC_PRICE_SKIP adults={adults} sailing_id={sailing_id} sail_date={sail_date}")
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
            "referer": "https://www.royalcaribbean.com/aus/en/cruises",
        },
        timeout=30000,
    )
    text = await response.text()
    print(f"RC_PRICE_STATUS adults={adults} package={package} date={sail_date} status={response.status}")
    try:
        body = json.loads(text)
    except Exception:
        print(f"RC_PRICE_NON_JSON adults={adults} {' '.join(text.split())[:2000]}")
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
            f"RC_PRICE_NO_EXACT_MATCH adults={adults} package={package} date={sail_date} "
            + json.dumps(alternatives, separators=(",", ":"))
        )
        return None

    summary = _room_summary(exact)
    print(
        f"RC_PRICE_EXACT_MATCH adults={adults} package={package} date={sail_date} "
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
            it = ((cruise.get("masterSailing") or {}).get("itinerary") or {})
            sailings = cruise.get("sailings") or []
            if not sailings:
                continue
            print(
                "RC_SAMPLE_CRUISE "
                + json.dumps({
                    "id": cruise.get("id"),
                    "name": it.get("name"),
                    "ship": (it.get("ship") or {}).get("name"),
                    "departure": (it.get("departurePort") or {}).get("name"),
                    "departureCode": (it.get("departurePort") or {}).get("code"),
                    "nights": it.get("totalNights"),
                    "sailing": sailings[0],
                }, separators=(",", ":"))
            )
            sample = sailings[0]
            break

        if sample:
            solo = await _price(request, sample, 1)
            double = await _price(request, sample, 2)
            print(
                "RC_OCCUPANCY_RESULT "
                + json.dumps({"solo_available": solo is not None, "double_available": double is not None})
            )
        else:
            print("RC_NO_SAMPLE_SAILING")
        await request.dispose()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError(
        "Royal Caribbean API diagnostic complete. Inspect RC_PRICE_EXACT_MATCH / RC_PRICE_NO_EXACT_MATCH before publication."
    )
