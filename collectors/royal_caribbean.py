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
    # Royal's public cruise catalogue is reliably populated with its standard
    # anonymous US search headers. We only use this call for sailing metadata;
    # pricing below is still requested explicitly for AUS / AUD / SYD.
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
    print(f"RC_GRAPH_BODY {' '.join(text.split())[:12000]}")
    try:
        data = json.loads(text)
    except Exception:
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


async def _price(request, sailing: dict, adults: int):
    sailing_id = sailing.get("id")
    package = _package_code(sailing_id)
    sail_date = sailing.get("sailDate") or sailing.get("startDate")
    if not package or not sail_date:
        print(f"RC_PRICE_SKIP adults={adults} sailing_id={sailing_id} sail_date={sail_date}")
        return

    params = {
        "packageCode": package,
        "sailDate": sail_date[:10],
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
    print(f"RC_PRICE_STATUS adults={adults} package={package} date={sail_date[:10]} status={response.status}")
    print(f"RC_PRICE_BODY adults={adults} {' '.join(text.split())[:12000]}")


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
            await _price(request, sample, 1)
            await _price(request, sample, 2)
        else:
            print("RC_NO_SAMPLE_SAILING")
        await request.dispose()


async def collect(headless: bool = True):
    await diagnostic(headless=headless)
    raise RuntimeError(
        "Royal Caribbean API diagnostic complete. Inspect RC_GRAPH_* and RC_PRICE_* lines before enabling publication."
    )
